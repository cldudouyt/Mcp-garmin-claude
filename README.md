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

Le dépôt fournit un serveur local pour Claude Desktop. Pour un test sur Android, prépare une instance MCP distante dédiée à ton compte Garmin. Les étapes ci-dessous demandent une adaptation de `server.py` et un hébergement HTTPS.

### 1. Préparer une instance de test en lecture seule

1. Sur l'hôte qui exécutera le MCP, crée un environnement Python, installe `garminconnect` et `mcp`, puis initialise ta session Garmin avec `python server.py login`. Exécute ensuite le serveur avec le même utilisateur système pour qu'il retrouve les jetons. Conserve le dossier de jetons sur l'hôte, hors du dépôt.
2. Dans la copie dédiée au test, garde `@mcp.tool()` uniquement sur `dernieres_activites`. Dans le point d'entrée, remplace `mcp.run()` par `mcp.run(transport="streamable-http")`. Lance ensuite `python server.py`. [Transports du SDK MCP](https://py.sdk.modelcontextprotocol.io/run/)
3. Place un proxy HTTPS devant le serveur et publie le chemin MCP, par exemple `https://mcp.example.net/mcp`. Claude se connecte depuis les serveurs d'Anthropic sur le port HTTPS 443. Garde le port du processus Python privé pour que les requêtes passent toutes par le proxy.
4. Configure le proxy pour exiger un secret sur chaque requête au MCP, par exemple via l'en-tête `X-MCP-Key`. Utilise un secret dédié à ce test, distinct des identifiants Garmin. Depuis l'extérieur, vérifie qu'une requête vers l'URL sans ce secret reçoit un refus HTTP (401 ou 403). [Exigences réseau et authentification des connecteurs Claude](https://support.claude.com/fr/articles/11175166-commencer-avec-les-connecteurs-personnalises-utilisant-mcp-distant)

### 2. Ajouter le connecteur dans Claude

1. Sur [claude.ai](https://claude.ai), ouvre **Personnaliser → Connecteurs → Ajouter** et choisis un connecteur personnalisé web. Donne-lui un nom, par exemple `Garmin (test Android)`, et saisis l'URL HTTPS complète, chemin `/mcp` compris.
2. Si tu utilises le secret du proxy, choisis le mode sans connexion OAuth et ajoute, dans **En-têtes de requête**, l'en-tête attendu par le proxy (`X-MCP-Key` dans l'exemple) avec sa valeur. Termine l'ajout du connecteur. [Configuration des connecteurs personnalisés](https://support.claude.com/fr/articles/11175166-commencer-avec-les-connecteurs-personnalises-utilisant-mcp-distant)

### 3. Vérifier le résultat sur Android

1. Dans l'application Claude, connecte-toi avec le même compte que sur claude.ai. Ouvre une nouvelle conversation et active **Garmin (test Android)** dans le menu des connecteurs. [Utiliser les connecteurs sur Claude Mobile](https://support.claude.com/fr/articles/11176164-utiliser-les-connecteurs-pour-etendre-les-capacites-de-claude)
2. Demande « Donne-moi la date, le sport et la distance de ma dernière activité Garmin. » Vérifie que Claude utilise le connecteur et compare les trois valeurs avec ton activité dans Garmin Connect.
3. Si le connecteur échoue, vérifie d'abord l'URL HTTPS, le chemin `/mcp` et l'en-tête du proxy. Si le connecteur répond mais que Garmin demande une reconnexion, relance `python server.py login` sur l'hôte, puis redémarre l'instance.

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
