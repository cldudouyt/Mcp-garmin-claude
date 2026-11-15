"""MCP distant Garmin : lecture des activités et publication de séances.

Deux façons de se connecter depuis Claude :
- OAuth : Claude ouvre la page /connexion, la personne s'identifie chez Garmin et Claude reçoit
  le jeton tout seul. Demande la variable d'environnement OAUTH_SECRET.
- En-têtes : clé API personnelle Garmin (Authorization) pour la lecture, et jeton de publication
  (X-Auth-Token, donné par /connexion) pour les séances.

Le serveur ne conserve rien : tout ce qu'il délivre est chiffré et renvoyé par Claude à chaque requête.
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
import zlib
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from xml.etree import ElementTree

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, BaseModel, Field
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

# ---------------------------------------------------------------- OAuth sans stockage

PREFIXE_EN_TETES = "en-tetes."
DUREE_ACCES_S = 3600
DUREE_CODE_S = 300
DUREE_DEMANDE_S = 1800
REDIRECTIONS_CLAUDE = ("https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback")
REDIRECTION_LOCALE = re.compile(r"http://(localhost|127\.0\.0\.1)(:\d{1,5})?/[^\s]*")


def _url_publique() -> str:
    """Adresse publique du site : URL_PUBLIQUE, sinon celle que Vercel annonce pour ce déploiement."""
    if os.getenv("URL_PUBLIQUE"):
        return os.environ["URL_PUBLIQUE"].rstrip("/")
    if os.getenv("VERCEL_ENV") == "production":
        hote = os.getenv("VERCEL_PROJECT_PRODUCTION_URL")
    else:
        hote = os.getenv("VERCEL_BRANCH_URL") or os.getenv("VERCEL_URL")
    return f"https://{hote}" if hote else "http://localhost:8000"


URL_PUBLIQUE = _url_publique()


def _chiffre() -> Fernet | None:
    """Chiffrement dérivé d'OAUTH_SECRET ; None si la variable est absente ou trop courte."""
    secret = os.getenv("OAUTH_SECRET", "")
    if len(secret) < 32:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _sceller(genre: str, contenu: dict[str, Any]) -> str:
    chiffre = _chiffre()
    if chiffre is None:
        raise RuntimeError("OAUTH_SECRET absent")
    brut = json.dumps({"_": genre, **contenu}, separators=(",", ":")).encode()
    return chiffre.encrypt(zlib.compress(brut)).decode().rstrip("=")


def _ouvrir(jeton: str, genre: str, duree_s: int | None = None) -> dict[str, Any] | None:
    """Déchiffre un jeton scellé ; None s'il est illisible, d'un autre genre ou expiré."""
    chiffre = _chiffre()
    if chiffre is None or not jeton or len(jeton) > 8192:
        return None
    try:
        brut = chiffre.decrypt((jeton + "=" * (-len(jeton) % 4)).encode(), ttl=duree_s)
        contenu = json.loads(zlib.decompress(brut))
    except (InvalidToken, ValueError, zlib.error):
        return None
    return contenu if isinstance(contenu, dict) and contenu.get("_") == genre else None


def _empreinte(texte: str) -> str:
    return hashlib.sha256(texte.encode()).hexdigest()[:32]


def _renouveler_garmin(garmin: list[str]) -> list[str]:
    """Demande à Garmin un jeton de renouvellement plus récent. Lève si Garmin refuse l'ancien."""
    from garminconnect import Garmin

    client = Garmin().client
    client.loads(json.dumps({"di_token": "-", "di_client_id": garmin[0], "di_refresh_token": garmin[1]}))
    client._refresh_di_token()
    return [str(client.di_client_id), str(client.di_refresh_token)]


class CodeGarmin(AuthorizationCode):
    garmin: list[str]


class RenouvellementGarmin(RefreshToken):
    garmin: list[str]


