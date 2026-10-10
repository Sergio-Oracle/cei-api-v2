"""Régression du cycle de vie d'un examen (base locale, données de test nettoyées après).

Lancer :  .venv-base/bin/python tests/test_exam_lifecycle.py
Couvre les incidents du 09-10/10/2026 : copie vide jamais notée, tentative avec temps
supplémentaire restée « en cours », anomalies signalées, dates sans fuseau.
"""
import os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cycle_de_vie():
    import redis
    from models import (get_session, OnlineExam, ExamAttempt, ExamStatus, AttemptStatus,
                        User, UserRole, utc_iso)
    from services.exam_health import collect_anomalies
    from routes.exams import _sweep_uncorrected
    s = get_session()
    ex = s.query(OnlineExam).order_by(OnlineExam.id).first()
    assert ex, "il faut au moins un examen dans la base de test"
    old = (ex.status, ex.end_time, ex.auto_correct)
    studs = s.query(User).filter_by(role=UserRole.STUDENT).limit(4).all()
    assert len(studs) == 4
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    made = []
    try:
        ex.status, ex.end_time, ex.auto_correct = ExamStatus.CLOSED, now - timedelta(hours=3), True
        s.commit()
        att = [
            ExamAttempt(exam_id=ex.id, student_id=studs[0].id, status=AttemptStatus.IN_PROGRESS, answers='{}', extra_minutes=3, started_at=now - timedelta(hours=4)),
            ExamAttempt(exam_id=ex.id, student_id=studs[1].id, status=AttemptStatus.AUTO_SUBMITTED, answers='{}', submitted_at=now - timedelta(hours=3), started_at=now - timedelta(hours=4)),
            ExamAttempt(exam_id=ex.id, student_id=studs[2].id, status=AttemptStatus.IN_PROGRESS, answers='{}', extra_minutes=400, started_at=now - timedelta(hours=4)),
        ]
        s.add_all(att); s.commit(); made = [a.id for a in att]
        keys = {a['key'] for a in collect_anomalies(s)}
        assert f'stuck:{ex.id}' in keys, "tentative bloquée non détectée par le contrôle de santé"
        redis.Redis().delete('cei:sweep:uncorrected')
        _sweep_uncorrected()
        s.expire_all()
        a, b, c = (s.get(ExamAttempt, i) for i in made)
        assert a.status == AttemptStatus.AUTO_SUBMITTED, "tentative échue non clôturée"
        assert b.score == 0.0 and b.corrected_at is not None, "copie vide non notée 0/20"
        assert c.status == AttemptStatus.IN_PROGRESS, "temps supplémentaire non respecté"
    finally:
        s.rollback()
        s.query(ExamAttempt).filter(ExamAttempt.id.in_(made)).delete(synchronize_session=False)
        ex.status, ex.end_time, ex.auto_correct = old
        s.commit()
        redis.Redis().delete('cei:sweep:uncorrected')
    assert utc_iso(datetime(2026, 10, 9, 12, 0)).endswith('Z')


if __name__ == '__main__':
    test_cycle_de_vie()
    print('cycle de vie : OK')
