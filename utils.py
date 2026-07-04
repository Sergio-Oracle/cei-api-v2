"""Utilitaires avancés - VERSION CORRIGÉE
- Envoi d'emails (SANS timeout sur starttls - compatible Python < 3.9)
- Détection de doublons (hash)
- Extraction de texte avec OCR
- Export PDF de copies corrigées
"""
import logging
import os
import hashlib
import smtplib

_log = logging.getLogger('cei.utils')
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import PyPDF2
import docx
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from io import BytesIO
from datetime import datetime
import re
import unicodedata

# Configuration SMTP — chemin absolu vers .env pour éviter tout problème de répertoire courant
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')

def _smtp_config():
    from dotenv import dotenv_values
    env = dotenv_values(_ENV_PATH)
    return {
        'server':     env.get('SMTP_SERVER', os.getenv('SMTP_SERVER', 'smtp.gmail.com')),
        'port':       int(env.get('SMTP_PORT', os.getenv('SMTP_PORT', 587))),
        'username':   env.get('SMTP_USERNAME', os.getenv('SMTP_USERNAME', '')),
        'password':   env.get('SMTP_PASSWORD', os.getenv('SMTP_PASSWORD', '')),
        'from_email': env.get('SMTP_FROM_EMAIL', os.getenv('SMTP_FROM_EMAIL', 'noreply@examgrading.com')),
        'from_name':  env.get('SMTP_FROM_NAME', os.getenv('SMTP_FROM_NAME', "CEI — Centre d'Examen Intelligent")),
        'app_url':    env.get('APP_URL', os.getenv('APP_URL', 'https://dev-cei.ddns.net')).rstrip('/'),
    }

# Compatibilité ascendante (utilisé dans send_email)
SMTP_SERVER   = 'smtp.gmail.com'
SMTP_PORT     = 587
SMTP_USERNAME = ''
SMTP_PASSWORD = ''
SMTP_FROM_EMAIL = 'noreply@examgrading.com'
SMTP_FROM_NAME  = 'CEI — Centre d\'Examen Intelligent'

# ============================================================================
# DÉTECTION DE DOUBLONS (SHA256)
# ============================================================================

def calculate_file_hash(file_path):
    """Calculer le hash SHA256 d'un fichier"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def calculate_content_hash(content):
    """Calculer le hash SHA256 d'un contenu texte"""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()

# ============================================================================
# EXTRACTION DE TEXTE (avec OCR si nécessaire)
# ============================================================================

def extract_text_from_file(filepath):
    """Extraire le texte d'un fichier PDF, DOCX ou TXT"""
    ext = filepath.lower().split('.')[-1]

    try:
        if ext == 'pdf':
            return extract_text_from_pdf(filepath)
        elif ext in ['docx', 'doc']:
            return extract_text_from_docx(filepath)
        elif ext == 'txt':
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
        else:
            return None
    except Exception as e:
        print(f"⚠️ Erreur extraction texte: {e}")
        return None

def _is_garbled(text):
    """Détecter si le texte extrait est du charabia CIDFont."""
    if not text or len(text) < 20:
        return True

    # Format PyPDF2 : /0 /1 /i255 /3/4 ...
    tokens = text.split()
    if tokens:
        garbled_tokens = sum(1 for t in tokens if re.match(r'^(/[i]?\d+)+$', t))
        if garbled_tokens / len(tokens) > 0.15:
            return True

    # Format pdfplumber : (cid:3) séquences intégrées
    cid_count = len(re.findall(r'\(cid:\d+\)', text))
    if cid_count > 5:
        return True

    # Texte lisible : au moins 50 caractères de vraies lettres latines
    real_chars = re.findall(r'[a-zA-ZÀ-ÿ]', text)
    if len(real_chars) < 50:
        return True

    return False


def _ocr_pdf(filepath):
    """OCR via pdftoppm + tesseract (fallback ultime pour PDFs non décodables)."""
    import subprocess, tempfile, glob
    try:
        res = subprocess.run(['which', 'tesseract'], capture_output=True)
        if res.returncode != 0:
            print("⚠️ tesseract absent — lance en root : apt install -y tesseract-ocr tesseract-ocr-fra")
            return None
        with tempfile.TemporaryDirectory() as tmpdir:
            img_prefix = os.path.join(tmpdir, 'page')
            r = subprocess.run(
                ['pdftoppm', '-r', '200', '-png', filepath, img_prefix],
                capture_output=True, timeout=120
            )
            if r.returncode != 0:
                print(f"⚠️ pdftoppm erreur : {r.stderr.decode()[:200]}")
                return None
            images = sorted(glob.glob(os.path.join(tmpdir, '*.png')))
            if not images:
                return None
            pages_text = []
            for img_path in images:
                out_prefix = img_path.replace('.png', '_ocr')
                subprocess.run(
                    ['tesseract', img_path, out_prefix, '-l', 'fra+eng', '--psm', '6'],
                    capture_output=True, timeout=60
                )
                out_txt = out_prefix + '.txt'
                if os.path.exists(out_txt):
                    with open(out_txt, 'r', encoding='utf-8', errors='replace') as f:
                        pages_text.append(f.read())
            result = '\n'.join(pages_text).strip()
            return result if result else None
    except Exception as e:
        print(f"⚠️ OCR erreur : {e}")
        return None


