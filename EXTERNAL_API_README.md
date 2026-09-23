# API externe CEI — Guide d'intégration (ENT UNCHK)

Ce document décrit comment intégrer le **Centre d'Examen Intelligent (CEI)** dans une plateforme tierce (ENT UNCHK) : authentification unique (SSO) des utilisateurs, et appels à l'API CEI par module de rôle.

Il existe deux mécanismes **indépendants**, à ne pas confondre :

| Mécanisme | Sert à | Porté par |
|---|---|---|
| **SSO (Keycloak UNCHK)** | Faire atterrir un utilisateur déjà connecté sur l'ENT directement sur son tableau de bord CEI, sans reconnexion | Navigateur (redirection complète) |
| **Clé API (`X-CEI-API-Key`)** | Identifier votre application quand elle appelle l'API CEI par programmation | Serveur à serveur |

---

## 1. Base URLs

| Environnement | URL |
|---|---|
| Préproduction (tests) | `https://preprod-cei.unchk.sn` |
| Production | `https://cei.unchk.sn` |

**Testez toujours en préproduction d'abord.** Les identifiants de test préproduction sont distincts de ceux de production.

---

## 2. SSO — Connexion unique via Keycloak UNCHK

CEI est enregistré comme client OIDC dans le realm Keycloak `UNCHK` (`https://senid.unchk.sn/realms/UNCHK`) — le même serveur d'identité que Moodle.

### Flux (Authorization Code)

1. Depuis votre plateforme, dirigez le navigateur de l'utilisateur (navigation complète, **pas** un appel `fetch`/XHR — ceci mène chez Keycloak) vers :
   ```
   GET https://preprod-cei.unchk.sn/api/auth/oidc/login
   ```
2. Si l'utilisateur a déjà une session Keycloak active (ex. déjà connecté sur l'ENT ou Moodle), il est redirigé **immédiatement** sans revoir de formulaire de connexion. Sinon, Keycloak lui présente sa page de connexion habituelle.
3. Keycloak redirige vers CEI, qui recherche un compte CEI existant correspondant à l'email de l'utilisateur.
   - **Compte trouvé et actif** → CEI ouvre une session et redirige l'utilisateur vers son tableau de bord CEI (`/dashboard`), automatiquement dirigé vers la bonne interface selon son rôle CEI (étudiant, professeur, surveillant, superviseur).
   - **Aucun compte CEI correspondant** → l'utilisateur est redirigé vers la page de connexion CEI avec un message l'invitant à contacter l'administration CEI. **CEI ne crée jamais de compte automatiquement** : le rôle et l'accès restent entièrement gérés côté CEI.

### Important

- Le rôle CEI (étudiant/professeur/surveillant/superviseur) **n'est jamais déduit de Keycloak** — Keycloak ne fait qu'authentifier l'identité (email). Le compte CEI correspondant doit exister au préalable, créé par un administrateur CEI.
- Il n'y a **rien à implémenter côté ENT** au-delà du lien de redirection ci-dessus : toute la mécanique (échange de code, validation du jeton, ouverture de session) est gérée par CEI.

---

## 3. Clé API — Appels programmatiques

Si votre plateforme a besoin d'appeler l'API CEI directement (par exemple pour afficher des données CEI dans une page de l'ENT sans rediriger l'utilisateur), utilisez une **clé API**, fournie par l'administration CEI.

### Authentification requise sur les routes externes

Chaque appel doit fournir **les deux** éléments suivants :

```
Authorization: Bearer <jeton PASETO de l'utilisateur CEI>
X-CEI-API-Key: <votre clé API>
```

- Le jeton PASETO s'obtient via une connexion classique (`POST /api/auth/login`) ou via le flux SSO ci-dessus.
- La clé API identifie votre application ; elle est scopée à un ou plusieurs modules (rôles) — un appel vers un module non autorisé par votre clé renvoie `403`.
- Une clé API **seule, sans jeton utilisateur valide, ne donne accès à rien**.

### Format des erreurs

| Code | Signification |
|---|---|
| `401` | Jeton absent/invalide, ou clé API absente/invalide/révoquée |
| `403` | Rôle de l'utilisateur non autorisé pour cette route, ou clé API non autorisée pour ce module |
| `404` | Module de documentation inconnu (`/api/docs/<role>`) |

---

## 4. API externe par module de rôle

Surface **volontairement restreinte et stable** (distincte des routes internes de l'application CEI, qui évoluent librement). Toutes les routes sont en lecture seule (`GET`).

### Module Étudiant

| Route | Description |
|---|---|
| `GET /api/external/student/exams` | Examens à venir / récents de l'étudiant connecté |
| `GET /api/external/student/transcripts` | Relevés de notes **publiés** de l'étudiant connecté |

### Module Professeur

| Route | Description |
|---|---|
| `GET /api/external/professor/exams` | Examens créés par le professeur connecté |
| `GET /api/external/professor/corrections` | Copies en attente de correction pour ses examens |

### Module Surveillant

| Route | Description |
|---|---|
| `GET /api/external/surveillant/assignments` | Affectations de surveillance à venir |

### Module Superviseur

| Route | Description |
|---|---|
| `GET /api/external/superviseur/groups` | Groupes de surveillants supervisés |

### Exemple

```bash
curl https://preprod-cei.unchk.sn/api/external/student/exams \
  -H "Authorization: Bearer <jeton>" \
  -H "X-CEI-API-Key: <clé>"
```

```json
{
  "exams": [
    {
      "id": 42,
      "title": "Examen final — Réseaux",
      "status": "scheduled",
      "start_time": "2026-10-01T09:00:00",
      "end_time": "2026-10-01T11:00:00",
      "duration_minutes": 120,
      "my_attempt_status": null,
      "my_score": null
    }
  ]
}
```

---

## 5. Documentation interactive (Swagger)

Chaque module dispose de sa propre documentation Swagger interactive, protégée par ses propres identifiants (fournis séparément par l'administration CEI, jamais dans ce document) :

- `https://preprod-cei.unchk.sn/api/docs/professor`
- `https://preprod-cei.unchk.sn/api/docs/student`
- `https://preprod-cei.unchk.sn/api/docs/surveillant`
- `https://preprod-cei.unchk.sn/api/docs/superviseur`

Remplacez `preprod-cei.unchk.sn` par `cei.unchk.sn` pour la documentation de production (identifiants différents).

Il n'existe pas de documentation pour un module « Administrateur » — cette partie de CEI n'est pas exposée à l'intégration externe.

---

## 6. Obtenir vos accès

Contactez l'administration CEI pour obtenir :
1. Les identifiants Basic Auth de la documentation Swagger du/des module(s) qui vous concernent.
2. Une clé API scopée aux modules dont votre intégration a besoin.
3. Si vous intégrez le SSO : confirmation que votre plateforme n'a rien à configurer côté Keycloak — c'est CEI qui est enregistré comme client, pas l'ENT.

---

## 7. Limites de fréquence

Les routes externes sont limitées à **60 requêtes/minute** par combinaison utilisateur + clé API. Une réponse `429` indique un dépassement — respectez un intervalle raisonnable entre les appels plutôt que de solliciter en boucle serrée.
