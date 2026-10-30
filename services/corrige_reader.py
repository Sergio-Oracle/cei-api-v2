"""Lecture d'un CORRIGÉ officiel (Word ou PDF) pour écrire le barème sans IA, sans erreur de recopie.

Format traité : exercices « Exercice N. Titre (X points) » avec, par exercice :
  QCM          « 3. B. AD.NA.PI.JA. justification… »
  Vrai/Faux    tableau « N | affirmation | Vrai ou Faux | justification »
  Association  tableau « 1. Notion [S1] | d | définition »
  Texte à trous phrase complétée, la réponse est EN GRAS dans le Word (la justification est en italique)
  Mots croisés « 3. EXHAUSTIVITE  définition… » (ou la grille remplie du Word)

  read_corrige_file(path, text) -> {'qcm': [..], 'vf': [..], 'assoc': {k: lettre}, 'trous': [[réponses]], 'cw': {(sens, n): MOT}, 'warnings': [..]}
  rubric_from_exercise_key(content, key) -> (barème, avertissements)  — vide si UNE seule question n'a pas sa réponse (l'IA lit alors le corrigé)
"""
import re

_EX_RE = re.compile(r'^\s*Exercice\s+(\d+)\s*\.\s*(.+?)\s*(?:\(\s*\d+(?:[.,]\d+)?\s*points?\s*\))?\s*$', re.I)
_PTS_RE = re.compile(r'\(\s*(\d+(?:[.,]\d+)?)\s*points?\s*\)', re.I)


def _kind(title):
    t = (title or '').lower()
    if 'vrai' in t and 'faux' in t:
        return 'vf'
    if 'associ' in t:
        return 'assoc'
    if 'trous' in t or 'compl' in t:
        return 'trous'
    if 'crois' in t:
        return 'cw'
    if 'qcm' in t or 'choix' in t or 'questionnaire' in t:
        return 'qcm'
    return 'other'


def _text_keys(text, key):
    """Clés lisibles dans le TEXTE (Word comme PDF) : QCM, Vrai/Faux, association, mots croisés."""
    kind = None
    for raw in (text or '').split('\n'):
        l = raw.strip()
        m = _EX_RE.match(l)
        if m:
            kind = _kind(m.group(2))
            continue
        if kind == 'qcm':
            q = re.match(r'^(\d{1,2})\.\s+([A-F])\.\s', l)
            if q:
                key['qcm'].append(q.group(2))
        elif kind == 'vf':
            q = re.match(r'^(\d{1,2})\s+.+?\s+(Vrai|Faux)\s', l + ' ')
            if q:
                key['vf'].append(q.group(2))
        elif kind == 'assoc':
            q = re.match(r'^(\d{1,2})\.\s+.+?\s+(?:\[[^\]]*\]\s+)?([a-f])\s+\S', l)
            if q:
                key['assoc'][int(q.group(1))] = q.group(2).upper()
        elif kind == 'cw':
            q = re.match(r'^(\d{1,2})\.\s+([A-ZÉÈÀÂÊÎÔÛÇ]{3,})\s', l)
            if q:
                key.setdefault('_cw_lines', []).append((int(q.group(1)), q.group(2)))
    return key


def _docx_trous(path, key):
    """Réponses des textes à trous = passages en GRAS (hors numéro) des phrases de l'exercice « trous »."""
    try:
        import docx
        doc = docx.Document(path)
    except Exception:
        return
    kind = None
    for p in doc.paragraphs:
        m = _EX_RE.match(p.text.strip())
        if m:
            kind = _kind(m.group(2))
            continue
        if kind != 'trous' or not re.match(r'^\s*\d{1,2}\.\s', p.text):
            continue
        answers = [r.text.strip() for r in p.runs if r.bold and not r.italic and r.text.strip() and not re.fullmatch(r'\d+\.', r.text.strip())]
        if answers:
            key['trous'].append(answers)


def _pdf_trous(path, key):
    """PDF : mêmes réponses = mots en GRAS (police « Bold ») des phrases numérotées de l'exercice « trous »."""
    try:
        import pdfplumber
    except Exception:
        return
    kind, cur, sentences = None, None, []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                rows = {}
                for w in page.extract_words(extra_attrs=['fontname']):
                    rows.setdefault(round(w['top'] / 3), []).append(w)
                for _, ws in sorted(rows.items()):
                    ws.sort(key=lambda w: w['x0'])
                    line = ' '.join(w['text'] for w in ws)
                    m = _EX_RE.match(line)
                    if m:
                        if cur is not None:
                            sentences.append(cur); cur = None
                        kind = _kind(m.group(2))
                        continue
                    if kind != 'trous':
                        continue
                    if re.match(r'^\d{1,2}\.\s', line):
                        if cur is not None:
                            sentences.append(cur)
                        cur = []
                    if cur is not None:
                        run = []
                        for w in ws:
                            if 'bold' in (w.get('fontname') or '').lower() and not re.fullmatch(r'\d{1,2}\.', w['text']):
                                run.append(w['text'])
                            elif run:
                                cur.append(' '.join(run)); run = []
                        if run:
                            cur.append(' '.join(run))
        if cur is not None:
            sentences.append(cur)
    except Exception:
        return
    key['trous'] += [x for x in sentences if x]


def _docx_crossword(path, key):
    try:
        from services.crossword_pdf import extract_crosswords_docx
        for g in extract_crosswords_docx(path):
            for w in g['words']:
                if w.get('answer'):
                    key['cw'][(w['dir'], w['num'])] = w['answer']
    except Exception:
        pass