def extract_text_from_pdf(filepath):
    """Extraire le texte d'un PDF : pdfplumber → PyPDF2 → OCR tesseract."""
    # 1. pdfplumber (meilleur support CMap/CIDFont)
    try:
        import pdfplumber
        pages_text = []
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
        text = "\n".join(pages_text).strip()
        if text and not _is_garbled(text):
            print(f"✅ pdfplumber OK — {len(text)} caractères")
            return text
        print("⚠️ pdfplumber : texte garbled, bascule PyPDF2")
    except Exception as e:
        print(f"⚠️ pdfplumber erreur : {e}")

    # 2. PyPDF2
    try:
        text = ""
        with open(filepath, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        text = text.strip()
        if text and not _is_garbled(text):
            print(f"✅ PyPDF2 OK — {len(text)} caractères")
            return text
        print("⚠️ PyPDF2 : texte garbled, bascule OCR")
    except Exception as e:
        print(f"⚠️ Erreur PDF PyPDF2: {e}")

    # 3. OCR tesseract (pour PDFs avec polices non-Unicode)
    print("🔍 Tentative OCR tesseract...")
    text = _ocr_pdf(filepath)
    if text and not _is_garbled(text):
        print(f"✅ OCR OK — {len(text)} caractères")
        return text

    print("❌ Impossible d'extraire le texte de ce PDF (police non-Unicode, tesseract requis)")
    return None

def extract_text_from_docx(filepath):
    """Extraire le texte d'un DOCX"""
    try:
        doc = docx.Document(filepath)
        text = "\n".join([para.text for para in doc.paragraphs])
        return text.strip()
    except Exception as e:
        print(f"⚠️ Erreur DOCX: {e}")
        return None

def extract_student_name_from_content_improved(content):
    """Extraction améliorée du nom étudiant
    Évite les patterns incorrects comme "Copie Sdn/..." 
    """
    # Nettoyer le contenu
    content_lines = content.split('\n')

    # Patterns à éviter (false positives)
    avoid_patterns = [
        r'^Copie\s+',
        r'^\d+\.',
        r'^Question\s+',
        r'^Exercice\s+',
        r'^Problème\s+',
        r'^Examen\s+',
        r'^Test\s+',
        r'^Devoir\s+',
    ]

    # Patterns pour trouver le nom
    name_patterns = [
        r'Nom\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Nom\s+et\s+Prénom\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Prénom\s+et\s+Nom\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Nom\s+Prénom\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Étudiant\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Candidat\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Nom\s+complet\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
        r'Élève\s*:\s*([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\s\'-]+)',
    ]

    # Essayer chaque pattern
    for pattern in name_patterns:
        match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
        if match:
            name = match.group(1).strip()

            # Vérifier que ce n'est pas un false positive
            is_valid = True
            for avoid in avoid_patterns:
                if re.match(avoid, name, re.IGNORECASE):
                    is_valid = False
                    break

            if is_valid and len(name) >= 4 and ' ' in name:
                return name

    # Fallback: chercher dans les 10 premières lignes un pattern NOM Prénom
    for line in content_lines[:10]:
        line = line.strip()
        if len(line) < 5 or len(line) > 50:
            continue

        # Vérifier les patterns à éviter
        skip = False
        for avoid in avoid_patterns:
            if re.match(avoid, line, re.IGNORECASE):
                skip = True
                break

        if skip:
            continue

        # Pattern: MAJUSCULE Minuscule (ex: BOUNGUELE Serge)
        match = re.match(r'^([A-ZÀ-Ÿ]{2,})\s+([A-ZÀ-Ÿ][a-zà-ÿ]+)$', line)
        if match:
            return f"{match.group(1)} {match.group(2)}"

        # Pattern: Majuscule MAJUSCULE (ex: Serge BOUNGUELE)
        match = re.match(r'^([A-ZÀ-Ÿ][a-zà-ÿ]+)\s+([A-ZÀ-Ÿ]{2,})$', line)
        if match:
            return f"{match.group(1)} {match.group(2)}"

    return None

extract_student_name_from_content = extract_student_name_from_content_improved

# ============================================================================
# EXPORT PDF DE COPIES CORRIGÉES
# ============================================================================

def generate_corrected_paper_pdf(paper_data, output_path):
    """Générer un PDF de la copie corrigée
    paper_data = {'student_name', 'subject_title', 'score', 'grade', 'corrected_at'}
    """
    doc = SimpleDocTemplate(output_path, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    # Style personnalisé
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor('#2563eb'),
        spaceAfter=30,
        alignment=1 # Centré
    )

    header_style = ParagraphStyle(
        'CustomHeader',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=colors.HexColor('#1e293b'),
        spaceAfter=12
    )

    # En-tête
    story.append(Paragraph("📋 COPIE CORRIGÉE", title_style))
    story.append(Spacer(1, 0.2*inch))

    # Informations
    info_data = [
        ['Étudiant:', paper_data.get('student_name', 'N/A')],
        ['Sujet:', paper_data.get('subject_title', 'N/A')],
        ['Note:', f"{paper_data.get('score', 0)}/20"],
        ['Date:', datetime.fromisoformat(paper_data.get('corrected_at', datetime.now().isoformat())).strftime('%d/%m/%Y')]
    ]

    info_table = Table(info_data, colWidths=[2*inch, 4*inch])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#f1f5f9')),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#1e293b')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
        ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, colors.HexColor('#f8fafc')])
    ]))

    story.append(info_table)
    story.append(Spacer(1, 0.3*inch))

    # Correction détaillée
    story.append(Paragraph("📝 Correction Détaillée", header_style))
    story.append(Spacer(1, 0.1*inch))

    grade_text = paper_data.get('grade', 'Pas de correction disponible')
    for line in grade_text.split('\n'):
        if line.strip():
            story.append(Paragraph(line, styles['BodyText']))
            story.append(Spacer(1, 0.05*inch))

    # Footer
    story.append(Spacer(1, 0.5*inch))
    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=9,
        textColor=colors.HexColor('#64748b'),
        alignment=1
    )
    story.append(Paragraph("Système de Notation Automatisé - Document généré le " +
                          datetime.now().strftime('%d/%m/%Y à %H:%M'), footer_style))

    # Générer le PDF
    doc.build(story)
    return output_path

# ============================================================================
# ENVOI D'EMAILS — SMTP (config .env) → fallback livraison MX directe
# ============================================================================

def _get_mx_host(domain):
    """Résoudre le serveur MX d'un domaine via DNS (sans subprocess/injection)."""
    import re
    # Validation stricte : seuls les noms de domaine légitimes sont autorisés
    if not re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z]{2,})+$', domain):
        return None
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, 'MX')
        return sorted(answers, key=lambda r: r.preference)[0].exchange.to_text().rstrip('.')
    except Exception:
        pass
    fallbacks = {
        'gmail.com':      'gmail-smtp-in.l.google.com',
        'googlemail.com': 'gmail-smtp-in.l.google.com',
        'yahoo.fr':       'mta5.am0.yahoodns.net',
        'yahoo.com':      'mta5.am0.yahoodns.net',
        'outlook.com':    'outlook-com.olc.protection.outlook.com',
        'hotmail.com':    'outlook-com.olc.protection.outlook.com',
    }
    return fallbacks.get(domain.lower())


def _send_direct_mx(to_email, msg_obj, from_email):
    """Livraison directe au serveur MX du destinataire sur port 25."""
    domain = to_email.split('@')[-1]
    mx_host = _get_mx_host(domain)
    if not mx_host:
        _log.warning('MX introuvable pour domaine %s', domain)
        return False
    print(f"📡 Livraison directe → {mx_host}:25")
    try:
        with smtplib.SMTP(mx_host, 25, timeout=15) as s:
            s.ehlo()
            try:
                s.starttls()
                s.ehlo()
            except Exception:
                pass
            s.sendmail(from_email, [to_email], msg_obj.as_string())
            _log.info('Livraison directe MX OK (domaine: %s)', to_email.split('@')[-1])
            return True
    except Exception as e:
        _log.warning('Livraison directe échouée (domaine: %s): %s', to_email.split('@')[-1], e)
        return False


def _app_domain(cfg):
    """Extrait le domaine racine depuis APP_URL (ex: dev-cei.ddns.net → ddns.net)."""
    import urllib.parse
    netloc = urllib.parse.urlparse(cfg.get('app_url', '')).netloc or 'dev-cei.ddns.net'
    parts  = netloc.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else netloc

def _build_msg(from_header, to_email, subject, html_body, text_body, domain='dev-cei.ddns.net'):
    """Construit un message multipart/alternative avec text+html (norme RFC 2046)."""
    from email.utils import formatdate, make_msgid
    msg = MIMEMultipart('alternative')
    msg['From']       = from_header
    msg['To']         = to_email
    msg['Subject']    = subject
    msg['Date']       = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain=domain)
    msg['X-Mailer']   = 'CEI Platform'
    # plain text en premier (RFC 2046 : le dernier est préféré → HTML sera choisi par défaut)
    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html',  'utf-8'))
    return msg

def send_email(to_email, subject, html_body, attachments=None, text_body=None):
    """Envoyer un email via le SMTP configuré dans .env, fallback livraison MX directe."""
    cfg    = _smtp_config()
    domain = _app_domain(cfg)

    from email.header import Header
    if text_body is None:
        import re
        text_body = re.sub(r'<[^>]+>', '', html_body).strip()

    try:
        cfg['from_name'].encode('ascii')
        from_header = f"{cfg['from_name']} <{cfg['from_email']}>"
    except UnicodeEncodeError:
        from_header = f"{Header(cfg['from_name'], 'utf-8').encode()} <{cfg['from_email']}>"

    msg = _build_msg(from_header, to_email, subject, html_body, text_body, domain=domain)

    if attachments:
        for attachment in attachments:
            try:
                with open(attachment['path'], 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition',
                                   f"attachment; filename= {attachment['filename']}")
                    msg.attach(part)
            except Exception as attach_error:
                print(f"Erreur pièce jointe {attachment['filename']}: {attach_error}")

    # Tentative 1 : SMTP configuré dans .env (port 587 STARTTLS)
    if cfg['username'] and cfg['password']:
        try:
            with smtplib.SMTP(cfg['server'], cfg['port'], timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg['username'], cfg['password'])
                server.send_message(msg)
            _log.info('Email envoyé (domaine: %s, relay: %s)', to_email.split('@')[-1], cfg['server'])
            return True
        except (ConnectionResetError, OSError, smtplib.SMTPServerDisconnected) as conn_err:
            _log.warning('SMTP %s inaccessible (%s) → basculement livraison directe', cfg['server'], conn_err)
        except smtplib.SMTPException as smtp_error:
            _log.error('Erreur SMTP (domaine: %s): %s', to_email.split('@')[-1], smtp_error)
            return False
        except Exception as e:
            print(f"Erreur SMTP ({e}) → basculement livraison directe")

    # Tentative 2 : livraison directe MX (port 25) — expéditeur dérivé de APP_URL pour SPF
    direct_from = f"noreply@{domain}"
    try:
        cfg['from_name'].encode('ascii')
        from_header2 = f"{cfg['from_name']} <{direct_from}>"
    except UnicodeEncodeError:
        from_header2 = f"{Header(cfg['from_name'], 'utf-8').encode()} <{direct_from}>"
    msg2 = _build_msg(from_header2, to_email, subject, html_body, text_body, domain=domain)
    return _send_direct_mx(to_email, msg2, direct_from)

