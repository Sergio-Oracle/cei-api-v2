"""
Activité « CEI » présente dans tous les cours Moodle (phase 6).

Moodle n'offre aucun webservice pour créer une activité, seulement pour
copier le contenu d'un cours dans un autre (core_course_import_course).
L'admin crée donc UNE fois l'activité CEI (outil externe LTI, lancement
« Fenêtre existante », pas de note) dans un cours modèle, et CEI la copie
dans chaque cours à la synchronisation (manuelle, programmée ou déclenchée
par le webhook).

L'import Moodle copie TOUTES les activités du modèle, et un forum
« Annonces » importé ferait un doublon dans chaque cours (vérifié dans le
code de restauration de Moodle 4.5) : le modèle doit donc contenir
exactement une activité, l'outil externe CEI — sinon rien n'est copié.
"""
from cache import cache_get, cache_set
from services.moodle_sync import MoodleError


def _modules(client, course_id: int) -> list:
    return [m for section in client.call('core_course_get_contents', {'courseid': course_id})
            for m in (section.get('modules') or [])]


def template_for(inst, client) -> dict:
    """{'course': cours modèle, 'module': activité CEI} ou {'problem': raison}."""
    if not inst.lti_template_course:
        return {'problem': "Aucun cours modèle défini (réglage « Cours modèle » de la section LTI)."}
    key = f"cei:moodle:template:{inst.id}:{inst.lti_template_course}"
    cached = cache_get(key)
    if cached:
        return cached
    course = client.find_course_by_code(inst.lti_template_course)
    if not course:
        return {'problem': f"Cours modèle « {inst.lti_template_course} » introuvable dans Moodle."}
    modules = _modules(client, course['id'])
    lti_mods = [m for m in modules if m.get('modname') == 'lti']
    if len(lti_mods) != 1:
        result = {'problem': f"Le cours modèle « {inst.lti_template_course} » doit contenir exactement une activité "
                             f"« Outil externe » (CEI) ; il en contient {len(lti_mods)}."}
    elif len(modules) != 1:
        others = ', '.join(sorted({m.get('name', '?') for m in modules if m.get('modname') != 'lti'}))
        result = {'problem': f"Le cours modèle « {inst.lti_template_course} » contient d'autres activités ({others}) : "
                             "elles seraient copiées dans tous les cours. Supprimez-les (y compris le forum Annonces)."}
    else:
        result = {'course': {'id': course['id'], 'shortname': course['shortname']},
                  'module': {'name': lti_mods[0].get('name')}}
    cache_set(key, result, ttl=300)
    return result


def ensure_activity(inst, client, course: dict, dry_run: bool = True) -> dict:
    """Copie l'activité CEI dans ce cours si elle n'y est pas encore.
    Renvoie {'status': 'present'|'installed'|'to_install'|'skipped'|'error', 'detail': …}."""
    tpl = template_for(inst, client)
    if 'problem' in tpl:
        return {'status': 'skipped', 'detail': tpl['problem']}
    if course['id'] == tpl['course']['id']:
        return {'status': 'present', 'detail': 'cours modèle'}
    try:
        name = tpl['module']['name']
        if any(m.get('modname') == 'lti' and m.get('name') == name for m in _modules(client, course['id'])):
            return {'status': 'present', 'detail': name}
        if dry_run:
            return {'status': 'to_install', 'detail': name}
        client.call('core_course_import_course', {
            'importfrom': tpl['course']['id'], 'importto': course['id'], 'deletecontent': 0,
            'options': [{'name': 'activities', 'value': 1}, {'name': 'blocks', 'value': 0},
                        {'name': 'filters', 'value': 0}],
        })
        return {'status': 'installed', 'detail': name}
    except MoodleError as e:
        return {'status': 'error', 'detail': str(e)}
