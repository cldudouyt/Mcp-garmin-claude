"""MCP distant, réservé au test Android d'un seul compte Garmin."""

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from secrets import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from garminconnect import Garmin
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from typing_extensions import TypedDict


mcp = FastMCP(
    "Garmin (test Android)",
    instructions="Lit la date, le sport et la distance de la dernière activité Garmin.",
    stateless_http=True,
    json_response=True,
    # Le point d'entrée HTTPS de Vercel et la clé ci-dessous protègent l'accès distant.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@lru_cache(maxsize=1)
def _garmin() -> Garmin:
    client = Garmin()
    client.login(os.environ["GARMIN_TOKENS_JSON"])
    return client


class Activite(TypedDict):
    date_locale: str | None
    sport: str | None
    distance_km: float | None


@mcp.tool()
def derniere_activite() -> Activite | None:
    """Renvoie la date locale, le sport et la distance de la dernière activité Garmin."""
    try:
        activites = _garmin().get_activities(start=0, limit=1)
    except Exception:
        raise ToolError("Impossible de lire Garmin. Vérifie les jetons Garmin du déploiement.") from None

    if not activites:
        return None

    activite = activites[0]
    distance = activite.get("distance")
    return {
        "date_locale": activite.get("startTimeLocal"),
        "sport": (activite.get("activityType") or {}).get("typeKey"),
        "distance_km": round(distance / 1000, 2) if distance is not None else None,
    }


mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def proteger_mcp(request: Request, call_next):
    cle = os.environ.get("MCP_ACCESS_KEY", "").encode()
    fournie = request.headers.get("X-MCP-Key", "").encode()
    if not cle or not compare_digest(cle, fournie):
        return JSONResponse({"error": "Accès refusé"}, status_code=401)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/", mcp_app)