def send_account_created_email(user_email, user_name, role, temp_password=None):
    """Email de création de compte — SVG inline, text/plain, footer générique."""
    smtp_cfg  = _smtp_config()
    app_url   = smtp_cfg['app_url']
    from_name = smtp_cfg.get('from_name', "CEI — Centre d'Examen Intelligent")
    subject   = "Votre compte CEI a été créé — Identifiants de connexion"

    # SVG icons par rôle
    _svg = {
        'student': (
            '#059669', '#ecfdf5', '#a7f3d0', 'Étudiant',
            f'{app_url}/guide-etudiant', 'Guide Étudiant',
            'Vous pouvez désormais accéder à vos examens en ligne, soumettre vos copies et consulter vos résultats.',
            '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" style="display:inline-block;">'
            '<path d="M12 3L1 9l11 6 9-4.91V17h2V9L12 3z" fill="white"/>'
            '<path d="M5 13.18v4L12 21l7-3.82v-4L12 17l-7-3.82z" fill="rgba(255,255,255,0.75)"/>'
            '</svg>'
        ),
        'professor': (
            '#2563eb', '#eff6ff', '#bfdbfe', 'Enseignant',
            f'{app_url}/guide-enseignant', 'Guide Enseignant',
            "Vous pouvez créer des examens, surveiller les sessions en temps réel, corriger les copies avec l'IA et publier les notes.",
            '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" style="display:inline-block;">'
            '<path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="white" stroke-width="2" stroke-linecap="round"/>'
            '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="white" stroke-width="2"/>'
            '<line x1="9" y1="7" x2="15" y2="7" stroke="white" stroke-width="1.5" stroke-linecap="round"/>'
            '<line x1="9" y1="11" x2="15" y2="11" stroke="white" stroke-width="1.5" stroke-linecap="round"/>'
            '</svg>'
        ),
        'surveillant': (
            '#d97706', '#fffbeb', '#fcd34d', 'Surveillant',
            f'{app_url}/guide-surveillant', 'Guide Surveillant',
            "Vous serez affecté à des examens par les enseignants et surveillerez un groupe d'étudiants qui vous sera assigné.",
            '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" style="display:inline-block;">'
            '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="white" stroke-width="2"/>'
            '<circle cx="12" cy="12" r="3" fill="white"/>'
            '</svg>'
        ),
        'admin': (
            '#7c3aed', '#f5f3ff', '#c4b5fd', 'Administrateur',
            f'{app_url}/app', "Accéder à l'interface",
            "Vous disposez d'un accès complet à la plateforme : gestion des utilisateurs, des examens et des statistiques globales.",
            '<svg width="44" height="44" viewBox="0 0 24 24" fill="none" style="display:inline-block;">'
            '<circle cx="12" cy="12" r="3" stroke="white" stroke-width="2"/>'
            '<path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" stroke="white" stroke-width="1.5"/>'
            '</svg>'
        ),
    }
    color, bg, border, label, guide_url, guide_label, desc, icon_svg = _svg.get(
        role.lower(), _svg['student'])

    # SVG icônes dans le corps
    email_svg = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="display:inline-block;vertical-align:middle;margin-right:4px;">'
                 '<rect x="2" y="4" width="20" height="16" rx="2" stroke="#64748b" stroke-width="2"/>'
                 '<path d="M2 7l10 7 10-7" stroke="#64748b" stroke-width="2"/></svg>')
    lock_svg  = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" style="display:inline-block;vertical-align:middle;margin-right:4px;">'
                 '<rect x="5" y="11" width="14" height="10" rx="2" stroke="#64748b" stroke-width="2"/>'
                 '<path d="M8 11V7a4 4 0 018 0v4" stroke="#64748b" stroke-width="2" stroke-linecap="round"/></svg>')
    link_svg  = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" style="display:inline-block;vertical-align:middle;margin-right:4px;">'
                 '<path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
                 '<polyline points="15 3 21 3 21 9" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
                 '<line x1="10" y1="14" x2="21" y2="3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>')
    warn_svg  = ('<svg width="15" height="15" viewBox="0 0 24 24" fill="none" style="display:inline-block;vertical-align:middle;margin-right:5px;">'
                 '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" fill="#fde68a" stroke="#92400e" stroke-width="1.5"/>'
                 '<line x1="12" y1="9" x2="12" y2="13" stroke="#92400e" stroke-width="2" stroke-linecap="round"/>'
                 '<circle cx="12" cy="17" r="1" fill="#92400e"/></svg>')

    pwd_row = (f'<p style="margin:8px 0;font-size:14px;color:#0f172a;">{lock_svg}'
               f'<span style="color:#64748b;">Mot de passe :</span> '
               f'<strong style="font-family:monospace;background:#f1f5f9;padding:2px 8px;border-radius:4px;">{temp_password}</strong></p>'
               if temp_password else '')

    html_body = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;margin:0;padding:24px;">
<div style="max-width:560px;margin:0 auto;">

  <!-- En-tête -->
  <div style="background:linear-gradient(135deg,#1e3a8a 0%,#2563eb 100%);border-radius:14px 14px 0 0;padding:36px;text-align:center;">
    <div style="margin-bottom:14px;">{icon_svg}</div>
    <h1 style="color:#fff;font-size:22px;font-weight:800;margin:0 0 6px;">Centre d'Examen Intelligent</h1>
    <p style="color:rgba(255,255,255,.85);font-size:14px;margin:0;">Votre compte a été créé avec succès</p>
  </div>

  <!-- Corps -->
  <div style="background:#fff;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 14px 14px;padding:36px;">

    <!-- Badge rôle -->
    <div style="display:inline-block;background:{bg};border:1px solid {border};color:{color};padding:6px 18px;border-radius:99px;font-size:13px;font-weight:700;margin-bottom:22px;">
      Rôle : {label}
    </div>

    <p style="font-size:16px;color:#0f172a;margin:0 0 10px;">Bonjour <strong>{user_name}</strong>,</p>
    <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 26px;">{desc}</p>

    <!-- Identifiants -->
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-left:4px solid {color};border-radius:8px;padding:20px;margin-bottom:26px;">
      <p style="font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin:0 0 14px;">Vos identifiants de connexion</p>
      <p style="margin:8px 0;font-size:14px;color:#0f172a;">{email_svg}<span style="color:#64748b;">Email :</span> <strong>{user_email}</strong></p>
      {pwd_row}
    </div>

    <!-- Bouton connexion -->
    <div style="text-align:center;margin-bottom:20px;">
      <a href="{app_url}/app"
         style="display:inline-block;padding:13px 30px;background:{color};color:#fff;text-decoration:none;border-radius:9px;font-size:15px;font-weight:700;">
        Se connecter à la plateforme
      </a>
    </div>

    <!-- Lien guide -->
    <p style="text-align:center;margin:0 0 26px;">
      <a href="{guide_url}" style="font-size:13px;color:{color};text-decoration:underline;">
        {link_svg}Consulter le {guide_label}
      </a>
    </p>

    <!-- Note sécurité -->
    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:14px 16px;">
      <p style="margin:0;font-size:13px;color:#92400e;line-height:1.7;">
        {warn_svg}<strong>Sécurité :</strong> changez votre mot de passe dès votre première connexion
        via les paramètres de votre profil.
      </p>
    </div>
  </div>

  <!-- Pied de page -->
  <div style="text-align:center;padding:18px 0 0;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      {from_name} &nbsp;·&nbsp; <a href="{app_url}" style="color:#94a3b8;">{app_url}</a>
    </p>
  </div>

