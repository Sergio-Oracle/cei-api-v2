# CEI API v2 — Backend REST Flask + PASETO v4

**Centre d'Examen Intelligent** — UNCHK VisioPLUS  
Production : `https://dev-cei.ddns.net` · Swagger UI : `https://dev-cei.ddns.net/api/docs`

## Dépôts du projet

| Partie | Dépôt | Description |
|--------|-------|-------------|
| Backend API (ce dépôt) | [Sergio-Oracle/cei-api-v2](https://github.com/Sergio-Oracle/cei-api-v2) | Flask + Gunicorn + PostgreSQL + Redis |
| Frontend Next.js | [Sergio-Oracle/cei-next](https://github.com/Sergio-Oracle/cei-next) | Next.js 16 + TypeScript + PWA |

---

## Mises à jour — 04/07/2026 (session 2 — scalabilité H-scale + notifications)

### Système de notifications temps réel

| Fichier | Rôle |
|---------|------|
| `ntfy_client.py` *(nouveau)* | Thread daemon non-bloquant — pousse vers ntfy (push mobile / hors navigateur) |
| `notif_bus.py` *(nouveau)* | Bus combiné : Redis Pub/Sub + ntfy en 2 threads parallèles |
| `routes/notifications.py` | Nouveau endpoint `GET /api/notifications/poll` — long-polling Redis 25 s |
| `proctoring_routes.py` | `notify_exam()` après ban étudiant + après risk_score ≥ 75 |
| `routes/exams.py` | `notify_user()` après correction IA terminée |

**Architecture** :
```
Événement CEI → notif_bus.py
  ├── Redis Pub/Sub → /api/notifications/poll → badge Header navigateur (< 1 s)
  └── ntfy server  → useNtfy (SSE) → dashboard surveillant / app mobile
```

### Scalabilité horizontale — alertes agent

| Fichier | Migration |
|---------|-----------|
| `proctoring_routes.py` | `agent_alerts.json` + `fcntl.flock()` → **Redis List** `cei:agent:alerts` (LPUSH/LTRIM) + **Redis Set** `cei:agent:alerts:read` |

Les alertes de l'agent de proctoring autonome sont désormais stockées dans Redis partagé entre tous les workers Gunicorn. Plus de fichier JSON local — le système est prêt pour la scalabilité multi-serveur.

### Scalabilité horizontale — snapshots caméra

| Fichier | Migration |
|---------|-----------|
| `s3_client.py` *(nouveau)* | Client boto3 singleton — upload MinIO, URLs pré-signées 1 h |
| `proctoring_routes.py` | `CameraLog.image_data` (TEXT base64 PostgreSQL) → **MinIO** bucket `cei-snapshots` |
| `models.py` | `CameraLog.to_dict()` retourne `image_url` (URL pré-signée) au lieu de `image_data` |

Format clé S3 : `snapshots/{exam_id}/{attempt_id}/{timestamp}.jpg`
Rétrocompat : anciennes entrées avec `image_data` base64 restent accessibles.

### Variables d'environnement ajoutées

```env
S3_SNAPSHOTS_BUCKET=cei-snapshots   # bucket MinIO dédié aux snapshots caméra
NTFY_URL=                           # URL ntfy (vide = désactivé)
NTFY_TOKEN=                         # Token admin ntfy
```

---

## Mises à jour — 04/07/2026 (session 1 — audit sécurité + correctifs)

### Correctifs sécurité critiques

| Fichier | Problème | Correction |
|---------|----------|------------|
| `utils.py` | **RCE** — injection de code Python via domaine email dans f-string subprocess | Validation regex stricte + appel direct `dns.resolver` sans subprocess |
| `auth_paseto.py` | TTL par défaut access token = 480 min (8h) au lieu de 15 min | Valeur par défaut corrigée à 15 min |
| `routes/auth.py` | Mot de passe brut envoyé en clair dans email de bienvenue | Email de bienvenue sans mot de passe |
| `routes/auth.py` | Longueur minimum mot de passe incohérente (6 vs 8 selon la route) | Harmonisé à 8 caractères partout |
| `app.py` | Absence de FAIL FAST — variables d'env critiques non vérifiées au boot | Vérification `SECRET_KEY`, `DATABASE_URL`, `PASETO_PRIVATE_KEY`, `PASETO_PUBLIC_KEY` |
| `utils.py` | PII (emails, domaines) dans `print()` exposés aux journaux système | Migré vers `logging.getLogger('cei.utils')` avec masquage |

### Correctifs logique métier

| Fichier | Problème | Correction |
|---------|----------|------------|
| `routes/exams.py` | Race condition non-atomique sur `tab_switches`/`warnings_count` — contournement anti-fraude possible | `UPDATE ... SET col = col + 1` via SQLAlchemy (atomique) |
| `proctoring_routes.py` | Race condition non-atomique sur `risk_score` | `LEAST(risk_score + increment, 100)` via `func.least()` SQLAlchemy |
| `routes/exams.py` | `close_online_exam` ne soumettait pas les copies `IN_PROGRESS` → réponses perdues | Auto-submit `IN_PROGRESS → AUTO_SUBMITTED` avant fermeture |
| `routes/exams.py` | `request.json` sans garde `None` → `AttributeError` sans `Content-Type` | Remplacé par `request.get_json(silent=True) or {}` |
| `routes/exams.py` | Score IA non borné [0, 20] → notes hors barème en base | `max(0.0, min(20.0, float(score)))` après extraction |
| `routes/exams.py` | `manual_grade_attempt` sans vérification de propriété → prof peut noter examen d'un collègue | Ajout du check `exam.created_by_id != user_id` |
| `proctoring_routes.py` | `agent_alerts.json` sans verrou fichier → JSON corrompu multi-workers | `fcntl.flock()` LOCK_EX en écriture, LOCK_SH en lecture |

### Correctifs performance

| Fichier | Problème | Correction |
|---------|----------|------------|
| `cache.py` | Invalidation cache cassée : clés SHA-256 incompatibles avec glob patterns | Clés lisibles `cei:category:id` ; `make_content_key()` séparé pour le hachage IA |
| `models.py` | Index DB manquants sur `exam_attempts.exam_id`, `student_id`, `status`, `activity_logs.attempt_id` | `index=True` ajouté sur toutes les colonnes de FK et de filtre proctoring |
| `proctoring_routes.py` | N+1 queries dans `get_active_proctoring` — `a.student` lazy-loadé dans boucle | `options(joinedload(ExamAttempt.student))` |

---

## Mises à jour — 03/07/2026

### Scalabilité et normes professionnelles

| Composant | Amélioration |
|-----------|-------------|
| `models.py` | Pool SQLAlchemy : `pool_size=3, max_overflow=7` → 10/worker × 9 workers = 90 connexions (< 100 max PostgreSQL) |
| `gunicorn.conf.py` | Hooks `post_fork` + `worker_exit` : `engine.dispose()` pour éviter le partage de connexions DB après fork |
| `extensions.py` | Rate limiter migré de `memory://` (compteurs indépendants) vers Redis DB 1 (partagé entre workers) |
| `app.py` | Health check `GET /api/health` (DB + Redis), logging structuré `X-Request-ID`, error handlers 404/405/413/429/500 |
| `routes/formations.py` | Cache Redis sur endpoints hot (formations, semestres, UE, EC) — TTL 5 min, invalidation sur mutations |
| `app.py` | CSP différenciée : stricte pour les routes API, permissive pour `/api/docs` (Swagger CDN) |

---

## Architecture

```
Internet
    │
    ▼
Nginx (TLS/HTTPS 443)
    │
    ├── / → Next.js standalone (port 5173)
    │
    └── /api/* → unix:/run/cei-api-v2.sock
                     │
                     ▼
               Gunicorn gthread
               9 workers × 4 threads = 36 slots
                     │
              ┌──────┴──────┐
              ▼             ▼
         PostgreSQL       Redis
         (pool 90)     DB0: cache
                       DB1: rate limit
```

---

## Stack technique

| Couche | Technologie | Version |
|--------|-------------|---------|
| Langage | Python | 3.10+ |
| Framework | Flask | 3.x |
| WSGI | Gunicorn gthread | 21+ |
| Auth | PASETO v4.public Ed25519 | python-paseto |
| ORM | SQLAlchemy | 2.x |
| Base de données | PostgreSQL | 15+ |
| Cache / Rate limit | Redis | 7+ |
| IA correction | Claude (Anthropic) → Gemini → DeepSeek → Ollama | - |
| Email | SMTP → Livraison directe MX | smtplib |
| Compression | flask-compress (gzip/brotli level 6) | - |

---

## Authentification — Architecture hybride

| Type | Mécanisme | Durée | Révocable |
|------|-----------|-------|-----------|
| Access token | PASETO v4.public stateless | 15 min | Non (court TTL) |
| Refresh token | Cookie httpOnly + blocklist DB | 7 jours | Oui (rotation) |

**Stateless** : l'access token est vérifié par signature Ed25519 sans requête DB.  
**Stateful** : le refresh token est vérifié en base (`token_blocklist`) + rotation à chaque usage.  
**Hybride** : les deux mécanismes coexistent pour combiner performance (stateless) et révocabilité (stateful).

---

## Sécurité

| Mesure | Détail |
|--------|--------|
| Algorithme auth | Ed25519 — immunisé contre les algorithm confusion attacks |
| Rotation refresh | Token révoqué après chaque usage |
| Rate limiting | Flask-Limiter Redis — 10/min login, 5/min forgot-password |
| Headers HTTP | CSP, HSTS, X-Frame-Options DENY, X-Content-Type-Options |
| CORS | Origines depuis `ALLOWED_ORIGINS` dans `.env` |
| FAIL FAST | 4 variables critiques vérifiées au boot (arrêt si manquantes) |
| Mots de passe | bcrypt, minimum 8 caractères |
| Subprocess | Validation regex domaine avant tout appel DNS |
| Atomicité | UPDATE SQL pour les compteurs de surveillance (pas de += Python) |
| Logs | PII masqué (domaine uniquement, pas d'adresse complète) |

---

## Configuration Gunicorn

```python
bind            = 'unix:/run/cei-api-v2.sock'
workers         = 9       # 2 × CPU + 1
worker_class    = 'gthread'
threads         = 4       # 9 × 4 = 36 slots
timeout         = 600     # routes IA
preload_app     = True
```

**Pool PostgreSQL** : `pool_size=3, max_overflow=7` → max 10/worker × 9 = 90 connexions (PostgreSQL max_connections=100).

---

## Variables d'environnement requises

```env
# Obligatoires (FAIL FAST si absentes)
SECRET_KEY=
DATABASE_URL=postgresql://user:pass@localhost:5432/dbname
PASETO_PRIVATE_KEY=
PASETO_PUBLIC_KEY=

# Recommandées
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_LIMITER_URL=redis://127.0.0.1:6379/1
ALLOWED_ORIGINS=https://dev-cei.ddns.net
APP_URL=https://dev-cei.ddns.net
PASETO_ACCESS_TTL_MIN=15
PASETO_REFRESH_TTL_DAYS=7

# Email
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=

# Docs API
DOCS_USER=
DOCS_PASS=

# Proctoring LiveKit
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
AGENT_SECRET_KEY=

# IA
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
```

---

## Endpoints — Vue d'ensemble (160 routes)

### Authentification (10)
`POST /api/auth/login` · `POST /api/auth/register` · `POST /api/auth/refresh` · `POST /api/auth/logout` · `GET /api/auth/public-key` · `GET /api/auth/me` · `PUT /api/profile` · `PUT /api/profile/password` · `POST /api/auth/forgot-password` · `POST /api/auth/reset-password`

### Administration (11)
Dashboard stats · Gestion utilisateurs (CRUD) · Création étudiant sans email · Historique examens · Rapport sécurité

### Maquette pédagogique (28)
Formations → Semestres → UE → EC · Affectations professeurs · Inscriptions étudiants

### Import CSV (4)
Templates CSV utilisateurs et maquette · Import masse utilisateurs · Import maquette pédagogique

### Sujets et Copies (13)
Upload sujets (PDF/DOCX/OCR) · Correction IA · Batch ZIP · Export PDF · Statistiques

### Examens en ligne (29)
Cycle de vie complet · Banque de questions · Résultats · Export CSV/ZIP/QR

### Surveillance — Surveillant (15)
Monitoring temps réel · Avertissements · Bannissements · Messagerie bidirectionnelle

### Proctoring LiveKit (12)
Tokens flux vidéo · Snapshots caméra · Appel privé surveillant-étudiant · Enregistrements

### Agent autonome (4)
Heartbeat · Alertes push · Statut · Lecture alertes

### IA (3)
Génération sujets · Suggestions · Analyse domaine

### Réclamations (7)
Dépôt · Analyse IA · Décision prof · Historique corrections

### Relevés de notes (5)
Génération PDF · Téléchargement · Bilan semestriel · Validation LMD

### Tableaux de bord (9)
Dashboard admin · Dashboard prof · Dashboard étudiant · Analytics · Calendrier

---

## Lancer en développement

```bash
cd /root/cei-api-v2
source /root/cei-unchk.sn/.venv/bin/activate
python app.py
```

## Lancer en production

```bash
systemctl start cei-api-v2
systemctl status cei-api-v2
journalctl -u cei-api-v2 -f
```

## Health check

```bash
curl https://dev-cei.ddns.net/api/health
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}
```
