"""Qui a le droit de voir, ouvrir et démarrer un examen (côté étudiant).

Incident du 02/11/2026 (examen 152) : une étudiante non inscrite à l'UE de l'examen l'a démarré par son lien direct. Ni la liste
des examens, ni l'ouverture, ni le démarrage ne contrôlaient l'inscription. Règle, centralisée ici et appelée partout :

  un étudiant accède à l'examen s'il est inscrit à l'UE de l'EC du sujet, OU s'il a déjà une copie, OU s'il est dans une vague de
  l'examen (rattachements faits par le personnel)  (règle permanente, sans réglage : un enseignant ne peut pas la lever).

  EXCEPTION — session de rattrapage (`exam.makeup_of_id` renseigné) : seuls les étudiants que le personnel a choisis (liste du
  rattrapage) y accèdent, ou ceux qui y ont déjà une copie. L'inscription à l'UE ne suffit PAS : le rattrapage ne concerne pas
  tous les inscrits.
"""
from models import ExamAttempt, ExamMakeupStudent, ExamWaveStudent, StudentUEEnrollment

REFUSAL = "Vous n'êtes pas inscrit à cet examen. Si c'est une erreur, contactez votre enseignant."


# UE d'un examen (examen → sujet → EC → UE) : change très rarement. Mémorisée 60 s dans le processus (19/11/2026) : la relire pour chaque étudiant à chaque ouverture de liste
# chargeait la ligne complète de l'EC (une vingtaine de colonnes) des milliers de fois par minute. Une réorganisation d'UE est prise en compte au plus 60 s plus tard.
import time as _time
_UE_CACHE = {}
_UE_TTL_S = 60.0


def _ue_id(exam):
    now = _time.monotonic()
    hit = _UE_CACHE.get(exam.id)
    if hit and hit[1] > now:
        return hit[0]
    ec = exam.subject.ec if exam.subject else None
    v = ec.ue_id if ec else None
    if len(_UE_CACHE) > 5000:
        _UE_CACHE.clear()
    _UE_CACHE[exam.id] = (v, now + _UE_TTL_S)
    return v


def student_may_access(session, exam, student_id) -> bool:
    if getattr(exam, 'makeup_of_id', None):
        if session.query(ExamMakeupStudent.id).filter_by(exam_id=exam.id, student_id=student_id).first():
            return True
        return session.query(ExamAttempt.id).filter_by(exam_id=exam.id, student_id=student_id).first() is not None
    ue_id = _ue_id(exam)
    if ue_id and session.query(StudentUEEnrollment.id).filter_by(student_id=student_id, ue_id=ue_id).first():
        return True
    if session.query(ExamAttempt.id).filter_by(exam_id=exam.id, student_id=student_id).first():
        return True
    return session.query(ExamWaveStudent.id).filter_by(exam_id=exam.id, student_id=student_id).first() is not None


def accessible_exam_ids(session, exams, student_id) -> set:
    """Même règle pour une liste d'examens, en quelques requêtes (liste du tableau de bord)."""
    exams = list(exams)
    if not exams:
        return set()
    ids = [e.id for e in exams]
    my_ues = {r[0] for r in session.query(StudentUEEnrollment.ue_id).filter_by(student_id=student_id)}
    attempts = {r[0] for r in session.query(ExamAttempt.exam_id).filter(ExamAttempt.student_id == student_id, ExamAttempt.exam_id.in_(ids))}
    waves = {r[0] for r in session.query(ExamWaveStudent.exam_id).filter(ExamWaveStudent.student_id == student_id, ExamWaveStudent.exam_id.in_(ids))}
    makeup_ids = [e.id for e in exams if getattr(e, 'makeup_of_id', None)]
    rosters = {r[0] for r in session.query(ExamMakeupStudent.exam_id).filter(
        ExamMakeupStudent.student_id == student_id, ExamMakeupStudent.exam_id.in_(makeup_ids))} if makeup_ids else set()
    out = set()
    for e in exams:
        if getattr(e, 'makeup_of_id', None):          # rattrapage : liste choisie uniquement
            if e.id in rosters or e.id in attempts:
                out.add(e.id)
        elif e.id in attempts or e.id in waves or (_ue_id(e) in my_ues and _ue_id(e)):
            out.add(e.id)
    return out
