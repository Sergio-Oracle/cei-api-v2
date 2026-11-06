"""
Correction IA ancrée dans le cours (moteur RAG).

Un sujet créé par « Générer Suggestions » à partir de documents indexés dans le moteur RAG garde avec lui les
passages du cours sur lesquels il a été construit (`Subject.rag_passages`). À la correction, l'IA reçoit ces
passages — plus, pour chaque question, les passages que le moteur retrouve pour elle — et reçoit l'ordre de juger
les réponses de l'étudiant d'après le barème ET ces passages, jamais d'après ses connaissances générales.

Les autres sujets (importés, créés à la main, depuis un document) ne sont pas concernés : `reference_for` renvoie
une chaîne vide et la correction reste identique.

Robustesse : le moteur RAG peut être arrêté ou lent au moment de la correction (examens de masse). Les passages
retrouvés par question sont donc calculés UNE fois par sujet, mis en cache (Redis), et en cas d'échec on retombe
sur les passages enregistrés avec le sujet. Jamais d'erreur remontée : au pire, la correction se fait sans la
partie « retrouvés pour cette question ».
"""
import hashlib
import json
import re

MAX_STORED = 12            # passages gardés avec le sujet
STORED_CHARS = 3000        # taille max d'un passage gardé
BLOCK_BUDGET = 14000       # taille max du bloc envoyé à l'IA (caractères)
LIVE_PER_QUESTION = 2
LIVE_CHARS = 1200
MAX_QUESTIONS = 40
CACHE_TTL = 7 * 24 * 3600

_Q_SPLIT = re.compile(r'(?m)^\s*Question\s+(\d{1,3})\b')

RULES = """RÈGLES DE CORRECTION ANCRÉE DANS LE COURS (OBLIGATOIRES) :
- Ce sujet a été construit à partir des documents de cours reproduits plus bas. Pour décider si une réponse de
  l'étudiant est exacte, appuie-toi UNIQUEMENT sur le barème et sur ces passages, jamais sur tes connaissances générales.
- Une réponse équivalente à ce que dit le cours est juste, même si la formulation diffère. Une affirmation contredite
  par le cours est fausse. Une affirmation absente du barème et du cours ne rapporte aucun point, mais n'est pas
  pénalisée tant qu'elle ne contredit pas le cours.
- N'exige jamais une notion, une définition, une date ou un chiffre qui ne figure ni dans le barème ni dans les passages.
- Si le barème et le cours se contredisent, applique le barème et ajoute à la fin du retour de cette question la phrase
  « Point à vérifier par l'enseignant : le barème diffère du cours. »
- Dans ton retour à l'étudiant, tu peux écrire « d'après le cours » (avec le nom du document si utile), mais n'écris
  jamais de codes de passage comme [S2] ou [R4] : l'étudiant ne les voit pas."""


def _blocks(text: str) -> dict:
    out, marks = {}, list(_Q_SPLIT.finditer(text or ''))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.start():end].strip()
    return out


def clean_passages(raw) -> list:
    """Passages reçus du navigateur au moment de l'enregistrement du sujet : forme et tailles bornées."""
    out = []
    for p in (raw or []):
        if not isinstance(p, dict) or not str(p.get('content') or '').strip():
            continue
        out.append({
            'id': str(p.get('id') or f'S{len(out) + 1}')[:8],
            'filename': str(p.get('filename') or '')[:200],
            'module': str(p.get('module') or '')[:200],
            'fileurl': str(p.get('fileurl') or '')[:1000],
            'content': re.sub(r'\s+', ' ', str(p['content'])).strip()[:STORED_CHARS],
        })
        if len(out) >= MAX_STORED:
            break
    return out


def stored_passages(subject) -> list:
    if not getattr(subject, 'rag_grounded', False) or not getattr(subject, 'rag_passages', None):
        return []
    try:
        data = json.loads(subject.rag_passages)
    except (TypeError, ValueError):
        return []
    return clean_passages(data)