</div>
</body></html>"""

    text_body = f"""Votre compte CEI a été créé
===========================

Bonjour {user_name},

Rôle : {label}
{desc}

Vos identifiants de connexion :
  Email       : {user_email}
{"  Mot de passe : " + temp_password if temp_password else ""}

Se connecter : {app_url}/app
Guide         : {guide_url}

IMPORTANT : changez votre mot de passe dès votre première connexion.

---
{from_name}
{app_url}
"""
    return send_email(user_email, subject, html_body, text_body=text_body)

def send_paper_corrected_email(student_email, student_name, subject_title, score, paper_id, attachments=None):
    """Email de copie corrigée — SVG inline, text/plain, footer générique."""
    smtp_cfg  = _smtp_config()
    app_url   = smtp_cfg['app_url']
    from_name = smtp_cfg.get('from_name', "CEI — Centre d'Examen Intelligent")
    subject   = f"Votre copie ({subject_title}) a été corrigée"

    score_color  = '#16a34a' if score >= 10 else '#dc2626'
    score_bg     = '#f0fdf4' if score >= 10 else '#fef2f2'
    score_border = '#bbf7d0' if score >= 10 else '#fecaca'
    mention      = 'Admis(e)' if score >= 10 else 'Insuffisant'

    # SVG icône document corrigé
    doc_svg = (
        '<svg width="52" height="52" viewBox="0 0 24 24" fill="none" '
        'xmlns="http://www.w3.org/2000/svg" style="display:inline-block;">'
        '<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" '
        'stroke="white" stroke-width="2" stroke-linecap="round"/>'
        '<polyline points="14 2 14 8 20 8" stroke="white" stroke-width="2" stroke-linecap="round"/>'
        '<line x1="9" y1="13" x2="15" y2="13" stroke="white" stroke-width="2" stroke-linecap="round"/>'
        '<line x1="9" y1="17" x2="13" y2="17" stroke="white" stroke-width="2" stroke-linecap="round"/>'
        '<polyline points="9 9 10 10 12 7" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )
    # SVG icône info (délai reclamation)
    info_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'style="display:inline-block;vertical-align:middle;margin-right:5px;">'
        '<circle cx="12" cy="12" r="10" stroke="#3b82f6" stroke-width="2"/>'
        '<line x1="12" y1="8" x2="12" y2="12" stroke="#3b82f6" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="12" cy="16" r="1" fill="#3b82f6"/>'
        '</svg>'
    )

    html_body = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;margin:0;padding:24px;">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <!-- En-tête -->
  <div style="background:linear-gradient(135deg,#1d4ed8 0%,#2563eb 100%);padding:36px;text-align:center;">
    <div style="margin-bottom:14px;">{doc_svg}</div>
    <h1 style="color:#fff;font-size:21px;font-weight:800;margin:0 0 6px;">Copie corrigée</h1>
    <p style="color:rgba(255,255,255,.85);font-size:14px;margin:0;">{subject_title}</p>
  </div>

  <!-- Corps -->
  <div style="padding:36px;">
    <p style="font-size:15px;color:#0f172a;margin:0 0 22px;">Bonjour <strong>{student_name}</strong>,</p>
    <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 28px;">
      Votre copie pour le sujet <strong>{subject_title}</strong> vient d'être corrigée.
      Voici votre résultat :
    </p>

    <!-- Score -->
    <div style="background:{score_bg};border:2px solid {score_border};border-radius:12px;padding:28px;text-align:center;margin-bottom:28px;">
      <p style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px;">Votre note</p>
      <p style="font-size:52px;font-weight:900;color:{score_color};margin:0;line-height:1;">{score}<span style="font-size:26px;font-weight:600;">/20</span></p>
      <p style="font-size:14px;font-weight:700;color:{score_color};margin:10px 0 0;">{mention}</p>
    </div>

    <!-- CTA -->
    <div style="text-align:center;margin-bottom:26px;">
      <a href="{app_url}/app"
         style="display:inline-block;padding:13px 32px;background:#2563eb;color:#fff;text-decoration:none;border-radius:9px;font-size:15px;font-weight:700;">
        Consulter le détail de ma copie
      </a>
    </div>

    <!-- Info réclamation -->
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:14px 16px;">
      <p style="margin:0;font-size:13px;color:#1e40af;line-height:1.7;">
        {info_svg}<strong>Délai de réclamation :</strong> vous avez <strong>7 jours</strong>
        à compter de la correction pour contester votre note via l'application.
      </p>
    </div>
  </div>

  <!-- Pied de page -->
  <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      {from_name} &nbsp;·&nbsp; <a href="{app_url}" style="color:#94a3b8;">{app_url}</a>
    </p>
  </div>

</div>
</body></html>"""

    text_body = f"""Votre copie ({subject_title}) a été corrigée
{"=" * (len(subject_title) + 30)}

Bonjour {student_name},

Votre note : {score}/20 — {mention}

Consultez le détail de votre copie : {app_url}/app

Vous avez 7 jours pour déposer une réclamation via l'application.

---
{from_name}
{app_url}
"""
    return send_email(student_email, subject, html_body, attachments, text_body=text_body)

# ============================================================================
# VALIDATION DE FICHIERS
# ============================================================================

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ============================================================================
# GÉNÉRATION DE STATISTIQUES (conservé)
# ============================================================================

def generate_pdf_report(data, output_path):
    """Générer un rapport PDF (pour statistiques)"""
    # Implémentation existante...
    pass

def generate_statistics_chart(data):
    """Générer un graphique de statistiques"""
    # Implémentation existante...
    pass

# ============================================================================
# AMÉLIORATION MATCHING NOMS ÉTUDIANTS
# ============================================================================

def normalize_name(name):
    """Normaliser un nom pour comparaison
    - Minuscules
    - Suppression accents
    - Suppression espaces multiples
    """
    if not name:
        return ""

    # Minuscules
    name = name.lower().strip()

    # Supprimer accents
    name = ''.join(
        c for c in unicodedata.normalize('NFD', name)
        if unicodedata.category(c) != 'Mn'
    )

    # Espaces multiples -> un seul
    name = ' '.join(name.split())

    return name