class FournisseurOAuth:
    """Serveur d'autorisation OAuth dont l'état tient entièrement dans des jetons chiffrés."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        fiche = _ouvrir(client_id, "client")
        if fiche is None:
            return None
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=fiche.get("k"),
            redirect_uris=fiche["r"],
            token_endpoint_auth_method=fiche["m"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=fiche.get("n"),
            scope=fiche.get("s"),
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if _chiffre() is None:
            raise RegistrationError("invalid_client_metadata", "OAuth non configuré : variable OAUTH_SECRET absente")
        adresses = [str(u) for u in client_info.redirect_uris or []]
        if not adresses or any(
            a not in REDIRECTIONS_CLAUDE and not REDIRECTION_LOCALE.fullmatch(a) for a in adresses
        ):
            raise RegistrationError("invalid_redirect_uri", "Seul Claude peut se connecter à ce serveur.")
        client_info.client_secret_expires_at = None
        client_info.client_id = _sceller(
            "client",
            {
                "r": adresses,
                "m": client_info.token_endpoint_auth_method,
                "k": client_info.client_secret,
                "n": (client_info.client_name or "")[:80] or None,
                "s": client_info.scope,
            },
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        demande = _sceller(
            "demande",
            {
                "h": _empreinte(str(client.client_id)),
                "r": str(params.redirect_uri),
                "e": params.redirect_uri_provided_explicitly,
                "p": params.code_challenge,
                "s": params.scopes or [],
                "t": params.state,
                "u": params.resource,
            },
        )
        return f"/connexion?demande={demande}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CodeGarmin | None:
        contenu = _ouvrir(authorization_code, "code", DUREE_CODE_S)
        if contenu is None or contenu["h"] != _empreinte(str(client.client_id)):
            return None
        return CodeGarmin(
            code=authorization_code,
            scopes=contenu["s"],
            expires_at=time.time() + 60,  # la durée de vie réelle est vérifiée au déchiffrement
            client_id=str(client.client_id),
            code_challenge=contenu["p"],
            redirect_uri=contenu["r"],
            redirect_uri_provided_explicitly=contenu["e"],
            resource=contenu["u"],
            garmin=contenu["g"],
        )

    def _jetons(self, client: OAuthClientInformationFull, garmin: list[str], scopes: list[str]) -> OAuthToken:
        empreinte = _empreinte(str(client.client_id))
        return OAuthToken(
            access_token=_sceller("acces", {"g": garmin, "h": empreinte}),
            token_type="Bearer",
            expires_in=DUREE_ACCES_S,
            refresh_token=_sceller("renouvellement", {"g": garmin, "h": empreinte, "s": scopes}),
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: CodeGarmin
    ) -> OAuthToken:
        return self._jetons(client, authorization_code.garmin, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RenouvellementGarmin | None:
        contenu = _ouvrir(refresh_token, "renouvellement")
        if contenu is None or contenu["h"] != _empreinte(str(client.client_id)):
            return None
        return RenouvellementGarmin(
            token=refresh_token, client_id=str(client.client_id), scopes=contenu["s"], garmin=contenu["g"]
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RenouvellementGarmin, scopes: list[str]
    ) -> OAuthToken:
        from garminconnect import GarminConnectAuthenticationError

        garmin = refresh_token.garmin
        try:
            # Profite du renouvellement côté Claude pour faire avancer le jeton côté Garmin.
            garmin = await run_in_threadpool(_renouveler_garmin, garmin)
        except GarminConnectAuthenticationError:
            raise TokenError("invalid_grant", "Garmin a refusé le jeton : reconnecte-toi.") from None
        except Exception:
            pass  # Garmin indisponible : l'ancien jeton reste utilisable
        return self._jetons(client, garmin, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith(PREFIXE_EN_TETES):
            # Connexion par en-têtes, convertie par le middleware : la clé et le jeton sont ceux de l'appelant.
            try:
                contenu = json.loads(base64.urlsafe_b64decode(token[len(PREFIXE_EN_TETES):]))
            except ValueError:
                return None
            return AccessToken(token=token, client_id="en-tetes", scopes=[], claims=contenu)
        contenu = _ouvrir(token, "acces", DUREE_ACCES_S + 60)
        if contenu is None:
            return None
        return AccessToken(token=token, client_id=contenu["h"], scopes=[], claims={"garmin": contenu["g"]})

    async def revoke_token(self, token: Any) -> None:
        return None  # rien n'est stocké : un jeton cesse de servir quand Garmin le refuse


def _identite(ctx: Context) -> dict[str, Any]:
    """Ce que la requête apporte : {"cle": clé API ou None, "garmin": [client, jeton] ou None}."""
    requete = ctx.request_context.request
    utilisateur = requete.scope.get("user") if isinstance(requete, Request) else None
    jeton = getattr(utilisateur, "access_token", None)
    return (jeton.claims or {}) if jeton is not None else {}


mcp = FastMCP(
    "Garmin",
    instructions=(
        "Lit les activités et indicateurs d'entraînement du compte Garmin connecté. "
        "creer_seance publie une séance structurée dans Garmin Connect et la planifie au calendrier : "
        "donne toujours une date pour qu'elle arrive sur la montre. "
        "Si le connecteur ne porte que la clé API, preparer_seance valide la séance sans la publier. "
        "Un GPX produit n'est jamais publié : il reste à importer."
    ),
    auth_server_provider=FournisseurOAuth(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(URL_PUBLIQUE),
        resource_server_url=AnyHttpUrl(f"{URL_PUBLIQUE}/mcp"),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        validate_token_resource=False,
    ),
    stateless_http=True,
    json_response=True,
    # FastMCP sert un domaine public HTTPS derrière le proxy Vercel.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _lire_garmin(ctx: Context, chemin: str, params: dict[str, str | int] | None = None) -> Any:
    """Lit Garmin avec la clé API personnelle si le connecteur en porte une, sinon avec la session Garmin."""
    cle = _identite(ctx).get("cle")
    if cle is None:
        garmin = _garmin_session(ctx)
        from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

        try:
            return garmin.connectapi(chemin, params=params) or None
        except GarminConnectNotFoundError:
            return None
        except GarminConnectTooManyRequestsError:
            raise ToolError("Garmin limite les requêtes. Réessaie dans un instant.") from None
        except Exception:
            raise ToolError("Garmin est indisponible ou a renvoyé une réponse inattendue.") from None

    try:
        with httpx.Client(base_url=GARMIN_API, timeout=10, trust_env=False) as client:
            autorisation = client.post(
                GARMIN_TOKEN_PATH,
                headers={"Authorization": f"ApiKey {cle}"},
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


def _garmin_session(ctx: Context) -> Any:
    """Client Garmin ouvert avec le jeton Garmin de la requête (OAuth ou en-tête). Rien n'est conservé."""
    jeton = _identite(ctx).get("garmin")
    if jeton is None:
        raise ToolError(
            "Ce connecteur ne porte que la clé API, qui ne permet pas de publier. Reconnecte-le en OAuth, "
            f"ou ajoute-lui l'en-tête {EN_TETE_PUBLICATION} avec le jeton donné par la page /connexion."
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
            "Garmin a refusé le jeton. Reconnecte le connecteur Garmin dans Claude ; en mode en-têtes, "
            f"refais la connexion sur la page /connexion et remplace l'en-tête {EN_TETE_PUBLICATION}."
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
        raise ToolError("Garmin a refusé l'opération. Reconnecte le connecteur Garmin dans Claude.") from None
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

    Demande un connecteur relié en OAuth, ou l'en-tête X-Auth-Token obtenu sur la page /connexion.
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

    garmin = _garmin_session(ctx)
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
    """Liste les dernières séances de la bibliothèque Garmin Connect."""
    garmin = _garmin_session(ctx)
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
    """Supprime définitivement une séance de la bibliothèque Garmin Connect et du calendrier."""
    garmin = _garmin_session(ctx)
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
    """Convertit la connexion par en-têtes en jeton interne ; le reste suit le parcours OAuth."""
    en_tetes = request.headers.getlist("authorization")
    cle = AUTHORIZATION.fullmatch(en_tetes[0]) if len(en_tetes) == 1 else None
    if cle is not None:
        identite: dict[str, Any] = {"cle": cle.group(1), "garmin": None}
        publication = request.headers.getlist(EN_TETE_PUBLICATION)
        if publication:
            jeton = JETON_PUBLICATION.fullmatch(publication[0].strip()) if len(publication) == 1 else None
            if jeton is None:
                return JSONResponse(
                    {"error": f"En-tête {EN_TETE_PUBLICATION} illisible : recopie le jeton donné par /connexion"},
                    headers={"Cache-Control": "no-store"},
                    status_code=401,
                )
            identite["garmin"] = [jeton.group(1), jeton.group(2)]
        interne = PREFIXE_EN_TETES + base64.urlsafe_b64encode(json.dumps(identite).encode()).decode()
        request.scope["headers"] = [
            (nom, valeur) for nom, valeur in request.scope["headers"]
            if nom not in (b"authorization", EN_TETE_PUBLICATION.lower().encode())
        ] + [(b"authorization", f"Bearer {interne}".encode())]

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    defi = response.headers.get("www-authenticate")
    if defi:
        # Le SDK annonce l'adresse connue au démarrage : on la remplace par celle réellement appelée.
        response.headers["www-authenticate"] = defi.replace(URL_PUBLIQUE, _origine(request))
    return response


def _origine(request: Request) -> str:
    """Adresse par laquelle le site est appelé, pour que n'importe quel domaine du déploiement fonctionne."""
    hote = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}(:\d{1,5})?", hote):
        return URL_PUBLIQUE
    local = hote.split(":")[0] in ("localhost", "127.0.0.1")
    return f"{'http' if local else 'https'}://{hote}"


