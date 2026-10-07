"""
Création automatique de la maquette manquante à partir de Moodle.

Sur les plateformes Moodle de l'UNCHK, les catégories reproduisent la
maquette (vérifié sur 67 catégories le 26/09) :
    Formation (nom complet) > Niveau (Licence 1) > Semestre N > UE > cours = EC
Un cours Moodle sans EC CEI devient donc un EC, avec au besoin son UE, son
semestre et sa formation.

Cours rangés autrement (« hors schéma », règle du document d'intégration,
section 7) — créés aussi, jamais ignorés :
  Formation > Semestre > UE (sans niveau) : niveau déduit du nom de la
      formation (Licence/Master N), sinon « Hors niveau » ; une sous-catégorie
      plus profonde que l'UE est rattachée à cette UE ;
  Formation > catégorie quelconque : semestre « Hors semestre » (numéro 0),
      une UE par sous-catégorie ;
  Catégorie seule (TEST, Cours magistral…) : formation = cette catégorie,
      semestre « Hors semestre », une UE et un EC au nom du cours.
La formation porte dans sa description la référence de sa catégorie Moodle
([moodle:plateforme:catégorie]), pour être retrouvée aux passages suivants. Rien n'est supprimé ni renommé, sauf le nom
provisoire d'une formation créée plus tôt depuis un département
(« XXX — créée depuis Moodle »), remplacé par le vrai nom Moodle.

Moodle ne connaît ni crédits, ni coefficients, ni heures, ni répartition
CC/EX : les UE et EC créés prennent les valeurs par défaut de la maquette et
sont marqués values_confirmed=False, jusqu'à l'import de la maquette
officielle (Excel), qui les complète. Un relevé de notes refuse un EC non
confirmé.
"""
import re
from collections import Counter, defaultdict
from types import SimpleNamespace

from models import EC, UE, Semester, Formation, Niveau

NIVEAU_RE = re.compile(r'^\s*(licence|master)\s*(\d)', re.I)
SEMESTRE_RE = re.compile(r'semestre\s*(\d+)', re.I)
UE_NUMBER_RE = re.compile(r'\bUE\s*(\d+)', re.I)
NUMERIC_CODE_RE = re.compile(r'^([A-Z]+)(\d)(\d)(\d)(\d)$')   # AES1111 : filière, niveau, semestre, UE, rang
PLACEHOLDER_SUFFIX = '— créée depuis Moodle'


def _chain(categories, category_id):
    cat = categories.get(category_id)
    if not cat:
        return []
    return [categories[int(i)] for i in cat['path'].strip('/').split('/') if int(i) in categories]


