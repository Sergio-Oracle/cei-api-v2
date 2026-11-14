#!/usr/bin/env python
"""Contrôle automatique sujet ↔ barème avant chaque examen (14/11/2026).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_exam_preflight.py   (crée puis supprime ses propres données)"""
import sys, uuid
sys.path.insert(0, '.')
from app import app
from models import get_session, User, UserRole, Subject, OnlineExam, ExamStatus
from cache import cache_delete
from datetime import datetime, timedelta
import notif_bus
from services import exam_preflight as PF

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:240]}' if d and not ok else ''))

def q(i): return f"Question {i}. Énoncé {i} sur le thème {i * 7} [QCM]\nA. alpha{i}\nB. bravo{i * 3}\nC. cobalt{i * 5}\nD. delta{i * 9}\n"
CONTENT = '\n'.join(q(i) for i in range(1, 7))
GOOD = '\n\n'.join(f"Question {i} (1 pt) :\n  Bonne réponse : A) — x" for i in range(1, 7)) + "\n\nTotal : 6 points"
BAD_MD = "**Question 1 (1 pt) :**\n  Bonne réponse : A)\n\n" + '\n\n'.join(f"Question {i} (1 pt) :\n  Bonne réponse : A)" for i in range(2, 4))   # 3 questions sans barème

sent = []
real_exam, real_adm = notif_bus.notify_exam, notif_bus.notify_admins
notif_bus.notify_exam = lambda eid, ev, title, msg, priority='default', tags=None: sent.append(('exam', eid, ev, title, msg))
notif_bus.notify_admins = lambda ev, title, msg, priority='default', tags=None: sent.append(('admins', None, ev, title, msg))
s = get_session(); now = datetime.utcnow().replace(microsecond=0); made = {'sub': [], 'ex': []}
try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    def mk(title, rubric, content=CONTENT, start_in=20, status=ExamStatus.SCHEDULED):
        sub = Subject(title='ZZ pre ' + uuid.uuid4().hex[:4], content=content, rubric=rubric, creator_id=adm.id); s.add(sub); s.commit(); made['sub'].append(sub.id)
        ex = OnlineExam(subject_id=sub.id, title=title, duration_minutes=60, start_time=now + timedelta(minutes=start_in), end_time=now + timedelta(minutes=start_in + 120),
                        status=status, created_by_id=adm.id); s.add(ex); s.commit(); made['ex'].append(ex.id); return ex
    ok = mk('ZZ examen sain', GOOD)
    bad = mk('ZZ examen barème cassé', BAD_MD)
    late = mk('ZZ examen dans 2 h', BAD_MD, start_in=120)
    past = mk('ZZ examen déjà commencé', BAD_MD, start_in=-5, status=ExamStatus.ACTIVE)
    empty = mk('ZZ sans sujet', '', content='   ', start_in=25)

    print('== Contenu du contrôle ==')
    r = PF.check_exam(s.get(OnlineExam, ok.id)); chk("sujet sain : aucun problème", not r['errors'] and not r['warns'] and r['questions'] == 6, r)
    r = PF.check_exam(s.get(OnlineExam, bad.id))
    chk("barème qui ne couvre que 3 questions sur 6 : erreurs listées (questions 4 à 6)", len(r['errors']) >= 3 and any('Question 4' in e for e in r['errors']), r['errors'])
    r = PF.check_exam(s.get(OnlineExam, empty.id)); chk("sujet vide : erreur claire", r['errors'] and 'vide' in r['errors'][0], r)

    print('\n== Alertes ==')
    n = PF.run_once(now)
    mine = [x for x in sent if x[1] in (bad.id, empty.id) or x[0] == 'admins']
    chk("2 examens à problème (début dans 20 et 25 min) → alertes", n == 2, n)
    chk("l'examen sain, celui dans 2 h et celui déjà commencé ne déclenchent rien", not any(x[1] in (ok.id, late.id, past.id) for x in sent), [x[1] for x in sent])
    a = next(x for x in sent if x[0] == 'exam' and x[1] == bad.id)
    chk("le message dit combien de minutes restent, les questions concernées et où corriger", 'commence dans 20 min' in a[4] and 'Question 4' in a[4] and 'Modifier le sujet' in a[4], a[4])
    chk("les administrateurs sont prévenus aussi", any(x[0] == 'admins' and 'barème cassé' in x[3] for x in sent))
    before = len(sent); PF.run_once(now + timedelta(seconds=30))
    chk("pas de doublon au passage suivant (même point de contrôle)", len(sent) == before, len(sent) - before)
    PF.run_once(now + timedelta(minutes=9))        # il reste 11 minutes : pas encore le rappel
    chk("pas de rappel avant le point de contrôle de 10 minutes", len(sent) == before)
    PF.run_once(now + timedelta(minutes=11))
    PF.run_once(now + timedelta(minutes=15))
    after10 = len([x for x in sent if x[0] == 'exam' and x[1] == bad.id])
    chk("rappel UNE fois à moins de 10 minutes si le problème persiste", after10 == 2, after10)
    ex_bad = s.get(OnlineExam, bad.id); ex_bad.subject.rubric = GOOD; s.commit()
    cache_delete(f'cei:preflight:{bad.id}:exam:10:{ex_bad.start_time.isoformat()}')
    k = len(sent); PF.run_once(now + timedelta(minutes=17))      # barème corrigé entre-temps
    chk("problème corrigé avant le point de contrôle suivant : silence", len([x for x in sent[k:] if x[1] == bad.id]) == 0)
finally:
    notif_bus.notify_exam, notif_bus.notify_admins = real_exam, real_adm
    try:
        s.rollback()
        for eid in made['ex']:
            for tag in ('exam',):
                for cp in PF.CHECKPOINTS:
                    pass
            s.query(OnlineExam).filter_by(id=eid).delete(synchronize_session=False)
        s.query(Subject).filter(Subject.id.in_(made['sub'])).delete(synchronize_session=False); s.commit()
    except Exception as e:
        print('nettoyage :', e)
    s.close()
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
