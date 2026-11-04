#!/usr/bin/env python
"""Enregistrement vidéo des étudiants (bouton REC) — logique des trois routes, serveur LiveKit SIMULÉ.
Incident du 03/11/2026 (examen 149) : « service d'enregistrement momentanément indisponible » ; en cherchant
la cause on a aussi trouvé qu'un étudiant sans micro actif n'était pas enregistré par room_recording.
    cd cei-api-v2 && .venv-base/bin/python scripts/test_enregistrement.py   (crée puis supprime ses propres données)"""
import sys, uuid, json, socket, urllib.error
sys.path.insert(0, '.')
from unittest import mock
from app import app
from models import *
from routes.exams import get_session
from auth_paseto import create_access_token
from datetime import datetime, timedelta

s = get_session(); sfx = uuid.uuid4().hex[:6]; now = datetime.utcnow(); c = app.test_client()
ids = {'users': [], 'exams': []}
RES = []
def chk(n, ok, d=''):
    RES.append(bool(ok)); print(('  ✔ ' if ok else '  ✘ ') + n + (f'  — {d}' if d and not ok else ''))
def H(u): return {'Authorization': 'Bearer ' + create_access_token(u.id, u.role.value, u.email)}

class Resp:
    def __init__(self, body): self._b = json.dumps(body).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False

class LK:
    """Faux serveur LiveKit : programmable par scénario, mémorise les corps de requête."""
    def __init__(self): self.reset()
    def reset(self, participants=None, list_error=None, start_error=None):
        self.participants = participants or []; self.list_error = list_error; self.start_error = start_error; self.calls = []; self.n = 0; self.list_grants = []; self.active = {}; self.stopped = []
    def __call__(self, req, timeout=None, **kw):
        url = req.full_url; body = json.loads(req.data or b'{}')
        self.calls.append((url.rsplit('/', 1)[-1], body))
        if url.endswith('ListParticipants'):   # LiveKit refuse (401) de lister sans roomAdmin : le faux serveur fait pareil
            import jwt as _jwt
            grant = _jwt.decode(req.get_header('Authorization').split(' ', 1)[1], options={'verify_signature': False}).get('video', {})
            self.list_grants.append(grant)
            if not grant.get('roomAdmin'): raise urllib.error.HTTPError(url, 401, 'permissions denied', {}, None)
        if url.endswith('ListParticipants'):
            if self.list_error: raise self.list_error
            return Resp({'participants': self.participants})
        if url.endswith('StartTrackCompositeEgress'):
            if self.start_error: raise self.start_error
            self.n += 1; eid = f'EG_test{self.n}'
            self.active[eid] = {'egress_id': eid, 'status': 'EGRESS_ACTIVE', 'track_composite': {'file_outputs': body.get('file_outputs', [])}}
            return Resp({'egress_id': eid})
        if url.endswith('StopEgress'):
            self.stopped.append(body.get('egress_id')); self.active.pop(body.get('egress_id'), None); return Resp({})
        if url.endswith('ListEgress'): return Resp({'items': list(self.active.values())})
        raise AssertionError('appel inattendu ' + url)
    def starts(self): return [b for n, b in self.calls if n == 'StartTrackCompositeEgress']

lk = LK()
def part(sid, video=True, audio=False, muted_cam=False, screen=False):
    t = []
    if screen: t.append({'sid': 'TR_S', 'type': 'VIDEO', 'source': 'SCREEN_SHARE', 'muted': False})
    if video: t.append({'sid': 'TR_V', 'type': 'VIDEO', 'source': 'CAMERA', 'muted': muted_cam})
    if audio: t.append({'sid': 'TR_A', 'type': 'AUDIO', 'source': 'MICROPHONE', 'muted': False})
    return {'identity': f'student-{sid}', 'tracks': t}

