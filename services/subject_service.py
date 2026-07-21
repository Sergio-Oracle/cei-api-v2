"""SubjectService — pure business logic, no HTTP concerns."""
from __future__ import annotations
import os
import re
from datetime import datetime
from typing import Optional

from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from models import get_session, User, UserRole, EC, ECAssignment, QuestionBank
from repositories.subject_repository import SubjectRepository
from routes.question_bank import _similarity, DUPLICATE_THRESHOLD
from services.ai_service import call_ai as _call_ai
from utils import allowed_file, extract_text_from_file

_UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'static/uploads')

# ── Regex helpers for marker annotation ──────────────────────────────────────

_Q_TITLE_RE = re.compile(
    r'^(Question\s+\d+|Q\.\s*\d+|Q\d+\b|Exercice\s+\d+|Problème\s+\d+|Partie\s+[IVXivx]+)',
    re.IGNORECASE,
)
_CHOICE_RE = re.compile(r'^\s*[A-D][)\.]\s', re.MULTILINE)
_VF_RE     = re.compile(r'vrai\s*/?\s*faux|vrai\s+ou\s+faux|\(v/f\)', re.IGNORECASE)
_MARKER_RE = re.compile(r'\[(QCM|VF|OUVERT|SUBOPEN)\]', re.IGNORECASE)


def annotate_markers(content: str, has_qcm: bool, has_vf: bool) -> str:
    """Add [QCM], [VF] or [OUVERT] markers to question titles — pure Python, no AI."""
    lines, out = content.split('\n'), []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _Q_TITLE_RE.match(stripped) and not _MARKER_RE.search(line):
            ctx = '\n'.join(lines[i + 1:i + 12])
            if has_qcm and _CHOICE_RE.search(ctx):
                line = line.rstrip() + ' [QCM]'
            elif has_vf and (_VF_RE.search(stripped) or _VF_RE.search(ctx)):
                line = line.rstrip() + ' [VF]'
            else:
                line = line.rstrip() + ' [OUVERT]'
        out.append(line)
    return '\n'.join(out)


def _split_into_question_blocks(content: str) -> list[str]:
    """Découpe un document uploadé en blocs par question (un uploadé n'a pas
    forcément le format rigide "Question N — Titre" des sujets générés par
    l'IA — on se base donc sur `_Q_TITLE_RE`, plus permissif)."""
    lines = content.split('\n')
    starts = [i for i, line in enumerate(lines) if _Q_TITLE_RE.match(line.strip())]
    if not starts:
        return []
    blocks = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        blocks.append('\n'.join(lines[start:end]).strip())
    return blocks


def _find_bank_duplicates(blocks: list[str]) -> list[dict]:
    """Compare chaque question d'un document uploadé à TOUTE la banque de
    questions existante (contrairement à `generate_more_questions`, qui ne
    compare qu'au sein d'un même sujet) — un document uploadé est un contenu
    totalement nouveau pour la plateforme, il peut donc redire un examen déjà
    utilisé par un autre sujet."""
    if not blocks:
        return []
    session = get_session()
    try:
        existing_contents = [c for (c,) in session.query(QuestionBank.content).all()]
    finally:
        session.close()
    def _body(text: str) -> str:
        # Ignore la première ligne (titre/numéro de question) : son format
        # varie totalement entre un document uploadé ("Question 1 [OUVERT]")
        # et une entrée de banque ("Titre ............. (X pts)") même quand
        # le CORPS de la question est identique — comparer dès le caractère 0
        # diluerait la similarité et masquerait le doublon.
        return (text.split('\n', 1)[1] if '\n' in text else text).strip()

    duplicates = []
    existing_bodies = [_body(ex) for ex in existing_contents]
    for blk in blocks:
        blk_body = _body(blk)
        for ex_body in existing_bodies:
            sim = _similarity(blk_body[:300], ex_body[:300])
            if sim >= DUPLICATE_THRESHOLD:
                duplicates.append({'similarity': round(sim * 100, 1)})
                break
    return duplicates


def _build_manual_rubric_skeleton(blocks: list[str]) -> str:
    """Squelette de barème vierge (pur Python, sans IA) — donne au professeur
    une structure question par question à remplir lui-même, plutôt que de lui
    imposer un point de départ rédigé par l'IA. Répond au besoin exprimé de
    « garder la main » sur la notation."""
    if not blocks:
        return ''
    lines = ['=== BARÈME DE NOTATION (à compléter par vos soins) ===', '']
    for blk in blocks:
        first_line = blk.split('\n', 1)[0].strip()
        lines.append(first_line)
        lines.append('  • Critère : ___ pts — ___')
        lines.append('')
    lines += ['──────────────────────────', 'TOTAL : ___ / 20 points']
    return '\n'.join(lines)


