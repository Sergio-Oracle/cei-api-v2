"""
Indexation des documents de cours Moodle dans le moteur RAG (RAGFlow).

Une base documentaire RAGFlow par EC. Ses documents sont ceux, visibles et
exploitables (PDF, DOCX, DOC, TXT, chapitres de livre HTML), du cours Moodle
de même code. Un document est identifié par son adresse Moodle (fileurl) ;
une date de modification ou une taille différente signale un fichier
remplacé, qui est ré-indexé.

Parcours imposé par l'administration :
  simulation (rien n'est écrit) → rapport → indexation réelle validée par
  l'admin → indexation automatique permanente (service cei-moodle-sync).

Extraction : « Plain Text » d'abord (rapide sur processeur). Un document qui
ne donne aucun fragment (PDF scanné) est automatiquement repris avec
l'analyse de mise en page et l'OCR (DeepDOC), plus lente.

Génération ancrée : passages_for() renvoie des passages numérotés [S1]…[Sn]
des documents cochés par l'enseignant, à la place du texte brut tronqué.
"""
import json
import re
import time
from datetime import datetime, timezone

from cache import cache_set_nx, cache_delete, cache_get, cache_set
from services import moodle_sync, ragflow_client
from services.moodle_sync import MoodleError
from services.ragflow_client import RagflowError

MAX_FILE_MB = 100
FAST_LAYOUT = 'Plain Text'
OCR_LAYOUT = 'DeepDOC'

# Rythme du passage automatique (service cei-moodle-sync, passage de 30 s)
CHECKS_PER_TICK = 2          # cours Moodle relus par passage
UPLOADS_PER_TICK = 3         # fichiers envoyés au plus par passage
STATUS_EVERY = 60            # s — suivi des documents en cours d'indexation


def _lock(ec_id: int) -> bool:
    return cache_set_nx(f'cei:rag:ec:{ec_id}', 900)


def _unlock(ec_id: int) -> None:
    cache_delete(f'cei:rag:ec:{ec_id}')


def _upload_name(m: dict) -> str:
    """Nom lisible dans RAGFlow et dans les sources citées : un chapitre de
    livre s'appelle index.html dans Moodle, on lui donne son titre."""
    if m['extension'] in ('html', 'htm'):
        base = f"{m.get('module') or 'Livre'} - {m.get('title') or 'chapitre'}"
        name = f'{base}.html'
    else:
        name = m['filename']
    name = re.sub(r'[\\/:*?"<>|\r\n]+', ' ', name).strip()
    return name[-200:] if len(name) > 200 else name


def _materials(client, course_id: int) -> dict:
    # Modules masqués aux étudiants exclus (brouillons, corrigés non publiés).
    return {m['fileurl']: m for m in client.course_materials(course_id) if m['visible']}


def plan_ec(session, engine, ec, found=None, retry_failed: bool = True) -> dict:
    """Ce qu'une indexation changerait pour cet EC, sans rien écrire."""
    from models import RagDocument
    found = found or moodle_sync.find_course_for_ec(session, ec.code)
    if not found:
        return {'ec_code': ec.code, 'skipped': 'Aucun cours Moodle de ce code'}
    inst, client, course = found
    mats = _materials(client, course['id'])
    docs = {d.fileurl: d for d in session.query(RagDocument).filter_by(engine_id=engine.id, ec_id=ec.id)}
    add = [m for u, m in mats.items() if u not in docs]
    update = [m for u, m in mats.items() if u in docs
              and (docs[u].timemodified != m['timemodified'] or (docs[u].filesize or 0) != (m['filesize'] or 0))]
    upd_urls = {m['fileurl'] for m in update}
    retry = [docs[u] for u in mats if u in docs and u not in upd_urls and docs[u].status == 'failed'] if retry_failed else []
    remove = [d for u, d in docs.items() if u not in mats]
    return {'ec_code': ec.code, 'ec_id': ec.id, 'instance': inst.name, 'course': course,
            'client': client, 'add': add, 'update': update, 'retry': retry, 'remove': remove,
            'unchanged': len(mats) - len(add) - len(update) - len(retry), 'total': len(mats),
            'indexing': sum(1 for u in mats if u in docs and docs[u].status == 'indexing'),
            'failed': sum(1 for u in mats if u in docs and docs[u].status == 'failed')}