def build_structure(session, instance, client, dry_run=True):
    """Crée (ou, en simulation, liste) la structure manquante d'une plateforme.
    Renvoie un bilan : formations, semestres, UE, EC créés, formations
    renommées, UE reliées à leur catégorie, cours ignorés."""
    categories = {c['id']: c for c in client.call('core_course_get_categories')}
    courses = client.list_courses()
    ecs_by_code = {e.code: e for e in session.query(EC).all()}
    report = {'formations': [], 'formations_renamed': [], 'semesters': [], 'ues': [], 'ecs': [],
              'ue_links': 0, 'skipped': []}

    # Cours rangés par catégorie d'UE ; les cours hors de la structure
    # Formation > Niveau > Semestre > UE (tests, cours magistral…) sont ignorés.
    by_ue_category = defaultdict(list)
    off_schema = []                   # (cours, chaîne) rangés autrement : règle « hors schéma »
    report['off_schema'] = []
    prefixes = defaultdict(Counter)   # catégorie formation → filières des codes numériques (AES, SPO…)
    for course in courses:
        chain = _chain(categories, course['categoryid'])
        if len(chain) != 4 or not NIVEAU_RE.match(chain[1]['name']) or not SEMESTRE_RE.search(chain[2]['name']):
            if course['shortname'] not in ecs_by_code:
                if chain:
                    off_schema.append((course, chain))
                else:
                    report['skipped'].append({'code': course['shortname'], 'reason': 'hors maquette (sans catégorie)'})
            continue
        by_ue_category[chain[3]['id']].append((course, chain))
        m = NUMERIC_CODE_RE.match(course['shortname'])
        if m:
            prefixes[chain[0]['id']][m.group(1)] += 1

    # Pôle des formations créées : celui de la plateforme, sinon celui déjà
    # utilisé par les formations des cours de cette plateforme reliés à CEI.
    pole_id = instance.pole_id
    if not pole_id:
        poles = Counter()
        for code in {c['shortname'] for c in courses} & set(ecs_by_code):
            ue = ecs_by_code[code].ue
            formation = ue.semester.formation if ue and ue.semester else None
            if formation and formation.pole_id:
                poles[formation.pole_id] += 1
        pole_id = poles.most_common(1)[0][0] if poles else None

    planned = {}   # en simulation : objets « à créer », pour ne pas les compter deux fois

    def niveau_for(chain):
        m = NIVEAU_RE.match(chain[1]['name'])
        code = f"{'L' if m.group(1).lower() == 'licence' else 'M'}{m.group(2)}"
        niveau = session.query(Niveau).filter_by(code=code, pole_id=pole_id).first()
        if niveau:
            return niveau
        key = ('niveau', code, pole_id)
        if key in planned:
            return planned[key]
        if dry_run:
            niveau = SimpleNamespace(id=None, code=code, name=chain[1]['name'])
        else:
            niveau = Niveau(code=code, name=chain[1]['name'].strip(), pole_id=pole_id, is_active=True,
                            description='Créé automatiquement depuis Moodle.')
            session.add(niveau)
            session.flush()
        planned[key] = niveau
        return niveau

    def formation_for(chain):
        filiere = prefixes[chain[0]['id']].most_common(1)[0][0] if prefixes[chain[0]['id']] else None
        if not filiere:
            return None
        niveau = niveau_for(chain)
        code = f'{niveau.code}-{filiere}'
        formation = session.query(Formation).filter_by(code=code).first()
        moodle_name = chain[0]['name'].strip()
        if formation:
            if formation.name.endswith(PLACEHOLDER_SUFFIX) and ('rename', code) not in planned:
                planned[('rename', code)] = True
                report['formations_renamed'].append(f'{code} → {moodle_name}')
                if not dry_run:
                    formation.name = moodle_name
                    formation.description = (f'Créée depuis Moodle ; nom repris de la catégorie « {moodle_name} ». '
                                             'À compléter dans la maquette : crédits, coefficients, CC/EX.')
            return formation
        key = ('formation', code)
        if key in planned:
            return planned[key]
        report['formations'].append(f'{code} ({moodle_name})')
        if dry_run:
            formation = SimpleNamespace(id=None, code=code)
        else:
            formation = Formation(code=code, name=moodle_name, level=chain[1]['name'].strip(),
                                  niveau_id=niveau.id, pole_id=pole_id, department=filiere, is_active=True,
                                  description=f'Créée automatiquement depuis la catégorie Moodle « {moodle_name} ».')
            session.add(formation)
            session.flush()
        planned[key] = formation
        return formation

    def semester_for(formation, chain):
        number = int(SEMESTRE_RE.search(chain[2]['name']).group(1))
        if formation.id:
            semester = session.query(Semester).filter_by(formation_id=formation.id, number=number).first()
            if semester:
                return semester, number
        key = ('semester', formation.code, number)
        if key in planned:
            return planned[key], number
        report['semesters'].append(f'{formation.code} S{number}')
        if dry_run:
            semester = SimpleNamespace(id=None)
        else:
            semester = Semester(formation_id=formation.id, number=number, name=f'Semestre {number}',
                                total_credits=30, is_active=True)
            session.add(semester)
            session.flush()
        planned[key] = semester
        return semester, number

    def ue_code_for(courses_in_category, formation, number, category, filiere):
        codes = [c['shortname'] for c, _ in courses_in_category]
        numeric = {code[:-1] for code in codes if NUMERIC_CODE_RE.match(code)}
        candidates = []
        if len(numeric) == 1:
            candidates.append(numeric.pop())                          # AES1111 → AES111 (convention existante)
        if codes and all(code.upper().startswith('UN') for code in codes):
            candidates.append(f'UN/{filiere}')                         # transversales, comme UN/AES existant
        m = UE_NUMBER_RE.search(category['name'])
        if m:
            candidates.append(f"{filiere}{formation.code.split('-')[0][-1]}{number}{m.group(1)}")   # UE 5 → SPO115
        candidates.append(f'{formation.code}-S{number}-C{category["id"]}')
        for code in candidates:
            if not session.query(UE).filter_by(code=code).first() and ('ue', code) not in planned:
                return code
        return f'{formation.code}-S{number}-C{category["id"]}'

    for category_id, items in by_ue_category.items():
        chain = items[0][1]
        missing = [c for c, _ in items if c['shortname'] not in ecs_by_code]
        existing = [ecs_by_code[c['shortname']] for c, _ in items if c['shortname'] in ecs_by_code]

        # 1. UE déjà reliée à cette catégorie ; 2. UE des EC déjà présents dans la catégorie.
        ue = session.query(UE).filter_by(moodle_instance_id=instance.id, moodle_category_id=category_id).first()
        if not ue and existing:
            ue = session.get(UE, Counter(e.ue_id for e in existing).most_common(1)[0][0])
        if ue and ue.moodle_category_id is None:
            report['ue_links'] += 1
            if not dry_run:
                ue.moodle_instance_id, ue.moodle_category_id = instance.id, category_id
        if not missing:
            continue

        if not ue:
            formation = formation_for(chain)
            if formation is None:
                for c in missing:
                    report['skipped'].append({'code': c['shortname'], 'reason': 'filière indéterminable (aucun code de cours du type AES1111)'})
                continue
            semester, number = semester_for(formation, chain)
            filiere = formation.code.split('-', 1)[-1]
            code = ue_code_for(items, formation, number, chain[3], filiere)
            report['ues'].append(f'{code} ({formation.code} S{number} — {chain[3]["name"].strip()})')
            if dry_run:
                ue = SimpleNamespace(id=None, code=code)
            else:
                ue = UE(semester_id=semester.id, code=code, name=chain[3]['name'].strip()[:200], credits=6,
                        ue_type='obligatoire', is_active=True, values_confirmed=False,
                        moodle_instance_id=instance.id, moodle_category_id=category_id)
                session.add(ue)
                session.flush()
            planned[('ue', code)] = ue

        for course in missing:
            report['ecs'].append(course['shortname'])
            if not dry_run:
                session.add(EC(ue_id=ue.id, code=course['shortname'], name=(course['fullname'] or course['shortname']).strip()[:200],
                               coefficient=1, cc_percentage=40, ex_percentage=60, is_active=True, values_confirmed=False))

    _build_off_schema(session, instance, off_schema, pole_id, planned, report, dry_run)

    if not dry_run:
        session.commit()
    return report


