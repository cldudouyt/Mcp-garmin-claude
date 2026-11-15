"""MCP distant en lecture seule utilisant la clé Garmin de chaque requête."""

import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from server import Etape, Repetition, construire_seance

GARMIN_API = "https://personal.connectapi.garmin.com"
GARMIN_TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
GARMIN_ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
AUTHORIZATION = re.compile(r"ApiKey ([A-Za-z0-9._~+/=-]{1,1024})", re.IGNORECASE)

mcp = FastMCP(
    "Garmin",
    instructions=(
        "Lit les activités et indicateurs d'entraînement du compte Garmin connecté. "
        "Prépare des séances structurées et des tracés GPX à importer. "
        "Une séance préparée ou un GPX produit n'a pas été publié dans Garmin Connect."
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
    en_tetes = request.headers.getlist("authorization")
    autorisation = AUTHORIZATION.fullmatch(en_tetes[0]) if len(en_tetes) == 1 else None
    if autorisation is None:
        return JSONResponse(
            {"error": "Clé API personnelle Garmin requise"},
            headers={"Cache-Control": "no-store"},
            status_code=401,
        )

    request.state.cle_garmin = autorisation.group(1)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/", mcp_app)