def _report(plan: dict, done: dict | None = None) -> dict:
    if plan.get('skipped'):
        return {'ec_code': plan['ec_code'], 'skipped': plan['skipped']}
    rep = {
        'ec_code': plan['ec_code'], 'instance': plan['instance'], 'documents': plan['total'],
        'to_add': [_upload_name(m) for m in plan['add']],
        'to_update': [_upload_name(m) for m in plan['update']],
        'to_retry': [d.filename for d in plan['retry']],
        'to_remove': [d.filename for d in plan['remove']],
        'unchanged': plan['unchanged'], 'indexing': plan['indexing'], 'failed': plan['failed'],
        'bytes_to_send': sum((m['filesize'] or 0) for m in plan['add'] + plan['update']),
    }
    if done is not None:
        rep.update(done)
    return rep


def _ensure_dataset(session, engine, rf, ec) -> str:
    from models import RagDataset
    row = session.query(RagDataset).filter_by(engine_id=engine.id, ec_id=ec.id).first()
    if row and rf.dataset_exists(row.dataset_id):
        return row.dataset_id
    name = f'{ec.code} · {ec.name}'[:120]
    try:
        dataset_id = rf.create_dataset(name, f'Documents du cours Moodle {ec.code} (indexés par CEI)')
    except RagflowError:
        # Nom déjà pris (base créée puis perdue côté CEI) : nom unique.
        dataset_id = rf.create_dataset(f'{name[:100]} · {int(time.time())}',
                                       f'Documents du cours Moodle {ec.code} (indexés par CEI)')
    if row:
        row.dataset_id = dataset_id
    else:
        session.add(RagDataset(engine_id=engine.id, ec_id=ec.id, dataset_id=dataset_id))
    session.commit()
    return dataset_id


def sync_ec(session, engine, ec, dry_run: bool = True, max_uploads: int | None = None, found=None,
            retry_failed: bool = True) -> dict:
    """Simulation (dry_run) ou indexation réelle des documents d'un EC.
    Un fichier en erreur n'arrête pas les autres. max_uploads borne le
    nombre d'envois (passage automatique) : le reste attend le suivant."""
    from models import RagDocument
    plan = plan_ec(session, engine, ec, found=found, retry_failed=retry_failed)
    if dry_run or plan.get('skipped'):
        return _report(plan)
    if not _lock(ec.id):
        return {'ec_code': ec.code, 'skipped': 'Indexation déjà en cours pour cet EC'}
    errors, sent, removed, retried = [], 0, 0, 0
    try:
        rf = ragflow_client.client_for(engine)
        client = plan['client']
        work = plan['add'] + plan['update']
        if max_uploads is not None:
            work = work[:max_uploads]
        if work or plan['retry']:
            dataset_id = _ensure_dataset(session, engine, rf, ec)
        for m in work:
            name = _upload_name(m)
            try:
                raw = client._download(m['fileurl'], MAX_FILE_MB * 1024 * 1024)
                doc_id = rf.upload(dataset_id, name, raw)
                rf.parse(dataset_id, [doc_id])
            except (MoodleError, RagflowError) as e:
                errors.append({'file': name, 'error': str(e)[:300]})
                continue
            row = session.query(RagDocument).filter_by(engine_id=engine.id, ec_id=ec.id, fileurl=m['fileurl']).first()
            if row:   # fichier remplacé : l'ancienne version part APRÈS l'arrivée de la nouvelle
                try:
                    rf.delete_documents(row.dataset_id, [row.document_id])
                except RagflowError:
                    pass
            else:
                row = RagDocument(engine_id=engine.id, ec_id=ec.id, fileurl=m['fileurl'])
                session.add(row)
            row.filename, row.title = name, m.get('title')
            row.section, row.module = m.get('section'), m.get('module')
            row.timemodified, row.filesize = m['timemodified'], m['filesize']
            row.dataset_id, row.document_id = dataset_id, doc_id
            row.status, row.layout, row.chunks, row.error = 'indexing', FAST_LAYOUT, 0, None
            session.commit()
            sent += 1
        for row in plan['retry']:
            try:
                rf.parse(row.dataset_id, [row.document_id])
                row.status, row.error = 'indexing', None
                session.commit()
                retried += 1
            except RagflowError as e:
                errors.append({'file': row.filename, 'error': str(e)[:300]})
        for row in plan['remove']:
            try:
                rf.delete_documents(row.dataset_id, [row.document_id])
            except RagflowError:
                pass   # déjà absent de RAGFlow : on retire quand même la trace côté CEI
            session.delete(row)
            session.commit()
            removed += 1
    finally:
        _unlock(ec.id)
    remaining = len(plan['add']) + len(plan['update']) - sent - len(errors)
    return _report(plan, {'sent': sent, 'retried': retried, 'removed': removed,
                          'errors': errors, 'remaining': max(0, remaining)})


