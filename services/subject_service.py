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
from services.exam_normalizer import normalize_exam_text, rubric_from_corrige

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


def _build_manual_rubric_skeleton(blocks: list[str], total_points: int = 20) -> str:
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
    lines += ['──────────────────────────', f'TOTAL : ___ / {total_points} points']
    return '\n'.join(lines)


_NEW_TYPE_RUBRIC = {
    'ASSOCIATION': 'Pour chaque question [ASSOCIATION] : une ligne  "Réponse : 1-H, 2-E, 3-K, …"  (numéro de l\'élément « - » dans l\'ordre où ils sont listés, tiret, LETTRE de la proposition correspondante) — une entrée pour CHAQUE élément.',
    'TRI': 'Pour chaque question [TRI] : une ligne  "Réponse : 1-A, 2-B, 3-A"  (numéro de l\'élément « - », tiret, lettre de sa catégorie : A = première catégorie de la ligne « Catégories : », B = deuxième, etc.) — une entrée pour CHAQUE élément.',
    'TROUS': 'Pour chaque question [TROUS] : une ligne  "Réponse : 1-B, 2-A"  (numéro du trou {n}, tiret, lettre de la proposition).',
    'ORDRE': 'Pour chaque question [ORDRE] : une ligne  "Ordre : C, A, B, D"  (lettres des éléments du premier au dernier).',
    'COURT': 'Pour chaque question [COURT] : une ligne  "Réponses acceptées : réponse 1 | variante 2"  (formulations équivalentes séparées par « | »).',
    'CLOZE': 'Pour chaque question [CLOZE] : une ligne  "Champs : 1=réponse|variante ; 2=14 (± 0,5) ; 3=B"  (champ COURT : réponses acceptées séparées par | ; champ NUM : valeur et tolérance ; champ CHOIX : LETTRE de l\'option, A = la première) — une entrée par champ.',
    'CALCULEE': 'Pour chaque question [CALCULEE] : une ligne  "Formule : a*b*n/100 (± 1)"  (formule avec les variables de l\'énoncé, opérateurs + - * / ** et parenthèses ; tolérance absolue ou « (± 2 %) »).',
    'ZONES': 'Pour chaque question [ZONES] : une ligne  "Zones : 1=[x1,y1,x2,y2] ; 2=[x1,y1,x2,y2]"  (rectangle de la bonne zone de chaque étiquette, dans l\'ordre des étiquettes, en % de la largeur et de la hauteur de l\'image : 0,0 = coin haut-gauche, 100,100 = coin bas-droit).',
    'NUMERIQUE': 'Pour chaque question [NUMERIQUE] : une ligne  "Valeur : 12,5 (± 0,1)"  (valeur attendue et tolérance absolue, ou « (± 2 %) »).',
}


def _build_rubric_prompt(question_types: str, total_points: int = 20, content: str = '', has_corrige: bool = False) -> tuple[str, str]:
    """Return (system_prompt, rubric_instruction) based on selected types.
    `content` : sujet balisé — les types présents (balises [ASSOCIATION], [TRI]…) ajoutent leur format de clé.
    `has_corrige` : un corrigé officiel est fourni — ses réponses sont recopiées, jamais devinées."""
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

    present = [m for m in _NEW_TYPE_RUBRIC if re.search(r'\[' + m + r'\]', content or '', re.I)]
    if present:
        instruction += '\n' + '\n'.join(_NEW_TYPE_RUBRIC[m] for m in present)
    if content and re.search(r'\(\s*\d+(?:[.,]\d+)?\s*pts?\s*\)', content):
        instruction += ('\nLes points de chaque question sont DÉJÀ indiqués dans son titre « (X pts) » : reprends-les exactement '
                        'dans le barème (« Question N (X pts) : ») sans les modifier ni les redistribuer.')
    if has_corrige:
        instruction += ('\nUn CORRIGÉ OFFICIEL est fourni après le sujet : recopie ses réponses telles quelles pour les clés '
                        '(lettres, Vrai/Faux, associations, valeurs). Ne devine jamais une réponse qui figure dans le corrigé ; '
                        'pour les questions ouvertes, reprends ses critères et leurs points.')
    qt_label = question_types if question_types else 'Questions ouvertes'
    system_prompt = (
        f'Tu es un expert en évaluation pédagogique universitaire.\n'
        f'Génère UNIQUEMENT le barème de notation pour ce sujet d\'examen.\n'
        f'Types de questions présents : {qt_label}\n\n'
        f'{instruction}\n\n'
        f'RÈGLE ABSOLUE SUR LE TOTAL : répartis les points entre TOUTES les '
        f'questions du sujet de façon à ce que la somme fasse EXACTEMENT '
        f'{total_points} points au total — pas 20 par défaut, {total_points} '
        f'précisément, en tenant compte du nombre et de la difficulté relative '
        f'des questions présentes dans le document.\n\n'
        f'FORMAT DE SORTIE OBLIGATOIRE :\n'
        f'=== BARÈME DE NOTATION ===\n'
        f'Question 1 (X pts) :\n  ...\nTotal : {total_points} points'
    )
    return system_prompt, instruction