def match_student_by_name(extracted_name, session):
    """Trouver un étudiant par nom avec matching intelligent

    Gère :
    - Ordre inversé (Prénom Nom vs Nom Prénom)
    - Casse différente
    - Accents
    - Espaces

    Args:
        extracted_name: Nom extrait de la copie
        session: Session SQLAlchemy

    Returns:
        User object ou None
    """
    from models import User, UserRole

    if not extracted_name:
        return None

    # Normaliser le nom extrait
    norm_extracted = normalize_name(extracted_name)

    # Récupérer tous les étudiants
    students = session.query(User).filter_by(role=UserRole.STUDENT).all()

    for student in students:
        norm_student = normalize_name(student.full_name)

        # 1. Matching exact
        if norm_extracted == norm_student:
            print(f" ✅ Match exact: {student.full_name}")
            return student

        # 2. Matching inversé (Prénom Nom vs Nom Prénom)
        parts_extracted = norm_extracted.split()
        parts_student = norm_student.split()

        if len(parts_extracted) >= 2 and len(parts_student) >= 2:
            # Inverser et comparer
            reversed_extracted = ' '.join(reversed(parts_extracted))
            if reversed_extracted == norm_student:
                print(f" ✅ Match inversé: {student.full_name}")
                return student

            # 3. Nom de famille + prénom partiel
            # Ex: "Serge Bounguele" match "BOUNGUELE Serge"
            if parts_extracted[-1] == parts_student[0] or parts_extracted[-1] == parts_student[-1]:
                if parts_extracted[0] in parts_student or parts_student[0] in parts_extracted:
                    print(f"Match partiel: {student.full_name}")
                    return student

            # 4. Les deux parties du nom sont présentes (ordre peu importe)
            if all(part in parts_student for part in parts_extracted[:2]):
                print(f"Match multi-partie: {student.full_name}")
                return student

    return None

def find_or_create_student(student_name, extracted_name, session):
    """Trouver ou créer un étudiant avec matching intelligent

    Args:
        student_name: Nom fourni manuellement (peut être None)
        extracted_name: Nom extrait de la copie
        session: Session SQLAlchemy

    Returns:
        User object
    """
    from models import User, UserRole
    from flask_bcrypt import Bcrypt

    bcrypt = Bcrypt()

    # Essayer de matcher avec le nom fourni
    if student_name:
        student = match_student_by_name(student_name, session)
        if student:
            return student

    # Essayer de matcher avec le nom extrait
    if extracted_name:
        student = match_student_by_name(extracted_name, session)
        if student:
            return student

    # Si aucun match, créer un étudiant temporaire
    name_to_use = student_name or extracted_name or "Étudiant Inconnu"
    temp_email = f"{normalize_name(name_to_use).replace(' ', '.')}@temp.edu"
    temp_password = bcrypt.generate_password_hash('TempPassword123').decode('utf-8')

    student = User(
        email=temp_email,
        password_hash=temp_password,
        full_name=name_to_use,
        role=UserRole.STUDENT
    )

    session.add(student)
    session.flush()

    print(f"Étudiant créé temporairement: {student.full_name} ({student.email})")
    print(f"Associez-le à un compte réel via l'interface admin")

    return student


def send_exam_started_email(student_email, student_name, exam_title, exam_url, end_time_str):
    """Email aux étudiants quand un examen en ligne est activé."""
    smtp_cfg  = _smtp_config()
    app_url   = smtp_cfg.get('app_url', 'https://dev-cei.ddns.net')
    from_name = smtp_cfg.get('from_name', "CEI — Centre d'Examen Intelligent")
    subject   = f"Examen disponible : {exam_title}"

    bell_svg = (
        '<svg width="52" height="52" viewBox="0 0 24 24" fill="none" '
        'xmlns="http://www.w3.org/2000/svg" style="display:inline-block;">'
        '<path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9" '
        'stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M13.73 21a2 2 0 01-3.46 0" '
        'stroke="white" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="18" cy="6" r="4" fill="#fbbf24"/>'
        '</svg>'
    )
    warn_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'style="display:inline-block;vertical-align:middle;margin-right:5px;">'
        '<circle cx="12" cy="12" r="10" stroke="#d97706" stroke-width="2"/>'
        '<line x1="12" y1="8" x2="12" y2="12" stroke="#d97706" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="12" cy="16" r="1" fill="#d97706"/>'
        '</svg>'
    )

    html_body = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;margin:0;padding:24px;">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <div style="background:linear-gradient(135deg,#7c3aed 0%,#4f46e5 100%);padding:36px;text-align:center;">
    <div style="margin-bottom:14px;">{bell_svg}</div>
    <h1 style="color:#fff;font-size:21px;font-weight:800;margin:0 0 6px;">Examen disponible !</h1>
    <p style="color:rgba(255,255,255,.85);font-size:14px;margin:0;">{exam_title}</p>
  </div>

  <div style="padding:36px;">
    <p style="font-size:15px;color:#0f172a;margin:0 0 16px;">Bonjour <strong>{student_name}</strong>,</p>
    <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 26px;">
      Votre examen <strong>{exam_title}</strong> est maintenant disponible sur la plateforme CEI.
      Connectez-vous dès que possible pour le commencer.
    </p>

    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:14px 16px;margin-bottom:26px;">
      <p style="margin:0;font-size:13px;color:#92400e;line-height:1.7;">
        {warn_svg}<strong>Date de clôture :</strong> {end_time_str}<br>
        Passé ce délai, l'examen ne sera plus accessible.
      </p>
    </div>

    <div style="text-align:center;">
      <a href="{exam_url}"
         style="display:inline-block;padding:14px 36px;background:#4f46e5;color:#fff;text-decoration:none;border-radius:9px;font-size:15px;font-weight:700;">
        Accéder à l'examen maintenant
      </a>
    </div>
  </div>

  <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      {from_name} &nbsp;·&nbsp; <a href="{app_url}" style="color:#94a3b8;">{app_url}</a>
    </p>
  </div>
</div>
</body></html>"""

    text_body = f"""Examen disponible : {exam_title}
{"=" * (len(exam_title) + 20)}

Bonjour {student_name},

Votre examen "{exam_title}" est maintenant disponible sur la plateforme CEI.

Date de clôture : {end_time_str}

Accéder à l'examen : {exam_url}

---
{from_name}
{app_url}
"""
    return send_email(student_email, subject, html_body, text_body=text_body)


def send_password_changed_email(user_email, user_name, reset_url):
    """Email de sécurité envoyé après un changement de mot de passe réussi."""
    smtp_cfg  = _smtp_config()
    app_url   = smtp_cfg.get('app_url', 'https://dev-cei.ddns.net')
    from_name = smtp_cfg.get('from_name', "CEI — Centre d'Examen Intelligent")
    subject   = "Votre mot de passe CEI a été modifié"

    shield_svg = (
        '<svg width="52" height="52" viewBox="0 0 24 24" fill="none" '
        'xmlns="http://www.w3.org/2000/svg" style="display:inline-block;">'
        '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" fill="rgba(255,255,255,0.2)" '
        'stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<polyline points="9 12 11 14 15 10" stroke="white" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )
    warn_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'style="display:inline-block;vertical-align:middle;margin-right:5px;">'
        '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" '
        'fill="#fde68a" stroke="#92400e" stroke-width="1.5"/>'
        '<line x1="12" y1="9" x2="12" y2="13" stroke="#92400e" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="12" cy="17" r="1" fill="#92400e"/>'
        '</svg>'
    )

    html_body = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f8fafc;margin:0;padding:24px;">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <div style="background:linear-gradient(135deg,#059669 0%,#047857 100%);padding:36px;text-align:center;">
    <div style="margin-bottom:14px;">{shield_svg}</div>
    <h1 style="color:#fff;font-size:21px;font-weight:800;margin:0 0 6px;">Mot de passe modifié</h1>
    <p style="color:rgba(255,255,255,.85);font-size:14px;margin:0;">Confirmation de sécurité</p>
  </div>

  <div style="padding:36px;">
    <p style="font-size:15px;color:#0f172a;margin:0 0 16px;">Bonjour <strong>{user_name}</strong>,</p>
    <p style="font-size:14px;color:#475569;line-height:1.7;margin:0 0 26px;">
      Le mot de passe de votre compte CEI vient d'être <strong>modifié avec succès</strong>.
      Si vous êtes à l'origine de ce changement, vous n'avez rien à faire.
    </p>

    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:16px;margin-bottom:26px;">
      <p style="margin:0;font-size:13px;color:#92400e;line-height:1.7;">
        {warn_svg}<strong>Ce n'était pas vous ?</strong><br>
        Réinitialisez immédiatement votre mot de passe en cliquant sur le bouton ci-dessous
        et contactez l'administrateur de la plateforme.
      </p>
    </div>

    <div style="text-align:center;">
      <a href="{reset_url}"
         style="display:inline-block;padding:13px 30px;background:#dc2626;color:#fff;text-decoration:none;border-radius:9px;font-size:14px;font-weight:700;">
        Ce n'était pas moi — Sécuriser mon compte
      </a>
    </div>
  </div>

  <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      {from_name} &nbsp;·&nbsp; <a href="{app_url}" style="color:#94a3b8;">{app_url}</a>
    </p>
  </div>
</div>
</body></html>"""

    text_body = f"""Mot de passe CEI modifié — Confirmation de sécurité
====================================================

Bonjour {user_name},

Le mot de passe de votre compte CEI vient d'être modifié.

Si vous êtes à l'origine de ce changement : aucune action requise.

Ce n'était pas vous ? Réinitialisez votre mot de passe immédiatement :
{reset_url}

---
{from_name}
{app_url}
"""
    return send_email(user_email, subject, html_body, text_body=text_body)


def send_password_reset_email(user_email, user_name, reset_link):
    """Email de réinitialisation de mot de passe — token valide 1 heure."""
    smtp_cfg  = _smtp_config()
    app_url   = smtp_cfg.get('app_url', 'https://dev-cei.ddns.net')
    from_name = smtp_cfg.get('from_name', "CEI — Centre d'Examen Intelligent")
    subject   = "Réinitialisation de votre mot de passe CEI"

    # Icône cadenas SVG (inline — compatible Gmail, Apple Mail, Outlook Web)
    lock_svg = (
        '<svg width="52" height="52" viewBox="0 0 24 24" fill="none" '
        'xmlns="http://www.w3.org/2000/svg" style="display:inline-block;">'
        '<rect x="5" y="11" width="14" height="10" rx="2" fill="white"/>'
        '<path d="M8 11V7a4 4 0 0 1 8 0v4" stroke="white" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="12" cy="16" r="1.5" fill="#3b82f6"/>'
        '</svg>'
    )
    # Icône avertissement SVG
    warn_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
        'style="display:inline-block;vertical-align:middle;margin-right:5px;">'
        '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3'
        'L13.71 3.86a2 2 0 00-3.42 0z" fill="#fde68a" stroke="#92400e" stroke-width="1.5"/>'
        '<line x1="12" y1="9" x2="12" y2="13" stroke="#92400e" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="12" cy="17" r="1" fill="#92400e"/>'
        '</svg>'
    )

    html_body = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:Arial,Helvetica,sans-serif;background:#f1f5f9;margin:0;padding:24px;">