@app.get("/.well-known/oauth-authorization-server")
def metadonnees_oauth(request: Request) -> JSONResponse:
    """Métadonnées du serveur d'autorisation ; annonce aussi les clients publics, comme Claude."""
    metadonnees = build_metadata(
        AnyHttpUrl(_origine(request)), None, ClientRegistrationOptions(enabled=True), RevocationOptions()
    )
    metadonnees.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    return JSONResponse(
        metadonnees.model_dump(mode="json", exclude_none=True),
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def metadonnees_ressource(request: Request) -> JSONResponse:
    origine = _origine(request)
    return JSONResponse(
        {"resource": f"{origine}/mcp", "authorization_servers": [f"{origine}/"], "bearer_methods_supported": ["header"]},
        headers={"Access-Control-Allow-Origin": "*"},
    )


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


def _champ_demande(demande: str) -> str:
    return f'<input type="hidden" name="demande" value="{html.escape(demande, quote=True)}">' if demande else ""


def _page_identifiants(erreur: str = "", statut: int = 200, demande: str = "") -> HTMLResponse:
    alerte = f'<p class="alerte">{html.escape(erreur)}</p>' if erreur else ""
    but = "relier Claude à ton compte Garmin" if demande else "obtenir ton jeton de publication"
    return _page(
        "<h1>Connexion Garmin</h1>"
        f"<p>Connecte-toi pour {but}. Ton e-mail et ton mot de passe "
        "sont transmis à Garmin et ne sont ni enregistrés ni journalisés par ce site.</p>"
        f'{alerte}<form method="post" action="/connexion" autocomplete="on">{_champ_demande(demande)}'
        '<label for="email">E-mail Garmin</label>'
        '<input id="email" name="email" type="email" autocomplete="username" required>'
        '<label for="mot_de_passe">Mot de passe</label>'
        '<input id="mot_de_passe" name="mot_de_passe" type="password" autocomplete="current-password" required>'
        "<button>Se connecter</button></form>",
        statut,
    )


def _page_code(etat: str, erreur: str = "", statut: int = 200, demande: str = "") -> HTMLResponse:
    alerte = f'<p class="alerte">{html.escape(erreur)}</p>' if erreur else ""
    return _page(
        "<h1>Code de vérification</h1>"
        "<p>Garmin t'a envoyé un code. Saisis-le pour terminer la connexion.</p>"
        f'{alerte}<form method="post" action="/connexion" autocomplete="off">{_champ_demande(demande)}'
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


def _page_retour(adresse: str) -> HTMLResponse:
    """Renvoie vers Claude avec le code d'autorisation, sans dépendre d'une redirection de formulaire."""
    return _page(
        "<h1>Connexion réussie</h1><p>Retour vers Claude…</p>"
        f'<p><a href="{html.escape(adresse, quote=True)}">Continuer vers Claude</a></p>',
        script=f"location.replace({json.dumps(adresse).replace('<', chr(92) + 'u003c')});",
    )


def _page_demande_perimee() -> HTMLResponse:
    return _page(
        "<h1>Demande expirée</h1>"
        '<p class="alerte">Cette demande de connexion n\'est plus valable.</p>'
        "<p>Retourne dans Claude et relance la connexion du connecteur Garmin.</p>",
        400,
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
def page_connexion(demande: str = "") -> HTMLResponse:
    if demande and _ouvrir(demande, "demande", DUREE_DEMANDE_S) is None:
        return _page_demande_perimee()
    return _page_identifiants(demande=demande)


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
    demande = champs.get("demande", "")
    autorisation = _ouvrir(demande, "demande", DUREE_DEMANDE_S) if demande else None
    if demande and autorisation is None:
        return _page_demande_perimee()
    if not etat and not (champs.get("email") and champs.get("mot_de_passe")):
        return _page_identifiants("Saisis ton e-mail et ton mot de passe.", 400, demande)

    def retour(message: str, statut: int) -> HTMLResponse:
        if etat:
            return _page_code(etat, message, statut, demande)
        return _page_identifiants(message, statut, demande)

    try:
        etape, valeur = await run_in_threadpool(_connexion_garmin, champs)
    except GarminConnectTooManyRequestsError:
        return retour("Garmin limite les connexions. Attends une heure avant de réessayer.", 429)
    except GarminConnectAuthenticationError:
        return retour("Garmin a refusé le code." if etat else "Garmin a refusé ces identifiants.", 401)
    except Exception:
        return retour("Garmin est indisponible ou a bloqué la connexion depuis ce site. Réessaie plus tard.", 502)
    if etape == "code":
        return _page_code(valeur, demande=demande)
    if autorisation is None:
        return _page_jeton(valeur)
    code = _sceller("code", {**{c: autorisation[c] for c in "hrepsu"}, "g": valeur.split(":", 1)})
    return _page_retour(construct_redirect_uri(autorisation["r"], code=code, state=autorisation["t"]))


app.mount("/", mcp_app)