def refresh_status(session, engine, ec_id: int | None = None) -> dict:
    """Met à jour les documents en cours d'indexation d'après RAGFlow. Un
    document sans aucun fragment en « Plain Text » (PDF scanné) est relancé
    avec DeepDOC (OCR)."""
    from models import RagDocument
    q = session.query(RagDocument).filter_by(engine_id=engine.id, status='indexing')
    if ec_id:
        q = q.filter_by(ec_id=ec_id)
    pending = q.all()
    if not pending:
        return {'checked': 0}
    rf = ragflow_client.client_for(engine)
    by_dataset = {}
    for row in pending:
        by_dataset.setdefault(row.dataset_id, []).append(row)
    counts = {'checked': len(pending), 'ready': 0, 'failed': 0, 'ocr': 0}
    for dataset_id, rows in by_dataset.items():
        try:
            remote = {d['id']: d for d in rf.documents(dataset_id)}
        except RagflowError:
            continue
        for row in rows:
            d = remote.get(row.document_id)
            if not d:
                # Supprimé dans RAGFlow : la trace part, le prochain passage le renvoie.
                session.delete(row)
                continue
            run, chunks = d.get('run'), d.get('chunk_count') or 0
            if run == 'DONE':
                if chunks:
                    row.status, row.chunks, row.error = 'ready', chunks, None
                    counts['ready'] += 1
                elif row.layout == FAST_LAYOUT:
                    try:
                        rf.set_layout(dataset_id, row.document_id, OCR_LAYOUT)
                        rf.parse(dataset_id, [row.document_id])
                        row.layout = OCR_LAYOUT
                        counts['ocr'] += 1
                    except RagflowError as e:
                        row.status, row.error = 'failed', str(e)[:300]
                else:
                    row.status, row.error = 'failed', 'Aucun texte exploitable, même avec OCR'
                    counts['failed'] += 1
            elif run in ('FAIL', 'CANCEL'):
                row.status = 'failed'
                row.error = (d.get('progress_msg') or 'Échec de l’indexation').strip()[-500:]
                counts['failed'] += 1
    session.commit()
    return counts


# ── Passage automatique (service cei-moodle-sync) ────────────────────────────

def _ec_rotation(session) -> list:
    """EC ayant un cours Moodle (codes égaux), plateformes actives."""
    from models import EC
    codes = {}
    for inst in moodle_sync.active_instances(session):
        try:
            for c in moodle_sync.client_for(inst).list_courses():
                codes.setdefault(c['shortname'], inst.id)
        except MoodleError:
            continue
    return [ec_id for (ec_id, code) in session.query(EC.id, EC.code).order_by(EC.code).all() if code in codes]