# ── Cours hors schéma ────────────────────────────────────────────────────────

_SMALL_WORDS = {'de', 'des', 'du', 'la', 'le', 'les', 'et', 'en', 'au', 'aux', 'a', 'à', 'd', 'l', 'pour', 'sur'}


def _acronym(name: str) -> str:
    """« Formation continue RTN » → FCRTN ; « TEST » → TEST ; « Cours magistral » → CM."""
    words = [w for w in re.split(r"[^\w]+", name) if w and w.lower() not in _SMALL_WORDS]
    out = ''.join(w if (w.isupper() and len(w) > 1) else w[0] for w in words).upper()
    out = re.sub(r'[^A-Z0-9]', '', out)
    return (out or 'MOODLE')[:12]


def _build_off_schema(session, instance, items, pole_id, planned, report, dry_run):
    if not items:
        return

    def marker(cat):
        return f'[moodle:{instance.id}:{cat["id"]}]'

    def niveau_for(name):
        m = NIVEAU_RE.search(name)
        code, label = (f"{'L' if m.group(1).lower() == 'licence' else 'M'}{m.group(2)}",
                       f"{m.group(1).capitalize()} {m.group(2)}") if m else ('HN', 'Hors niveau')
        niveau = session.query(Niveau).filter_by(code=code, pole_id=pole_id).first()
        key = ('niveau', code, pole_id)
        if niveau or key in planned:
            return niveau or planned[key]
        if dry_run:
            niveau = SimpleNamespace(id=None, code=code, name=label)
        else:
            niveau = Niveau(code=code, name=label, pole_id=pole_id, is_active=True,
                            description='Créé automatiquement depuis Moodle (cours rangés hors du schéma habituel).')
            session.add(niveau)
            session.flush()
        planned[key] = niveau
        return niveau

    def formation_for(root):
        key = ('hs-formation', root['id'])
        if key in planned:
            return planned[key]
        mk = marker(root)
        formation = session.query(Formation).filter(Formation.description.contains(mk)).first()
        if not formation:
            niveau = niveau_for(root['name'])
            base = _acronym(root['name']) if niveau.code == 'HN' else f'{niveau.code}-{_acronym(root["name"])}'
            code = base
            if session.query(Formation).filter_by(code=code).first():
                code = f'{base}-{root["id"]}'          # code déjà pris par une autre formation
            name = root['name'].strip()[:200]
            report['formations'].append(f'{code} ({name}) — hors schéma')
            if dry_run:
                formation = SimpleNamespace(id=None, code=code)
            else:
                formation = Formation(code=code, name=name, level=niveau.name, niveau_id=niveau.id, pole_id=pole_id,
                                      department=_acronym(root['name']), is_active=True,
                                      description=(f'Créée automatiquement depuis la catégorie Moodle « {name} », '
                                                   f'rangée hors du schéma Formation > Niveau > Semestre > UE. {mk}'))
                session.add(formation)
                session.flush()
        planned[key] = formation
        return formation

    def semester_for(formation, number):
        if formation.id:
            sem = session.query(Semester).filter_by(formation_id=formation.id, number=number).first()
            if sem:
                return sem
        key = ('semester', formation.code, number)
        if key in planned:
            return planned[key]
        label = f'Semestre {number}' if number else 'Hors semestre'
        report['semesters'].append(f'{formation.code} {"S" + str(number) if number else "hors semestre"}')
        if dry_run:
            sem = SimpleNamespace(id=None)
        else:
            sem = Semester(formation_id=formation.id, number=number, name=label, total_credits=30, is_active=True)
            session.add(sem)
            session.flush()
        planned[key] = sem
        return sem

    def ue_for(formation, semester, number, category=None, course=None):
        """UE d'une sous-catégorie (category) ou propre à un cours (course)."""
        if category is not None:
            ue = session.query(UE).filter_by(moodle_instance_id=instance.id, moodle_category_id=category['id']).first()
            if ue:
                return ue
            code, name = f'{formation.code}-C{category["id"]}', category['name'].strip()
        else:
            slug = re.sub(r'[^A-Za-z0-9_-]+', '-', course['shortname']).strip('-') or str(course['id'])
            code, name = f'{formation.code}-{slug}'[:50], (course['fullname'] or course['shortname']).strip()
            ue = session.query(UE).filter_by(code=code).first()
            if ue:
                return ue
        key = ('ue', code)
        if key in planned:
            return planned[key]
        where = f'S{number}' if number else 'hors semestre'
        report['ues'].append(f'{code} ({formation.code} {where} — {name[:80]}) — hors schéma')
        if dry_run:
            ue = SimpleNamespace(id=None, code=code)
        else:
            ue = UE(semester_id=semester.id, code=code, name=name[:200], credits=6, ue_type='obligatoire',
                    is_active=True, values_confirmed=False, moodle_instance_id=instance.id,
                    moodle_category_id=category['id'] if category is not None else None)
            session.add(ue)
            session.flush()
        planned[key] = ue
        return ue

    for course, chain in items:
        root = chain[0]
        formation = formation_for(root)
        sem_idx = next((i for i, c in enumerate(chain[1:], 1) if SEMESTRE_RE.search(c['name'])), None)
        if sem_idx is not None:
            number = int(SEMESTRE_RE.search(chain[sem_idx]['name']).group(1))
            category = chain[sem_idx + 1] if len(chain) > sem_idx + 1 else None
            rule = 'semestre sans niveau' if not NIVEAU_RE.match(chain[1]['name']) else 'sous-catégorie sous l’UE'
        elif len(chain) >= 2:
            number = 0
            category = chain[1] if not NIVEAU_RE.match(chain[1]['name']) else (chain[2] if len(chain) > 2 else None)
            rule = 'sans semestre'
        else:
            number, category, rule = 0, None, 'catégorie seule'
        semester = semester_for(formation, number)
        ue = ue_for(formation, semester, number, category=category, course=None if category is not None else course)
        report['ecs'].append(course['shortname'])
        report['off_schema'].append({'code': course['shortname'], 'rule': rule,
                                     'where': ' > '.join(c['name'] for c in chain),
                                     'ue': getattr(ue, 'code', None), 'formation': formation.code})
        if not dry_run:
            session.add(EC(ue_id=ue.id, code=course['shortname'][:50],
                           name=(course['fullname'] or course['shortname']).strip()[:200],
                           coefficient=1, cc_percentage=40, ex_percentage=60, is_active=True, values_confirmed=False))
            session.flush()
