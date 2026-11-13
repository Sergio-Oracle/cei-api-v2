#!/usr/bin/env python
"""Qualité de la génération (13/11/2026, examen 170) : un texte à trous dont une proposition est déjà écrite dans la phrase est
refusé ; une question dont la clé est contestée par la seconde lecture est REFAITE au lieu d'être livrée « à relire ».

    cd cei-api-v2 && .venv-base/bin/python scripts/test_generation_qualite.py
Aucune IA réelle (réponses simulées). Données temporaires supprimées en fin de test."""
import os, re, sys, uuid
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as A
from routes import exams as R
from services import generation_check as G
from services.answer_verify import disagreements, verify_keys
from models import get_session, User, UserRole
from auth_paseto import create_access_token

RESULTS = []
def check(nom, ok, detail=''):
    RESULTS.append((nom, bool(ok)))
    print(('  ✔ ' if ok else '  ✘ ') + nom + (f'  — {detail!r}' if not ok else ''))

print('== Texte à trous : défauts de fond ==')
q20 = ("Question 20 — Plan de reprise ............. (1.25 pts) [Difficile] [TROUS]\nUn bon PRA doit inclure {1} la sauvegarde hors site et {2} la procédure de basculement.\n"
       "A. la sauvegarde hors site\nB. la mise à jour du BIOS\nC. la désinstallation des applications\nD. la procédure de basculement\nE. le nettoyage du clavier")
d = G.defects('trous', q20, "• Réponse : 1-A, 2-D")
check("proposition déjà écrite dans la phrase = défaut (cas réel Q20)", any('déjà écrite' in x for x in d), d)
q15 = ("Question 15 — Importance (1.25 pts) [Facile] [TROUS]\nPour garantir la sécurité du système, il faut activer les {1} du {2}.\n"
       "A. mises à jour automatiques\nB. système d'exploitation\nC. sauvegardes manuelles\nD. antivirus gratuit\nE. désinstallation des pilotes")
check("question 15 de l'examen 170 : aucun défaut", G.defects('trous', q15, "• Réponse : 1-A, 2-B") == [], G.defects('trous', q15, "• Réponse : 1-A, 2-B"))
q16 = ("Question 16 — Procédure (1.25 pts) [Moyen] [TROUS]\nAvant d'ouvrir le boîtier, il faut {1} l'ordinateur et {2} le câble d'alimentation.\n"
       "A. éteindre\nB. débrancher\nC. redémarrer\nD. mettre en veille")
check("question 16 de l'examen 170 : aucun défaut", G.defects('trous', q16, "• Réponse : 1-A, 2-B") == [])
check("aucune proposition en trop = défaut", any('en trop' in x for x in G.defects('trous', "Question 1 — t (1 pt) [TROUS]\nIl faut {1} le {2}.\nA. activer\nB. système", "• Réponse : 1-A, 2-B")))
check("un mot isolé identique n'est PAS un défaut (« le » / « système »)", G.defects('trous', "Question 1 — t (1 pt) [TROUS]\nIl faut {1} le système {2}.\nA. activer\nB. mettre à jour\nC. système\nD. éteindre", "• Réponse : 1-A, 2-B") == [])

print('\n== Seconde lecture : disagreements() ==')
content = ("Question 1 — A ............. (1 pts) [QCM]\nÉnoncé 1 ?\nA) a\nB) b\nC) c\nD) d\n\nQuestion 2 — B ............. (1 pts) [QCM]\nÉnoncé 2 ?\nA) a\nB) b\nC) c\nD) d\n")
rubric = "Question 1 — A (1 pts)\n  • Bonne réponse : B) — x\n\nQuestion 2 — B (1 pts)\n  • Bonne réponse : B) — x\n"
dg = disagreements(content, rubric, ask=lambda p: "1: B\n2: A")
check("une divergence est rapportée avec le numéro", [x[0] for x in dg] == ['2'], dg)
check("verify_keys garde le même message pour l'enseignant", len(verify_keys(content, rubric, ask=lambda p: "1: B\n2: A")) == 1)
check("tout concorde : aucune divergence", disagreements(content, rubric, ask=lambda p: "1: B\n2: B") == [])

print('\n== Génération : une question à clé contestée est refaite ==')
s = get_session(); tag = uuid.uuid4().hex[:6]
adm = User(email=f'qual.{tag}@test.invalid', full_name='Qual Test', role=UserRole.ADMIN, is_active=True, email_verified=True, has_email=True, password_hash='x')
s.add(adm); s.commit()
H = {'Authorization': 'Bearer ' + create_access_token(adm.id, 'admin', adm.email)}
real = R.call_claude
calls = {'gen': 0, 'ver': 0}
_WORDS = ['mémoire vive volatile', 'chiffrement symétrique des données', 'pare-feu applicatif réseau', 'sauvegarde incrémentale nocturne', 'authentification multifacteur',
          'virtualisation des serveurs', 'gestion des correctifs', 'dissipation thermique du processeur', 'onduleur en ligne', 'redondance des disques']