def tick(session) -> None:
    """Appelé à chaque passage du service cei-moodle-sync."""
    from models import EC
    engine = ragflow_client.active_engine(session)
    if not engine or not engine.auto_index:
        return
    report = {}
    if cache_set_nx(f'cei:rag:{engine.id}:every:status', STATUS_EVERY - 5):
        try:
            report['status'] = refresh_status(session, engine)
        except RagflowError as e:
            report['status_error'] = str(e)[:200]
    rotation = cache_get(f'cei:rag:{engine.id}:rotation')
    if not rotation:
        rotation = _ec_rotation(session)
        cache_set(f'cei:rag:{engine.id}:rotation', rotation, ttl=3600)
    if not rotation:
        return
    pos = int(cache_get(f'cei:rag:{engine.id}:pos') or 0)
    budget, changes = UPLOADS_PER_TICK, []
    for i in range(min(CHECKS_PER_TICK, len(rotation))):
        ec = session.query(EC).filter_by(id=rotation[(pos + i) % len(rotation)]).first()
        if not ec:
            continue
        try:
            rep = sync_ec(session, engine, ec, dry_run=False, max_uploads=budget, retry_failed=False)
        except (MoodleError, RagflowError) as e:
            session.rollback()
            changes.append({'ec_code': ec.code, 'error': str(e)[:200]})
            continue
        budget -= rep.get('sent', 0)
        if rep.get('sent') or rep.get('removed') or rep.get('retried') or rep.get('errors'):
            changes.append({k: rep[k] for k in ('ec_code', 'sent', 'removed', 'retried', 'errors', 'remaining') if k in rep})
        if rep.get('remaining'):
            break   # même cours repris au passage suivant
    else:
        cache_set(f'cei:rag:{engine.id}:pos', (pos + CHECKS_PER_TICK) % len(rotation), ttl=7 * 86400)
    if changes or report.get('status', {}).get('checked'):
        engine.auto_index_last_at = datetime.now(timezone.utc)
        engine.auto_index_report = json.dumps({'at': engine.auto_index_last_at.isoformat(),
                                               'changes': changes, **report}, ensure_ascii=False)[:20000]
        session.commit()


# ── Génération ancrée ────────────────────────────────────────────────────────

def indexed_documents(session, engine, ec_id: int, fileurls: list | None = None) -> list:
    from models import RagDocument
    q = session.query(RagDocument).filter_by(engine_id=engine.id, ec_id=ec_id, status='ready')
    if fileurls is not None:
        q = q.filter(RagDocument.fileurl.in_(fileurls))
    return q.all()


def passages_for(session, engine, ec_id: int, fileurls: list, focus: str = '', budget_chars: int = 16000) -> list:
    """Passages des documents indexés cochés. Avec un thème (focus) : les
    plus pertinents pour ce thème. Sans thème : répartis régulièrement sur
    l'ensemble des documents, pour couvrir tout le cours au lieu de son
    seul début. Chaque passage garde son document d'origine (source citée)."""
    docs = indexed_documents(session, engine, ec_id, fileurls)
    if not docs:
        return []
    rf = ragflow_client.client_for(engine)
    by_id = {d.document_id: d for d in docs}
    picked = []
    if focus.strip():
        chunks = rf.retrieve(list({d.dataset_id for d in docs}), list(by_id), focus.strip()[:500], top_k=24)
        for c in chunks:
            d = by_id.get(c.get('document_id'))
            if d:
                picked.append((d, c.get('content') or ''))
    else:
        total = sum(d.chunks or 0 for d in docs)
        if not total:
            return []
        avg = 1600   # ~512 tokens
        want = max(1, min(total, budget_chars // avg))
        step = total / want
        targets = sorted({int(i * step) for i in range(want)})
        offset, cache = 0, {}
        for d in docs:
            n = d.chunks or 0
            for t in [t - offset for t in targets if offset <= t < offset + n]:
                page = t // 100 + 1
                if (d.id, page) not in cache:
                    try:
                        cache[(d.id, page)] = rf.chunks(d.dataset_id, d.document_id, page=page, page_size=100)[0]
                    except RagflowError:
                        cache[(d.id, page)] = []
                rows = cache[(d.id, page)]
                if t % 100 < len(rows):
                    picked.append((d, rows[t % 100].get('content') or ''))
            offset += n
    out, used = [], 0
    for d, content in picked:
        content = re.sub(r'\s+', ' ', content).strip()
        if not content:
            continue
        if used + len(content) > budget_chars and out:
            break
        out.append({'id': f'S{len(out) + 1}', 'filename': d.filename, 'module': d.module, 'fileurl': d.fileurl,
                    'content': content})
        used += len(content)
    return out
