"""Contrôle automatique « sujet ↔ barème » avant chaque examen (14/11/2026).

Incident du 14/11 (examen 181) : l'aperçu montrait « 15 questions lues — à corriger avant l'examen » et le barème n'était lu que pour la
question 1 ; personne n'avait été prévenu. Ce service relit, 30 puis 10 minutes avant le début de chaque examen (et de chaque vague), exactement
ce que la correction fera du sujet, et prévient TOUT le personnel de l'examen (créateur, responsables, enseignants de l'EC, surveillants,
superviseurs) ainsi que les administrateurs quand quelque chose ne va pas. Silence = tout va bien.

Contrôles : sujet absent ou vide ; erreurs de lecture (exercice vide, questions illisibles) ; barème qui ne donne pas de bonne réponse à une
question à correction automatique, bonne réponse absente des propositions, points manquants ; barème qui décrit des questions absentes du
sujet ; sujet non validé. Jamais bloquant : un contrôle qui échoue est simplement journalisé.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from cache import cache_set_nx

log = logging.getLogger(__name__)
CHECKPOINTS = (30, 10)            # minutes avant le début : premier contrôle, puis rappel si le problème persiste
TTL = 6 * 3600


def check_exam(exam) -> dict:
    """Problèmes bloquants ou à surveiller pour cet examen : {'errors': [...], 'warns': [...], 'questions': n}."""
    out = {'errors': [], 'warns': [], 'questions': 0}
    subject = exam.subject if exam else None
    if not subject or not (subject.content or '').strip():
        out['errors'].append("Aucun sujet (ou sujet vide) : les étudiants n'auront aucune question.")
        return out
    from services.subject_check import analyze
    from services.rubric_check import grading_report
    seen = set()
    def add(level, msg):
        if msg and msg not in seen:
            seen.add(msg)
            out['errors' if level == 'error' else 'warns'].append(msg)
    a = analyze(subject.content, subject.rubric or '')
    out['questions'] = a.get('questions', 0)
    for i in a.get('issues', []):
        if i.get('level') in ('error', 'warn'):
            add(i['level'], i.get('message'))
    g = grading_report(subject.content, subject.rubric or '')
    for i in g.get('issues', []):
        if i.get('level') in ('error', 'warn'):
            add(i['level'], i.get('message'))
    if not (subject.rubric or '').strip():
        add('warn', "Aucun barème : les questions seront corrigées par l'IA sans critères.")
    return out


def _starts(session, exam):
    """Débuts à surveiller : l'examen lui-même et chacune de ses vagues."""
    starts = []
    if exam.start_time:
        starts.append(('exam', exam.start_time))
    try:
        from models import ExamWave
        for w in session.query(ExamWave).filter_by(exam_id=exam.id).all():
            if w.start_time:
                starts.append((f'wave{w.id}', w.start_time))
    except Exception:
        pass
    return starts


def run_once(now: datetime | None = None) -> int:
    """Un passage ; retourne le nombre d'alertes envoyées."""
    from models import get_session, OnlineExam, ExamStatus
    import notif_bus
    now = now or datetime.utcnow()
    sent = 0
    session = get_session()
    try:
        horizon = now + timedelta(minutes=max(CHECKPOINTS) + 1)
        exams = session.query(OnlineExam).filter(
            OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE]),
            OnlineExam.start_time <= horizon + timedelta(days=2)).all()      # les vagues peuvent commencer après le début de l'enveloppe
        for exam in exams:
            for tag, start in _starts(session, exam):
                minutes = (start - now).total_seconds() / 60.0
                if minutes < 0 or minutes > max(CHECKPOINTS):
                    continue
                for cp in sorted(CHECKPOINTS, reverse=True):
                    if minutes > cp:
                        continue
                    if not cache_set_nx(f'cei:preflight:{exam.id}:{tag}:{cp}:{start.isoformat()}', TTL):
                        continue                                              # ce point de contrôle a déjà été traité
                    rep = check_exam(exam)
                    probs = rep['errors'] + rep['warns']
                    if not probs:
                        continue
                    n = int(round(minutes))
                    title = f"Contrôle avant examen — {len(probs)} point(s) à vérifier : {exam.title}"
                    lines = [("⛔ " if p in rep['errors'] else "⚠ ") + p for p in probs[:6]]
                    more = f"\n… et {len(probs) - 6} autre(s)." if len(probs) > 6 else ''
                    msg = (f"« {exam.title} » commence dans {n} min.\n" + '\n'.join(lines) + more
                           + "\nOuvrez l'aperçu du sujet (Modifier le sujet → contrôle) pour corriger avant le début.")
                    try:
                        notif_bus.notify_exam(exam.id, 'exam_preflight', title, msg, priority='high')
                        notif_bus.notify_admins('exam_preflight', title, msg, priority='high')
                    except Exception as e:
                        log.warning('preflight notif: %s', e)
                    sent += 1
    except Exception as e:
        print(f"[exam_preflight] contrôle : {e}")
    finally:
        session.close()
    return sent
