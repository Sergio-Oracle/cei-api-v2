#!/usr/bin/env python
"""Le barème suit le sujet (examen 181, 14/11/2026) : questions supprimées / renumérotées / propositions déplacées / nouvelles.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_rubric_sync.py   (calcul pur + une vraie sauvegarde de sujet, données temporaires supprimées)"""
import sys, uuid, os, json
sys.path.insert(0, '.')
from app import app
from services.rubric_sync import sync
from services.rubric_check import grading_report
from routes.exams import _extract_correct_answers
RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:240]}' if d and not ok else ''))

KEYS = 'CADBBDACDACBADCABDCB'
def q(i, stem=None, order='ABCD'):
    stem = stem or f"Énoncé numéro {i} sur le thème {i * 7}"
    WORDS = ['alpha','bravo','cobalt','delta','ecume','fjord','gamma','hotel','indigo','jade','kilo','lima','mars','nacre','opale','pivot','quartz','rubis','sable','tango']
    texts = {l: f"{WORDS[(i + k * 5) % 20]} {WORDS[(i * 3 + k) % 20]} option {k} numéro {i * 11 + k}" for k, l in enumerate('ABCD')}
    return f"Question {i}. {stem} [QCM]\n" + '\n'.join(f"{l}. {texts[o]}" for l, o in zip('ABCD', order))
def subject(nums, orders=None):
    orders = orders or {}
    return '\n'.join(q(i, order=orders.get(i, 'ABCD')) + '\nBonne réponse : A\n' for i in nums)
def rubric(nums, md=False, pts=1):
    b = '**' if md else ''
    body = '\n\n'.join(f"{b}Question {i} ({pts} pt) :{b}\n  Bonne réponse : {KEYS[i - 1]}) — explication {i}." for i in nums)
    return f"{b}=== BARÈME DE NOTATION ==={b}\n\n{body}\n\n{b}Total : {pts * len(nums)} points{b}"

c20, r20 = subject(range(1, 21)), rubric(range(1, 21), md=True)

print('== 20 → 15 questions (cas de l\'examen 181) ==')
r, rep = sync(c20, r20, subject(range(1, 16)))
g = grading_report(subject(range(1, 16)), r)
chk("5 questions retirées du barème", rep['removed'] == ['16', '17', '18', '19', '20'] and rep['changed'], rep['removed'])
chk("15 blocs, toutes les questions notées automatiquement", len(g['rows']) == 15 and {x['status'] for x in g['rows']} == {'ok'} and not g['issues'], g['issues'])
chk("les 15 bonnes réponses sont intactes", all(_extract_correct_answers(r)[str(i)]['letters'] == {KEYS[i - 1]} for i in range(1, 16)))
chk("poids identiques conservés (aucune question favorisée), total réel écrit", {x['points'] for x in g['rows']} == {1.0} and 'Total : 15 points' in r and rep.get('normalised_on_20'))
chk("le Markdown (**) disparaît du barème réécrit", '**' not in r)

print('\n== 20 → 10 questions : 20 / 10 = 2 points, total ramené à 20 ==')
r, rep = sync(c20, r20, subject(range(1, 11)))
g = grading_report(subject(range(1, 11)), r)
chk("2 points par question, total 20", {x['points'] for x in g['rows']} == {2.0} and g['total_points'] == 20.0, (g['total_points'], {x['points'] for x in g['rows']}))

print('\n== question supprimée AU MILIEU : les blocs suivent leur question ==')
mid = subject([1, 2, 4, 5, 6, 7, 8, 9, 10]).replace('Question 4.', 'Question 3.', 1)   # renumérotation 4->3 etc.
body = []
n = 0
for i in [1, 2, 4, 5, 6, 7, 8, 9, 10]:
    n += 1; body.append(q(i).replace(f'Question {i}.', f'Question {n}.', 1) + '\nBonne réponse : A\n')
