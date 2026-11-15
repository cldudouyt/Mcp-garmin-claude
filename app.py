"""MCP distant en lecture seule utilisant la clé Garmin de chaque requête."""

import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from typing_extensions import TypedDict


GARMIN_API = "https://personal.connectapi.garmin.com"
GARMIN_TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
GARMIN_ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
AUTHORIZATION = re.compile(r"ApiKey ([A-Za-z0-9._~+/=-]{1,1024})", re.IGNORECASE)

mcp = FastMCP(
    "Garmin",
    instructions="Lit la date, le sport et la distance de la dernière activité Garmin.",
    stateless_http=True,
    json_response=True,
    # FastMCP sert un domaine public HTTPS derrière le proxy Vercel.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _lire_activites(cle: str) -> list[dict[str, Any]]:
    with httpx.Client(base_url=GARMIN_API, timeout=10, trust_env=False) as client:
        autorisation = client.post(GARMIN_TOKEN_PATH, headers={"Authorization": f"ApiKey {cle}"})
        if autorisation.status_code in (400, 401, 403):
            raise ToolError("Garmin a refusé la clé personnelle. Vérifie la clé et sa date d'expiration.")
        autorisation.raise_for_status()
        jeton = autorisation.text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", jeton):
            raise ValueError("Jeton Garmin inattendu")

        reponse = client.get(
            GARMIN_ACTIVITIES_PATH,
            headers={"Authorization": f"Bearer {jeton}"},
            params={"start": 0, "limit": 1},
        )
        reponse.raise_for_status()
        if reponse.status_code == 204:
            return []
        activites = reponse.json()
        if not isinstance(activites, list):
            raise ValueError("Réponse inattendue de Garmin")
        return activites


class Activite(TypedDict):
    date_locale: str | None
    sport: str | None
    distance_km: float | None


@mcp.tool()
def derniere_activite(ctx: Context) -> Activite | None:
    """Renvoie la date locale, le sport et la distance de la dernière activité Garmin."""
    requete = ctx.request_context.request
    if not isinstance(requete, Request) or not hasattr(requete.state, "cle_garmin"):
        raise ToolError("Ajoute la clé API personnelle Garmin dans les en-têtes du connecteur Claude.")
    try:
        activites = _lire_activites(requete.state.cle_garmin)
        if not activites:
            return None

        activite = activites[0]
        distance = activite.get("distance")
        return {
            "date_locale": activite.get("startTimeLocal"),
            "sport": (activite.get("activityType") or {}).get("typeKey"),
            "distance_km": round(distance / 1000, 2) if distance is not None else None,
        }
    except ToolError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise ToolError("Garmin est indisponible ou a renvoyé une réponse inattendue.") from None


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
