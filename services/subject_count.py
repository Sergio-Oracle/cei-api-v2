"""Nombre de questions d'un sujet importé : compté comme l'enseignant les compte (une réponse notée = une question).

  count_units(blocks)        -> association de 5 notions = 5, texte à trous à 3 champs = 3, mots croisés de 5 mots = 5, QCM = 1…
  declared_total(text)       -> « Total 25 questions » lu dans le document, s'il y figure
  count_from_files(files)    -> comptage SANS rien enregistrer (bouton « Compter » de la page de création)
"""
import os
import re
import tempfile

from werkzeug.utils import secure_filename


def count_units(blocks):
    total, by_type = 0, {}
    for b in blocks:
        t = b.get('type')
        if t in ('section', 'description', 'text'):
            continue
        if t in ('association', 'tri', 'zones', 'cloze', 'trous') and b.get('items'):
            n = len(b['items'])
        elif t == 'appariement' and b.get('pairs'):
            n = int(b.get('draw') or len(b['pairs']))
        else:
            n = 1
        total += n
        by_type[t] = by_type.get(t, 0) + n
    return total, by_type


def declared_total(text):
    m = re.search(r'^\s*Total\s+(\d{1,3})\s+questions?\b', text or '', re.I | re.M)
    return int(m.group(1)) if m else None


def count_from_files(files):
    from utils import extract_text_from_file
    from routes.exams import _parse_subject_blocks_ordered
    from services.exam_normalizer import normalize_exam_text
    from services.subject_normalizer import loose_mcq
    from services.type_detector import detect_and_annotate
    from services.crossword_pdf import extract_crosswords
    texts, crosswords, declared = [], [], None
    with tempfile.TemporaryDirectory() as d:
        for f in files or []:
            if not f or not f.filename:
                continue
            path = os.path.join(d, secure_filename(f.filename) or 'f')
            f.save(path)
            if re.search(r'corrig|correction|bar[eè]me|solution', f.filename, re.I):
                continue
            text = extract_text_from_file(path)
            if not text:
                continue
            declared = declared or declared_total(text)
            crosswords += extract_crosswords(path)
            part, rep = normalize_exam_text(text)
            if rep.get('structured'):
                texts.append(part)
            else:
                part, _k, _n = loose_mcq(part, 1)
                texts.append(detect_and_annotate(part, crosswords)[0])
    content = '\n\n'.join(texts)
    total, by_type = count_units(_parse_subject_blocks_ordered(content)) if content else (0, {})
    return {'questions': total, 'by_type': by_type, 'declared': declared}
