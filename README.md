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

## Utiliser Garmin dans Claude sur le web et sur mobile

Chaque personne ajoute le connecteur distant dans son propre compte Claude avec sa propre clé API personnelle Garmin. La même adresse sert à tous.

1. Connecte-toi à Garmin Connect Web et termine la double authentification sur Garmin si elle est demandée. Crée ensuite ta clé API personnelle et choisis sa durée de validité. [Procédure Garmin](https://developer.garmin.com/gc-developer-program/personal/personal-api/)
2. Sur [claude.ai](https://claude.ai), ouvre **Personnaliser → Connecteurs → Ajouter → Personnalisé → Web**. Nomme le connecteur **Garmin** et saisis `https://mcp-garmin-claude.vercel.app/mcp`.
3. Choisis **Pas de connexion** pour l'option OAuth. Dans **En-têtes de requête**, sélectionne `Authorization` et mets `ApiKey TA_CLÉ_GARMIN` comme valeur, avec un espace après `ApiKey`. Marque l'en-tête comme obligatoire. Claude déploie cette fonction progressivement. [Configuration des en-têtes dans Claude](https://claude.com/docs/connectors/custom/add-unlisted#authenticate-with-request-headers)
4. Dans une conversation Claude sur le web, sur iOS ou sur Android, active **Garmin** et demande « Donne-moi la date, le sport et la distance de ma dernière activité Garmin. » Compare le résultat avec Garmin Connect.

Claude conserve la clé personnelle dans la configuration du connecteur. À chaque lecture, le serveur échange la clé contre un jeton Garmin et utilise ce jeton pour la requête. Le serveur ne conserve ni la clé ni le jeton et aucune variable Garmin n'est à renseigner sur Vercel. L'accès fonctionne entre les conversations et les redémarrages du serveur jusqu'à l'expiration ou la révocation de la clé. Dans ce cas, crée une nouvelle clé dans Garmin Connect Web et renseigne-la dans Claude. [Durée et révocation des clés](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)

Pour héberger ta propre instance, déploie `app.py`, `server.py` et `requirements.txt` avec le préréglage FastAPI sur Vercel. Vérifie que `/mcp` refuse une requête sans clé avec le statut HTTP 401.

### Publier des séances depuis le téléphone

La clé personnelle ne permet que la lecture. Pour publier, chaque personne ajoute à son connecteur un jeton de publication obtenu sur la page de connexion du site. Tout se fait depuis le téléphone.

1. Ouvre `https://mcp-garmin-claude.vercel.app/connexion` et connecte-toi avec ton e-mail et ton mot de passe Garmin, puis le code de vérification s'il est demandé.
2. Copie le jeton affiché. Il donne accès à ton compte Garmin : traite-le comme un mot de passe.
3. Dans Claude, supprime le connecteur Garmin puis ajoute-le de nouveau, car les en-têtes ne se modifient pas après coup. Remets l'en-tête `Authorization` habituel et ajoute un second en-tête `X-Auth-Token` avec le jeton comme valeur.
4. Demande par exemple : « Crée sur Garmin pour mardi 6 x 1000 m à 4:55-5:05, 15 min d'échauffement en zone 2, 2 min de récup, 10 min de retour au calme. »

Le site transmet l'e-mail et le mot de passe à Garmin sans les enregistrer ni les journaliser, et ne garde aucune copie du jeton. À chaque publication, le serveur échange le jeton contre un accès temporaire à Garmin. Si Garmin refuse un jour le jeton, refais ces étapes pour en obtenir un nouveau.

La publication passe par la bibliothèque non officielle `garminconnect`, comme le serveur local. Garmin peut la bloquer ou changer son site sans préavis.

### Outils du connecteur distant

| Outil | Utilisation |
|---|---|
| `derniere_activite` | Date locale, sport et distance de la dernière activité. |
| `historique_activites` | Activités filtrables par sport et date, avec distance, durée, dénivelé, fréquence cardiaque et puissance quand Garmin les fournit. |
| `detail_activite` | Mesures détaillées à partir de l'identifiant fourni par l'historique. |
| `indicateurs_entrainement` | Sommeil, VFC, Body Battery, VO₂ max, résumé quotidien ou zones de fréquence cardiaque. |
| `preparer_seance` | Valide une séance de course, vélo ou natation avec étapes, répétitions et cibles, sans la publier. |
| `creer_seance` | Publie la séance dans Garmin Connect et, avec une date, la met au calendrier. Demande le jeton de publication. |
| `lister_seances` | Liste les séances de la bibliothèque. Demande le jeton de publication. |
| `supprimer_seance` | Supprime une séance. Demande le jeton de publication. |
| `exporter_parcours_gpx` | Produit le contenu d'un fichier GPX à partir des points ordonnés d'un tracé. Enregistre ce contenu dans un fichier `.gpx`, puis importe-le dans Garmin Connect. |

La clé personnelle permet la lecture et le jeton de publication permet de créer des séances. La publication d'un parcours GPX, elle, demande un accès autorisé aux API Business [Training](https://developer.garmin.com/gc-developer-program/enterprise/training-api/) et [Courses](https://developer.garmin.com/gc-developer-program/enterprise/courses-api/). Garmin décrit l'[import manuel d'un GPX](https://support.garmin.com/en-GB/?faq=wKuZXCaZRP4mWPX5aRz5h5) sur le web et sur mobile.

## Utilisation du serveur local

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