<div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <!-- En-tête -->
  <div style="background:linear-gradient(135deg,#3b82f6 0%,#1d4ed8 100%);padding:36px;text-align:center;">
    <div style="margin-bottom:14px;">{lock_svg}</div>
    <h1 style="color:#ffffff;margin:0;font-size:22px;font-weight:700;line-height:1.3;">Réinitialisation du mot de passe</h1>
    <p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">{from_name}</p>
  </div>

  <!-- Corps -->
  <div style="padding:36px;">
    <p style="color:#1e293b;font-size:15px;margin:0 0 14px;">Bonjour <strong>{user_name}</strong>,</p>
    <p style="color:#475569;font-size:14px;line-height:1.8;margin:0 0 28px;">
      Nous avons reçu une demande de réinitialisation du mot de passe associé à votre compte CEI.<br>
      Cliquez sur le bouton ci-dessous pour définir un nouveau mot de passe.
    </p>

    <!-- Bouton CTA -->
    <div style="text-align:center;margin:0 0 28px;">
      <a href="{reset_link}"
         style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;
                padding:15px 36px;border-radius:10px;font-weight:700;font-size:15px;
                letter-spacing:0.01em;">
        Réinitialiser mon mot de passe
      </a>
    </div>

    <!-- Lien texte de secours -->
    <p style="font-size:12px;color:#94a3b8;text-align:center;margin:0 0 24px;word-break:break-all;">
      Bouton non cliquable ? Copiez ce lien dans votre navigateur :<br>
      <a href="{reset_link}" style="color:#3b82f6;">{reset_link}</a>
    </p>

    <!-- Avertissement expiration -->
    <div style="background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:14px 16px;">
      <p style="margin:0;font-size:13px;color:#92400e;line-height:1.7;">
        {warn_svg}<strong>Ce lien expire dans 1 heure.</strong><br>
        Si vous n'avez pas demandé cette réinitialisation, ignorez simplement cet email.
        Votre mot de passe reste inchangé.
      </p>
    </div>
  </div>

  <!-- Pied de page -->
  <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 36px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#94a3b8;">
      CEI — Centre d'Examen Intelligent &nbsp;·&nbsp; <a href="{app_url}" style="color:#64748b;text-decoration:none;">{app_url}</a>
    </p>
  </div>

</div>
</body></html>"""

    # Version texte brut (obligatoire pour éviter le spam)
    text_body = f"""Réinitialisation de votre mot de passe CEI
==========================================

Bonjour {user_name},

Nous avons reçu une demande de réinitialisation du mot de passe de votre compte CEI.

Cliquez sur le lien ci-dessous pour définir un nouveau mot de passe :

{reset_link}

IMPORTANT : Ce lien expire dans 1 heure.

Si vous n'avez pas demandé cette réinitialisation, ignorez cet email.
Votre mot de passe reste inchangé.

