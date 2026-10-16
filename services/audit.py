"""Journal d'activité des comptes à privilèges.

Chaque requête d'un administrateur ou d'un responsable d'examen laisse une trace lisible : connexion,
création / modification / suppression, consultation de données sensibles (exports, listes de comptes,
copies…). Les traces sont ajoutées dans `staff_audit_log` (jamais modifiées ni supprimées par l'application)
et ne sont consultables que par le compte propriétaire (voir routes/audit.py).

Principes :
- l'audit ne doit JAMAIS faire échouer ni ralentir sensiblement une requête : tout est protégé par try/except ;
- on enregistre aussi les refus (403) et les erreurs : une tentative interdite est une information ;
- aucun secret n'est conservé (mots de passe, jetons, codes) ; les gros textes sont réduits à leur longueur.
"""
from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timedelta

from flask import g, request

AUDITED_ROLES = ('admin', 'responsable_examen')
MUTATING = ('POST', 'PUT', 'PATCH', 'DELETE')
ONLINE_SECONDS = 300                      # « connecté » = une requête dans les 5 dernières minutes

# Requêtes de fond (sondage, battements de cœur…) : jamais journalisées
_IGNORED = re.compile(
    r'heartbeat|camera_snapshot|log_activity|proctoring_event|/save$|/submit$|/recording|phone_camera|'
    r'mark-read|recent_incidents|^/api/agent/|/auth/refresh|check_duplicate|suggest-question-count|'
    r'/api/biometric/verify|pause/start|student_message|call_request|/api/notifications|/api/audit|'
    r'/api/health|group_recording|room_recording|claim_lock')

# Consultations de données sensibles : journalisées (une fois par minute et par page)
_SENSITIVE_READ = re.compile(
    r'export|csv|pdf|download|bilan|security_report|recordings|snapshot_local|/review$|/attempts$|'
    r'^/api/admin/users$|^/api/students/list$|corrected_papers|exams_history|^/api/transcripts|^/api/reclamations$')

_SECRET_KEY = re.compile(r'pass|pwd|token|secret|otp|code$|_code|key$|answers?$', re.I)
_BIG_TEXT = ('content', 'rubric', 'instructions', 'description', 'text')