def _build_rubric_prompt(question_types: str) -> tuple[str, str]:
    """Return (system_prompt, rubric_instruction) based on selected types."""
    qt = question_types.lower()
    has_qcm  = 'qcm'   in qt
    has_vf   = 'vrai'  in qt or 'faux' in qt
    has_open = 'ouvert' in qt or 'open' in qt or not (has_qcm or has_vf)

    # Comme dans Moodle (qtype_multichoice « one answer only » vs « multiple »),
    # une question QCM à réponse unique et une question QCM à réponses multiples
    # se notent différemment (correspondance exacte vs crédit partiel par choix
    # correct) — l'IA doit donc détecter, question par question, laquelle des
    # deux formulations est utilisée dans le document plutôt que de supposer un
    # format unique pour tout le sujet.
    qcm_instruction = (
        'Pour chaque question QCM, détermine d\'abord si une seule réponse est '
        'attendue (« Choisissez la meilleure réponse », une seule case à cocher '
        'pertinente) ou si plusieurs réponses sont attendues (« Cochez toutes '
        'les réponses correctes », plusieurs choix corrects) :\n'
        '  • Réponse unique : Bonne réponse : X) — [justification en une ligne]\n'
        '  • Réponses multiples : Bonnes réponses : X), Y) — [justification en une ligne]'
    )

    if has_qcm and not has_open and not has_vf:
        instruction = qcm_instruction
    elif has_vf and not has_qcm and not has_open:
        instruction = 'Pour chaque question Vrai/Faux :\n  • Réponse : Vrai / Faux — [justification en une ligne]'
    elif has_qcm or has_vf:
        instruction = (
            f'{qcm_instruction}\n'
            'Pour les questions VF  : "Réponse : Vrai/Faux — [justification]"\n'
            'Pour les questions ouvertes : critères de notation avec points'
        )
    else:
        instruction = (
            'Pour chaque question ouverte :\n'
            '  • Critère 1 : X pts — [attendu précis]\n'
            '  • Critère 2 : Y pts — [attendu précis]'
        )

    qt_label = question_types if question_types else 'Questions ouvertes'
    system_prompt = (
        f'Tu es un expert en évaluation pédagogique universitaire.\n'
        f'Génère UNIQUEMENT le barème de notation pour ce sujet d\'examen.\n'
        f'Types de questions présents : {qt_label}\n\n'
        f'{instruction}\n\n'
        f'FORMAT DE SORTIE OBLIGATOIRE :\n'
        f'=== BARÈME DE NOTATION ===\n'
        f'Question 1 (X pts) :\n  ...\nTotal : 20 points'
    )
    return system_prompt, instruction