def read_corrige_file(path, text):
    key = {'qcm': [], 'vf': [], 'assoc': {}, 'trous': [], 'cw': {}, 'warnings': []}
    _text_keys(text, key)
    low = (path or '').lower()
    if low.endswith('.docx'):
        _docx_trous(path, key)
        _docx_crossword(path, key)
    elif low.endswith('.pdf'):
        _pdf_trous(path, key)
    # mots croisés lus dans le texte (« 3. EXHAUSTIVITE … ») : sens = dernier « Horizontalement / Verticalement » rencontré
    if not key['cw'] and key.get('_cw_lines'):
        direction, lines = None, (text or '').split('\n')
        for raw in lines:
            l = raw.strip()
            if re.match(r'^Horizontalement', l, re.I):
                direction = 'H'
            elif re.match(r'^Verticalement', l, re.I):
                direction = 'V'
            elif direction:
                q = re.match(r'^(\d{1,2})\.\s+([A-ZÉÈÀÂÊÎÔÛÇ]{3,})\s', l)
                if q:
                    key['cw'][(direction, int(q.group(1)))] = q.group(2)
    key.pop('_cw_lines', None)
    return key


def merge(keys):
    out = {'qcm': [], 'vf': [], 'assoc': {}, 'trous': [], 'cw': {}, 'warnings': []}
    for k in keys:
        out['qcm'] += k['qcm']; out['vf'] += k['vf']; out['trous'] += k['trous']
        out['assoc'].update(k['assoc']); out['cw'].update(k['cw'])
    return out


def _fmt(x):
    return (str(int(x)) if float(x).is_integer() else f'{x:.2f}'.rstrip('0').rstrip('.')).replace('.', ',')


def _variants(ans):
    v = [ans.strip()]
    inner = re.sub(r'^[(\[]\s*|\s*[)\]]$', '', ans.strip())
    if inner and inner != ans.strip():
        v.append(inner)                                # « (II) » accepté aussi sous la forme « II »
    return '|'.join(dict.fromkeys(x for x in v if x))


def rubric_from_exercise_key(content, key):
    """Barème écrit question par question d'après les clés ; ('', avertissements) si une question n'a pas sa réponse."""
    from routes.exams import _parse_subject_blocks_ordered
    blocks = _parse_subject_blocks_ordered(content)
    # points par exercice : « Exercice N. … (4 points) » du sujet
    pts_of, cur = {}, None
    for b in blocks:
        if b['type'] == 'section':
            cur = b['title']
            m = _PTS_RE.search(cur)
            pts_of[cur] = float(m.group(1).replace(',', '.')) if m else None
    count = {}
    cur = None
    for b in blocks:
        if b['type'] == 'section':
            cur = b['title']
        elif b['type'] != 'description':
            count[cur] = count.get(cur, 0) + 1
    qcm, vf, trous = list(key['qcm']), list(key['vf']), list(key['trous'])
    out, warns, total = ['=== BARÈME DE NOTATION (écrit depuis le corrigé officiel) ===', ''], [], 0.0
    cur = None
    for b in blocks:
        if b['type'] == 'section':
            cur = b['title']; continue
        if b['type'] == 'description':
            continue
        n = b['num']
        pts = pts_of.get(cur)
        if not pts:
            warns.append(f"Question {n} : points de l'exercice introuvables"); continue
        per = pts / max(1, count.get(cur, 1))
        head = f"Question {n} ({_fmt(per)} pts) :"
        if b['type'] == 'qcm':
            L = qcm.pop(0) if qcm else None
            txt = next((c['text'] for c in b['choices'] if c['letter'] == L), None)
            if not L or txt is None:
                warns.append(f"Question {n} : bonne réponse introuvable dans le corrigé"); continue
            out += [head, f"  • Bonne réponse : {L}) {txt}", '']
        elif b['type'] == 'vf':
            v = vf.pop(0) if vf else None
            if not v:
                warns.append(f"Question {n} : réponse Vrai/Faux introuvable"); continue
            out += [head, f"  • Réponse : {v}", '']
        elif b['type'] == 'association':
            items = b.get('items') or []
            if not items or any(i + 1 not in key['assoc'] for i in range(len(items))):
                warns.append(f"Question {n} : association incomplète dans le corrigé"); continue
            out += [head, "  • Réponse : " + ', '.join(f"{i + 1}-{key['assoc'][i + 1]}" for i in range(len(items))), '']
        elif b['type'] == 'cloze' and b.get('grid'):
            fields = []
            for w in b['grid']:
                a = key['cw'].get((w['dir'], w['num']))
                if not a or len(a) != w['len']:
                    fields = None; break
                fields.append(f"{w['k']}={a}")
            if not fields:
                warns.append(f"Question {n} : mots croisés incomplets dans le corrigé"); continue
            out += [head, "  • Champs : " + ' ; '.join(fields), '']
        elif b['type'] == 'cloze':
            a = trous.pop(0) if trous else None
            items = b.get('items') or []
            if not a or len(a) != len(items):
                warns.append(f"Question {n} : réponse(s) du texte à trous introuvable(s) ({len(items)} champ(s) attendu(s))"); continue
            out += [head, "  • Champs : " + ' ; '.join(f"{it['gap']}={_variants(x)}" for it, x in zip(items, a)), '']
        else:
            warns.append(f"Question {n} ({b['type']}) : type sans clé automatique"); continue
        total += per
    if warns:
        return '', warns
    out += ['──────────────────────────', f'TOTAL : {_fmt(total)} / {_fmt(total)} points']
    return '\n'.join(out), []
