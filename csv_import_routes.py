"""Routes d'import CSV - Utilisateurs et Maquette Pédagogique - VERSION CORRIGÉE avec département"""
from flask import jsonify, request, send_file
from auth_paseto import paseto_required, get_current_user_id
from flask_bcrypt import Bcrypt
from threading import Thread
import pandas as pd
import io
import re
import chardet
from datetime import datetime
from models import (
    User, UserRole, Formation, Semester, UE, EC,
    get_session
)
from utils import send_account_created_email

bcrypt = Bcrypt()

# ============================================================================
# FONCTION UTILITAIRE - CORRECTION ENCODAGE WINDOWS-1252
# ============================================================================

def fix_windows_1252_chars(text):
    """Corriger les caractères Windows-1252 mal encodés"""
    if not isinstance(text, str):
        return text

    conversion_table = {
        '\x82': 'é',  '\x85': 'à',  '\x88': 'ê',  '\x8a': 'è',
        '\x8e': 'é',  '\x93': 'ô',  '\x97': 'ù',  '\x87': 'ç',
        '\x81': 'ü',  '\x83': 'â',  '\x84': 'ä',  '\x89': 'ë',
    }

    for old_char, new_char in conversion_table.items():
        text = text.replace(old_char, new_char)

    return text

# ============================================================================
# FONCTION UTILITAIRE - DÉTECTION ENCODAGE
# ============================================================================

def detect_encoding(file):
    """Détecter automatiquement l'encodage d'un fichier"""
    file.seek(0)
    raw_data = file.read()
    file.seek(0)

    result = chardet.detect(raw_data)
    encoding = result['encoding']
    confidence = result['confidence']

    print(f"📊 Encodage détecté: {encoding} (confiance: {confidence:.2%})")

    # Fallback si confiance faible
    if confidence < 0.7:
        print("⚠️ Confiance faible, tentative avec UTF-8")
        return 'utf-8'

    # Mapping des encodages courants
    encoding_map = {
        'ISO-8859-1': 'latin1',
        'ISO-8859-2': 'latin2',
        'Windows-1252': 'cp1252',
        'ascii': 'utf-8'
    }

    return encoding_map.get(encoding, encoding)

# ============================================================================
# TEMPLATES CSV - GÉNÉRATION
# ============================================================================

def generate_users_csv_template():
    """Générer template CSV pour import utilisateurs"""
    template_data = {
        'full_name': ['Jean Dupont', 'Marie Martin'],
        'email': ['jean.dupont@exemple.com', 'marie.martin@exemple.com'],
        'password': ['MotDePasse123', 'MotDePasse456'],
        'role': ['student', 'professor']
    }

    df = pd.DataFrame(template_data)
    output = io.BytesIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    return output

def generate_maquette_csv_template():
    """Générer template CSV pour import maquette - ✅ AVEC DÉPARTEMENT"""
    template_data = {
        'type': ['formation', 'semester', 'ue', 'ec'],
        'formation_code': ['MASTER_TR', 'MASTER_TR', 'MASTER_TR', 'MASTER_TR'],
        'formation_name': ['Master Telecoms', '', '', ''],
        'formation_level': ['Master 1', '', '', ''],
        'formation_department': ['Génie Électrique', '', '', ''],  # ✅ NOUVEAU
        'semester_number': ['', '1', '1', '1'],
        'semester_name': ['', 'Semestre 1', '', ''],
        'semester_credits': ['', '30', '', ''],
        'ue_code': ['', '', 'UEM111', 'UEM111'],
        'ue_name': ['', '', 'Informatique generale', ''],
        'ue_credits': ['', '', '6', ''],
        'ec_code': ['', '', '', 'M1111'],
        'ec_name': ['', '', '', 'SDN_NFV'],
        'ec_cm': ['', '', '', '20'],
        'ec_td': ['', '', '', '20'],
        'ec_tp': ['', '', '', '10'],
        'ec_tpe': ['', '', '', '10'],
        'ec_vht': ['', '', '', '60'],
        'ec_coefficient': ['', '', '', '2']
    }

    df = pd.DataFrame(template_data)
    output = io.BytesIO()
    df.to_csv(output, index=False, encoding='utf-8-sig')
    output.seek(0)
    return output