class SubjectService:

    # ── Queries (delegate to repository) ─────────────────────────────────────

    @staticmethod
    def list_for_user(user_id: int, role: UserRole) -> list[dict]:
        return SubjectRepository.find_all(user_id, role)

    @staticmethod
    def get_detail(subject_id: int, user_id: int, role: UserRole) -> dict:
        subj = SubjectRepository.find_by_id_dict(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.STUDENT and not subj.get('is_active'):
            raise PermissionError('Sujet non accessible')
        if role == UserRole.PROFESSOR and subj.get('creator_id') != user_id:
            raise PermissionError('Accès non autorisé')
        return subj

    # ── Create manual (sans fichier) ─────────────────────────────────────────

    @staticmethod
    def create_manual(
        title: str,
        content: str,
        rubric: str,
        creator_id: int,
        role: UserRole,
        ec_id: Optional[int] = None,
    ) -> dict:
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN):
            raise PermissionError('Accès non autorisé')
        return SubjectRepository.create(
            title=title,
            content=content,
            rubric=rubric,
            filename='',
            creator_id=creator_id,
            ec_id=ec_id,
        )

    # ── Upload ────────────────────────────────────────────────────────────────

    @staticmethod
    def upload(
        title: str,
        file: FileStorage,
        creator_id: int,
        role: UserRole,
        ec_id: Optional[int],
        question_types: str,
        rubric_mode: str = 'ai',
    ) -> dict:
        # Authorization check
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN):
            raise PermissionError('Accès non autorisé')

        # File validation
        if not file or file.filename == '':
            raise ValueError('Aucun fichier fourni')
        if not allowed_file(file.filename):
            raise ValueError('Type de fichier non autorisé. Utilisez PDF, DOCX ou TXT')

        # EC access check for professors
        if ec_id and role == UserRole.PROFESSOR:
            session = get_session()
            try:
                asgn = session.query(ECAssignment).filter_by(
                    ec_id=ec_id, professor_id=creator_id).first()
                if not asgn:
                    raise PermissionError("Vous n'êtes pas responsable de cet EC")
                ec = session.query(EC).filter_by(id=ec_id).first()
                if not ec:
                    raise LookupError('EC non trouvé')
            finally:
                session.close()

        # Save file
        filename = f"subject_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secure_filename(file.filename)}"
        filepath = os.path.join(_UPLOAD_FOLDER, filename)
        file.save(filepath)

        # Extract text
        content = extract_text_from_file(filepath)
        if not content:
            os.remove(filepath)
            raise ValueError("Impossible d'extraire le texte du fichier")

        # Annotate question markers (pure Python — no AI needed)
        qt = question_types.lower()
        has_qcm = 'qcm' in qt
        has_vf  = 'vrai' in qt or 'faux' in qt
        annotated = annotate_markers(content, has_qcm, has_vf)

        # Détection de doublons — contre TOUTE la banque de questions, puisque
        # ce document est un contenu neuf pour la plateforme (pas une extension
        # d'un sujet déjà en cours d'édition).
        question_blocks = _split_into_question_blocks(annotated)
        duplicates = _find_bank_duplicates(question_blocks)

        # Barème : soit généré par l'IA (comportement historique), soit un
        # squelette vierge que le professeur rédige lui-même — le champ reste
        # de toute façon modifiable ensuite dans les deux cas, mais le mode
        # manuel évite d'imposer un point de départ écrit par l'IA à un
        # professeur qui veut garder la main dès le départ.
        if rubric_mode == 'manual':
            rubric = _build_manual_rubric_skeleton(question_blocks)
        else:
            rubric = ''
            try:
                system_prompt, _ = _build_rubric_prompt(question_types)
                rubric = _call_ai(system_prompt, annotated, temperature=0.1)
            except Exception as ai_err:
                print(f'[SubjectService] AI unavailable for rubric: {ai_err}')

        result = SubjectRepository.create(
            title=title,
            content=annotated,
            rubric=rubric,
            filename=filename,
            creator_id=creator_id,
            ec_id=ec_id,
        )
        result['duplicates'] = duplicates
        return result

    # ── Delete ────────────────────────────────────────────────────────────────

    @staticmethod
    def update(subject_id: int, user_id: int, role: UserRole,
               title: Optional[str] = None, content: Optional[str] = None,
               rubric: Optional[str] = None) -> dict:
        """Édite un sujet déjà validé — bloqué si un examen lié est déjà
        actif/clôturé ou a reçu des tentatives (contenu déjà vu/corrigé)."""
        if role not in (UserRole.ADMIN, UserRole.PROFESSOR):
            raise PermissionError('Non autorisé')
        subj = SubjectRepository.find_by_id(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.PROFESSOR and subj.creator_id != user_id:
            raise PermissionError('Vous ne pouvez modifier que vos propres sujets')
        if SubjectRepository.has_locked_exam(subject_id):
            raise PermissionError(
                "Ce sujet est lié à un examen déjà actif, clôturé ou ayant reçu des "
                "tentatives — il ne peut plus être modifié pour ne pas désynchroniser "
                "le contenu vu par les étudiants."
            )
        return SubjectRepository.update_content(subject_id, title=title, content=content, rubric=rubric)

    @staticmethod
    def delete(subject_id: int, user_id: int, role: UserRole) -> None:
        if role not in (UserRole.ADMIN, UserRole.PROFESSOR):
            raise PermissionError('Non autorisé')
        subj = SubjectRepository.find_by_id(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.PROFESSOR and subj.creator_id != user_id:
            raise PermissionError('Vous ne pouvez supprimer que vos propres sujets')
        SubjectRepository.delete_with_cascade(subject_id)

    # ── Image upload ──────────────────────────────────────────────────────────

    @staticmethod
    def upload_image(subject_id: int, img: FileStorage, user_id: int, role: UserRole) -> str:
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN):
            raise PermissionError('Accès non autorisé')
        subj = SubjectRepository.find_by_id(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.PROFESSOR and subj.creator_id != user_id:
            raise PermissionError('Accès non autorisé')

        ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in img.filename else ''
        if ext not in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
            raise ValueError('Format image non autorisé (png, jpg, gif, webp)')

        fname = f"subjectimg_{subject_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        fpath = os.path.join(_UPLOAD_FOLDER, fname)
        img.save(fpath)
        image_url = f'/static/uploads/{fname}'
        SubjectRepository.update_image(subject_id, image_url)
        return image_url
