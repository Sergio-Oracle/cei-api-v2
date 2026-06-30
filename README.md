# CEI API v2 — Backend REST Flask + PASETO v4

**Centre d'Examen Intelligent** — Backend indépendant conçu pour séparer la couche API de la couche frontend.  
Port public : `http://62.171.190.6:8100` · Swagger UI : `http://62.171.190.6:8100/api/docs`

## Dépôts du projet

| Partie | Dépôt | Port |
|--------|-------|------|
| Backend API (ce dépôt) | [Sergio-Oracle/cei-api-v2](https://github.com/Sergio-Oracle/cei-api-v2) | 8100 |
| Frontend Next.js | [Sergio-Oracle/cei-next](https://github.com/Sergio-Oracle/cei-next) | 5173 |

---

## Mises à jour récentes

### 30/06/2026 — Corrections critiques

| Endpoint | Problème | Correction |
|---|---|---|
| `POST /api/exam_attempts/<id>/unban` | `ExamActivityLog` créé avec `details=` et `risk_score=` — champs inexistants → `TypeError` 500 | Remplacé par `event_data=json.dumps({...})` |
| `GET /api/exam_attempts/<id>/review` | `attempt.corrector` accédé après `session.close()` → `DetachedInstanceError` 500 | Tous les champs extraits dans `result` dict avant `session.close()` |
| `GET /api/exam_attempts/<id>/review` | `corrector_name` absent de la réponse JSON | Ajouté : `corrector_name: attempt.corrector.full_name if attempt.corrector else None` |
| `GET /api/exam_attempts/<id>/integrity-report` | PDF : "Tab switches" en anglais ; types d'événements en anglais dans la chronologie | Traduits en français : "Changements d'onglet", "Tentative de copie", etc. |
| `GET /api/exam_attempts/<id>/integrity-report` | Nom du fichier téléchargé = `rapport_integrite_<id>.pdf` | Nom inclut le prénom/nom de l'étudiant : `rapport_integrite_<nom>_<id>.pdf` |

---

## Présentation

Ce backend est la **version 2** de la plateforme CEI. Il reprend l'intégralité des fonctionnalités de la plateforme existante en séparant clairement :

- **Backend** (ce dépôt) : API REST Flask, pur JSON, aucun template HTML servi
- **Frontend** (à venir) : React + Vite, consommateur de cette API

### Différences majeures par rapport à la v1

| Aspect | v1 (existante) | v2 (ce dépôt) |
|--------|---------------|---------------|
| Auth | JWT (flask-jwt-extended) | **PASETO v4.public Ed25519** |
| Architecture | Monolithique Flask (API + templates) | API REST pure (JSON uniquement) |
| Refresh token | Absent | Cookie httpOnly 7 jours + rotation |
| Token révocation | Absent | Table `token_blocklist` en base |
| Documentation | Swagger partiel | **Swagger 100% couvert — 147 endpoints** |
| Scalabilité | 1 worker | **Gunicorn 9 workers gthread (1000+ users)** |
| Déploiement | Port 7000 | **Port 8100** |

---

## Acteurs et rôles

| Rôle | Description |
|------|-------------|
| `admin` | Gestion complète : utilisateurs, maquette pédagogique, examens, relevés |
| `professor` | Création sujets, correction copies, gestion examens en ligne, analytics |
| `surveillant` | Monitoring en direct, avertissements, bannissements, messages étudiants |
| `student` | Passage examens, soumission, consultation résultats et relevés |
| `PUBLIC` | Routes sans auth : login, register, reset password, statut agent |

---

## Authentification PASETO v4

### Flux complet

```
POST /api/auth/login
  → access_token (v4.public, 15 min, stocker en mémoire JS)
  → cookie httpOnly cei_refresh (7 jours, path=/api/auth)

GET /api/* avec Header: Authorization: Bearer <access_token>

POST /api/auth/refresh   (cookie envoyé automatiquement)
  → nouvel access_token + rotation du refresh token

POST /api/auth/logout
  → révocation refresh token (token_blocklist) + suppression cookie
```

### Sécurité

- **Algorithme fixe** Ed25519 — résistant aux attaques algorithm confusion (impossible avec JWT)
- **Clé publique exposée** sur `GET /api/auth/public-key` — le frontend peut vérifier localement
- **Rotation** du refresh token à chaque usage — un token volé ne peut être utilisé qu'une fois
- **Révocation en base** via table `token_blocklist` (hash SHA-256)
- **Access token en mémoire** — non exposé au localStorage (XSS-safe)
- **Refresh token httpOnly** — non accessible en JavaScript (XSS-safe)

---

## Endpoints — Vue d'ensemble (147 routes)

### Authentification (10 routes)

| Méthode | Route | Rôle | Description |
|---------|-------|------|-------------|
| `POST` | `/api/auth/login` | PUBLIC | Connexion → token PASETO + cookie refresh |
| `POST` | `/api/auth/register` | PUBLIC | Inscription étudiant |
| `POST` | `/api/auth/refresh` | PUBLIC (cookie) | Renouveler l'access token |
| `POST` | `/api/auth/logout` | Authentifié | Révoquer le refresh token |
| `GET` | `/api/auth/public-key` | PUBLIC | Clé publique Ed25519 du serveur |
| `GET` | `/api/auth/me` | Tous | Profil de l'utilisateur connecté |
| `PUT` | `/api/profile` | Tous | Modifier son profil |
| `PUT` | `/api/profile/password` | Tous | Changer son mot de passe |
| `POST` | `/api/auth/forgot-password` | PUBLIC | Demander réinitialisation |
| `POST` | `/api/auth/reset-password` | PUBLIC | Valider token + nouveau mot de passe |

### Administration (11 routes)

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/admin/dashboard` | Stats globales (users, examens, copies) |
| `GET/POST` | `/api/admin/users` | Lister / créer utilisateurs |
| `PUT/DELETE` | `/api/admin/users/<id>` | Modifier / supprimer utilisateur |
| `POST` | `/api/admin/users/student-no-email` | Créer étudiant sans email |
| `GET` | `/api/admin/corrected_papers` | 50 dernières copies corrigées |
| `GET` | `/api/admin/exams_history` | Historique tous les examens |
| `GET` | `/api/admin/security_report` | Rapport de sécurité global |
| `GET` | `/api/admin/online_exams/<id>` | Détail examen (admin) |
| `PUT` | `/api/admin/online_exams/<id>` | Modifier examen (admin) |

### Maquette pédagogique — Académique (28 routes)

Gestion complète de la structure : **Formation → Semestre → UE → EC → Affectation professeur → Inscription étudiant**

| Ressource | Routes disponibles |
|-----------|-------------------|
| Formations | GET liste, POST créer, PUT modifier, DELETE supprimer |
| Semestres | GET par formation, POST, PUT, DELETE |
| UE (Unités d'Enseignement) | GET par semestre, GET toutes, POST, PUT, DELETE |
| EC (Éléments Constitutifs) | GET par UE, GET tous, POST, PUT, DELETE |
| Affectations EC | POST assigner prof, DELETE retirer, POST alt |
| Inscriptions UE | POST inscrire étudiant, DELETE retirer, GET enrollments |
| Formation complète | POST inscrire étudiant à toute une formation |

### Sujets et Copies (13 routes)

| Méthode | Route | Rôle | Description |
|---------|-------|------|-------------|
| `GET` | `/api/subjects` | Tous | Lister sujets (filtrés par rôle) |
| `GET` | `/api/subjects/<id>` | Tous | Détail sujet + barème |
| `POST` | `/api/subjects/upload` | Prof/Admin | Créer sujet + génération barème IA |
| `DELETE` | `/api/subjects/<id>` | Prof/Admin | Supprimer sujet |
| `POST` | `/api/subjects/<id>/upload_image` | Prof/Admin | Ajouter image au sujet |
| `POST` | `/api/papers/upload` | Prof/Admin | Correction IA d'une copie |
| `POST` | `/api/papers/correct` | Prof/Admin | Alias upload |
| `POST` | `/api/papers/upload-batch` | Prof/Admin | Batch ZIP (plusieurs copies) |
| `GET` | `/api/papers/subject/<id>` | Tous | Copies d'un sujet |
| `GET` | `/api/papers/detail/<id>` | Tous | Détail copie corrigée |
| `GET` | `/api/papers/<id>/export` | Prof/Admin/Étudiant | Export PDF copie |
| `GET` | `/api/statistics/<subject_id>` | Prof/Admin | Stats d'un sujet |
| `GET` | `/api/student/papers` | Étudiant | Mes copies papier |

### Examens en ligne (29 routes)

Gestion du cycle de vie complet : **Création → Activation → Passage → Correction → Clôture**

| Phase | Routes |
|-------|--------|
| Gestion | Créer, modifier, supprimer, activer, prolonger, clore |
| Étudiant | Démarrer tentative, sauvegarder réponses, soumettre, voir résultats |
| Professeur | Corriger automatiquement, noter manuellement, accorder temps supplémentaire |
| Export | CSV résultats, PDF bilan, ZIP corrections, QR code |
| Analyse | Stats examen, bilan détaillé, détection plagiat, rapport intégrité |
| Banque de questions | Lister, ajouter, supprimer, assembler examen |

### Surveillant (15 routes dédiées)

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/surveillant/exams` | Examens assignés au surveillant connecté |
| `GET` | `/api/online_exams/<id>/active_proctoring` | Vue temps réel des étudiants actifs |
| `GET` | `/api/exam_attempts/<id>/risk_status` | Score de risque + statut bannissement |
| `POST` | `/api/exam_attempts/<id>/send_warning` | Envoyer avertissement à un étudiant |
| `POST` | `/api/exam_attempts/<id>/proctor_ban` | Exclure définitivement un étudiant |
| `GET` | `/api/online_exams/<id>/student_messages` | Messages des étudiants (vue surveillant) |
| `POST` | `/api/exam_attempts/<id>/student_message` | Envoyer message à un étudiant |
| `GET` | `/api/exam_attempts/<id>/pending_messages` | Messages en attente (polling étudiant) |
| `GET` | `/api/online_exams/<id>/proctor_token` | Token LiveKit surveillant (tous les flux) |
| `GET` | `/api/exam_attempts/<id>/private_token` | Token appel privé surveillant ↔ étudiant |
| `POST` | `/api/exam_attempts/<id>/proctor-note` | Ajouter note de surveillance |
| `GET` | `/api/exam_attempts/<id>/proctor-notes` | Lire notes de surveillance |
| `GET/POST` | `/api/online_exams/<id>/proctors` | Gérer les surveillants d'un examen |
| `DELETE` | `/api/online_exams/<id>/proctors/<proctor_id>` | Retirer un surveillant |
| `POST` | `/api/online_exams/<id>/distribute_proctors` | Distribuer étudiants entre surveillants |

### Proctoring — Infrastructure LiveKit (14 routes)

Surveillance vidéo WebRTC : tokens LiveKit, snapshots caméra, événements de fraude, signatures, enregistrements vidéo.

### Intelligence Artificielle (7 routes)

| Méthode | Route | Description |
|---------|-------|-------------|
| `POST` | `/api/ai/generate-exam-suggestions` | Générer suggestions de sujets (Claude/Gemini) |
| `POST` | `/api/subjects/generate-full-exam` | Générer un examen complet par IA |
| `POST` | `/api/subjects/create-from-suggestion` | Créer sujet depuis suggestion IA |

### Réclamations (6 routes)

Dépôt → Traitement IA → Proposition → Accepter/Rejeter

### Relevés de notes (7 routes)

Génération PDF individuel/groupé, publication, suppression.

### Agent autonome (6 routes)

API consommée par `agent_proctor/monitor.py` — heartbeat, alertes fraude, liste examens actifs.

---

## Architecture technique

```
/root/cei-api-v2/
├── app.py                    # Application Flask principale (130 routes)
├── auth_paseto.py            # PASETO v4.public Ed25519 — tokens, décorateurs
├── models.py                 # SQLAlchemy — User, Subject, OnlineExam, TokenBlocklist...
├── proctoring_routes.py      # Blueprint proctoring (31 routes LiveKit)
├── csv_import_routes.py      # Blueprint import CSV (4 routes)
├── export_route.py           # Blueprint export PDF (1 route)
├── swagger_docs.py           # Swagger UI + spec OpenAPI 3.0 (147 endpoints)
├── utils.py                  # Email, PDF, extraction texte, hashing
├── gunicorn.conf.py          # Config Gunicorn — 9 workers gthread
├── scripts/
│   └── generate_paseto_keys.py
├── agent_proctor/
│   └── monitor.py            # Agent IA de surveillance autonome
└── static/
    ├── uploads/              # Copies et sujets uploadés
    └── models/               # Modèles face-api.js
```

### Stack technique

| Composant | Technologie |
|-----------|-------------|
| Framework | Flask 3.x |
| Auth | PASETO v4.public (pyseto 1.9.3) + Ed25519 |
| ORM | SQLAlchemy + PostgreSQL |
| Serveur WSGI | Gunicorn (gthread, 9 workers) |
| IA correction | Anthropic Claude Sonnet + Google Gemini |
| Vidéo surveillance | LiveKit WebRTC |
| Export PDF | WeasyPrint / ReportLab |
| Documentation | Swagger UI + OpenAPI 3.0 |
| Process manager | Systemd (`cei-api-v2.service`) |

---

## Installation et démarrage

### Prérequis

- Python 3.10+
- PostgreSQL (base `exam_grader_db`)
- LiveKit Server (pour le proctoring vidéo)

### Variables d'environnement (`.env`)

```env
# Base de données
DATABASE_URL=postgresql://user:pass@localhost:5432/exam_grader_db

# PASETO v4 Ed25519 (générer avec scripts/generate_paseto_keys.py)
PASETO_PRIVATE_KEY=<PEM encodé en base64>
PASETO_PUBLIC_KEY=<PEM encodé en base64>
PASETO_ACCESS_TTL_MIN=15
PASETO_REFRESH_TTL_DAYS=7

# IA
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...

# LiveKit
LIVEKIT_URL=wss://...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...

# Email
SMTP_HOST=smtp.gmail.com
SMTP_USERNAME=...
SMTP_PASSWORD=...

# Swagger docs
DOCS_USER=admin@cei.sn
DOCS_PASS=motdepasse

# CORS (frontend)
ALLOWED_ORIGINS=http://localhost:5173,https://votre-domaine.com
APP_URL=http://62.171.190.6:8100
```

### Génération des clés PASETO

```bash
python scripts/generate_paseto_keys.py
# Copier PASETO_PRIVATE_KEY et PASETO_PUBLIC_KEY dans .env
# Ne jamais commiter la clé privée dans git
```

### Démarrage

```bash
# Développement
python app.py

# Production (Gunicorn)
gunicorn --config gunicorn.conf.py app:app

# Systemd
systemctl start cei-api-v2
systemctl status cei-api-v2
```

---

## Accès Swagger UI

```
URL    : http://62.171.190.6:8100/api/docs
Login  : DOCS_USER / DOCS_PASS (définis dans .env)
Format : OpenAPI 3.0 — spec brute sur /api/docs/openapi.json
```

Le Swagger UI permet de tester toutes les routes directement depuis le navigateur.  
Cliquer sur **Authorize** → saisir le Bearer token obtenu via `POST /api/auth/login`.

---

## Exemple d'utilisation

### 1. Login

```bash
curl -s -c cookies.txt -X POST http://62.171.190.6:8100/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@cei.sn","password":"motdepasse"}'
```

```json
{
  "success": true,
  "access_token": "v4.public.eyJzdWIiOiIxIiwicm9sZS...",
  "user": {"id": 1, "role": "admin", "email": "admin@cei.sn"}
}
```

### 2. Requête authentifiée

```bash
TOKEN="v4.public.eyJzdWIiOiIx..."
curl -H "Authorization: Bearer $TOKEN" http://62.171.190.6:8100/api/admin/dashboard
```

### 3. Rafraîchir le token

```bash
curl -s -b cookies.txt -c cookies.txt -X POST http://62.171.190.6:8100/api/auth/refresh
```

### 4. Déconnexion

```bash
curl -b cookies.txt -H "Authorization: Bearer $TOKEN" \
  -X POST http://62.171.190.6:8100/api/auth/logout
```

---

## Prochaines étapes

- [ ] Déploiement du frontend React (Vite + React Router + Zustand)
- [ ] Configuration HTTPS (Let's Encrypt) avec nom de domaine
- [ ] Redis pour le rate limiting et le cache tokens
- [ ] Tests automatisés (pytest + httpx)
- [ ] CI/CD GitHub Actions

---

## Relation avec la plateforme existante

Ce backend est une **image indépendante** de la plateforme `cei-unchk.sn` (port 7000).  
Les deux coexistent sur le même serveur sans interférence.  
La migration vers cette v2 se fera progressivement une fois le frontend React validé.

---

*Développé pour l'UNCHK — Université Numérique Cheikh Hamidou Kane*