# ============================================================================
# ROUTES TÉLÉCHARGEMENT TEMPLATES ET IMPORTS
# ============================================================================

def register_csv_routes(app):
    """Enregistrer toutes les routes CSV"""

    @app.route('/api/admin/users/csv-template', methods=['GET'])
    @paseto_required
    def download_users_csv_template():
        """Télécharger template CSV utilisateurs"""
        try:
            user_id = get_current_user_id()
            session = get_session()

            user = session.query(User).filter_by(id=user_id).first()
            if user.role != UserRole.ADMIN:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            session.close()

            csv_file = generate_users_csv_template()

            return send_file(
                csv_file,
                mimetype='text/csv',
                as_attachment=True,
                download_name=f'template_utilisateurs_{datetime.now().strftime("%Y%m%d")}.csv'
            )
        except Exception as e:
            print(f"❌ Erreur download_users_csv_template: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/admin/maquette/csv-template', methods=['GET'])
    @paseto_required
    def download_maquette_csv_template():
        """Télécharger template CSV maquette"""
        try:
            user_id = get_current_user_id()
            session = get_session()

            user = session.query(User).filter_by(id=user_id).first()
            if user.role != UserRole.ADMIN:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            session.close()

            csv_file = generate_maquette_csv_template()

            return send_file(
                csv_file,
                mimetype='text/csv',
                as_attachment=True,
                download_name=f'template_maquette_{datetime.now().strftime("%Y%m%d")}.csv'
            )
        except Exception as e:
            print(f"❌ Erreur download_maquette_csv_template: {e}")
            return jsonify({'error': str(e)}), 500

    # ========================================================================
    # IMPORT CSV UTILISATEURS - VERSION CORRIGÉE
    # ========================================================================

    @app.route('/api/admin/users/import-csv', methods=['POST'])
    @paseto_required
    def import_users_csv():
        """Importer utilisateurs depuis CSV - VERSION ROBUSTE"""
        try:
            user_id = get_current_user_id()
            session = get_session()

            user = session.query(User).filter_by(id=user_id).first()
            if user.role != UserRole.ADMIN:
                session.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            if 'file' not in request.files:
                session.close()
                return jsonify({'error': 'Aucun fichier fourni'}), 400

            file = request.files['file']

            if file.filename == '':
                session.close()
                return jsonify({'error': 'Aucun fichier sélectionné'}), 400

            if not file.filename.endswith('.csv'):
                session.close()
                return jsonify({'error': 'Format invalide. Utilisez CSV'}), 400

            # Détecter l'encodage automatiquement
            encoding = detect_encoding(file)

            # Lire le CSV avec l'encodage détecté
            try:
                df = pd.read_csv(file, encoding=encoding)
            except Exception as e:
                print(f"⚠️ Échec avec {encoding}, tentative UTF-8")
                file.seek(0)
                df = pd.read_csv(file, encoding='utf-8', errors='ignore')

            # Valider colonnes
            required_cols = ['full_name', 'email', 'password', 'role']
            if not all(col in df.columns for col in required_cols):
                session.close()
                return jsonify({'error': f'Colonnes requises: {", ".join(required_cols)}'}), 400

            created_users = []
            errors = []
            email_queued_count = 0

            for idx, row in df.iterrows():
                try:
                    # Valider rôle
                    role_str = str(row['role']).strip().upper()
                    if role_str not in ['STUDENT', 'PROFESSOR', 'ADMIN']:
                        errors.append(f"Ligne {idx+2}: Rôle invalide '{row['role']}'")
                        continue

                    # Vérifier email existant
                    existing = session.query(User).filter_by(email=row['email']).first()
                    if existing:
                        errors.append(f"Ligne {idx+2}: Email '{row['email']}' déjà utilisé")
                        continue

                    # Créer utilisateur
                    hashed_password = bcrypt.generate_password_hash(str(row['password'])).decode('utf-8')

                    new_user = User(
                        email=str(row['email']).strip(),
                        password_hash=hashed_password,
                        full_name=str(row['full_name']).strip(),
                        role=UserRole[role_str]
                    )

                    session.add(new_user)
                    session.flush()

                    # Envoyer email en tâche de fond — un import de N lignes ne doit
                    # pas attendre N × jusqu'à 30s de SMTP avant de répondre.
                    try:
                        Thread(target=send_account_created_email, kwargs=dict(
                            user_email=row['email'],
                            user_name=row['full_name'],
                            role=row['role'].lower(),
                            temp_password=row['password']
                        ), daemon=True).start()
                        email_queued_count += 1
                    except Exception as email_error:
                        print(f"⚠️ Erreur mise en file email à {row['email']}: {email_error}")
                        # Ne pas bloquer l'import si la mise en file échoue

                    created_users.append({
                        'full_name': str(row['full_name']).strip(),
                        'email': str(row['email']).strip(),
                        'role': row['role'].lower()
                    })

                except Exception as e:
                    errors.append(f"Ligne {idx+2}: {str(e)}")

            session.commit()
            session.close()

            return jsonify({
                'success': True,
                'created': len(created_users),
                'errors': len(errors),
                'emails_queued': email_queued_count,
                'users': created_users,
                'error_details': errors
            })

        except Exception as e:
            print(f"❌ Erreur import_users_csv: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500

    # ========================================================================
    # IMPORT CSV MAQUETTE - ✅ AVEC DÉPARTEMENT
    # ========================================================================

    @app.route('/api/admin/maquette/import-csv', methods=['POST'])
    @paseto_required
    def import_maquette_csv():
        """Import CSV de la maquette pédagogique - ✅ AVEC DÉPARTEMENT"""
        try:
            current_user_id = get_current_user_id()
            session_db = get_session()

            # Vérifier admin
            user = session_db.query(User).filter_by(id=current_user_id).first()
            if not user or user.role != UserRole.ADMIN:
                session_db.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            # Récupérer le fichier
            if 'file' not in request.files:
                session_db.close()
                return jsonify({'error': 'Aucun fichier fourni'}), 400

            file = request.files['file']
            if file.filename == '':
                session_db.close()
                return jsonify({'error': 'Nom de fichier vide'}), 400

            # Détecter l'encodage
            encoding = detect_encoding(file)

            # ⭐ LECTURE ROBUSTE AVEC CORRECTION AUTOMATIQUE
            try:
                df = pd.read_csv(file, encoding=encoding)
                print(f"✅ Lecture CSV réussie avec {encoding}")
            except UnicodeDecodeError as e:
                print(f"⚠️ Erreur avec {encoding}: {e}")
                file.seek(0)
                try:
                    df = pd.read_csv(file, encoding='cp1252')
                    print(f"✅ Lecture CSV réussie avec cp1252")
                except:
                    print(f"⚠️ Erreur avec cp1252, tentative UTF-8...")
                    file.seek(0)
                    df = pd.read_csv(file, encoding='utf-8', errors='ignore')
                    print(f"⚠️ Lecture avec UTF-8 errors='ignore'")

            # ⭐ CORRECTION DES CARACTÈRES MAL ENCODÉS
            print("🔧 Correction des caractères mal encodés...")
            for col in df.columns:
                if df[col].dtype == 'object':
                    df[col] = df[col].apply(lambda x: fix_windows_1252_chars(x) if isinstance(x, str) else x)

            # Nettoyer les espaces blancs (map remplace applymap qui est déprécié)
            df = df.map(lambda x: x.strip() if isinstance(x, str) else x)

            # ⭐ LOG DE VÉRIFICATION
            print("\n📊 Aperçu des données après correction:")
            print(f"   Total lignes: {len(df)}")

            for row_type in ['formation', 'semester', 'ue', 'ec']:
                count = len(df[df['type'] == row_type])
                print(f"   - {row_type}: {count} ligne(s)")

            # Afficher les formations
            formations_df = df[df['type'] == 'formation']
            for idx, row in formations_df.iterrows():
                print(f"   Formation: {row['formation_name']} (code: {row['formation_code']})")

            # Compteurs
            created_formations = []
            created_semesters = []
            created_ues = []
            created_ecs = []
            errors = []

            # ⭐ MAPS POUR RÉFÉRENCER LES IDs
            formation_map = {}  # {code: formation_id}
            semester_map = {}   # {(formation_id, numero): semester_id}
            ue_map = {}         # {code: ue_id}

            print("\n🔄 Début de l'import hiérarchique...\n")

            # ====================================================================
            # TRAITER CHAQUE LIGNE
            # ====================================================================

            for index, row in df.iterrows():
                try:
                    row_type = str(row['type']).strip().lower()
                    print(f"📍 Ligne {index+2}: type={row_type}")

                    # === FORMATION - ✅ AVEC DÉPARTEMENT ===
                    if row_type == 'formation':
                        formation_code = str(row['formation_code']).strip()
                        formation_name = str(row['formation_name']).strip()
                        formation_level = str(row['formation_level']).strip() if pd.notna(row['formation_level']) else ''
                        # ✅ NOUVEAU: Lire le département
                        formation_department = str(row['formation_department']).strip() if pd.notna(row.get('formation_department')) else ''

                        print(f"   🎓 Formation: {formation_name} ({formation_code})")
                        print(f"      → Niveau: {formation_level}, Département: {formation_department}")

                        # Vérifier doublon
                        existing = session_db.query(Formation).filter_by(code=formation_code).first()
                        if existing:
                            formation_map[formation_code] = existing.id
                            print(f"   ℹ️  Formation existe déjà (ID: {existing.id})")
                            continue

                        formation = Formation(
                            code=formation_code,
                            name=formation_name,
                            level=formation_level,
                            department=formation_department  # ✅ AJOUTÉ
                        )
                        session_db.add(formation)
                        session_db.flush()

                        formation_map[formation_code] = formation.id
                        created_formations.append(f"{formation_name} ({formation_code})")
                        print(f"   ✅ Formation créée (ID: {formation.id})")

                    # === SEMESTRE ===
                    elif row_type == 'semester':
                        formation_code = str(row['formation_code']).strip()
                        semester_number = int(row['semester_number'])
                        semester_name = str(row['semester_name']).strip()
                        semester_credits = int(row['semester_credits']) if pd.notna(row['semester_credits']) else 30

                        print(f"   📅 Semestre: {semester_name} (formation: {formation_code}, numéro: {semester_number})")

                        if formation_code not in formation_map:
                            error_msg = f"Formation {formation_code} non trouvée"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            print(f"   🗺️  formation_map actuel: {formation_map}")
                            continue

                        formation_id = formation_map[formation_code]

                        # Vérifier doublon
                        existing = session_db.query(Semester).filter_by(
                            formation_id=formation_id,
                            number=semester_number
                        ).first()

                        if existing:
                            semester_map[(formation_id, semester_number)] = existing.id
                            print(f"   ℹ️  Semestre existe déjà (ID: {existing.id})")
                            continue

                        semester = Semester(
                            formation_id=formation_id,
                            number=semester_number,
                            name=semester_name,
                            total_credits=semester_credits
                        )
                        session_db.add(semester)
                        session_db.flush()

                        semester_map[(formation_id, semester_number)] = semester.id
                        created_semesters.append(f"{semester_name}")
                        print(f"   ✅ Semestre créé (ID: {semester.id})")

                    # === UE ===
                    elif row_type == 'ue':
                        formation_code = str(row['formation_code']).strip() if pd.notna(row['formation_code']) else None
                        semester_number = int(row['semester_number']) if pd.notna(row['semester_number']) else None
                        ue_code = str(row['ue_code']).strip()
                        ue_name = str(row['ue_name']).strip()
                        ue_credits = int(row['ue_credits']) if pd.notna(row['ue_credits']) else 6

                        print(f"   📚 UE: {ue_name} ({ue_code})")
                        print(f"      → formation_code: {formation_code}, semester_number: {semester_number}")

                        # Validation
                        if not formation_code or not semester_number:
                            error_msg = "Formation code ou semester number manquant"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            continue

                        if formation_code not in formation_map:
                            error_msg = f"Formation {formation_code} non trouvée dans formation_map"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            print(f"   🗺️  formation_map actuel: {formation_map}")
                            continue

                        formation_id = formation_map[formation_code]
                        semester_key = (formation_id, semester_number)

                        if semester_key not in semester_map:
                            error_msg = f"Semestre {semester_number} non trouvé pour formation {formation_code}"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            print(f"   🗺️  semester_map actuel: {semester_map}")
                            continue

                        semester_id = semester_map[semester_key]
                        print(f"      → Semestre trouvé (ID: {semester_id})")

                        # Vérifier doublon
                        existing = session_db.query(UE).filter_by(code=ue_code).first()
                        if existing:
                            ue_map[ue_code] = existing.id
                            print(f"   ℹ️  UE existe déjà (ID: {existing.id})")
                            continue

                        ue = UE(
                            semester_id=semester_id,
                            code=ue_code,
                            name=ue_name,
                            credits=ue_credits
                        )
                        session_db.add(ue)
                        session_db.flush()

                        ue_map[ue_code] = ue.id
                        created_ues.append(f"{ue_name} ({ue_code})")
                        print(f"   ✅ UE créée (ID: {ue.id})")

                    # === EC ===
                    elif row_type == 'ec':
                        formation_code = str(row['formation_code']).strip() if pd.notna(row['formation_code']) else None
                        semester_number = int(row['semester_number']) if pd.notna(row['semester_number']) else None
                        ue_code = str(row['ue_code']).strip() if pd.notna(row['ue_code']) else None
                        ec_code = str(row['ec_code']).strip()
                        ec_name = str(row['ec_name']).strip()

                        cm_hours = int(row['ec_cm']) if pd.notna(row['ec_cm']) else 0
                        td_hours = int(row['ec_td']) if pd.notna(row['ec_td']) else 0
                        tp_hours = int(row['ec_tp']) if pd.notna(row['ec_tp']) else 0
                        tpe_hours = int(row['ec_tpe']) if pd.notna(row['ec_tpe']) else 0
                        total_hours = int(row['ec_vht']) if pd.notna(row['ec_vht']) else (cm_hours + td_hours + tp_hours + tpe_hours)
                        coefficient = int(row['ec_coefficient']) if pd.notna(row['ec_coefficient']) else 1

                        print(f"   📖 EC: {ec_name} ({ec_code})")
                        print(f"      → ue_code: {ue_code}")

                        # Validation
                        if not ue_code:
                            error_msg = "UE code manquant"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            continue

                        if ue_code not in ue_map:
                            error_msg = f"UE {ue_code} non trouvée dans ue_map"
                            errors.append(f"Ligne {index+2}: {error_msg}")
                            print(f"   ❌ {error_msg}")
                            print(f"   🗺️  ue_map actuel: {ue_map}")
                            continue

                        ue_id = ue_map[ue_code]
                        print(f"      → UE trouvée (ID: {ue_id})")

                        # Vérifier doublon
                        existing = session_db.query(EC).filter_by(code=ec_code).first()
                        if existing:
                            print(f"   ℹ️  EC existe déjà (ID: {existing.id})")
                            continue

                        ec = EC(
                            ue_id=ue_id,
                            code=ec_code,
                            name=ec_name,
                            cm=cm_hours,
                            td=td_hours,
                            tp=tp_hours,
                            tpe=tpe_hours,
                            vht=total_hours,
                            coefficient=coefficient
                        )
                        session_db.add(ec)
                        session_db.flush()

                        created_ecs.append(f"{ec_name} ({ec_code})")
                        print(f"   ✅ EC créé (ID: {ec.id})")

                    else:
                        print(f"   ⚠️ Type inconnu: {row_type}")

                except Exception as e:
                    error_msg = f"Erreur: {str(e)}"
                    errors.append(f"Ligne {index+2}: {error_msg}")
                    print(f"   ❌ {error_msg}")
                    import traceback
                    traceback.print_exc()

            # Commit final
            session_db.commit()

            print("\n" + "="*70)
            print("✅ IMPORT TERMINÉ")
            print("="*70)
            print(f"📊 Résultats:")
            print(f"   - Formations: {len(created_formations)}")
            print(f"   - Semestres: {len(created_semesters)}")
            print(f"   - UEs: {len(created_ues)}")
            print(f"   - ECs: {len(created_ecs)}")
            print(f"   - Erreurs: {len(errors)}")
            print("="*70)

            return jsonify({
                'success': True,
                'message': 'Import terminé',
                'created': {
                    'formations': len(created_formations),
                    'semesters': len(created_semesters),
                    'ues': len(created_ues),
                    'ecs': len(created_ecs)
                },
                'details': {
                    'formations': created_formations,
                    'semesters': created_semesters,
                    'ues': created_ues,
                    'ecs': created_ecs
                },
                'errors': errors
            })

        except Exception as e:
            session_db.rollback()
            print(f"\n❌ ERREUR GLOBALE: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500
        finally:
            session_db.close()

    # ========================================================================
    # IMPORT EXCEL MAQUETTE — format réel des tableaux UE/EC de l'établissement
    # (Pôle/Formation déjà créés via l'UI ; ce fichier ajoute UE+EC à UN semestre)
    # ========================================================================

    def _parse_maquette_excel(file_storage):
        """Parse un fichier Excel de maquette au format réel fourni par l'école :
        tableau à 7 colonnes (Code/Nom/Crédit/Type UE, puis Code/Nom/Coef EC),
        cellules UE fusionnées (répétées seulement à la 1re ligne de chaque
        groupe d'EC), pourcentages CC/EX imbriqués dans le nom de l'EC
        (ex: "Fondamentaux de la Communication [CC:40%, EX:60%]")."""
        df = pd.read_excel(file_storage, header=None, dtype=str)

        # Localise la ligne d'en-têtes (contient "Code" au moins 2 fois) —
        # tolère un nombre variable de lignes de titre au-dessus
        header_row = None
        for i in range(min(8, len(df))):
            vals = [str(v).strip() for v in df.iloc[i].tolist() if pd.notna(v)]
            if vals.count('Code') >= 2:
                header_row = i
                break
        if header_row is None:
            raise ValueError("Impossible de localiser la ligne d'en-têtes (attendu : 'Code' répété pour UE et EC)")

        data = df.iloc[header_row + 1:, :7].copy()
        data.columns = ['ue_code', 'ue_name', 'ue_credit', 'ue_type', 'ec_code', 'ec_name', 'ec_coef']
        # Cellules UE fusionnées dans Excel → vides sur les lignes EC suivantes
        data[['ue_code', 'ue_name', 'ue_credit', 'ue_type']] = data[['ue_code', 'ue_name', 'ue_credit', 'ue_type']].ffill()
        data = data.dropna(subset=['ec_code', 'ec_name'], how='all')

        cc_ex_re = re.compile(r'\[\s*CC\s*:\s*(\d+)\s*%.*?EX\s*:\s*(\d+)\s*%\s*\]', re.I | re.S)

        ues = {}
        for _, row in data.iterrows():
            ue_code = str(row['ue_code']).strip() if pd.notna(row['ue_code']) else None
            ec_code = str(row['ec_code']).strip() if pd.notna(row['ec_code']) else None
            if not ue_code or not ec_code or ue_code == 'nan' or ec_code == 'nan':
                continue
            if ue_code not in ues:
                try:
                    credits = int(float(row['ue_credit'])) if pd.notna(row['ue_credit']) else 6
                except ValueError:
                    credits = 6
                ues[ue_code] = {
                    'code': ue_code,
                    'name': str(row['ue_name']).strip() if pd.notna(row['ue_name']) else ue_code,
                    'credits': credits,
                    'ue_type': (str(row['ue_type']).strip().lower() if pd.notna(row['ue_type']) else 'obligatoire'),
                    'ecs': [],
                }
            ec_name_raw = (str(row['ec_name']).strip() if pd.notna(row['ec_name']) else ec_code).replace('\n', ' ')
            m = cc_ex_re.search(ec_name_raw)
            cc, ex = (int(m.group(1)), int(m.group(2))) if m else (40, 60)
            ec_name_clean = cc_ex_re.sub('', ec_name_raw).strip()
            try:
                coef = int(float(row['ec_coef'])) if pd.notna(row['ec_coef']) else 1
            except ValueError:
                coef = 1
            ues[ue_code]['ecs'].append({
                'code': ec_code, 'name': ec_name_clean, 'coefficient': coef,
                'cc_percentage': cc, 'ex_percentage': ex,
            })
        return list(ues.values())

    @app.route('/api/admin/maquette/import-excel-preview', methods=['POST'])
    @paseto_required
    def preview_maquette_excel():
        """Analyse un fichier Excel de maquette (format réel école) SANS écrire
        en base — retourne l'aperçu UE/EC détecté pour validation avant import."""
        try:
            current_user_id = get_current_user_id()
            session_db = get_session()
            user = session_db.query(User).filter_by(id=current_user_id).first()
            if not user or user.role != UserRole.ADMIN:
                session_db.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            semester_id = request.form.get('semester_id')
            semester = session_db.query(Semester).filter_by(id=semester_id).first() if semester_id else None
            if not semester:
                session_db.close()
                return jsonify({'error': 'Semestre cible invalide — sélectionnez le semestre où importer'}), 400

            if 'file' not in request.files:
                session_db.close()
                return jsonify({'error': 'Aucun fichier fourni'}), 400
            file = request.files['file']
            if not file.filename.lower().endswith(('.xlsx', '.xls')):
                session_db.close()
                return jsonify({'error': 'Format invalide. Utilisez un fichier Excel (.xlsx)'}), 400

            ues = _parse_maquette_excel(file)
            if not ues:
                session_db.close()
                return jsonify({'error': "Aucune UE/EC détectée dans le fichier — vérifiez qu'il respecte le format attendu"}), 400

            existing_ue_codes = {u.code for u in session_db.query(UE.code).all()}
            existing_ec_codes = {e.code for e in session_db.query(EC.code).all()}
            for u in ues:
                u['already_exists'] = u['code'] in existing_ue_codes
                for e in u['ecs']:
                    e['already_exists'] = e['code'] in existing_ec_codes

            session_db.close()
            return jsonify({
                'success': True,
                'semester_id': semester.id,
                'semester_name': semester.name,
                'ues': ues,
                'ue_count': len(ues),
                'ec_count': sum(len(u['ecs']) for u in ues),
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            try: session_db.close()
            except: pass
            return jsonify({'error': f"Échec de l'analyse du fichier : {e}"}), 500

    @app.route('/api/admin/maquette/import-excel-confirm', methods=['POST'])
    @paseto_required
    def confirm_maquette_excel():
        """Écrit en base les UE/EC précédemment prévisualisés (aucun re-parsing
        du fichier — reçoit directement la structure validée par l'admin)."""
        try:
            current_user_id = get_current_user_id()
            session_db = get_session()
            user = session_db.query(User).filter_by(id=current_user_id).first()
            if not user or user.role != UserRole.ADMIN:
                session_db.close()
                return jsonify({'error': 'Accès non autorisé'}), 403

            data = request.json or {}
            semester_id = data.get('semester_id')
            ues = data.get('ues') or []
            semester = session_db.query(Semester).filter_by(id=semester_id).first() if semester_id else None
            if not semester:
                session_db.close()
                return jsonify({'error': 'Semestre cible invalide'}), 400

            created_ues, created_ecs, skipped = [], [], []
            for u in ues:
                ue_code = (u.get('code') or '').strip()
                if not ue_code:
                    continue
                ue = session_db.query(UE).filter_by(code=ue_code).first()
                if not ue:
                    ue = UE(semester_id=semester.id, code=ue_code, name=u.get('name') or ue_code,
                            credits=int(u.get('credits') or 6), ue_type=u.get('ue_type') or 'obligatoire')
                    session_db.add(ue)
                    session_db.flush()
                    created_ues.append(ue_code)
                for e in (u.get('ecs') or []):
                    ec_code = (e.get('code') or '').strip()
                    if not ec_code:
                        continue
                    if session_db.query(EC).filter_by(code=ec_code).first():
                        skipped.append(ec_code)
                        continue
                    # Attention : utiliser "or" ici tronquerait un pourcentage
                    # CC/EX légitimement à 0 (ex: "Legal Tech [CC:0%, EX:100%]"
                    # dans les maquettes réelles) — il faut un test explicite sur None.
                    cc_pct = e.get('cc_percentage')
                    ex_pct = e.get('ex_percentage')
                    session_db.add(EC(
                        ue_id=ue.id, code=ec_code, name=e.get('name') or ec_code,
                        coefficient=int(e.get('coefficient') or 1),
                        cc_percentage=int(cc_pct) if cc_pct is not None else 40,
                        ex_percentage=int(ex_pct) if ex_pct is not None else 60,
                    ))
                    created_ecs.append(ec_code)
            session_db.commit()
            session_db.close()
            return jsonify({
                'success': True,
                'created_ues': len(created_ues),
                'created_ecs': len(created_ecs),
                'skipped_existing': len(skipped),
            })
        except Exception as e:
            session_db.rollback()
            import traceback
            traceback.print_exc()
            try: session_db.close()
            except: pass
            return jsonify({'error': str(e)}), 500
