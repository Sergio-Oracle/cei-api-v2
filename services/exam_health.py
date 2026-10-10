"""
Contrôle de santé des examens — dernier filet derrière les correctifs automatiques.

`_sweep_uncorrected` répare tout seul ce qu'il peut (tentatives restées en cours,
copies non corrigées). Ce module détecte ce qui reste ANORMAL malgré cela et
PRÉVIENT les humains au lieu de laisser un problème silencieux :

  - copies non corrigées depuis plus de 20 min sur un examen « IA auto »
    (la correction automatique échoue : fournisseur d'IA en panne, barème...)
  - tentatives restées « en cours » bien après l'échéance
  - examen prévu dans les 24 h (ou déjà en cours) sans aucun surveillant

Chaque anomalie n'est signalée qu'une fois par 24 h (verrou Redis), aux
administrateurs et à l'enseignant créateur de l'examen.
"""
from datetime import datetime, timedelta, timezone

from cache import cache_set_nx
from models import (get_session, OnlineExam, ExamAttempt, ExamProctor,
                    AttemptStatus, ExamStatus)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def collect_anomalies(session) -> list:
    """[{key, exam_id, creator_id, title, message}] — sans effet de bord."""
    now = _now()
    out = []

    # 1. copies non corrigées malgré la correction automatique
    rows = (session.query(ExamAttempt.exam_id, OnlineExam.title, OnlineExam.created_by_id)
            .join(OnlineExam, OnlineExam.id == ExamAttempt.exam_id)
            .filter(OnlineExam.auto_correct == True,
                    ExamAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]),
                    ExamAttempt.corrected_at.is_(None),
                    ExamAttempt.submitted_at < now - timedelta(minutes=20)).all())
    by_exam = {}
    for eid, title, creator in rows:
        by_exam.setdefault(eid, [title, creator, 0])[2] += 1
    for eid, (title, creator, n) in by_exam.items():
        out.append({'key': f'uncorrected:{eid}', 'exam_id': eid, 'creator_id': creator,
                    'title': 'Copies non corrigées',
                    'message': f"{n} copie(s) de « {title} » restent sans note malgré la correction automatique. Ouvrez l'examen et utilisez « Corriger »."})

    # 2. tentatives en cours bien après l'échéance
    stuck = (session.query(ExamAttempt, OnlineExam)
             .join(OnlineExam, OnlineExam.id == ExamAttempt.exam_id)
             .filter(ExamAttempt.status == AttemptStatus.IN_PROGRESS,
                     OnlineExam.end_time < now - timedelta(minutes=20)).all())
    seen = {}
    for att, ex in stuck:
        if now > ex.end_time + timedelta(minutes=(att.extra_minutes or 0) + 20):
            seen.setdefault(ex.id, [ex.title, ex.created_by_id, 0])[2] += 1
    for eid, (title, creator, n) in seen.items():
        out.append({'key': f'stuck:{eid}', 'exam_id': eid, 'creator_id': creator,
                    'title': 'Tentatives bloquées',
                    'message': f"{n} étudiant(s) sont restés « en cours » sur « {title} » après la fin de l'examen."})

    # 3. examen proche ou en cours sans surveillant
    soon = (session.query(OnlineExam)
            .filter(OnlineExam.status.in_([ExamStatus.SCHEDULED, ExamStatus.ACTIVE]),
                    OnlineExam.start_time < now + timedelta(hours=24),
                    OnlineExam.end_time > now).all())
    for ex in soon:
        if not session.query(ExamProctor.id).filter_by(exam_id=ex.id).first():
            out.append({'key': f'noproctor:{ex.id}', 'exam_id': ex.id, 'creator_id': ex.created_by_id,
                        'title': 'Examen sans surveillant',
                        'message': f"« {ex.title} » n'a aucun surveillant affecté. Rattachez le groupe de surveillants à l'EC ou ajoutez-en un."})
    return out


def notify_anomalies():
    """Signale chaque anomalie nouvelle (1 fois / 24 h). Ne lève jamais d'exception."""
    try:
        from notif_bus import notify_admins, notify_user
        session = get_session()
        try:
            anomalies = collect_anomalies(session)
        finally:
            session.close()
        for a in anomalies:
            if not cache_set_nx(f"cei:health:{a['key']}", 24 * 3600):
                continue
            notify_admins('exam_health', a['title'], a['message'])
            if a.get('creator_id'):
                notify_user(a['creator_id'], 'exam_health', a['title'], a['message'], extra={'exam_id': a['exam_id']})
            print(f"[santé examens] {a['title']} — {a['message']}")
    except Exception as exc:
        print(f"[santé examens] échec : {exc}")