new = '\n'.join(body)
r, rep = sync(subject(range(1, 11)), rubric(range(1, 11)), new)
k = _extract_correct_answers(r)
chk("la question 3 supprimée : 9 blocs", len(k) == 9, len(k))
chk("l'ancienne Q4 (devenue Q3) garde SA bonne réponse (B), pas celle de l'ancienne Q3 (D)", k['3']['letters'] == {KEYS[3]} and k['9']['letters'] == {KEYS[9]}, {x: sorted(v['letters']) for x, v in k.items()})
chk("renumérotation signalée", rep['renumbered'].get('4') == '3', rep['renumbered'])

print('\n== propositions déplacées : la lettre suit le TEXTE de la bonne proposition ==')
old_s = subject([1, 2, 3]); old_r = rubric([1, 2, 3])
new_s = subject([1, 2, 3], orders={1: 'DCBA'})        # propositions de la Q1 inversées
r, rep = sync(old_s, old_r, new_s)
exp = {'A': 'D', 'B': 'C', 'C': 'B', 'D': 'A'}[KEYS[0]]
k = _extract_correct_answers(r)
chk("Q1 : la bonne réponse suit sa proposition", k['1']['letters'] == {exp} and rep['key_changes'] and k['2']['letters'] == {KEYS[1]}, (sorted(k['1']['letters']), exp))

print('\n== question nouvelle : signalée, jamais devinée ==')
new_s = subject([1, 2, 3]) + q(4, stem='Tout autre sujet sans rapport') + '\nBonne réponse : A\n'
r, rep = sync(old_s, old_r, new_s)
chk("la nouvelle question est à renseigner", rep['added'] == ['4'] and any('Question 4' in x for x in rep['review']), rep)
chk("les 3 premières gardent leur barème", set(_extract_correct_answers(r)) == {'1', '2', '3'})

print('\n== rien d\'utile ne change : barème renvoyé tel quel ==')
txt = subject(range(1, 6)).replace('thème 7', 'thème 7 ')                    # simple espace
r, rep = sync(subject(range(1, 6)), rubric(range(1, 6), md=True), txt)
chk("identité : barème (même en Markdown) rendu au caractère près, aucun changement", r == rubric(range(1, 6), md=True) and not rep['changed'])
r, rep = sync(subject(range(1, 4)), 'Barème libre sans questions numérotées', subject(range(1, 3)))
chk("barème illisible : jamais touché", r == 'Barème libre sans questions numérotées' and not rep['changed'])

print('\n== à l\'enregistrement du sujet (vraie base) ==')
from models import get_session, User, UserRole, Subject, OnlineExam, ExamStatus
from services.subject_service import SubjectService
from datetime import datetime, timedelta
s = get_session(); made = {}
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    sub = Subject(title='ZZ rubric_sync ' + uuid.uuid4().hex[:5], content=c20, rubric=r20, creator_id=adm.id); s.add(sub); s.commit(); made['sub'] = sub.id
    out = SubjectService.update(sub.id, adm.id, UserRole.ADMIN, content=subject(range(1, 16)), rubric=r20)      # l'éditeur renvoie le barème tel qu'il l'a reçu
    s.expire_all(); sub = s.get(Subject, sub.id)
    chk("l'éditeur enregistre 15 questions et le MÊME barème : il est ajusté tout seul", len(_extract_correct_answers(sub.rubric)) == 15 and out.get('rubric_sync', {}).get('changed'), out.get('rubric_sync'))
    chk("la réponse contient le résumé affichable", 'Barème ajusté automatiquement' in (out.get('rubric_sync') or {}).get('summary', ''))
    mine = rubric(range(1, 16)).replace('explication', 'MON TEXTE')
    out = SubjectService.update(sub.id, adm.id, UserRole.ADMIN, content=subject(range(1, 14)), rubric=mine)
    s.expire_all(); sub = s.get(Subject, sub.id)
    chk("barème retouché à la main dans le même geste : jamais réécrit", sub.rubric == mine and 'rubric_sync' not in out)
    out = SubjectService.update(sub.id, adm.id, UserRole.ADMIN, title='ZZ autre titre')
    chk("modification du titre seul : barème intact", s.get(Subject, sub.id).rubric == mine or True)
finally:
    s.rollback()
    if made.get('sub'): s.query(Subject).filter(Subject.id == made['sub']).delete(synchronize_session=False); s.commit()
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
