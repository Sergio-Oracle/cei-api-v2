"""Vérification et recalcul des notes d'un examen corrigé : filet contre les notes déformées (voir scripts/test_bareme_correction.py).

Pour chaque copie corrigée automatiquement, la note est recalculée avec les règles actuelles :
- points des questions notées automatiquement (QCM, vrai/faux, appariement...) ;
- points de l'IA conservés seulement pour les questions que l'épreuve pose réellement et qui ne sont pas déjà notées
  automatiquement, plafonnés à ce qui reste à l'IA ;
- total visé = points des questions réellement posées, note ramenée proportionnellement sur 20.

Laissées intactes : copies vides (0/20), notes saisies à la main (copie sans détail par question), copies dont le détail
a été retouché question par question, et copies dont la part de l'IA ne peut pas être revérifiée (pas de détail IA).
Avant toute écriture, chaque ancienne valeur est sauvegardée dans un fichier."""
import json
import os
import re
import time
from datetime import datetime

from models import ExamAttempt, ExamActivityLog, OnlineExam, AttemptStatus

SUBMITTED = (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED)
_FINAL_LINE = re.compile(r'Note totale\s*:\s*[\d.,]+\s*/\s*20', re.I)


def _backup_dir() -> str:
    return os.getenv('GRADE_BACKUP_DIR') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backups')


def recompute(session, exam_id: int, apply: bool = False, backup_dir=None) -> dict:
    from routes import exams as R
    exam = session.query(OnlineExam).filter_by(id=exam_id).first()
    sub = exam.subject
    report = {'exam': exam_id, 'copies': 0, 'checked': 0, 'changed': 0, 'unchanged': 0, 'skipped_empty': 0,
              'skipped_manual': 0, 'skipped_edited': 0, 'skipped_unverifiable': 0, 'lower': 0, 'higher': 0,
              'before': [], 'after': [], 'backup': None}
    todo, backup = [], []
    for a in session.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.status.in_(SUBMITTED),
                                               ExamAttempt.score.isnot(None)).all():
        report['copies'] += 1
        try:
            ans = json.loads(a.answers) if a.answers else {}
        except Exception:
            ans = {}
        if not R._has_real_answers(ans):
            report['skipped_empty'] += 1
            continue
        if a.question_scores is None:                      # note saisie à la main : jamais touchée
            report['skipped_manual'] += 1
            continue
        det, dmax, total, breakdown, nums, structured = R._deterministic_grade(sub.content, sub.rubric or '', ans, seed=a.id)
        if total <= 0.01:
            continue
        try:
            old_entries = json.loads(a.question_scores or '[]')
        except Exception:
            old_entries = []
        old = {str(q.get('num')): float(q.get('score', 0)) for q in old_entries
               if q.get('type') in ('qcm', 'vf') and q.get('num') is not None}
        if any(abs(old.get(str(q['num']), q['score']) - q['score']) > 0.001 for q in structured if q.get('type') in ('qcm', 'vf')):
            report['skipped_edited'] += 1
            continue
        remaining = round(total - dmax, 2)
        ai_entries = [q for q in old_entries if q.get('type') == 'open' and str(q.get('num')) not in nums]
        if remaining > 0.01 and not ai_entries:
            report['skipped_unverifiable'] += 1            # part de l'IA impossible à revérifier : on n'y touche pas
            continue
        ai_part = min(remaining, sum(float(q.get('score', 0)) for q in ai_entries)) if remaining > 0.01 else 0.0
        kept = ai_entries if remaining > 0.01 else []
        new = round(max(0.0, min(20.0, (det + ai_part) / total * 20)), 2)
        report['checked'] += 1
        report['before'].append(a.score)
        report['after'].append(new)
        if abs(new - a.score) <= 0.005:
            report['unchanged'] += 1
            continue
        report['changed'] += 1
        report['lower' if new < a.score else 'higher'] += 1
        todo.append((a, new, breakdown, structured + kept, bool(kept)))
        backup.append({'id': a.id, 'score': a.score, 'feedback': a.feedback, 'question_scores': a.question_scores,
                       'corrected_at': a.corrected_at.isoformat() if a.corrected_at else None, 'corrected_by_id': a.corrected_by_id})
    if apply and todo:
        directory = backup_dir or _backup_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"recalcul_examen{exam_id}_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w') as f:
            json.dump(backup, f, ensure_ascii=False)
        report['backup'] = path
        now = datetime.utcnow()
        changed_notes = []
        for a, new, breakdown, entries, has_ai in todo:
            old_score = a.score
            changed_notes.append((a.id, old_score))
            if has_ai and a.feedback and _FINAL_LINE.search(a.feedback):
                a.feedback = _FINAL_LINE.sub(f"Note totale: {new:.2f}/20", a.feedback)       # le retour de l'IA est conservé
            else:
                a.feedback = ("=== Tes réponses aux questions à choix ===\n" + '\n'.join(breakdown) + "\n\n" if breakdown else '') \
                    + f"Note totale: {new:.2f}/20"
            a.score = new
            a.question_scores = json.dumps(entries)
            a.corrected_at = now
            a.corrected_by_id = None
            session.add(ExamActivityLog(attempt_id=a.id, event_type='grade_recomputed', event_data=json.dumps(
                {'old_score': old_score, 'new_score': new, 'timestamp': now.isoformat(),
                 'reason': "total de points aligné sur les questions posées, points comptés deux fois retirés"})))
        session.commit()
        R._push_grades_if_published(exam)
        for _aid, _old in changed_notes:                       # résultats déjà publiés : chaque étudiant concerné est averti
            R._notify_grade_change(exam, _aid, _old)
    return report
