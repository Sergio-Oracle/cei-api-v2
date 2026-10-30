"""Recalcul des notes d'un examen corrigé automatiquement avec une note déformée (voir scripts/test_bareme_correction.py).

Seules les questions notées automatiquement (QCM, vrai/faux, appariement...) sont recalculées, avec le bon total de points ;
les points accordés par l'IA à des questions déjà notées sont supprimés. Sont laissées intactes : les copies vides (0/20),
les notes saisies à la main (copie sans détail par question) et les copies dont le détail a été retouché question par question.
Chaque ancienne valeur est sauvegardée dans un fichier avant toute écriture."""
import json
import os
import time
from datetime import datetime

from models import ExamAttempt, ExamActivityLog, OnlineExam, AttemptStatus

SUBMITTED = (AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED)


def recompute(session, exam_id: int, apply: bool = False, backup_dir: str = '/home/serge/backups') -> dict:
    from routes import exams as R
    exam = session.query(OnlineExam).filter_by(id=exam_id).first()
    sub = exam.subject
    report = {'exam': exam_id, 'copies': 0, 'changed': 0, 'unchanged': 0, 'skipped_empty': 0, 'skipped_manual': 0,
              'skipped_edited': 0, 'lower': 0, 'higher': 0, 'before': [], 'after': [], 'details': [], 'backup': None}
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
        # détail retouché à la main ? les points des questions notées automatiquement doivent être ceux du recalcul
        old = {str(q.get('num')): float(q.get('score', 0)) for q in json.loads(a.question_scores or '[]')
               if q.get('type') in ('qcm', 'vf') and q.get('num') is not None}
        if any(abs(old.get(str(q['num']), q['score']) - q['score']) > 0.001 for q in structured if q.get('type') in ('qcm', 'vf')):
            report['skipped_edited'] += 1
            report['details'].append((a.id, 'détail modifié à la main'))
            continue
        new = round(max(0.0, min(20.0, det / total * 20)), 2)
        report['before'].append(a.score)
        report['after'].append(new)
        if abs(new - a.score) <= 0.005:
            report['unchanged'] += 1
            continue
        report['changed'] += 1
        report['lower' if new < a.score else 'higher'] += 1
        todo.append((a, new, breakdown, structured))
        backup.append({'id': a.id, 'score': a.score, 'feedback': a.feedback, 'question_scores': a.question_scores,
                       'corrected_at': a.corrected_at.isoformat() if a.corrected_at else None, 'corrected_by_id': a.corrected_by_id})
    if apply and todo:
        os.makedirs(backup_dir, exist_ok=True)
        path = os.path.join(backup_dir, f"recalcul_examen{exam_id}_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w') as f:
            json.dump(backup, f, ensure_ascii=False)
        report['backup'] = path
        now = datetime.utcnow()
        for a, new, breakdown, structured in todo:
            old_score = a.score
            a.score = new
            a.feedback = ("=== Tes réponses aux questions à choix ===\n" + '\n'.join(breakdown) + "\n\n" if breakdown else '') \
                + f"Note totale: {new:.2f}/20"
            a.question_scores = json.dumps(structured)
            a.corrected_at = now
            a.corrected_by_id = None
            session.add(ExamActivityLog(attempt_id=a.id, event_type='grade_recomputed', event_data=json.dumps(
                {'old_score': old_score, 'new_score': new, 'reason': 'total de points aligné sur les questions posées, '
                 'points de l\'IA sur des questions déjà notées retirés', 'timestamp': now.isoformat()})))
        session.commit()
        R._push_grades_if_published(exam)
    return report
