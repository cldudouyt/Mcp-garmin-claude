# MCP Garmin

Crée des séances structurées depuis Claude et les envoie sur Garmin Connect, qui les synchronise sur la montre. Pas de câble. Lit aussi tes activités récentes et ton état de forme pour adapter l'intensité des séances.

## Installation (Windows)

1. Mets `server.py` dans un dossier, par exemple `C:\Users\<toi>\garmin-mcp\`.
2. Installe les dépendances :
   ```
   pip install garminconnect mcp
   ```
3. Connecte-toi à Garmin une seule fois (e-mail, mot de passe, code de vérification si demandé) :
   ```
   python C:\Users\<toi>\garmin-mcp\server.py login
   ```
   Seuls des jetons sont enregistrés, dans `C:\Users\<toi>\.garminconnect`. Le mot de passe n'est pas conservé.

## Brancher sur Claude

**Application Claude pour ordinateur** : ouvre `%APPDATA%\Claude\claude_desktop_config.json` et ajoute :

```json
{
  "mcpServers": {
    "garmin": {
      "command": "python",
      "args": ["C:\\Users\\<toi>\\garmin-mcp\\server.py"]
    }
  }
}
```

Puis quitte complètement Claude et relance-le.

**Claude Code** :

```
claude mcp add garmin -- python C:\Users\<toi>\garmin-mcp\server.py
```

## Tester sur Android avec Claude Mobile

Le dépôt fournit `server.py` pour Claude Desktop et `app.py` pour un test distant sur Android. L'instance distante utilise un seul compte Garmin et expose uniquement la date, le sport et la distance de sa dernière activité.

### 1. Configurer l'instance de test sur Vercel

1. Sur ton ordinateur, installe les versions de `requirements.txt` avec `python -m pip install -r requirements.txt`. Pour créer une session Garmin dédiée, choisis un dossier de jetons distinct de celui de Claude Desktop. Dans PowerShell, exécute `$env:GARMINTOKENS = "$env:USERPROFILE\.garminconnect-android"`, puis `python server.py login`.
2. Dans le projet Vercel dédié, ajoute deux variables d'environnement sensibles. Pour `MCP_ACCESS_KEY`, génère une clé avec `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Pour `GARMIN_TOKENS_JSON`, colle le contenu du fichier `garmin_tokens.json` produit dans le dossier dédié. Saisis ces valeurs directement dans Vercel et garde le fichier hors du dépôt.
3. Déploie le projet avec le préréglage FastAPI. Pour ce projet dédié, utilise une URL joignable par les serveurs d'Anthropic. Ajoute `/mcp` à cette URL et vérifie qu'une requête directe sans clé répond `401`. [FastAPI sur Vercel](https://vercel.com/docs/frameworks/backend/fastapi)

### 2. Ajouter le connecteur dans Claude

1. Sur [claude.ai](https://claude.ai), ouvre **Personnaliser → Connecteurs → Ajouter** et choisis un connecteur personnalisé web. Donne-lui un nom, par exemple `Garmin (test Android)`, et saisis l'URL HTTPS complète, chemin `/mcp` compris.
2. Choisis le mode sans connexion OAuth et ajoute, dans **En-têtes de requête**, `X-MCP-Key` avec la valeur de `MCP_ACCESS_KEY`. Termine l'ajout du connecteur. [Configuration des connecteurs personnalisés](https://support.claude.com/fr/articles/11175166-commencer-avec-les-connecteurs-personnalises-utilisant-mcp-distant)

### 3. Vérifier le résultat sur Android

1. Dans l'application Claude, connecte-toi avec le même compte que sur claude.ai. Ouvre une nouvelle conversation et active **Garmin (test Android)** dans le menu des connecteurs. [Utiliser les connecteurs sur Claude Mobile](https://support.claude.com/fr/articles/11176164-utiliser-les-connecteurs-pour-etendre-les-capacites-de-claude)
2. Demande « Donne-moi la date, le sport et la distance de ma dernière activité Garmin. » Vérifie que Claude utilise le connecteur et compare les trois valeurs avec ton activité dans Garmin Connect.
3. En cas d'erreur d'accès, vérifie l'URL HTTPS, le chemin `/mcp` et l'en-tête `X-MCP-Key`. Si la session Garmin expire, relance `python server.py login` dans le dossier dédié, remplace `GARMIN_TOKENS_JSON` et redéploie. Les jetons renouvelés à l'exécution restent en mémoire tant que l'instance Vercel tourne.

Cette instance utilise une seule session Garmin. Pour ouvrir le connecteur au club, il faut identifier chaque adhérent et utiliser ses propres jetons Garmin.

## Utilisation

Demande simplement, par exemple : « Crée sur Garmin pour mardi 6 x 1000 m à 4:55-5:05, 15 min d'échauffement en zone 2, 2 min de récup, 10 min de retour au calme. »

| Outil | Rôle |
|---|---|
| `creer_seance` | Crée la séance et, avec une date, la met au calendrier Garmin |
| `lister_seances` | Liste les séances de la bibliothèque |
| `supprimer_seance` | Supprime une séance |
| `dernieres_activites` | Activités récentes : distance, durée, allure, FC, puissance, charge ; détail tour par tour en option |
| `etat_de_forme` | VFC, sommeil, Body Battery, FC de repos, charge d'entraînement, VO2max, seuil lactique, prédictions de course |

Sports : `course`, `velo`, `natation`.
Fin d'étape : durée (`"10:00"`, `"45s"`), distance en mètres, ou bouton Lap si rien n'est donné.
Cibles : allure min/km (course), zone ou plage de FC, zone ou plage de puissance, cadence.

Avec une date, la séance apparaît sur la montre à la synchronisation suivante. Sans date, elle reste dans la bibliothèque Garmin Connect et il faut l'envoyer vers la montre à la main.

`simulation: true` renvoie le JSON Garmin sans rien envoyer : pratique pour vérifier une séance.

## Adapter l'intensité

Demande par exemple : « Regarde ma forme et mes dernières séances, puis prépare mon fractionné de mardi. » Claude lit `etat_de_forme` et `dernieres_activites`, puis choisit les allures et le volume avant d'appeler `creer_seance`.

- `dernieres_activites` couvre 14 jours par défaut (jusqu'à 90), avec un filtre par sport. `avec_tours: true` ajoute l'allure et la FC de chaque tour pour les 5 activités les plus récentes.
- `etat_de_forme` donne le jour même par défaut, ou une date `AAAA-MM-JJ`. Ce que la montre ne mesure pas (par exemple le score de préparation à l'entraînement sur certains modèles) est listé dans `indisponible`.

L'adaptation se fait à la création : une fois sur la montre, la séance ne change plus. Pour la modifier, supprime-la et recrée-la avant de synchroniser.

## En cas de souci

- « Pas connecté à Garmin » : relance `python server.py login`.
- Garmin bloque parfois la connexion par mot de passe après plusieurs essais rapprochés : attends une heure avant de réessayer.
- La bibliothèque `garminconnect` n'est pas officielle : si Garmin change son site, `pip install -U garminconnect` règle en général le problème.