def _live_by_question(subject, stored: list) -> dict:
    """{numéro de question: [passages]} retrouvés par le moteur dans les documents du sujet. {} si indisponible."""
    from models import get_session, RagDocument
    from services import ragflow_client
    urls = sorted({p['fileurl'] for p in stored if p.get('fileurl')})
    questions = _blocks(subject.content)
    if not urls or not questions or not subject.ec_id:
        return {}
    session = get_session()
    try:
        engine = ragflow_client.active_engine(session)
        if not engine:
            return {}
        docs = session.query(RagDocument).filter(
            RagDocument.engine_id == engine.id, RagDocument.ec_id == subject.ec_id,
            RagDocument.status == 'ready', RagDocument.fileurl.in_(urls)).all()
        if not docs:
            return {}
        by_id = {d.document_id: d for d in docs}
        dataset_ids = list({d.dataset_id for d in docs})
        rf = ragflow_client.client_for(engine)
        out = {}
        for num in sorted(questions)[:MAX_QUESTIONS]:
            q = re.sub(r'\s+', ' ', questions[num]).strip()[:500]
            try:
                chunks = rf.retrieve(dataset_ids, list(by_id), q, top_k=LIVE_PER_QUESTION)
            except Exception as e:            # moteur lent ou arrêté : on s'arrête là, le reste du bloc suffit
                print(f"[rag_correction] passages non retrouvés (question {num}) : {e}")
                break
            found = []
            for c in chunks[:LIVE_PER_QUESTION]:
                d = by_id.get(c.get('document_id'))
                txt = re.sub(r'\s+', ' ', c.get('content') or '').strip()[:LIVE_CHARS]
                if d and txt:
                    found.append({'filename': d.filename, 'content': txt})
            if found:
                out[num] = found
        return out
    except Exception as e:
        print(f"[rag_correction] recherche par question impossible : {e}")
        return {}
    finally:
        session.close()


def _render(stored: list, live: dict) -> str:
    parts, used, seen = [], 0, set()
    for p in stored:
        chunk = f"[{p['id']}] ({p['filename']})\n{p['content']}"
        if parts and used + len(chunk) > BLOCK_BUDGET:
            break
        parts.append(chunk); used += len(chunk); seen.add(p['content'][:200])
    extra = []
    for num in sorted(live):
        for p in live[num]:
            key = p['content'][:200]
            if key in seen:
                continue
            seen.add(key)
            if used + len(p['content']) > BLOCK_BUDGET:
                break
            extra.append(f"(question {num} — {p['filename']})\n{p['content']}")
            used += len(p['content'])
    block = "PASSAGES DU COURS (source de vérité) :\n" + '\n\n'.join(parts)
    if extra:
        block += "\n\nAUTRES PASSAGES DU COURS RETROUVÉS POUR CERTAINES QUESTIONS :\n" + '\n\n'.join(extra)
    return block


def reference_for(subject) -> str:
    """Bloc à ajouter au message de correction (règles + passages), ou '' si le sujet n'est pas ancré dans un cours."""
    stored = stored_passages(subject)
    if not stored:
        return ''
    key = 'cei:ragref:%s:%s' % (subject.id, hashlib.sha256(
        ((subject.content or '') + '|' + (subject.rag_passages or '')).encode()).hexdigest()[:16])
    live = None
    try:
        from cache import cache_get, cache_set
        cached = cache_get(key)
        if isinstance(cached, dict):
            live = {int(k): v for k, v in cached.items()}
    except Exception:
        cached = None
    if live is None:
        try:
            from cache import cache_get as _cg
            failed_recently = bool(_cg(key + ':ko'))
        except Exception:
            failed_recently = False
        live = {} if failed_recently else _live_by_question(subject, stored)
        try:
            from cache import cache_set
            if live:
                cache_set(key, {str(k): v for k, v in live.items()}, ttl=CACHE_TTL)
            elif not failed_recently:
                # moteur arrêté ou sans résultat : pas de nouvel essai pendant 5 min (les copies se corrigent par centaines)
                cache_set(key + ':ko', 1, ttl=300)
        except Exception:
            pass
    return RULES + "\n\n" + _render(stored, live)


def safe_reference_for(subject) -> str:
    """Comme reference_for, mais ne lève jamais : '' au moindre problème (la correction se fait alors sans)."""
    try:
        return reference_for(subject) if subject is not None else ''
    except Exception as e:
        print(f"[rag_correction] référence du cours indisponible : {e}")
        return ''