# (méthode, règle) -> (nature, libellé, type de la cible)
L = {
    ('POST', '/api/online_exams'): ('create', "Création d'un examen", 'exam'),
    ('PUT', '/api/admin/online_exams/<int:exam_id>'): ('update', "Modification d'un examen", 'exam'),
    ('DELETE', '/api/online_exams/<int:exam_id>'): ('delete', "Suppression d'un examen", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/activate'): ('update', "Activation d'un examen", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/close'): ('update', "Clôture d'un examen", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/extend'): ('update', "Prolongation d'un examen", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/start'): ('other', "Entrée dans un examen", 'exam'),
    ('PUT', '/api/online_exams/<int:exam_id>/publish-results'): ('update', "Publication / retrait des résultats", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/import-grades'): ('update', "Import de notes", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/proctors'): ('update', "Ajout d'un surveillant à l'examen", 'exam'),
    ('DELETE', '/api/online_exams/<int:exam_id>/proctors/<int:proctor_id>'): ('update', "Retrait d'un surveillant de l'examen", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/distribute_proctors'): ('update', "Répartition des étudiants entre surveillants", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/preview/grade'): ('read', "Essai de correction (prévisualisation)", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/waves/plan'): ('update', "Organisation d'un examen par vagues", 'exam'),
    ('PUT', '/api/online_exams/<int:exam_id>/waves/<int:wave_id>'): ('update', "Modification d'une vague", 'exam'),
    ('POST', '/api/online_exams/<int:exam_id>/waves/<int:wave_id>/students'): ('update', "Déplacement d'étudiants entre vagues", 'exam'),
    ('DELETE', '/api/online_exams/<int:exam_id>/waves'): ('delete', "Suppression des vagues d'un examen", 'exam'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/correct'): ('update', "Correction d'une copie en ligne", 'attempt'),
    ('PUT', '/api/exam_attempts/<int:attempt_id>/manual-grade'): ('update', "Note saisie à la main", 'attempt'),
    ('PUT', '/api/exam_attempts/<int:attempt_id>/question-grades'): ('update', "Notes par question", 'attempt'),
    ('PUT', '/api/exam_attempts/<int:attempt_id>/extra-time'): ('update', "Temps supplémentaire accordé", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/proctor_ban'): ('update', "Exclusion d'un étudiant", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/unban'): ('update', "Réintégration d'un étudiant exclu", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/send_warning'): ('update', "Avertissement / message / appel à un étudiant", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/proctor-note'): ('update', "Note de surveillance", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/access_code'): ('update', "Code de reprise généré", 'attempt'),
    ('POST', '/api/exam_attempts/<int:attempt_id>/identity_manual_verify'): ('update', "Vérification d'identité", 'attempt'),
    ('POST', '/api/subjects'): ('create', "Création d'un sujet", 'subject'),
    ('PUT', '/api/subjects/<int:subject_id>'): ('update', "Modification d'un sujet", 'subject'),
    ('DELETE', '/api/subjects/<int:subject_id>'): ('delete', "Suppression d'un sujet", 'subject'),
    ('POST', '/api/subjects/upload'): ('create', "Création d'un sujet à partir de fichiers", 'subject'),
    ('POST', '/api/subjects/create-from-suggestion'): ('create', "Enregistrement d'un sujet généré par l'IA", 'subject'),
    ('POST', '/api/subjects/generate-full-exam'): ('create', "Génération d'un examen complet par l'IA", 'subject'),
    ('POST', '/api/subjects/generate-more-questions'): ('update', "Génération de questions supplémentaires", 'subject'),
    ('POST', '/api/subjects/<int:subject_id>/upload_image'): ('update', "Image ajoutée à un sujet", 'subject'),
    ('POST', '/api/subjects/upload_media'): ('update', "Média ajouté à un sujet", 'subject'),
    ('POST', '/api/ai/generate-exam-suggestions'): ('create', "Génération de suggestions de sujets par l'IA", None),
    ('POST', '/api/question_bank'): ('create', "Question ajoutée à la banque", 'question'),
    ('PUT', '/api/question_bank/<int:q_id>'): ('update', "Question de la banque modifiée", 'question'),
    ('DELETE', '/api/question_bank/<int:q_id>'): ('delete', "Question de la banque supprimée", 'question'),
    ('POST', '/api/question_bank/<int:q_id>/duplicate'): ('create', "Question de la banque dupliquée", 'question'),
    ('POST', '/api/question_bank/assemble'): ('create', "Sujet assemblé depuis la banque", None),
    ('POST', '/api/question_bank/bulk_delete'): ('delete', "Suppression groupée de questions", None),
    ('POST', '/api/question_bank/bulk_move'): ('update', "Déplacement groupé de questions", None),
    ('POST', '/api/question_bank/duplicates/auto-clean'): ('delete', "Nettoyage automatique des doublons", None),
    ('PUT', '/api/reclamations/<int:rid>'): ('update', "Réponse à une réclamation", 'reclamation'),
    ('DELETE', '/api/reclamations/<int:rid>'): ('delete', "Suppression d'une réclamation", 'reclamation'),
    ('POST', '/api/reclamations/bulk_delete'): ('delete', "Suppression groupée de réclamations", None),
    ('POST', '/api/reclamations/<int:rid>/apply_proposal'): ('update', "Proposition de l'IA appliquée à une réclamation", 'reclamation'),
    ('POST', '/api/reclamations/<int:rid>/reject_proposal'): ('update', "Proposition de l'IA rejetée", 'reclamation'),
    ('POST', '/api/reclamations/<int:rid>/process_ia'): ('update', "Réclamation traitée par l'IA", 'reclamation'),
    ('PUT', '/api/papers/<int:paper_id>/publish'): ('update', "Publication d'une copie", 'paper'),
    ('PUT', '/api/papers/publish-bulk'): ('update', "Publication groupée de copies", None),
    ('POST', '/api/papers/upload'): ('create', "Copie déposée", None),
    ('POST', '/api/papers/upload-batch'): ('create', "Copies déposées en lot", None),
    ('POST', '/api/papers/correct'): ('update', "Correction de copie", None),
    ('POST', '/api/transcripts/generate/<int:student_id>/<int:semester_id>'): ('create', "Génération d'un relevé de notes", 'user'),
    ('PUT', '/api/transcripts/<int:tid>/publish'): ('update', "Publication d'un relevé de notes", 'transcript'),
    ('DELETE', '/api/transcripts/<int:tid>'): ('delete', "Suppression d'un relevé de notes", 'transcript'),
    ('POST', '/api/restitution_examples'): ('create', "Copie-exemple créée", None),
    ('PUT', '/api/restitution_examples/<int:eid>'): ('update', "Copie-exemple modifiée", None),
    ('PUT', '/api/restitution_examples/<int:eid>/publish'): ('update', "Copie-exemple publiée", None),
    ('DELETE', '/api/restitution_examples/<int:eid>'): ('delete', "Copie-exemple supprimée", None),
    # utilisateurs et maquette
    ('POST', '/api/admin/users'): ('create', "Création d'un compte", None),
    ('PUT', '/api/admin/users/<int:target_id>'): ('update', "Modification d'un compte", 'user'),
    ('DELETE', '/api/admin/users/<int:target_id>'): ('delete', "Suppression d'un compte", 'user'),
    ('POST', '/api/admin/users/import-csv'): ('create', "Import de comptes (CSV)", None),
    ('POST', '/api/admin/users/student-no-email'): ('create', "Étudiant créé sans e-mail", None),
    ('POST', '/api/admin/students/<int:student_id>/enroll'): ('update', "Inscription d'un étudiant", 'user'),
    ('POST', '/api/admin/students/<int:student_id>/set_formation'): ('update', "Formation d'un étudiant modifiée", 'user'),
    ('POST', '/api/admin/student_enrollments'): ('update', "Inscription à une UE", None),
    ('DELETE', '/api/admin/student_enrollments/<int:eid>'): ('delete', "Désinscription d'une UE", None),
    ('POST', '/api/admin/student_enrollments/bulk'): ('update', "Inscriptions groupées", None),
    ('POST', '/api/admin/student_enrollments/bulk_remove'): ('delete', "Désinscriptions groupées", None),
    ('POST', '/api/admin/ec_assignments'): ('update', "Enseignant affecté à un EC", None),
    ('DELETE', '/api/admin/ec_assignments/<int:aid>'): ('delete', "Affectation d'EC retirée", None),
    ('POST', '/api/admin/ecs'): ('create', "EC créé", None),
    ('PUT', '/api/admin/ecs/<int:eid>'): ('update', "EC modifié", 'ec'),
    ('DELETE', '/api/admin/ecs/<int:eid>'): ('delete', "EC supprimé", 'ec'),
    ('POST', '/api/admin/ecs/<int:eid>/assign'): ('update', "Affectation d'EC", 'ec'),
    ('POST', '/api/admin/ues'): ('create', "UE créée", None),
    ('PUT', '/api/admin/ues/<int:uid>'): ('update', "UE modifiée", None),
    ('DELETE', '/api/admin/ues/<int:uid>'): ('delete', "UE supprimée", None),
    ('POST', '/api/admin/semesters'): ('create', "Semestre créé", None),
    ('PUT', '/api/admin/semesters/<int:sid>'): ('update', "Semestre modifié", None),
    ('DELETE', '/api/admin/semesters/<int:sid>'): ('delete', "Semestre supprimé", None),
    ('POST', '/api/admin/formations'): ('create', "Formation créée", None),
    ('PUT', '/api/admin/formations/<int:fid>'): ('update', "Formation modifiée", None),
    ('DELETE', '/api/admin/formations/<int:fid>'): ('delete', "Formation supprimée", None),
    ('POST', '/api/admin/niveaux'): ('create', "Niveau créé", None),
    ('PUT', '/api/admin/niveaux/<int:nid>'): ('update', "Niveau modifié", None),
    ('DELETE', '/api/admin/niveaux/<int:nid>'): ('delete', "Niveau supprimé", None),
    ('POST', '/api/admin/poles'): ('create', "Pôle créé", None),
    ('PUT', '/api/admin/poles/<int:pid>'): ('update', "Pôle modifié", None),
    ('DELETE', '/api/admin/poles/<int:pid>'): ('delete', "Pôle supprimé", None),
    ('POST', '/api/admin/maquette/import-csv'): ('create', "Import de la maquette (CSV)", None),
    ('POST', '/api/admin/maquette/import-excel-confirm'): ('create', "Import de la maquette (Excel) confirmé", None),
    ('POST', '/api/admin/maquette/import-excel-preview'): ('read', "Aperçu d'un import de maquette", None),
    ('POST', '/api/admin/proctor_groups'): ('create', "Groupe de surveillants créé", None),
    ('PUT', '/api/admin/proctor_groups/<int:gid>'): ('update', "Groupe de surveillants modifié", 'proctor_group'),
    ('DELETE', '/api/admin/proctor_groups/<int:gid>'): ('delete', "Groupe de surveillants supprimé", 'proctor_group'),
    ('POST', '/api/admin/proctor_groups/<int:gid>/members'): ('update', "Surveillant ajouté à un groupe", 'proctor_group'),
    ('DELETE', '/api/admin/proctor_groups/<int:gid>/members/<int:mid>'): ('update', "Surveillant retiré d'un groupe", 'proctor_group'),
    ('POST', '/api/admin/proctor_groups/<int:gid>/supervisors'): ('update', "Superviseur ajouté à un groupe", 'proctor_group'),
    ('DELETE', '/api/admin/proctor_groups/<int:gid>/supervisors/<int:sid>'): ('update', "Superviseur retiré d'un groupe", 'proctor_group'),
    ('POST', '/api/admin/proctor_groups/<int:gid>/ecs'): ('update', "EC rattaché à un groupe", 'proctor_group'),
    ('DELETE', '/api/admin/proctor_groups/<int:gid>/ecs/<int:ec_id>'): ('update', "EC détaché d'un groupe", 'proctor_group'),
    ('POST', '/api/admin/proctor_groups/<int:gid>/exams'): ('update', "Examen rattaché à un groupe", 'proctor_group'),
    ('DELETE', '/api/admin/proctor_groups/<int:gid>/exams/<int:exam_id>'): ('update', "Examen détaché d'un groupe", 'proctor_group'),
    ('POST', '/api/superviseur/proctor_call/<int:proctor_id>/request'): ('update', "Appel d'un surveillant", 'user'),
    ('PUT', '/api/profile'): ('update', "Profil modifié", None),
    ('PUT', '/api/profile/password'): ('update', "Mot de passe changé", None),
}

# Consultations sensibles : libellés lisibles (premier motif qui correspond)
_READ_LABELS = [
    (r'^/api/admin/users$', 'Consultation de la liste des comptes'),
    (r'^/api/students/list$', 'Consultation de la liste des étudiants'),
    (r'exams_history', "Consultation de l'historique des examens"),
    (r'corrected_papers', 'Consultation des copies corrigées'),
    (r'/review$', "Consultation d'une copie d'examen en ligne"),
    (r'/attempts$', "Consultation des résultats d'un examen"),
    (r'recordings', 'Consultation des enregistrements vidéo'),
    (r'snapshot_local', "Consultation d'une capture de surveillance"),
    (r'security_report|bilan|pdf', "Téléchargement d'un rapport"),
    (r'export|csv|download', "Export de données"),
    (r'^/api/transcripts', 'Consultation des relevés de notes'),
    (r'^/api/reclamations', 'Consultation des réclamations'),
]


def _read_label(rule):
    for pat, label in _READ_LABELS:
        if re.search(pat, rule):
            return label
    return 'Consultation : ' + re.sub(r'<[^>]+>', '…', rule).replace('/api/', '')


_ARG_TYPES = {'exam_id': 'exam', 'attempt_id': 'attempt', 'subject_id': 'subject', 'target_id': 'user',
              'student_id': 'user', 'gid': 'proctor_group', 'q_id': 'question', 'rid': 'reclamation',
              'paper_id': 'paper', 'tid': 'transcript', 'proctor_id': 'user'}


# ── utilitaires ──────────────────────────────────────────────────────────────

def _peek_role():
    """Rôle lu dans le jeton SANS vérifier la signature — uniquement pour décider s'il faut photographier la cible
    AVANT une suppression ; l'enregistrement, lui, se fonde sur le rôle vérifié (g.token_data)."""
    try:
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        body = auth[7:].split('.')[2]
        raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))[:-64]
        return json.loads(raw).get('role')
    except Exception:
        return None


def _redis():
    try:
        from cache import _get_client
        return _get_client()
    except Exception:
        return None


def _resolve_label(session, ttype, tid):
    """Nom lisible de la cible (titre d'examen, nom de compte…), « None » si introuvable."""
    import models as M
    try:
        if ttype == 'exam':
            o = session.get(M.OnlineExam, tid); return o and o.title
        if ttype == 'subject':
            o = session.get(M.Subject, tid); return o and o.title
        if ttype == 'user':
            o = session.get(M.User, tid); return o and f"{o.full_name} ({o.email})"
        if ttype == 'attempt':
            o = session.get(M.ExamAttempt, tid)
            if not o:
                return None
            st = session.get(M.User, o.student_id)
            ex = session.get(M.OnlineExam, o.exam_id)
            return f"{st.full_name if st else '?'} — {ex.title if ex else '?'}"
        if ttype == 'proctor_group':
            o = session.get(M.ProctorGroup, tid); return o and o.name
        if ttype == 'question':
            o = session.get(M.QuestionBank, tid); return o and o.title
        if ttype == 'ec':
            o = session.get(M.EC, tid); return o and f"{o.code} — {o.name}"
        if ttype == 'reclamation':
            return f"Réclamation n° {tid}"
        if ttype == 'paper':
            return f"Copie n° {tid}"
        if ttype == 'transcript':
            return f"Relevé n° {tid}"
    except Exception:
        return None
    return None


def _target_of(rule_info):
    """(type, id) de la cible déduits des arguments de l'URL."""
    ttype_rule = rule_info[2] if rule_info else None
    args = request.view_args or {}
    for name, ttype in _ARG_TYPES.items():
        if name in args:
            return (ttype if ttype_rule in (None, ttype) else ttype_rule), args[name]
    for name, v in args.items():
        if isinstance(v, int):
            return ttype_rule, v
    return ttype_rule, None


def _safe_details():
    """Champs envoyés, sans secret ni gros texte (réduit à sa longueur)."""
    out = {}
    try:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            for k, v in list(data.items())[:40]:
                if _SECRET_KEY.search(k):
                    out[k] = '«masqué»'
                elif isinstance(v, str):
                    out[k] = f"({len(v)} caractères)" if (k in _BIG_TEXT and len(v) > 120) else v[:300]
                elif isinstance(v, (int, float, bool)) or v is None:
                    out[k] = v
                elif isinstance(v, (list, tuple)):
                    out[k] = f"[{len(v)} élément(s)]"
                elif isinstance(v, dict):
                    out[k] = '{…}'
        if request.files:
            out['fichiers'] = [f.filename for f in request.files.values()][:10]
        if request.method == 'GET' and request.args:
            out.update({k: v[:100] for k, v in list(request.args.items())[:10] if not _SECRET_KEY.search(k)})
    except Exception:
        pass
    return json.dumps(out, ensure_ascii=False)[:2000] if out else None


_user_cache = {}                       # id -> (nom, e-mail, instant) ; évite une requête par action


def _user_info(session, uid):
    hit = _user_cache.get(uid)
    if hit and time.time() - hit[2] < 300:
        return hit[0], hit[1]
    from models import User
    u = session.get(User, uid)
    name, email = (u.full_name, u.email) if u else ('?', '?')
    _user_cache[uid] = (name, email, time.time())
    return name, email


def _humanize(method, rule):
    verbs = {'POST': 'Création / action', 'PUT': 'Modification', 'PATCH': 'Modification', 'DELETE': 'Suppression', 'GET': 'Consultation'}
    short = re.sub(r'<[^>]+>', '…', rule).replace('/api/', '').strip('/')
    return f"{verbs.get(method, method)} : {short}"


def _write(session, **row):
    from models import StaffAuditLog
    session.add(StaffAuditLog(**row))
    session.commit()


# ── crochets Flask ───────────────────────────────────────────────────────────

def _before():
    try:
        if request.method not in MUTATING or request.url_rule is None:
            return
        rule = request.url_rule.rule
        if _IGNORED.search(rule) or _peek_role() not in AUDITED_ROLES:
            return
        info = L.get((request.method, rule))
        ttype, tid = _target_of(info)
        if tid is not None and ttype and request.method == 'DELETE':
            # la cible n'existera plus après la requête : on mémorise son nom maintenant
            from models import get_session
            s = get_session()
            try:
                g._audit_target_label = _resolve_label(s, ttype, tid)
            finally:
                s.close()
    except Exception:
        pass


def _after(response):
    try:
        rule = request.url_rule.rule if request.url_rule is not None else request.path
        if request.method == 'POST' and rule == '/api/auth/login':
            _log_login(response)
            return response
        td = getattr(g, 'token_data', None)
        if not td or td.get('role') not in AUDITED_ROLES or not td.get('sub'):
            return response
        uid = int(td['sub'])

        # présence : dernière requête de ce compte
        r = _redis()
        if r is not None:
            try:
                r.set(f'cei:audit:seen:{uid}', datetime.utcnow().isoformat(), ex=3600)
            except Exception:
                pass

        method = request.method
        if _IGNORED.search(rule) or request.path.startswith('/api/audit'):
            return response
        if rule == '/api/auth/logout' and method == 'POST':
            kind, label, ttype = 'logout', 'Déconnexion', None
        elif method in MUTATING:
            kind, label, ttype = L.get((method, rule), ('update' if method != 'DELETE' else 'delete', _humanize(method, rule), None))
        elif method == 'GET' and _SENSITIVE_READ.search(rule):
            # une trace par minute et par page (pagination, rechargements) ; en cas de panne Redis on trace quand même
            fresh = True
            if r is not None:
                try:
                    fresh = bool(r.set(f"cei:audit:read:{uid}:{request.path}", '1', nx=True, ex=60))
                except Exception:
                    fresh = True
            if not fresh:
                return response
            kind, label, ttype = 'read', _read_label(rule), None
        else:
            return response

        from models import get_session
        info = L.get((method, rule))
        tt, tid = _target_of(info)
        target_label = getattr(g, '_audit_target_label', None)
        s = get_session()
        try:
            # création : l'identifiant et le titre sont dans la réponse
            if tid is None and 200 <= response.status_code < 300 and response.mimetype == 'application/json' \
                    and (response.content_length or 0) < 20000:
                try:
                    body = json.loads(response.get_data(as_text=True))
                    obj = next((body[k] for k in ('exam', 'subject', 'user', 'question') if isinstance(body.get(k), dict)), body)
                    if isinstance(obj, dict) and isinstance(obj.get('id'), int):
                        tid = obj['id']
                        target_label = obj.get('title') or obj.get('full_name') or obj.get('name')
                except Exception:
                    pass
            if tid is not None and tt and not target_label:
                target_label = _resolve_label(s, tt, tid)
            name, email = _user_info(s, uid)
            _write(s, user_id=uid, user_name=name, user_email=email, role=td.get('role'), kind=kind,
                   method=method, path=request.path[:400], rule=rule[:200], status_code=response.status_code,
                   label=label[:300], target_type=tt, target_id=tid if isinstance(tid, int) else None,
                   target_label=(target_label or '')[:300] or None, ip=(request.remote_addr or '')[:64],
                   user_agent=(request.headers.get('User-Agent') or '')[:300], details=_safe_details())
        finally:
            s.close()
    except Exception as e:                                   # l'audit ne casse jamais une requête
        try:
            print(f"[audit] ignoré : {e}")
        except Exception:
            pass
    return response


def _log_login(response):
    """Connexion réussie ou échouée d'un compte à privilèges (la requête n'est pas encore authentifiée)."""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email:
        return
    from models import get_session, User, UserRole
    s = get_session()
    try:
        u = s.query(User).filter(User.email.ilike(email)).first()
        if not u or u.role not in (UserRole.ADMIN, UserRole.RESPONSABLE_EXAMEN):
            return
        ok = 200 <= response.status_code < 300
        _write(s, user_id=u.id, user_name=u.full_name, user_email=u.email, role=u.role.value,
               kind='login' if ok else 'login_failed', method='POST', path='/api/auth/login', rule='/api/auth/login',
               status_code=response.status_code, label='Connexion' if ok else 'Tentative de connexion refusée',
               target_type=None, target_id=None, target_label=None, ip=(request.remote_addr or '')[:64],
               user_agent=(request.headers.get('User-Agent') or '')[:300], details=None)
        if ok:
            r = _redis()
            if r is not None:
                try:
                    r.set(f'cei:audit:seen:{u.id}', datetime.utcnow().isoformat(), ex=3600)
                except Exception:
                    pass
    finally:
        s.close()


def install(app):
    """À appeler une fois, après la création de l'application."""
    app.before_request(_before)
    app.after_request(_after)


# ── qui peut consulter ───────────────────────────────────────────────────────

def viewer_ids(session):
    """Comptes autorisés à consulter le journal : ceux de AUDIT_VIEWER_EMAILS (séparés par des virgules), à
    défaut le PREMIER administrateur (identifiant le plus bas) — le propriétaire de la plateforme. Un
    administrateur ajouté plus tard n'y a pas accès."""
    import os
    from sqlalchemy import func
    from models import User, UserRole
    emails = [e.strip().lower() for e in os.getenv('AUDIT_VIEWER_EMAILS', '').split(',') if e.strip()]
    if emails:
        return [u.id for u in session.query(User).filter(User.role == UserRole.ADMIN, User.is_active == True)
                .filter(func.lower(User.email).in_(emails)).all()]
    first = session.query(User).filter(User.role == UserRole.ADMIN, User.is_active == True).order_by(User.id).first()
    return [first.id] if first else []
