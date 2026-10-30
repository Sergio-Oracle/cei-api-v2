"""Lecture de la GRILLE de mots croisés dessinée dans un PDF (cases = petits rectangles, numéros = petits chiffres).

Le texte d'un PDF ne contient pas la grille (incident du 30/10/2026, examen 142 : seules les définitions restaient).
On reconstruit la grille à partir des traits dessinés, puis on la rattache aux définitions « Horizontalement /
Verticalement » du texte. Si quoi que ce soit ne concorde pas (longueurs, numéros), on ne renvoie RIEN : le système
retombe alors sur de simples réponses courtes, jamais sur une grille fausse.

  extract_crosswords(path_pdf) -> [ {'page': n, 'rows': R, 'cols': C, 'words': [ {'num','dir','row','col','len'} ] } ]
"""
import statistics


def _cells(page):
    """Cases = traits horizontaux dont le trait du dessous existe aussi une case plus bas. Retourne (cases, pas, x0, y0)."""
    horiz = [(r['x0'], r['top']) for r in page.rects
             if (r['bottom'] - r['top']) <= 2 and 15 <= (r['x1'] - r['x0']) <= 60]
    if len(horiz) < 8:
        return None
    xs = sorted({round(x, 0) for x, _ in horiz})
    ys = sorted({round(y, 0) for _, y in horiz})
    dx = [b - a for a, b in zip(xs, xs[1:]) if b - a > 10]
    dy = [b - a for a, b in zip(ys, ys[1:]) if b - a > 10]
    if not dx or not dy:
        return None
    pitch = statistics.median(dx + dy)
    x0, y0 = min(x for x, _ in horiz), min(y for _, y in horiz)
    edges = {(round((x - x0) / pitch), round((y - y0) / pitch)) for x, y in horiz}
    ymax = max(y for _, y in horiz) + pitch
    vert = [(r['x0'], r['top']) for r in page.rects
            if (r['x1'] - r['x0']) <= 2 and 15 <= (r['bottom'] - r['top']) <= 60 and y0 - 3 <= r['top'] <= ymax]
    if not vert:
        return None
    vx0 = min(x for x, _ in vert)
    vedges = {(round((x - vx0) / pitch), round((y - y0) / pitch)) for x, y in vert}
    # une case a ses 4 côtés : haut + bas (traits horizontaux) ET gauche + droite (traits verticaux) — sinon c'est un vide entouré
    cells = {(row, col) for (col, row) in edges
             if (col, row + 1) in edges and (col, row) in vedges and (col + 1, row) in vedges}
    return (cells, pitch, x0, y0) if cells else None


def extract_crosswords(path):
    try:
        import pdfplumber
    except Exception:
        return []
    grids = []
    try:
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ''
                if not ('Horizontalement' in text and 'Verticalement' in text):
                    continue
                got = _cells(page)
                if not got:
                    continue
                cells, size, x0, y0 = got
                rows = max(r for r, _ in cells) + 1
                cols = max(c for _, c in cells) + 1
                # numéros : petits chiffres posés dans l'angle haut-gauche d'une case
                labels = {}
                for w in page.extract_words():
                    if w['text'].isdigit() and (w['bottom'] - w['top']) < 8:
                        c = int((w['x0'] - x0) // size)
                        r = int((w['top'] - y0) // size)
                        if (r, c) in cells:
                            labels[(r, c)] = int(w['text'])
                words = []
                for (r, c), num in sorted(labels.items(), key=lambda kv: kv[1]):
                    if (r, c - 1) not in cells and (r, c + 1) in cells:
                        n = 0
                        while (r, c + n) in cells:
                            n += 1
                        words.append({'num': num, 'dir': 'H', 'row': r, 'col': c, 'len': n})
                    if (r - 1, c) not in cells and (r + 1, c) in cells:
                        n = 0
                        while (r + n, c) in cells:
                            n += 1
                        words.append({'num': num, 'dir': 'V', 'row': r, 'col': c, 'len': n})
                if words:
                    grids.append({'page': pno, 'rows': rows, 'cols': cols, 'words': words})
    except Exception:
        return []
    return grids
