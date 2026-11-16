#!/usr/bin/env python
"""Repli d'extraction PDF : pdfplumber → pypdf (remplace PyPDF2, abandonné et vulnérable, 16/11/2026).
    cd cei-api-v2 && .venv-base/bin/python scripts/test_extraction_pdf_repli.py   (aucune écriture)"""
import sys, os, signal, tempfile
sys.path.insert(0, '.')
import utils

RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {str(d)[:200]}' if d and not ok else ''))

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
tmp = tempfile.mkdtemp()
pdf = os.path.join(tmp, 'sujet.pdf')
c = canvas.Canvas(pdf, pagesize=A4)
for page in range(2):
    y = 800
    for i in range(1, 22):
        c.drawString(60, y, f"Question {page * 21 + i}. Quelle est la reponse correcte parmi les propositions suivantes du cours ?"); y -= 28
    c.showPage()
c.save()
ref = utils.extract_text_from_pdf(pdf)
chk("extraction normale (pdfplumber) : texte obtenu", bool(ref) and len(ref) > 200)

real = sys.modules.get('pdfplumber')
sys.modules['pdfplumber'] = None                      # `import pdfplumber` lève ImportError → on force le repli
try:
    fb = utils.extract_text_from_pdf(pdf)
finally:
    if real is not None: sys.modules['pdfplumber'] = real
    else: sys.modules.pop('pdfplumber', None)
chk("repli pypdf (pdfplumber indisponible) : texte obtenu", bool(fb) and len(fb) > 200)
if ref and fb:
    a, b = set(ref.split()), set(fb.split())
    chk("le repli retrouve au moins 90 % des mots de l'extraction normale", len(a & b) / max(1, len(a)) >= 0.9, round(len(a & b) / max(1, len(a)), 3))

def _timeout(*_): raise TimeoutError('trop long')
signal.signal(signal.SIGALRM, _timeout)
with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
    f.write(b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n' + os.urandom(2000) + b'\n%%EOF'); bad = f.name
signal.alarm(60)
try:
    out = utils.extract_text_from_pdf(bad); ok = out is None or isinstance(out, str)
except TimeoutError:
    ok = False
except Exception as e:
    ok = False
finally:
    signal.alarm(0); os.unlink(bad); os.unlink(pdf); os.rmdir(tmp)
chk("fichier PDF corrompu : pas de blocage ni de plantage (réponse en moins de 60 s)", ok)
print(f"\n{sum(RES)}/{len(RES)} vérifications réussies"); sys.exit(0 if all(RES) else 1)