try:
    adm = s.query(User).filter(User.role == UserRole.ADMIN).first()
    stu = User(email=f'zz_rec_{sfx}@test.local', full_name='ZZ rec', role=UserRole.STUDENT, is_active=True, password_hash='x')
    s.add(stu); s.commit(); ids['users'].append(stu.id)
    subj = s.query(Subject).first()
    e = OnlineExam(subject_id=subj.id, title='TEST enregistrement', duration_minutes=60, start_time=now - timedelta(minutes=5),
                   end_time=now + timedelta(minutes=55), status=ExamStatus.ACTIVE, created_by_id=adm.id)
    s.add(e); s.commit(); ids['exams'].append(e.id); eid = e.id
    a = ExamAttempt(exam_id=eid, student_id=stu.id, status=AttemptStatus.IN_PROGRESS, started_at=now); s.add(a); s.commit(); aid = a.id
    sid = stu.id
    G = f'/api/online_exams/{eid}/group_recording'; R = f'/api/online_exams/{eid}/room_recording'; I = f'/api/exam_attempts/{aid}/recording'
    with mock.patch('urllib.request.urlopen', lk):
        print('Enregistrement du groupe (group_recording)')
        lk.reset([part(sid)]); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('étudiant SANS micro : enregistré', r.status_code == 200 and j.get('started') == 1, (r.status_code, j))
        chk('  demande sans piste audio', lk.starts() and 'audio_track_id' not in lk.starts()[0])
        lk.reset([part(sid, audio=True)]); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('étudiant avec micro : enregistré, piste audio jointe', r.status_code == 200 and lk.starts()[0].get('audio_track_id') == 'TR_A', (r.status_code, lk.starts()))
        eids = j.get('all_egress_ids', [])
        r = c.post(G, headers=H(adm), json={'action': 'stop', 'egress_ids': eids}); chk('arrêt : accepté', r.status_code == 200 and (r.get_json() or {}).get('stopped') == len(eids) >= 1, (r.status_code, r.get_json()))
        lk.reset([part(sid)], start_error=urllib.error.URLError('timed out')); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk("service d'enregistrement en panne : 503 + message exact (et non « vérifiez les caméras »)", r.status_code == 503 and 'momentanément indisponible' in j.get('error', '') and 'caméra' not in j.get('error', ''), (r.status_code, j))
        lk.reset(list_error=urllib.error.URLError('refused')); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('LiveKit injoignable : 503 + message exact', r.status_code == 503 and 'momentanément indisponible' in j.get('error', ''), (r.status_code, j))
        lk.reset([part(sid, muted_cam=True)]); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('caméra réellement coupée : 400 + message sur les caméras', r.status_code == 400 and 'caméra' in j.get('error', ''), (r.status_code, j))

        chk('groupe : la liste des participants est demandée avec roomAdmin (sinon LiveKit répond 401)', lk.list_grants and all(g.get('roomAdmin') for g in lk.list_grants), lk.list_grants)

        print("Arrêt de l'enregistrement du groupe sans liste d'identifiants (ce que fait l'interface)")
        lk.reset([part(sid, audio=True, screen=True)]); r = c.post(G, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('démarrage : caméra + écran enregistrés (2 enregistrements)', r.status_code == 200 and j.get('screens_recorded') == 1 and len(lk.active) == 2, (r.status_code, j, list(lk.active)))
        lk.active['EG_autre'] = {'egress_id': 'EG_autre', 'status': 'EGRESS_ACTIVE', 'track_composite': {'file_outputs': [{'filepath': f'recordings/exam-{eid}/groupe-proctor-987654/groupe-cam-99999-20261104.mp4'}]}}   # groupe d'un AUTRE surveillant
        lk.active['EG_salle'] = {'egress_id': 'EG_salle', 'status': 'EGRESS_ACTIVE', 'track_composite': {'file_outputs': [{'filepath': f'recordings/exam-{eid}/salle-cam-{sid}-20261104.mp4'}]}}      # REC Salle de l'enseignant
        r = c.post(G, headers=H(adm), json={'action': 'stop', 'egress_id': None}); j = r.get_json() or {}
        chk('arrêt sans liste : la caméra ET l\'écran du groupe sont arrêtés', r.status_code == 200 and j.get('stopped') == 2, (r.status_code, j, lk.stopped))
        chk("  l'enregistrement d'un autre groupe et celui de la salle ne sont PAS touchés", set(lk.active) == {'EG_autre', 'EG_salle'}, list(lk.active))

        print('Enregistrement de la salle (room_recording)')
        lk.reset([part(sid)]); r = c.post(R, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('étudiant SANS micro : enregistré (défaut corrigé)', r.status_code == 200 and j.get('started') == 1 and len(lk.starts()) == 1, (r.status_code, j, lk.starts()))
        lk.reset([part(sid, audio=True)]); r = c.post(R, headers=H(adm), json={'action': 'start'}); chk('étudiant avec micro : enregistré, audio joint', r.status_code == 200 and lk.starts()[0].get('audio_track_id') == 'TR_A', (r.status_code, lk.starts()))
        lk.reset([part(sid)], start_error=socket.timeout('timed out')); r = c.post(R, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk("service en panne : 503 + message exact", r.status_code == 503 and 'momentanément indisponible' in j.get('error', ''), (r.status_code, j))
        lk.reset([part(sid, muted_cam=True)]); r = c.post(R, headers=H(adm), json={'action': 'start'}); chk('caméra coupée : 400', r.status_code == 400, (r.status_code, r.get_json()))

        print('Enregistrement individuel (bouton REC d\'une tuile)')
        lk.reset([part(sid, audio=True)]); r = c.post(I, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('démarrage accepté, identifiant mémorisé', r.status_code == 200 and j.get('egress_id') == 'EG_test1', (r.status_code, j))
        s.expire_all(); chk('  current_egress_id enregistré sur la copie', s.get(ExamAttempt, aid).current_egress_id == 'EG_test1')
        r = c.post(I, headers=H(adm), json={'action': 'stop'}); chk("arrêt sans fournir l'identifiant (lu en base) : accepté", r.status_code == 200, (r.status_code, r.get_json()))
        s.expire_all(); chk('  current_egress_id effacé', s.get(ExamAttempt, aid).current_egress_id is None)
        lk.reset([part(sid)], start_error=socket.timeout('timed out')); r = c.post(I, headers=H(adm), json={'action': 'start'}); j = r.get_json() or {}
        chk('délai dépassé (cas observé à 10 h 41) : 503 + message exact', r.status_code == 503 and 'momentanément indisponible' in j.get('error', ''), (r.status_code, j))
        lk.reset([]); r = c.post(I, headers=H(adm), json={'action': 'start'}); chk("étudiant absent de la salle : 400", r.status_code == 400, (r.status_code, r.get_json()))
finally:
    s.close(); s2 = get_session()
    for i in ids['exams']:
        for x in s2.query(ExamAttempt).filter_by(exam_id=i).all(): s2.delete(x)
        s2.flush(); x = s2.get(OnlineExam, i)
        if x: s2.delete(x)
    s2.flush()
    for uid in ids['users']:
        u = s2.get(User, uid)
        if u: s2.delete(u)
    s2.commit(); print('données de test supprimées')
ok = sum(RES); print(f'\n{ok}/{len(RES)} vérifications réussies'); sys.exit(0 if ok == len(RES) else 1)