class SubjectService:

    # ── Queries (delegate to repository) ─────────────────────────────────────

    @staticmethod
    def list_for_user(user_id: int, role: UserRole) -> list[dict]:
        return SubjectRepository.find_all(user_id, role)

    @staticmethod
    def _check_author(creator_id: int, ec_id: Optional[int]) -> None:
        """Créer un sujet : administrateur/responsable d'examen partout ; enseignant seulement sur un
        EC dont il est responsable (voir services.ec_rights.require_author)."""
        from models import User
        from services.ec_rights import require_author
        session = get_session()
        try:
            user = session.get(User, creator_id)
            if user is None:
                raise PermissionError('Accès non autorisé')
            require_author(session, ec_id, user)
        finally:
            session.close()

    @staticmethod
    def get_detail(subject_id: int, user_id: int, role: UserRole) -> dict:
        subj = SubjectRepository.find_by_id_dict(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.STUDENT and not subj.get('is_active'):
            raise PermissionError('Sujet non accessible')
        if role == UserRole.PROFESSOR and subj.get('creator_id') != user_id:
            from services.ec_rights import assignment_kind
            session = get_session()
            try:
                if assignment_kind(session, subj.get('ec_id'), user_id) is None:
                    raise PermissionError('Accès non autorisé')
            finally:
                session.close()
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
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            raise PermissionError('Accès non autorisé')
        SubjectService._check_author(creator_id, ec_id)
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
        files: list[FileStorage],
        creator_id: int,
        role: UserRole,
        ec_id: Optional[int],
        question_types: str,
        rubric_mode: str = 'ai',
        total_points: int = 20,
        expected_questions: Optional[int] = None,
    ) -> dict:
        # Authorization check
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            raise PermissionError('Accès non autorisé')

        # Total du barème librement choisi par le professeur (20, 30, 40, 60,
        # etc. selon les conventions de son établissement) — ne pas supposer
        # que tout examen se note systématiquement sur 20.
        try:
            total_points = max(1, min(200, int(total_points)))
        except (TypeError, ValueError):
            total_points = 20

        # File validation
        files = [f for f in (files or []) if f and f.filename]
        if not files:
            raise ValueError('Aucun fichier fourni')
        for f in files:
            if not allowed_file(f.filename):
                raise ValueError(f"Type de fichier non autorisé ({f.filename}). Utilisez PDF, DOCX ou TXT")

        # Droit de créer sur cet EC (responsable de l'EC, ou administrateur / responsable d'examen)
        SubjectService._check_author(creator_id, ec_id)
        if ec_id:
            _s = get_session()
            try:
                if not _s.query(EC).filter_by(id=ec_id).first():
                    raise LookupError('EC non trouvé')
            finally:
                _s.close()

        # Save + extract text for each file, concatenated with clear separators
        # so l'IA distingue les sources sans qu'un professeur ait besoin de
        # fusionner ses documents à la main avant de les déposer.
        #
        # Stockage (03/09) : extract_text_from_file a besoin d'un vrai chemin
        # disque (catdoc pour les .doc legacy, notamment), donc on sauvegarde
        # toujours en local d'abord pour l'extraction — mais le fichier est
        # ensuite envoyé vers MinIO et supprimé du disque local, au lieu d'y
        # rester indéfiniment (~140 fichiers accumulés avant ce correctif).
        # Sécurité : on ne supprime JAMAIS le fichier local si l'upload S3 a
        # échoué (MinIO indisponible) — mieux vaut un fichier qui traîne
        # qu'un document de professeur perdu.
        saved_paths = []
        content_parts = []
        corrige_parts = []
        corrige_raw = []
        norm_reports = []
        crosswords = []
        corrige_keys = []
        orig_texts = []   # texte extrait des fichiers d'épreuve (sans corrigé), pour contrôler la fidélité de la reproduction
        loose_key, loose_n = {}, 0   # clés « Bonne réponse » d'énoncés sans numéros (voir subject_normalizer.loose_mcq)
        first_filename = None
        first_s3_key = None
        try:
            for f in files:
                filename = f"subject_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secure_filename(f.filename)}"
                filepath = os.path.join(_UPLOAD_FOLDER, filename)
                f.save(filepath)
                saved_paths.append(filepath)
                if first_filename is None:
                    first_filename = filename

                part = extract_text_from_file(filepath)
                if not part:
                    raise ValueError(f"Impossible d'extraire le texte du fichier « {f.filename} »")
                if filepath.lower().endswith(('.pdf', '.docx')):
                    from services.crossword_pdf import extract_crosswords
                    crosswords += extract_crosswords(filepath)      # grilles de mots croisés dessinées (absentes du texte)
                if re.search(r'corrig|correction|bar[eè]me|solution', f.filename, re.I):
                    # Corrigé officiel : sert uniquement à écrire les clés du barème, jamais montré aux étudiants
                    corrige_parts.append(f"--- Corrigé: {f.filename} ---\n{part}")
                    from services.corrige_reader import read_corrige_file
                    corrige_keys.append(read_corrige_file(filepath, part))   # clés lues dans le corrigé (gras, tableaux, grille remplie)
                    corrige_raw.append(part)
                else:
                    orig_texts.append(part)
                    part, nrep = normalize_exam_text(part)
                    if not nrep.get('structured'):
                        # Énoncés « A. B. C. D. / Bonne réponse : d » sans numéros : mis au format CEI, clés mises à part
                        from services.subject_normalizer import loose_mcq
                        part, _k, _n = loose_mcq(part, loose_n + 1)
                        loose_key.update(_k)
                        loose_n += _n
                    norm_reports.append(nrep)
                    content_parts.append((nrep['structured'], f"--- Fichier: {f.filename} ---\n{part}"))

                from s3_client import upload_document
                with open(filepath, 'rb') as fh:
                    raw = fh.read()
                s3_key = upload_document('subject', filename, raw, f.content_type or 'application/octet-stream')
                if s3_key:
                    if first_s3_key is None:
                        first_s3_key = s3_key
                    os.remove(filepath)
                    saved_paths.remove(filepath)
        except Exception:
            for p in saved_paths:
                if os.path.exists(p):
                    os.remove(p)
            raise

        filename = first_filename
        if not content_parts:
            raise ValueError("Seul un corrigé a été fourni : ajoutez aussi le fichier de l'épreuve")

        # Annotate question markers (pure Python — no AI needed) — sauf pour une épreuve déjà normalisée
        # (balises posées par exam_normalizer : les ré-annoter ajouterait [OUVERT] aux titres de parties).
        qt = question_types.lower()
        has_qcm = 'qcm' in qt
        has_vf  = 'vrai' in qt or 'faux' in qt
        # Le type de CHAQUE question est détecté d'après le document lui-même (QCM, plusieurs réponses, Vrai/Faux,
        # association, texte à trous, réponse courte, ouverte) — les cases de types cochées ne décident plus de rien.
        from services.type_detector import detect_and_annotate
        _detect_reports, _parts = [], []
        for structured, txt in content_parts:
            if structured:
                _parts.append(txt)
            else:
                _t, _r = detect_and_annotate(txt, crosswords)
                _parts.append(_t); _detect_reports.append(_r)
        annotated = '\n\n'.join(_parts)
        detection = {'counts': {}, 'converted': [], 'warnings': []}
        for _r in _detect_reports:
            for _k, _v in _r['counts'].items():
                detection['counts'][_k] = detection['counts'].get(_k, 0) + _v
            detection['converted'] += _r['converted']; detection['warnings'] += _r['warnings']
        corrige_text = '\n\n'.join(corrige_parts)
        # Fidélité : (1) contrôle exact de ce qui aurait disparu, (2) relecture COMPLÈTE par l'IA en parallèle du barème
        from services import subject_fidelity as _fid
        _orig_all = '\n\n'.join(orig_texts)
        _lost = _fid.lost_lines(_orig_all, annotated)
        _review = {'ok': None, 'issues': []}
        def _run_review():
            _review.update(_fid.ai_review(_orig_all, annotated, _call_ai))
        import threading as _th
        _review_thread = _th.Thread(target=_run_review, daemon=True)
        _review_thread.start()
        # Types pour le barème : ceux RÉELLEMENT trouvés dans le document (plus jamais ceux cochés par l'enseignant)
        _found = set(detection['counts'])
        question_types = ', '.join(filter(None, [
            'QCM' if _found & {'qcm', 'qcm_multi'} else '',
            'Vrai/Faux' if 'vf' in _found else '',
            'Questions ouvertes' if (_found & {'open', 'subopen', 'code'}) or not _found else '']))

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
        # Corrigé fourni par l'enseignant : barème écrit DIRECTEMENT depuis lui (sans IA, donc sans erreur de
        # recopie) quand l'épreuve est structurée et que toutes les clés sont retrouvées ; sinon l'IA le lit.
        rubric_source = 'ia' if rubric_mode != 'manual' else 'manuel'
        det_rubric, det_warns = '', []
        if corrige_raw:
            first_rep = next((r for r in norm_reports if r.get('structured')), None)
            if first_rep:
                det_rubric, det_warns = rubric_from_corrige('\n'.join(corrige_raw), first_rep)
        if det_rubric and not det_warns:
            rubric = det_rubric
            rubric_source = 'corrigé'
        elif rubric_mode == 'manual':
            rubric = _build_manual_rubric_skeleton(question_blocks, total_points)
        else:
            rubric = ''
            try:
                system_prompt, _ = _build_rubric_prompt(question_types, total_points, annotated, bool(corrige_text))
                user_msg = annotated + (f"\n\n=== CORRIGÉ OFFICIEL (réponses à recopier) ===\n{corrige_text}" if corrige_text else '')
                rubric = _call_ai(system_prompt, user_msg, temperature=0.1)
            except Exception as ai_err:
                print(f'[SubjectService] AI unavailable for rubric: {ai_err}')

        if corrige_keys and not (det_rubric and not det_warns):
            from services.corrige_reader import merge as _merge_keys, rubric_from_exercise_key
            _ex_rubric, _ex_warns = rubric_from_exercise_key(annotated, _merge_keys(corrige_keys))
            if _ex_rubric:
                rubric, rubric_source, det_warns = _ex_rubric, 'corrigé', []
            elif _ex_warns:
                det_warns = list(det_warns) + _ex_warns
        if loose_key:
            from services.subject_normalizer import rubric_from_key
            _kr = rubric_from_key(annotated, loose_key, total_points)
            if _kr:
                rubric, rubric_source = _kr, 'corrigé'

        # ── Contrôles posés SUR LE BARÈME RÉDIGÉ PAR L'IA (jamais sur un corrigé officiel ni sur les points du document) ──────────
        # (a) la bonne réponse écrite dans le document fait foi ; (b) le total des points est EXACTEMENT celui demandé.
        # Constat du 12/11/2026 : l'IA avait écrit 10 points au lieu de 20 (« Test Blanc ») sans que rien ne le vérifie.
        _points_note = None
        _key_fixes = []
        if rubric_source == 'ia' and (rubric or '').strip():
            from services.points_total import enforce_total, reconcile_inline_keys
            rubric, _key_fixes = reconcile_inline_keys(annotated, rubric)
            annotated, rubric, _changed, _old_total = enforce_total(annotated, rubric, total_points)
            if _changed:
                _points_note = f"Le barème totalisait {_old_total:g} points : il a été ramené à {total_points} points en gardant les proportions."

        result = SubjectRepository.create(
            title=title,
            content=annotated,
            rubric=rubric,
            filename=filename,
            creator_id=creator_id,
            ec_id=ec_id,
            s3_key=first_s3_key,
        )
        result['duplicates'] = duplicates
        _review_thread.join(timeout=90)
        detection['lost_lines'] = _lost
        from services.subject_count import count_units, declared_total
        from routes.exams import _parse_subject_blocks_ordered as _psb
        _found, _by = count_units(_psb(annotated))
        detection['questions_found'] = _found
        detection['declared'] = declared_total(_orig_all)
        if expected_questions:
            detection['expected'] = int(expected_questions)
            if int(expected_questions) != _found:
                detection['warnings'].append(
                    f"Vous attendiez {int(expected_questions)} question(s), la plateforme en a reconnu {_found}"
                    + (f" (le document annonce {detection['declared']})" if detection['declared'] else '')
                    + " : vérifiez l'aperçu avant d'enregistrer.")
        elif detection['declared'] and detection['declared'] != _found:
            detection['warnings'].append(f"Le document annonce {detection['declared']} questions, la plateforme en a reconnu {_found} : vérifiez l'aperçu.")
        detection['review'] = dict(_review)
        # Rien ne doit disparaître en silence : lignes qui ne seraient affichées nulle part, questions qui citent un élément absent
        try:
            from services.subject_coverage import coverage_issues
            from services.subject_normalizer import normalize as _norm_saved
            detection['warnings'] += [i['message'] for i in coverage_issues(_norm_saved(annotated)[0])]
        except Exception as _cov_err:
            print(f'[SubjectService] contrôle de couverture indisponible: {_cov_err}')
        for _n, _a, _b in _key_fixes:
            detection['warnings'].append(f"Question {_n} : la bonne réponse du barème ({_a}) ne correspondait pas à celle écrite dans votre document ({_b}) : "
                                         "elle a été alignée sur votre document.")
        # Contrôle complet du sujet enregistré (mêmes règles que l'activation de l'examen) : le défaut est vu dès le dépôt
        try:
            from services.subject_check import analyze as _analyze
            _chk = _analyze(annotated, rubric)
            for _i in _chk['issues']:
                if _i['level'] in ('error', 'warn') and _i['message'] not in detection['warnings']:
                    detection['warnings'].append(_i['message'])
            detection['subject_check_ok'] = _chk['ok']
        except Exception as _chk_err:
            print(f'[SubjectService] contrôle du sujet indisponible: {_chk_err}')
        # Bonnes réponses écrites par l'IA (ni corrigé officiel, ni réponse dans le document) : seconde lecture indépendante,
        # divergence = « à relire » (jamais de modification automatique)
        if rubric_source == 'ia' and (rubric or '').strip():
            try:
                from services.answer_verify import verify_keys as _verify_keys
                for _w in _verify_keys(annotated, rubric):
                    if _w not in detection['warnings']:
                        detection['warnings'].append(_w)
            except Exception as _vk_err:
                print(f'[SubjectService] vérification des réponses indisponible: {_vk_err}')
        detection['points_note'] = _points_note
        result['detection'] = detection
        result['normalization'] = [r for r in norm_reports if r.get('structured')]
        result['has_corrige'] = bool(corrige_text)
        result['rubric_source'] = rubric_source
        result['corrige_warnings'] = det_warns
        return result

    # ── Delete ────────────────────────────────────────────────────────────────

    @staticmethod
    def update(subject_id: int, user_id: int, role: UserRole,
               title: Optional[str] = None, content: Optional[str] = None,
               rubric: Optional[str] = None) -> dict:
        """Édite un sujet déjà validé — bloqué si un examen lié est déjà
        actif/clôturé ou a reçu des tentatives (contenu déjà vu/corrigé)."""
        if role not in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN, UserRole.PROFESSOR):
            raise PermissionError('Non autorisé')
        subj = SubjectRepository.find_by_id(subject_id)
        if not subj:
            raise LookupError('Sujet non trouvé')
        if role == UserRole.PROFESSOR:
            from services.ec_rights import can_edit_subject
            _s = get_session()
            try:
                if not can_edit_subject(_s, subj, user_id):
                    raise PermissionError("Vous ne pouvez modifier que vos propres sujets ou ceux des EC dont vous êtes responsable")
            finally:
                _s.close()
        if SubjectRepository.has_locked_exam(subject_id):
            raise PermissionError(
                "Ce sujet est lié à un examen déjà actif, clôturé ou ayant reçu des "
                "tentatives — il ne peut plus être modifié pour ne pas désynchroniser "
                "le contenu vu par les étudiants."
            )
        return SubjectRepository.update_content(subject_id, title=title, content=content, rubric=rubric)

    @staticmethod
    def delete(subject_id: int, user_id: int, role: UserRole) -> None:
        if role not in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN, UserRole.PROFESSOR):
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
        if role not in (UserRole.PROFESSOR, UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
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
