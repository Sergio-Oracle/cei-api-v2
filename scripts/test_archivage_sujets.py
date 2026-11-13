#!/usr/bin/env python
"""Un sujet archivé (is_active faux) n'apparaît plus dans aucune liste (enseignant, administrateur, étudiant) mais reste lisible par id.

    cd cei-api-v2 && .venv-base/bin/python scripts/test_archivage_sujets.py"""
import os, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A  # noqa: F401
from models import get_session, User, UserRole, Subject
from repositories.subject_repository import SubjectRepository

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

s = get_session()
tag = uuid.uuid4().hex[:8]
prof = User(email=f'arch_{tag}@temp.edu', password_hash='x', full_name='Test Archivage', role=UserRole.PROFESSOR, has_email=False)
s.add(prof); s.commit()
ids = []
try:
    for actif in (True, False, None):
        sub = Subject(title=f'ARCH-{tag}-{actif}', content='Question 1 — a\n', rubric='', creator_id=prof.id, is_active=actif)
        s.add(sub); s.commit(); ids.append(sub.id)
    actif_id, archive_id, null_id = ids
    for role, label in ((UserRole.PROFESSOR, 'enseignant'), (UserRole.ADMIN, 'administrateur'), (UserRole.STUDENT, 'étudiant')):
        liste = {d['id'] for d in SubjectRepository.find_all(prof.id, role)}
        check(f"{label} : le sujet actif est listé", actif_id in liste)
        check(f"{label} : le sujet archivé n'est PAS listé", archive_id not in liste)
    liste = {d['id'] for d in SubjectRepository.find_all(prof.id, UserRole.PROFESSOR)}
    check("is_active vide (anciens sujets) reste listé", null_id in liste)
    check("le sujet archivé reste lisible par son identifiant", SubjectRepository.find_by_id(archive_id) is not None)
finally:
    s.rollback()
    for i in ids:
        o = s.get(Subject, i)
        if o: s.delete(o)
    s.delete(s.get(User, prof.id)); s.commit()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS)-len(bad)}/{len(RESULTS)} contrôles passent")
sys.exit(1 if bad else 0)