---
CEI — Centre d'Examen Intelligent
{app_url}
"""
    return send_email(user_email, subject, html_body, text_body=text_body)


def generate_transcript_pdf(transcript_data, output_path):
    """Relevé de notes officiel — en-tête CEI institutionnel, structure LMD par UE."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable, KeepTogether)
    from reportlab.platypus.flowables import Flowable
    from reportlab.lib import colors
    from datetime import datetime
    import pytz

    # ── Palette ──
    C_NAVY      = colors.HexColor('#0f2557')   # bleu très foncé (bandeau)
    C_BLUE      = colors.HexColor('#2563eb')
    C_BLUE_MID  = colors.HexColor('#1e3a8a')
    C_BLUE_PALE = colors.HexColor('#dbeafe')
    C_GOLD      = colors.HexColor('#ca8a04')
    C_GOLD_PALE = colors.HexColor('#fef9c3')
    C_GREEN     = colors.HexColor('#059669')
    C_GREEN_PALE= colors.HexColor('#d1fae5')
    C_RED       = colors.HexColor('#dc2626')
    C_RED_PALE  = colors.HexColor('#fee2e2')
    C_ORANGE    = colors.HexColor('#d97706')
    C_ORANGE_PALE=colors.HexColor('#fef3c7')
    C_GRAY_50   = colors.HexColor('#f8fafc')
    C_GRAY_100  = colors.HexColor('#f1f5f9')
    C_GRAY_200  = colors.HexColor('#e2e8f0')
    C_GRAY_400  = colors.HexColor('#94a3b8')
    C_GRAY_600  = colors.HexColor('#475569')
    C_GRAY_900  = colors.HexColor('#0f172a')
    C_WHITE     = colors.white

    # ── Logo CEI dessiné avec ReportLab (chapeau de diplômé) ──
    class CEILogo(Flowable):
        def __init__(self, size=44):
            Flowable.__init__(self)
            self.width = size
            self.height = size

        def draw(self):
            c = self.canv
            w, h = self.width, self.height
            # Fond bleu arrondi
            c.setFillColor(C_BLUE)
            c.roundRect(0, 0, w, h, w * 0.18, fill=1, stroke=0)
            # Chapeau (haut — losange) : beginPath() renvoie un Path object
            c.setFillColor(C_WHITE)
            p = c.beginPath()
            p.moveTo(w * 0.50, h * 0.80)
            p.lineTo(w * 0.87, h * 0.61)
            p.lineTo(w * 0.50, h * 0.44)
            p.lineTo(w * 0.13, h * 0.61)
            p.close()
            c.drawPath(p, fill=1, stroke=0)
            # Chapeau (corps — tronc avec base arrondie)
            p = c.beginPath()
            p.moveTo(w * 0.27, h * 0.58)
            p.lineTo(w * 0.73, h * 0.58)
            p.lineTo(w * 0.73, h * 0.36)
            p.curveTo(w * 0.73, h * 0.20, w * 0.27, h * 0.20, w * 0.27, h * 0.36)
            p.close()
            c.drawPath(p, fill=1, stroke=0)
            # Tige du pompon
            c.setStrokeColor(C_WHITE)
            c.setLineWidth(max(1.2, w * 0.065))
            c.line(w * 0.84, h * 0.61, w * 0.84, h * 0.37)
            # Pompon
            c.setFillColor(C_WHITE)
            c.circle(w * 0.84, h * 0.32, w * 0.07, fill=1, stroke=0)

    # ── Document ──
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=1.6*cm, rightMargin=1.6*cm,
        topMargin=1.2*cm, bottomMargin=1.5*cm
    )
    styles = getSampleStyleSheet()

    # ── Styles texte ──
    def ps(name, **kw):
        return ParagraphStyle(name, parent=styles['Normal'], **kw)

    S = {
        'cei_name':   ps('CN', fontSize=15, textColor=C_WHITE,
                          fontName='Helvetica-Bold', leading=18),
        'cei_sub':    ps('CS', fontSize=8,  textColor=C_BLUE_PALE, leading=11),
        'doc_title':  ps('DT', fontSize=17, textColor=C_NAVY,
                          fontName='Helvetica-Bold', alignment=1, spaceAfter=8),
        'doc_year':   ps('DY', fontSize=9,  textColor=C_GRAY_600, alignment=1, spaceAfter=8),
        'lbl':        ps('LB', fontSize=8.5, textColor=C_GRAY_600, fontName='Helvetica-Bold'),
        'val':        ps('VL', fontSize=8.5, textColor=C_GRAY_900),
        'ue_hdr':     ps('UH', fontSize=9.5, textColor=C_WHITE, fontName='Helvetica-Bold'),
        'ue_moy':     ps('UM', fontSize=9.5, textColor=C_WHITE,
                          fontName='Helvetica-Bold', alignment=2),
        'ue_badge':   ps('UB', fontSize=8.5, fontName='Helvetica-Bold', alignment=2),
        'ec_hdr':     ps('EH', fontSize=8,   textColor=C_WHITE, fontName='Helvetica-Bold'),
        'ec_cell':    ps('EC', fontSize=8.5, textColor=C_GRAY_900),
        'footer':     ps('FT', fontSize=7.5, textColor=C_GRAY_400, alignment=1),
        'legend':     ps('LG', fontSize=7.5, textColor=C_GRAY_600),
        'sig_label':  ps('SL', fontSize=8,   textColor=C_GRAY_400, alignment=1),
    }

    story = []
    now = datetime.now(pytz.timezone('Africa/Dakar'))

    # ── Année académique ──
    acad_year = f"{now.year - 1}/{now.year}" if now.month < 9 else f"{now.year}/{now.year + 1}"

    # ════════════════════════════════════════════
    # BANDEAU EN-TÊTE (logo + nom institution)
    # ════════════════════════════════════════════
    logo_cell   = CEILogo(size=44)
    name_block  = [
        Paragraph("CENTRE D'EXAMEN INTELLIGENT", S['cei_name']),
        Paragraph("Plateforme officielle d'examens numériques — CEI", S['cei_sub']),
    ]
    # Cellule droite : numéro de document / référence
    ref_text = (
        f"<b>Réf. :</b> CEI-RN-{transcript_data.get('semester_name','S?').replace(' ','')}"
        f"-{now.strftime('%Y%m%d')}<br/>"
        f"<b>Émis le :</b> {now.strftime('%d/%m/%Y à %H:%M')}"
    )
    ref_block = Paragraph(ref_text, ps('RF', fontSize=7.5, textColor=C_BLUE_PALE,
                                        alignment=2, leading=11))

    hdr_data = [[logo_cell, name_block, ref_block]]
    hdr_table = Table(hdr_data, colWidths=[1.8*cm, 11.8*cm, 4.2*cm])
    hdr_table.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), C_NAVY),
        ('LEFTPADDING',   (0,0), (0,0),   6),
        ('LEFTPADDING',   (1,0), (1,0),   10),
        ('RIGHTPADDING',  (2,0), (2,0),   10),
        ('TOPPADDING',    (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(hdr_table)

    # Liseré doré sous le bandeau
    story.append(HRFlowable(width='100%', thickness=3, color=C_GOLD, spaceAfter=8))

    # ════════════════════════════════════════════
    # TITRE DU DOCUMENT
    # ════════════════════════════════════════════
    story.append(Paragraph("RELEVÉ DE NOTES OFFICIEL", S['doc_title']))
    story.append(Paragraph(f"Année académique {acad_year}", S['doc_year']))
    story.append(HRFlowable(width='100%', thickness=1.2, color=C_BLUE, spaceAfter=8))

    # ════════════════════════════════════════════
    # FICHE ÉTUDIANT
    # ════════════════════════════════════════════
    gpa = transcript_data.get('gpa') or 0
    gen_date = transcript_data.get('generated_at', now.strftime('%d/%m/%Y'))

    def info_row(lbl1, val1, lbl2, val2):
        return [Paragraph(lbl1, S['lbl']), Paragraph(val1, S['val']),
                Paragraph(lbl2, S['lbl']), Paragraph(val2, S['val'])]

    info_data = [
        info_row('Étudiant :', transcript_data.get('student_name', '—'),
                 'Formation :', transcript_data.get('formation_name', '—')),
        info_row('Email :', transcript_data.get('student_email', '—'),
                 'Semestre :', transcript_data.get('semester_name', '—')),
        info_row('Date d\'émission :', gen_date,
                 'Année académique :', acad_year),
    ]
    info_table = Table(info_data, colWidths=[3*cm, 7.2*cm, 3*cm, 4.6*cm])
    info_table.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,-1), C_GRAY_50),
        ('BACKGROUND',    (0,0), (0,-1), C_BLUE_PALE),
        ('BACKGROUND',    (2,0), (2,-1), C_BLUE_PALE),
        ('GRID',          (0,0), (-1,-1), 0.4, C_GRAY_200),
        ('TOPPADDING',    (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING',   (0,0), (-1,-1), 7),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.35*cm))

    # ── Titre section notes ──
    story.append(Paragraph(
        "▌ DÉTAIL DES NOTES PAR UNITÉ D'ENSEIGNEMENT",
        ps('SN', fontSize=9, textColor=C_NAVY, fontName='Helvetica-Bold', spaceAfter=5)
    ))

    # ════════════════════════════════════════════
    # BLOCS UE
    # ════════════════════════════════════════════
    ue_details = transcript_data.get('ue_details') or []

    for ue in ue_details:
        moy_ue       = ue.get('moyenne')
        validated    = ue.get('validated')
        comp         = ue.get('validated_by_compensation', False)
        credits_ue   = ue.get('credits', 0)
        credits_acq  = ue.get('credits_acquis', 0)
        moy_str      = f"{moy_ue:.2f}/20" if moy_ue is not None else "—"

        if validated is True and comp:
            badge_bg, badge_col = C_ORANGE_PALE, C_ORANGE
            badge_txt = f"VALIDÉ PAR COMPENSATION  •  {credits_acq}/{credits_ue} crédit(s)"
        elif validated is True:
            badge_bg, badge_col = C_GREEN_PALE, C_GREEN
            badge_txt = f"VALIDÉ  •  {credits_acq}/{credits_ue} crédit(s) acquis"
        elif validated is False:
            badge_bg, badge_col = C_RED_PALE, C_RED
            badge_txt = f"NON VALIDÉ  •  0/{credits_ue} crédit(s)"
        else:
            badge_bg, badge_col = C_GRAY_100, C_GRAY_600
            badge_txt = "INCOMPLET"

        # En-tête UE
        ue_badge_style = ps('UBS', fontSize=8.5, textColor=badge_col,
                             fontName='Helvetica-Bold', alignment=2)
        ue_hdr_data = [[
            Paragraph(f"{ue.get('ue_code','?')}  —  {ue.get('ue_name','?')}", S['ue_hdr']),
            Paragraph(f"Moy. UE : {moy_str}", S['ue_moy']),
            Paragraph(badge_txt, ue_badge_style),
        ]]
        ue_hdr_table = Table(ue_hdr_data, colWidths=[9*cm, 3.8*cm, 5*cm])
        ue_hdr_table.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (1,0),  C_BLUE_MID),
            ('BACKGROUND',    (2,0), (2,0),  badge_bg),
            ('LEFTPADDING',   (0,0), (-1,-1), 8),
            ('RIGHTPADDING',  (0,0), (-1,-1), 8),
            ('TOPPADDING',    (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('LINEBELOW',     (0,0), (-1,-1), 0.5, C_BLUE),
        ]))

        # Lignes EC
        ecs = ue.get('ecs', [])
        ec_data = [[
            Paragraph('Code EC', S['ec_hdr']),
            Paragraph("Intitulé de l'EC", S['ec_hdr']),
            Paragraph('Coef.', S['ec_hdr']),
            Paragraph('Note /20', S['ec_hdr']),
            Paragraph('Décision', S['ec_hdr']),
        ]]
        ec_styles = [
            ('BACKGROUND',    (0,0), (-1,0), C_BLUE),
            ('ALIGN',         (2,0), (-1,-1), 'CENTER'),
            ('FONTSIZE',      (0,0), (-1,-1), 8.5),
            ('GRID',          (0,0), (-1,-1), 0.3, C_GRAY_200),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [C_WHITE, C_GRAY_50]),
            ('TOPPADDING',    (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING',   (0,0), (-1,-1), 6),
        ]
        for i, ec in enumerate(ecs, start=1):
            note = ec.get('note')
            note_str = f"{note:.2f}" if note is not None else "—"
            if note is None:
                dec, dec_col = "—", C_GRAY_400
            elif note >= 10:
                dec, dec_col = "✓  Acquis", C_GREEN
            else:
                dec, dec_col = "✗  Ajourné", C_RED
            ec_data.append([
                ec.get('ec_code', '?'),
                ec.get('ec_name', '?')[:52],
                str(ec.get('coefficient', 1)),
                note_str,
                dec,
            ])
            ec_styles += [
                ('TEXTCOLOR',  (4, i), (4, i), dec_col),
                ('FONTNAME',   (4, i), (4, i), 'Helvetica-Bold'),
            ]

        ec_table = Table(ec_data, colWidths=[2.4*cm, 9.1*cm, 1.5*cm, 2.3*cm, 2.5*cm])
        ec_table.setStyle(TableStyle(ec_styles))

        story.append(KeepTogether([ue_hdr_table, ec_table, Spacer(1, 0.28*cm)]))

    # ════════════════════════════════════════════
    # RÉCAPITULATIF SEMESTRIEL
    # ════════════════════════════════════════════
    story.append(Spacer(1, 0.1*cm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=C_NAVY, spaceAfter=6))

    gpa_col = C_GREEN if gpa >= 10 else C_RED
    decision = 'ADMIS(E)' if gpa >= 10 else 'AJOURNÉ(E)'
    total_cr = transcript_data.get('total_credits', 0)
    obt_cr   = transcript_data.get('obtained_credits', 0)

    recap_data = [
        [Paragraph('RÉCAPITULATIF SEMESTRIEL', ps('RC', fontSize=9, textColor=C_WHITE,
                    fontName='Helvetica-Bold')),
         '', '', ''],
        [Paragraph('Moyenne générale :', ps('RL', fontSize=9, textColor=C_GRAY_900,
                    fontName='Helvetica-Bold')),
         Paragraph(f"{gpa:.2f} / 20", ps('RV', fontSize=11, textColor=gpa_col,
                    fontName='Helvetica-Bold')),
         Paragraph('Crédits acquis :', ps('RL2', fontSize=9, textColor=C_GRAY_900,
                    fontName='Helvetica-Bold')),
         Paragraph(f"{obt_cr} / {total_cr}", ps('RV2', fontSize=11, textColor=gpa_col,
                    fontName='Helvetica-Bold'))],
        [Paragraph('Décision :', ps('RL3', fontSize=9, textColor=C_GRAY_900,
                    fontName='Helvetica-Bold')),
         Paragraph(decision, ps('RD', fontSize=13, textColor=gpa_col,
                    fontName='Helvetica-Bold')),
         '', ''],
    ]
    recap_table = Table(recap_data, colWidths=[4.5*cm, 4.5*cm, 4*cm, 4.8*cm])
    recap_table.setStyle(TableStyle([
        ('BACKGROUND',    (0,0), (-1,0), C_NAVY),
        ('BACKGROUND',    (0,1), (-1,-1), C_GRAY_50),
        ('SPAN',          (0,0), (-1,0)),
        ('SPAN',          (1,2), (3,2)),
        ('GRID',          (0,1), (-1,-1), 0.4, C_GRAY_200),
        ('TOPPADDING',    (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING',   (0,0), (-1,-1), 8),
        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(recap_table)

    # ════════════════════════════════════════════
    # ZONE SIGNATURE
    # ════════════════════════════════════════════
    story.append(Spacer(1, 0.5*cm))
    sig_data = [[
        Paragraph("Le Responsable Pédagogique", S['sig_label']),
        Paragraph("", S['sig_label']),
        Paragraph("Cachet et Signature", S['sig_label']),
    ]]
    sig_table = Table(sig_data, colWidths=[6*cm, 5.8*cm, 6*cm])
    sig_table.setStyle(TableStyle([
        ('TOPPADDING',    (0,0), (-1,-1), 28),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LINEABOVE',     (0,0), (0,0),   0.8, C_GRAY_400),
        ('LINEABOVE',     (2,0), (2,0),   0.8, C_GRAY_400),
        ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
    ]))
    story.append(sig_table)

    # ════════════════════════════════════════════
    # LÉGENDE + PIED DE PAGE
    # ════════════════════════════════════════════
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=C_GRAY_200, spaceAfter=4))
    legende = (
        "<b>Légende :</b>  "
        "<font color='#059669'><b>VALIDÉ</b></font> : moy. UE ≥ 10/20  —  "
        "<font color='#d97706'><b>VALIDÉ PAR COMPENSATION</b></font> : UE &lt; 10 mais moy. semestrielle ≥ 10  —  "
        "<font color='#dc2626'><b>NON VALIDÉ</b></font> : moy. UE &lt; 10 et semestre non compensé"
    )
    story.append(Paragraph(legende, S['legend']))
    story.append(Spacer(1, 0.2*cm))
    story.append(HRFlowable(width='100%', thickness=2, color=C_NAVY, spaceAfter=4))
    story.append(Paragraph(
        "Centre d'Examen Intelligent (CEI)  —  Document officiel généré le "
        + now.strftime('%d/%m/%Y à %H:%M')
        + "  —  Tout document modifié est nul et sans effet",
        S['footer']
    ))

    doc.build(story)
    return output_path