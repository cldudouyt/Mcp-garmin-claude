"""MCP distant : lecture avec la clé API personnelle Garmin de chaque requête,
publication de séances avec le jeton de publication fourni par la page /connexion.

Le serveur ne conserve rien : la clé et le jeton voyagent dans les en-têtes du connecteur Claude.
"""

import base64
import html
import json
import re
import secrets
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from typing_extensions import TypedDict

from server import Etape, Repetition, construire_seance

GARMIN_API = "https://personal.connectapi.garmin.com"
GARMIN_TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
GARMIN_ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
AUTHORIZATION = re.compile(r"ApiKey ([A-Za-z0-9._~+/=-]{1,1024})", re.IGNORECASE)
# Jeton de publication : « identifiant client:jeton de renouvellement », délivré par /connexion.
JETON_PUBLICATION = re.compile(r"([A-Za-z0-9_-]{1,128}):([A-Za-z0-9._~+/=-]{1,1024})")
EN_TETE_PUBLICATION = "X-Auth-Token"

mcp = FastMCP(
    "Garmin",
    instructions=(
        "Lit les activités et indicateurs d'entraînement du compte Garmin connecté. "
        "Avec un jeton de publication, creer_seance publie une séance structurée dans Garmin Connect "
        "et la planifie au calendrier : donne toujours une date pour qu'elle arrive sur la montre. "
        "Sans jeton, preparer_seance valide seulement la séance, qui n'est alors pas publiée. "
        "Un GPX produit n'est jamais publié : il reste à importer."
    ),
    stateless_http=True,
    json_response=True,
    # FastMCP sert un domaine public HTTPS derrière le proxy Vercel.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _lire_garmin(ctx: Context, chemin: str, params: dict[str, str | int] | None = None) -> Any:
    requete = ctx.request_context.request
    if not isinstance(requete, Request) or not hasattr(requete.state, "cle_garmin"):
        raise ToolError("Ajoute la clé API personnelle Garmin dans les en-têtes du connecteur Claude.")

    try:
        with httpx.Client(base_url=GARMIN_API, timeout=10, trust_env=False) as client:
            autorisation = client.post(
                GARMIN_TOKEN_PATH,
                headers={"Authorization": f"ApiKey {requete.state.cle_garmin}"},
            )
            if autorisation.status_code in (400, 401, 403):
                raise ToolError("Garmin a refusé la clé personnelle. Vérifie la clé et sa date d'expiration.")
            if autorisation.status_code == 429:
                raise ToolError("Garmin limite les requêtes. Réessaie dans un instant.")
            autorisation.raise_for_status()
            jeton = autorisation.text.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", jeton):
                raise ValueError("Jeton Garmin inattendu")

            reponse = client.get(chemin, headers={"Authorization": f"Bearer {jeton}"}, params=params)
            if reponse.status_code in (204, 404):
                return None
            if reponse.status_code == 429:
                raise ToolError("Garmin limite les requêtes. Réessaie dans un instant.")
            reponse.raise_for_status()
            return reponse.json()
    except (httpx.HTTPError, ValueError):
        raise ToolError("Garmin est indisponible ou a renvoyé une réponse inattendue.") from None


class Activite(TypedDict):
    date_locale: str | None
    sport: str | None
    distance_km: float | None


def _resume_activite(activite: dict[str, Any]) -> Activite:
    distance = activite.get("distance")
    type_activite = activite.get("activityType") or activite.get("activityTypeDTO")
    return {
        "date_locale": activite.get("startTimeLocal"),
        "sport": type_activite.get("typeKey") if isinstance(type_activite, dict) else None,
        "distance_km": round(distance / 1000, 2) if type(distance) in (int, float) else None,
    }


def _liste_activites(ctx: Context, params: dict[str, str | int]) -> list[dict[str, Any]]:
    resultat = _lire_garmin(ctx, GARMIN_ACTIVITIES_PATH, params)
    if resultat is None:
        return []
    if not isinstance(resultat, list) or any(not isinstance(item, dict) for item in resultat):
        raise ToolError("Garmin a renvoyé un format d'activités inattendu.")
    return resultat


@mcp.tool()
def derniere_activite(ctx: Context) -> Activite | None:
    """Renvoie la date locale, le sport et la distance de la dernière activité Garmin."""
    activites = _liste_activites(ctx, {"start": 0, "limit": 1})
    return _resume_activite(activites[0]) if activites else None


@mcp.tool()
def historique_activites(
    ctx: Context,
    debut: date | None = None,
    fin: date | None = None,
    sport: Literal["course", "velo", "natation", "marche", "randonnee"] | None = None,
    limite: Annotated[int, Field(ge=1, le=50)] = 20,
    decalage: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """Liste les activités de la plus récente à la plus ancienne. Dates inclusives et pagination."""
    if debut and fin and debut > fin:
        raise ToolError("La date de début doit précéder la date de fin.")
    types = {"course": "running", "velo": "cycling", "natation": "swimming",
             "marche": "walking", "randonnee": "hiking"}
    params: dict[str, str | int] = {"start": decalage, "limit": limite}
    if debut:
        params["startDate"] = debut.isoformat()
    if fin:
        params["endDate"] = fin.isoformat()
    if sport:
        params["activityType"] = types[sport]

    activites = _liste_activites(ctx, params)[:limite]
    return {
        "activites": [
            {**_resume_activite(a), "id": a.get("activityId"), "nom": a.get("activityName"),
             "duree_s": a.get("duration"), "denivele_m": a.get("elevationGain"),
             "fc_moyenne_bpm": a.get("averageHR"), "puissance_moyenne_w": a.get("avgPower")}
            for a in activites
        ],
        "decalage_suivant": decalage + len(activites) if len(activites) == limite else None,
    }


@mcp.tool()
def detail_activite(ctx: Context, id_activite: Annotated[int, Field(gt=0)]) -> dict[str, Any] | None:
    """Détaille l'activité du compte connecté à partir d'un identifiant de l'historique."""
    activite = _lire_garmin(ctx, f"/activity-service/activity/{id_activite}")
    if activite is None:
        return None
    if not isinstance(activite, dict):
        raise ToolError("Garmin a renvoyé un format d'activité inattendu.")
    mesures = activite.get("summaryDTO")
    if not isinstance(mesures, dict):
        mesures = activite
    return {
        **_resume_activite({**mesures, "activityType": activite.get("activityType"),
                            "activityTypeDTO": activite.get("activityTypeDTO")}),
        "id": id_activite,
        "nom": activite.get("activityName"),
        "duree_s": mesures.get("duration"),
        "duree_en_mouvement_s": mesures.get("movingDuration"),
        "denivele_m": mesures.get("elevationGain"),
        "fc_moyenne_bpm": mesures.get("averageHR"),
        "fc_max_bpm": mesures.get("maxHR"),
        "puissance_moyenne_w": mesures.get("averagePower", mesures.get("avgPower")),
    }


@mcp.tool()
def indicateurs_entrainement(
    ctx: Context,
    indicateur: Literal["sommeil", "vfc", "body_battery", "vo2max", "resume_quotidien", "zones_fc"],
    debut: date | None = None,
    fin: date | None = None,
) -> dict[str, Any]:
    """Lit un indicateur Garmin. Pour les séries, indique une période inclusive de 30 jours maximum."""
    if indicateur == "zones_fc":
        return {"indicateur": indicateur, "donnees": _lire_garmin(ctx, "/biometric-service/heartRateZones/v2")}

    premier = debut or fin
    dernier = fin or debut
    if premier is None or dernier is None:
        raise ToolError("Indique une date ou une plage de dates pour cet indicateur.")
    if not timedelta(0) <= dernier - premier < timedelta(days=30):
        raise ToolError("Choisis une période inclusive de 1 à 30 jours, dans l'ordre chronologique.")

    plage = {"startDate": premier.isoformat(), "endDate": dernier.isoformat()}
    endpoints = {
        "sommeil": ("/sleep-service/sleep/dailySleepSummariesByDate", plage),
        "vfc": (f"/hrv-service/hrv/daily/{premier}/{dernier}", None),
        "body_battery": ("/wellness-service/wellness/bodyBattery/reports/daily", plage),
        "vo2max": (f"/metrics-service/metrics/maxmet/daily/{premier}/{dernier}", None),
        "resume_quotidien": ("/usersummary-service/v2/dailysummaries", plage),
    }
    chemin, params = endpoints[indicateur]
    return {"indicateur": indicateur, "debut": premier.isoformat(), "fin": dernier.isoformat(),
            "donnees": _lire_garmin(ctx, chemin, params)}


@mcp.tool()
def preparer_seance(
    nom: Annotated[str, Field(min_length=1, max_length=128)],
    sport: Literal["course", "velo", "natation"],
    etapes: Annotated[list[Etape | Repetition], Field(min_length=1, max_length=50)],
    description: str | None = None,
    longueur_bassin_m: Annotated[int, Field(ge=1)] = 25,
) -> dict[str, Any]:
    """Prépare et valide une séance structurée à recréer dans Garmin Connect. Publication manuelle."""
    try:
        seance = construire_seance(nom, sport, etapes, description, longueur_bassin_m)
    except ValueError as erreur:
        raise ToolError(str(erreur)) from None
    return {"nom": nom, "sport": sport, "publiee": False, "json_garmin": seance}


# ---------------------------------------------------------------- publication


def _garmin_publication(ctx: Context) -> Any:
    """Client Garmin ouvert avec le jeton de publication de la requête. Rien n'est conservé."""
    requete = ctx.request_context.request
    jeton = getattr(requete.state, "jeton_publication", None) if isinstance(requete, Request) else None
    if jeton is None:
        raise ToolError(
            "Publication non activée pour ce connecteur. Ouvre la page /connexion de ce site, "
            f"connecte-toi à Garmin, puis ajoute l'en-tête {EN_TETE_PUBLICATION} au connecteur Claude."
        )
    from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

    garmin = Garmin()
    try:
        # Le jeton d'accès est redemandé à Garmin à chaque appel à partir du jeton de renouvellement.
        garmin.client.loads(
            json.dumps({"di_token": "-", "di_client_id": jeton[0], "di_refresh_token": jeton[1]})
        )
        garmin.client._refresh_di_token()
    except GarminConnectTooManyRequestsError:
        raise ToolError("Garmin limite les requêtes. Réessaie dans un instant.") from None
    except GarminConnectAuthenticationError:
        raise ToolError(
            "Garmin a refusé le jeton de publication. Refais la connexion sur la page /connexion "
            f"et remplace l'en-tête {EN_TETE_PUBLICATION} du connecteur."
        ) from None
    except Exception:
        raise ToolError("Garmin est indisponible ou a renvoyé une réponse inattendue.") from None
    return garmin


def _publier(action: Any) -> Any:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

    try:
        return action()
    except GarminConnectTooManyRequestsError:
        raise ToolError("Garmin limite les requêtes. Réessaie dans un instant.") from None
    except GarminConnectAuthenticationError:
        raise ToolError("Garmin a refusé l'opération. Refais la connexion sur la page /connexion.") from None
    except Exception as erreur:
        raise ToolError(f"Garmin a refusé ou n'a pas terminé l'opération ({type(erreur).__name__}).") from None


@mcp.tool()
def creer_seance(
    ctx: Context,
    nom: Annotated[str, Field(min_length=1, max_length=128)],
    sport: Literal["course", "velo", "natation"],
    etapes: Annotated[list[Etape | Repetition], Field(min_length=1, max_length=50)],
    date_prevue: Annotated[
        str | None,
        Field(description="Jour prévu AAAA-MM-JJ. Avec une date, la séance arrive seule sur la montre."),
    ] = None,
    description: str | None = None,
    longueur_bassin_m: Annotated[int, Field(ge=1)] = 25,
) -> dict[str, Any]:
    """Publie une séance structurée dans Garmin Connect et, avec `date_prevue`, la planifie au calendrier.

    Demande le jeton de publication (en-tête X-Auth-Token du connecteur, obtenu sur la page /connexion).
    """
    if date_prevue is not None:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_prevue):
                raise ValueError
            date.fromisoformat(date_prevue)
        except ValueError:
            raise ToolError(f"Date illisible : {date_prevue!r} (attendu AAAA-MM-JJ)") from None
    try:
        seance = construire_seance(nom, sport, etapes, description, longueur_bassin_m)
    except ValueError as erreur:
        raise ToolError(str(erreur)) from None

    garmin = _garmin_publication(ctx)
    id_seance = _publier(lambda: garmin.upload_workout(seance))["workoutId"]
    resultat: dict[str, Any] = {
        "id_seance": id_seance,
        "nom": nom,
        "publiee": True,
        "duree_estimee_min": round(seance["estimatedDurationInSecs"] / 60),
        "url": f"https://connect.garmin.com/modern/workout/{id_seance}",
    }
    if date_prevue is None:
        resultat["suite"] = "Séance dans la bibliothèque, sans date : envoie-la vers la montre depuis Garmin Connect."
        return resultat
    try:
        _publier(lambda: garmin.schedule_workout(id_seance, date_prevue))
    except ToolError as erreur:
        resultat["erreur_planification"] = f"Séance créée mais non planifiée : {erreur}"
    else:
        resultat["planifiee_le"] = date_prevue
        resultat["suite"] = "Synchronise la montre : la séance apparaît à cette date."
    return resultat


@mcp.tool()
def lister_seances(ctx: Context, limite: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
    """Liste les dernières séances de la bibliothèque Garmin Connect. Demande le jeton de publication."""
    garmin = _garmin_publication(ctx)
    return [
        {
            "id_seance": s.get("workoutId"),
            "nom": s.get("workoutName"),
            "sport": (s.get("sportType") or {}).get("sportTypeKey"),
            "creee_le": s.get("createdDate"),
        }
        for s in _publier(lambda: garmin.get_workouts(0, limite)) or []
    ]


@mcp.tool()
def supprimer_seance(ctx: Context, id_seance: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
    """Supprime définitivement une séance de la bibliothèque Garmin Connect et du calendrier.

    Demande le jeton de publication.
    """
    garmin = _garmin_publication(ctx)
    _publier(lambda: garmin.delete_workout(id_seance))
    return {"supprimee": id_seance}


class PointParcours(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    altitude_m: float | None = Field(default=None, allow_inf_nan=False)


@mcp.tool()
def exporter_parcours_gpx(
    nom: Annotated[str, Field(min_length=1, max_length=128)],
    points: Annotated[list[PointParcours], Field(min_length=2, max_length=250)],
) -> dict[str, str]:
    """Génère un GPX importable à partir de la géométrie ordonnée d'un itinéraire déjà calculé.

    Fournir les points du tracé réel. L'outil ne calcule pas d'itinéraire entre des adresses.
    """
    if any(ord(c) < 32 and c not in "\t\n\r" or 0xD800 <= ord(c) <= 0xDFFF
           or ord(c) in (0xFFFE, 0xFFFF) for c in nom):
        raise ToolError("Le nom contient un caractère incompatible avec GPX.")
    gpx = ElementTree.Element("gpx", xmlns="http://www.topografix.com/GPX/1/1",
                              version="1.1", creator="MCP Garmin")
    trace = ElementTree.SubElement(gpx, "trk")
    ElementTree.SubElement(trace, "name").text = nom
    segment = ElementTree.SubElement(trace, "trkseg")
    for point in points:
        sommet = ElementTree.SubElement(segment, "trkpt", lat=f"{point.latitude:.7f}",
                                       lon=f"{point.longitude:.7f}")
        if point.altitude_m is not None:
            ElementTree.SubElement(sommet, "ele").text = f"{point.altitude_m:.2f}"
    nom_fichier = re.sub(r"[^\w-]+", "-", nom).strip("-_")[:80] or "parcours"
    return {"nom_fichier": f"{nom_fichier}.gpx", "gpx": ElementTree.tostring(gpx, encoding="unicode")}


mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def proteger_mcp(request: Request, call_next):
    if request.url.path == "/connexion":
        return await call_next(request)

    en_tetes = request.headers.getlist("authorization")
    autorisation = AUTHORIZATION.fullmatch(en_tetes[0]) if len(en_tetes) == 1 else None
    if autorisation is None:
        return JSONResponse(
            {"error": "Clé API personnelle Garmin requise"},
            headers={"Cache-Control": "no-store"},
            status_code=401,
        )

    publication = request.headers.getlist(EN_TETE_PUBLICATION)
    if publication:
        jeton = JETON_PUBLICATION.fullmatch(publication[0].strip()) if len(publication) == 1 else None
        if jeton is None:
            return JSONResponse(
                {"error": f"En-tête {EN_TETE_PUBLICATION} illisible : recopie le jeton donné par /connexion"},
                headers={"Cache-Control": "no-store"},
                status_code=401,
            )
        request.state.jeton_publication = (jeton.group(1), jeton.group(2))

    request.state.cle_garmin = autorisation.group(1)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- page de connexion

TAILLE_MAX_FORMULAIRE = 64 * 1024

STYLE = """
:root{color-scheme:light dark;--fond:#f6f7f9;--carte:#fff;--texte:#16181d;--doux:#5b6270;--trait:#d5d9e0;--accent:#0b6bcb;--alerte:#b3261e}
@media (prefers-color-scheme:dark){:root{--fond:#111318;--carte:#1b1e25;--texte:#eceef2;--doux:#a3abb8;--trait:#343945;--accent:#6cb0ff;--alerte:#ff8a80}}
*{box-sizing:border-box}
body{margin:0;padding:24px 16px;background:var(--fond);color:var(--texte);font:16px/1.5 system-ui,sans-serif}
main{max-width:460px;margin:0 auto;background:var(--carte);border:1px solid var(--trait);border-radius:12px;padding:24px}
h1{font-size:1.35rem;margin:0 0 8px}
p,li{color:var(--doux);margin:0 0 12px}
ol{padding-left:20px;margin:0 0 12px}
label{display:block;font-weight:600;margin:16px 0 6px}
input,textarea,button{width:100%;font:inherit;border-radius:8px}
input,textarea{padding:12px;border:1px solid var(--trait);background:var(--fond);color:var(--texte)}
textarea{height:132px;resize:none;word-break:break-all;font-family:ui-monospace,monospace;font-size:.85rem}
button{margin-top:20px;padding:13px;border:0;background:var(--accent);color:var(--carte);font-weight:600}
.alerte{color:var(--alerte);font-weight:600}
code{font-family:ui-monospace,monospace;color:var(--texte)}
"""


def _page(corps: str, statut: int = 200, script: str = "") -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    document = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow"><title>Connexion Garmin</title>'
        f"<style>{STYLE}</style></head><body><main>{corps}</main>"
        + (f'<script nonce="{nonce}">{script}</script>' if script else "")
        + "</body></html>"
    )
    return HTMLResponse(
        document,
        status_code=statut,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{nonce}'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


def _page_identifiants(erreur: str = "", statut: int = 200) -> HTMLResponse:
    alerte = f'<p class="alerte">{html.escape(erreur)}</p>' if erreur else ""
    return _page(
        "<h1>Connexion Garmin</h1>"
        "<p>Connecte-toi pour obtenir ton jeton de publication. Ton e-mail et ton mot de passe "
        "sont transmis à Garmin et ne sont ni enregistrés ni journalisés par ce site.</p>"
        f'{alerte}<form method="post" action="/connexion" autocomplete="on">'
        '<label for="email">E-mail Garmin</label>'
        '<input id="email" name="email" type="email" autocomplete="username" required>'
        '<label for="mot_de_passe">Mot de passe</label>'
        '<input id="mot_de_passe" name="mot_de_passe" type="password" autocomplete="current-password" required>'
        "<button>Se connecter</button></form>",
        statut,
    )


def _page_code(etat: str, erreur: str = "", statut: int = 200) -> HTMLResponse:
    alerte = f'<p class="alerte">{html.escape(erreur)}</p>' if erreur else ""
    return _page(
        "<h1>Code de vérification</h1>"
        "<p>Garmin t'a envoyé un code. Saisis-le pour terminer la connexion.</p>"
        f'{alerte}<form method="post" action="/connexion" autocomplete="off">'
        f'<input type="hidden" name="etat" value="{html.escape(etat, quote=True)}">'
        '<label for="code">Code</label>'
        '<input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" required>'
        "<button>Valider</button></form>",
        statut,
    )


def _page_jeton(jeton: str) -> HTMLResponse:
    return _page(
        "<h1>Jeton de publication</h1>"
        "<p>Ce jeton donne accès à ton compte Garmin : traite-le comme un mot de passe. "
        "Il n'est affiché qu'ici et ce site n'en garde aucune copie.</p>"
        f'<label for="jeton">Valeur de l\'en-tête <code>{EN_TETE_PUBLICATION}</code></label>'
        f'<textarea id="jeton" readonly>{html.escape(jeton)}</textarea>'
        '<button type="button" id="copier">Copier le jeton</button>'
        "<p></p><ol>"
        "<li>Dans Claude, supprime le connecteur Garmin : ses en-têtes ne se modifient pas après coup.</li>"
        "<li>Ajoute-le de nouveau avec la même adresse et l'en-tête <code>Authorization</code> habituel.</li>"
        f"<li>Ajoute un second en-tête <code>{EN_TETE_PUBLICATION}</code> et colle ce jeton comme valeur.</li>"
        "</ol><p>Si Garmin refuse un jour le jeton, reviens sur cette page pour en obtenir un nouveau.</p>",
        script=(
            "const b=document.getElementById('copier'),j=document.getElementById('jeton');"
            "b.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(j.value)}"
            "catch(e){j.select();document.execCommand('copy')}b.textContent='Jeton copié'});"
        ),
    )


def _connexion_garmin(champs: dict[str, str]) -> tuple[str, str]:
    """Renvoie ("jeton", valeur) ou ("code", état à renvoyer avec le code de vérification)."""
    from garminconnect import Garmin

    if champs.get("etat"):
        garmin = Garmin(return_on_mfa=True)
        etat = json.loads(base64.urlsafe_b64decode(champs["etat"]))
        if not isinstance(etat, dict):
            raise ValueError("état illisible")
        garmin.client.resume_login(etat, champs.get("code", "").strip())
    else:
        garmin = Garmin(champs.get("email", "").strip(), champs.get("mot_de_passe", ""), return_on_mfa=True)
        statut, etat = garmin.login()
        if statut == "needs_mfa":
            return "code", base64.urlsafe_b64encode(json.dumps(etat).encode()).decode()
    jeton = f"{garmin.client.di_client_id}:{garmin.client.di_refresh_token}"
    if not JETON_PUBLICATION.fullmatch(jeton):
        raise ValueError("jeton Garmin inattendu")
    return "jeton", jeton


@app.get("/connexion")
def page_connexion() -> HTMLResponse:
    return _page_identifiants()


@app.post("/connexion")
async def connexion(request: Request) -> HTMLResponse:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

    corps = await request.body()
    if len(corps) > TAILLE_MAX_FORMULAIRE:
        return _page_identifiants("Formulaire trop volumineux.", 413)
    try:
        champs = {k: v[0] for k, v in parse_qs(corps.decode("utf-8"), max_num_fields=8).items()}
    except ValueError:
        return _page_identifiants("Formulaire illisible.", 400)
    etat = champs.get("etat", "")
    if not etat and not (champs.get("email") and champs.get("mot_de_passe")):
        return _page_identifiants("Saisis ton e-mail et ton mot de passe.", 400)

    def retour(message: str, statut: int) -> HTMLResponse:
        return _page_code(etat, message, statut) if etat else _page_identifiants(message, statut)

    try:
        etape, valeur = await run_in_threadpool(_connexion_garmin, champs)
    except GarminConnectTooManyRequestsError:
        return retour("Garmin limite les connexions. Attends une heure avant de réessayer.", 429)
    except GarminConnectAuthenticationError:
        return retour("Garmin a refusé le code." if etat else "Garmin a refusé ces identifiants.", 401)
    except Exception:
        return retour("Garmin est indisponible ou a bloqué la connexion depuis ce site. Réessaie plus tard.", 502)
    return _page_code(valeur) if etape == "code" else _page_jeton(valeur)


app.mount("/", mcp_app)