_cnt = [0]
def uniq():
    _cnt[0] += 1
    return f"{_WORDS[_cnt[0] % len(_WORDS)]} numéro {_cnt[0]} sujet {_WORDS[(_cnt[0] * 3) % len(_WORDS)]}"
def fake(sp, um, **kw):
    if 'Réponds à chaque question' in um:           # seconde lecture : « AMBIGUE » est une question dont le lecteur trouve autre chose
        calls['ver'] += 1
        lines = []
        for m in re.finditer(r'(?ms)^Question (\d+) —(.*?)(?=^Question \d+ —|\Z)', um):
            lines.append(f"{m.group(1)}: {'A' if 'AMBIGUE' in m.group(2) else 'B'}")
        return '\n'.join(lines)
    calls['gen'] += 1
    n = int(re.search(r'EXACTEMENT (\d+) questions', um).group(1))
    qs = ''.join(f"Question {i} — Titre {i} ............. (1 pt) [QCM]\n" + (f"Énoncé AMBIGUE {i} sur {uniq()} ?\n" if (n > 1 and i == 2) else f"Énoncé clair {i}{' REFAITE' if n == 1 else ''} sur {uniq()} ?\n") + "A) a\nB) b\nC) c\nD) d\n\n" for i in range(1, n + 1))
    return qs.rstrip() + "\n\nBARÈME:\n" + '\n\n'.join(f"  • Bonne réponse : B) — car b{i}" for i in range(1, n + 1))
R.call_claude = fake
try:
    body = {'suggestion': {'title': 'Test', 'question_types': 'QCM (une seule réponse)', 'question_count': 4, 'total_points': 4,
                           'key_points': ['a'], 'difficulty': 'Moyen', 'duration': 30, 'student_level': 'Licence 1'}}
    r = A.app.test_client().post('/api/subjects/generate-full-exam', headers=H, json=body); j = r.get_json() or {}
    cont = j.get('content', '')
    check("génération réussie, 4 questions", r.status_code == 200 and len(re.findall(r'(?m)^Question \d+ —', cont)) == 4, (r.status_code, str(j)[:200]))
    check("la question contestée n'est plus dans le sujet", 'AMBIGUE' not in cont, cont[-600:])
    check("elle a été remplacée par une question refaite", 'REFAITE' in cont)
    check("aucun avertissement « à relire »", not any('à relire' in w for w in (j.get('type_warnings') or [])), j.get('type_warnings'))
    check("le total reste 4 points", 'TOTAL : 4 / 4 points' in j.get('rubric', ''), j.get('rubric', '')[-100:])

    print('\n== Une question qui reste contestée est signalée, jamais bloquante ==')
    def fake_stubborn(sp, um, **kw):
        if 'Réponds à chaque question' in um:
            return '\n'.join(f"{m.group(1)}: {'A' if 'AMBIGUE' in m.group(2) else 'B'}" for m in re.finditer(r'(?ms)^Question (\d+) —(.*?)(?=^Question \d+ —|\Z)', um))
        n = int(re.search(r'EXACTEMENT (\d+) questions', um).group(1))
        qs = ''.join(f"Question {i} — Titre {i} ............. (1 pt) [QCM]\nÉnoncé AMBIGUE {i} sur {uniq()} ?\nA) a\nB) b\nC) c\nD) d\n\n" if i == 2 or n == 1 else
                     f"Question {i} — Titre {i} ............. (1 pt) [QCM]\nÉnoncé clair {i} sur {uniq()} ?\nA) a\nB) b\nC) c\nD) d\n\n" for i in range(1, n + 1))
        return qs.rstrip() + "\n\nBARÈME:\n" + '\n\n'.join(f"  • Bonne réponse : B) — car b{i}" for i in range(1, n + 1))
    R.call_claude = fake_stubborn
    r = A.app.test_client().post('/api/subjects/generate-full-exam', headers=H, json=body); j = r.get_json() or {}
    check("génération réussie malgré une question toujours contestée", r.status_code == 200 and len(re.findall(r'(?m)^Question \d+ —', j.get('content', ''))) == 4, (r.status_code, str(j)[:200]))
    check("l'enseignant est prévenu (« à relire »)", any('à relire' in w for w in (j.get('type_warnings') or [])), j.get('type_warnings'))
finally:
    R.call_claude = real
    s.rollback(); u = s.get(User, adm.id)
    if u: s.delete(u); s.commit()
bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS)-len(bad)}/{len(RESULTS)} vérifications réussies")
sys.exit(1 if bad else 0)
