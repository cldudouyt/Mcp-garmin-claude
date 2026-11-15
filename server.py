"""MCP Garmin : créer des séances structurées depuis Claude et les envoyer sur Garmin Connect.

Installation :   pip install garminconnect mcp
Connexion (1x) : python server.py login
Serveur MCP :    python server.py          (lancé par Claude, pas à la main)

Les jetons Garmin sont stockés dans ~/.garminconnect (ou $GARMINTOKENS).
Le mot de passe n'est jamais enregistré ni vu par Claude.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from datetime import date as _date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

try:  # SDK MCP 2.x
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # SDK MCP 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

TOKENSTORE = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))

mcp = MCPServer(
    "garmin",
    instructions=(
        "Crée des séances d'entraînement structurées sur Garmin Connect. "
        "Donne toujours une date à creer_seance pour que la séance arrive "
        "toute seule sur la montre à la prochaine synchronisation."
    ),
)

# ---------------------------------------------------------------- modèle d'entrée

SPORTS = {
    "course": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    "velo": {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
    "natation": {"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
}

TYPES_ETAPE = {
    "echauffement": (1, "warmup"),
    "effort": (3, "interval"),
    "recuperation": (4, "recovery"),
    "repos": (5, "rest"),
    "retour_au_calme": (2, "cooldown"),
}


class Etape(BaseModel):
    """Une étape simple. Fin : `duree` OU `distance_m` (sinon bouton Lap).
    Cible : au plus un champ parmi allure, zone_fc, fc, zone_puissance, puissance, cadence."""

    model_config = ConfigDict(extra="forbid")  # un champ inconnu est une erreur, pas ignoré

    type: Literal["echauffement", "effort", "recuperation", "repos", "retour_au_calme"]
    duree: str | None = Field(None, description="'10:00' (mm:ss), '1:15:00' (h:mm:ss) ou '45s'")
    distance_m: float | None = Field(None, gt=0, description="Distance en mètres, ex. 1000")
    allure: str | None = Field(
        None, description="Allure en min/km : '5:00' (±5 s) ou plage '4:55-5:05'. Course uniquement."
    )
    zone_fc: int | None = Field(None, ge=1, le=5, description="Zone de fréquence cardiaque 1 à 5")
    fc: str | None = Field(None, description="Plage de FC en bpm, ex. '140-150'")
    zone_puissance: int | None = Field(None, ge=1, le=7, description="Zone de puissance 1 à 7 (vélo)")
    puissance: str | None = Field(None, description="Plage de puissance en watts, ex. '190-210'")
    cadence: str | None = Field(None, description="Plage de cadence, ex. '85-95'")
    note: str | None = Field(None, description="Texte affiché sur la montre pendant l'étape")


class Repetition(BaseModel):
    """Un bloc répété, ex. 6 x (effort + récupération)."""

    model_config = ConfigDict(extra="forbid")

    repetitions: int = Field(ge=2, le=99)
    etapes: list[Etape] = Field(min_length=1)


# ---------------------------------------------------------------- conversion Garmin


def _secondes(texte: str) -> int:
    t = texte.strip().lower()
    if m := re.fullmatch(r"(\d+)\s*s", t):
        total = int(m[1])
    elif m := re.fullmatch(r"(\d+)\s*min", t):
        total = int(m[1]) * 60
    else:
        parts = t.split(":")
        if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
            raise ValueError(f"Durée illisible : {texte!r} (attendu '10:00', '1:15:00' ou '45s')")
        total = 0
        for p in parts:
            total = total * 60 + int(p)
    if total <= 0:
        raise ValueError(f"Durée nulle : {texte!r}")
    return total


def _plage(texte: str, nom: str) -> tuple[float, float]:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", texte)
    if not m:
        raise ValueError(f"{nom} illisible : {texte!r} (attendu 'min-max', ex. '140-150')")
    a, b = float(m[1]), float(m[2])
    return min(a, b), max(a, b)


def _allure_en_ms(texte: str) -> tuple[float, float]:
    """'5:00' ou '4:55-5:05' (min/km) -> (vitesse lente, vitesse rapide) en m/s."""
    erreur = ValueError(f"Allure illisible : {texte!r} (attendu '5:00' ou '4:55-5:05', en min/km)")
    try:
        bornes = [_secondes(p) for p in texte.split("-")]
    except ValueError:
        raise erreur from None
    if len(bornes) == 1:
        bornes = [bornes[0] - 5, bornes[0] + 5]
    if len(bornes) != 2:
        raise erreur
    rapide, lente = sorted(bornes)
    if not 90 <= rapide <= lente <= 1800:  # 1:30 à 30:00 par km
        raise ValueError(f"Allure invraisemblable : {texte!r} (attendu entre 1:30 et 30:00 par km)")
    return round(1000 / lente, 4), round(1000 / rapide, 4)


def _cible(e: Etape, sport: str) -> dict[str, Any]:
    donnees = {
        k: getattr(e, k)
        for k in ("allure", "zone_fc", "fc", "zone_puissance", "puissance", "cadence")
        if getattr(e, k) is not None
    }
    if len(donnees) > 1:
        raise ValueError(f"Une seule cible par étape, reçu : {', '.join(donnees)}")

    def cible(type_id: int, cle: str, **valeurs: Any) -> dict[str, Any]:
        return {
            "targetType": {"workoutTargetTypeId": type_id, "workoutTargetTypeKey": cle, "displayOrder": type_id},
            **valeurs,
        }

    if not donnees:
        return cible(1, "no.target")
    if sport == "natation":
        raise ValueError("Les cibles ne sont pas gérées en natation : utilise `note`.")
    if e.allure:
        if sport != "course":
            raise ValueError("`allure` n'est gérée qu'en course à pied.")
        lente, rapide = _allure_en_ms(e.allure)
        return cible(6, "pace.zone", targetValueOne=lente, targetValueTwo=rapide)
    if e.zone_fc:
        return cible(4, "heart.rate.zone", zoneNumber=e.zone_fc)
    if e.fc:
        bas, haut = _plage(e.fc, "fc")
        return cible(4, "heart.rate.zone", targetValueOne=bas, targetValueTwo=haut)
    if e.zone_puissance:
        return cible(2, "power.zone", zoneNumber=e.zone_puissance)
    if e.puissance:
        bas, haut = _plage(e.puissance, "puissance")
        return cible(2, "power.zone", targetValueOne=bas, targetValueTwo=haut)
    bas, haut = _plage(e.cadence, "cadence")
    return cible(3, "cadence", targetValueOne=bas, targetValueTwo=haut)


class _Constructeur:
    """Transforme les étapes en JSON Garmin et estime la durée totale."""

    def __init__(self, sport: str):
        self.sport = sport
        self.ordre = 0
        self.duree = 0.0

    def etape(self, e: Etape, facteur: int = 1) -> dict[str, Any]:
        self.ordre += 1
        type_id, cle = TYPES_ETAPE[e.type]
        if e.duree and e.distance_m:
            raise ValueError("Une étape se termine par `duree` OU `distance_m`, pas les deux.")
        cible = _cible(e, self.sport)
        if e.duree:
            valeur = _secondes(e.duree)
            fin = {"conditionTypeId": 2, "conditionTypeKey": "time"}
            self.duree += valeur * facteur
        elif e.distance_m:
            valeur = e.distance_m
            fin = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
            # estimation : allure cible si connue, sinon 6:00/km à pied, 25 km/h à vélo, 2:00/100 m en natation
            vitesse = cible.get("targetValueOne") if e.allure else None
            defaut = {"course": 2.78, "velo": 6.94, "natation": 0.83}[self.sport]
            self.duree += valeur / (vitesse or defaut) * facteur
        else:
            valeur = None
            fin = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
        pas: dict[str, Any] = {
            "type": "ExecutableStepDTO",
            "stepOrder": self.ordre,
            "stepType": {"stepTypeId": type_id, "stepTypeKey": cle, "displayOrder": type_id},
            "endCondition": {**fin, "displayOrder": fin["conditionTypeId"], "displayable": True},
            **cible,
        }
        if valeur is not None:
            pas["endConditionValue"] = valeur
        if e.note:
            pas["description"] = e.note
        if self.sport == "natation":
            pas["strokeType"] = {"strokeTypeId": 0, "displayOrder": 0}
            pas["equipmentType"] = {"equipmentTypeId": 0, "displayOrder": 0}
        return pas

    def bloc(self, b: Etape | Repetition) -> dict[str, Any]:
        if isinstance(b, Etape):
            return self.etape(b)
        self.ordre += 1
        ordre = self.ordre
        return {
            "type": "RepeatGroupDTO",
            "stepOrder": ordre,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
            "numberOfIterations": b.repetitions,
            "endCondition": {
                "conditionTypeId": 7,
                "conditionTypeKey": "iterations",
                "displayOrder": 7,
                "displayable": False,
            },
            "endConditionValue": b.repetitions,
            "smartRepeat": False,
            "workoutSteps": [self.etape(e, b.repetitions) for e in b.etapes],
        }


def construire_seance(
    nom: str,
    sport: str,
    etapes: list[Etape | Repetition],
    description: str | None = None,
    longueur_bassin_m: int = 25,
) -> dict[str, Any]:
    """Construit le JSON attendu par Garmin Connect (/workout-service/workout)."""
    if sport not in SPORTS:
        raise ValueError(f"Sport inconnu : {sport!r} (course, velo, natation)")
    if not etapes:
        raise ValueError("La séance doit contenir au moins une étape.")
    c = _Constructeur(sport)
    pas = [c.bloc(b) for b in etapes]
    seance: dict[str, Any] = {
        "workoutName": nom,
        "sportType": SPORTS[sport],
        "estimatedDurationInSecs": round(c.duree),
        "workoutSegments": [{"segmentOrder": 1, "sportType": SPORTS[sport], "workoutSteps": pas}],
    }
    if description:
        seance["description"] = description
    if sport == "natation":
        seance["poolLength"] = longueur_bassin_m
        seance["poolLengthUnit"] = {"unitId": 1, "unitKey": "meter", "factor": 100.0}
    return seance


# ---------------------------------------------------------------- client Garmin

_client = None


def _garmin():
    """Client Garmin connecté via les jetons enregistrés par `python server.py login`."""
    global _client
    if _client is None:
        from garminconnect import Garmin

        client = Garmin()
        try:
            client.login(TOKENSTORE)
        except Exception as exc:
            raise RuntimeError(
                "Pas connecté à Garmin. Dans un terminal, lance une fois : "
                f"python \"{os.path.abspath(__file__)}\" login   ({type(exc).__name__}: {exc})"
            ) from exc
        _client = client
    return _client


def _appel(fonction, *args, **kwargs):
    """Appelle Garmin ; si la session a expiré, se reconnecte une fois et réessaie."""
    global _client
    try:
        return fonction(_garmin(), *args, **kwargs)
    except RuntimeError:
        raise
    except Exception as exc:
        if "Authentication" not in type(exc).__name__:
            raise
        _client = None
        return fonction(_garmin(), *args, **kwargs)


# ---------------------------------------------------------------- outils MCP


def _erreurs_lisibles(outil):
    """Renvoie à Claude le vrai message d'erreur (sinon le SDK le masque)."""

    @functools.wraps(outil)
    def enveloppe(*args, **kwargs):
        try:
            return outil(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return enveloppe


@mcp.tool()
@_erreurs_lisibles
def creer_seance(
    nom: Annotated[str, Field(description="Nom affiché sur la montre, ex. '6x1000 m allure 10 km'")],
    sport: Literal["course", "velo", "natation"],
    etapes: Annotated[
        list[Etape | Repetition],
        Field(description="Étapes dans l'ordre. Un bloc {repetitions, etapes} répète ses étapes."),
    ],
    date: Annotated[
        str | None,
        Field(description="Jour prévu AAAA-MM-JJ. Avec une date, la séance arrive seule sur la montre."),
    ] = None,
    description: str | None = None,
    longueur_bassin_m: Annotated[int, Field(description="Natation uniquement")] = 25,
    simulation: Annotated[
        bool, Field(description="True = renvoie le JSON Garmin sans rien envoyer")
    ] = False,
) -> dict[str, Any]:
    """Crée une séance structurée dans Garmin Connect et, si `date` est donnée, la planifie au calendrier.

    Exemple d'étapes pour 6 x 1000 m :
    [{"type":"echauffement","duree":"15:00","zone_fc":2},
     {"repetitions":6,"etapes":[{"type":"effort","distance_m":1000,"allure":"4:55-5:05"},
                                {"type":"recuperation","duree":"2:00"}]},
     {"type":"retour_au_calme","duree":"10:00"}]
    """
    if date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError(f"Date illisible : {date!r} (attendu AAAA-MM-JJ)")
        _date.fromisoformat(date)
    seance = construire_seance(nom, sport, etapes, description, longueur_bassin_m)
    if simulation:
        return {"simulation": True, "date": date, "json_garmin": seance}

    cree = _appel(lambda g: g.upload_workout(seance))
    workout_id = cree["workoutId"]
    resultat: dict[str, Any] = {
        "workout_id": workout_id,
        "nom": nom,
        "duree_estimee_min": round(seance["estimatedDurationInSecs"] / 60),
        "url": f"https://connect.garmin.com/modern/workout/{workout_id}",
    }
    if date:
        try:
            _appel(lambda g: g.schedule_workout(workout_id, date))
            resultat["planifiee_le"] = date
            resultat["suite"] = "Synchronise la montre : la séance apparaît à cette date."
        except Exception as exc:
            resultat["erreur_planification"] = f"Séance créée mais non planifiée : {exc}"
    else:
        resultat["suite"] = (
            "Séance créée dans la bibliothèque, sans date : il faut l'envoyer "
            "vers la montre depuis Garmin Connect."
        )
    return resultat


@mcp.tool()
@_erreurs_lisibles
def lister_seances(limite: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict[str, Any]]:
    """Liste les dernières séances de la bibliothèque Garmin Connect (id, nom, sport, date de création)."""
    seances = _appel(lambda g: g.get_workouts(0, limite))
    return [
        {
            "workout_id": s.get("workoutId"),
            "nom": s.get("workoutName"),
            "sport": (s.get("sportType") or {}).get("sportTypeKey"),
            "creee_le": s.get("createdDate"),
        }
        for s in seances
    ]


@mcp.tool()
@_erreurs_lisibles
def supprimer_seance(workout_id: int) -> dict[str, Any]:
    """Supprime définitivement une séance de la bibliothèque Garmin Connect (et du calendrier)."""
    _appel(lambda g: g.delete_workout(workout_id))
    return {"supprimee": workout_id}


# ---------------------------------------------------------------- connexion


def login() -> None:
    """Connexion interactive : enregistre les jetons, jamais le mot de passe."""
    import shutil
    import tempfile
    from getpass import getpass

    from garminconnect import Garmin

    email = input("E-mail Garmin : ").strip()
    mot_de_passe = getpass("Mot de passe (non affiché) : ")
    client = Garmin(email, mot_de_passe, prompt_mfa=lambda: input("Code de vérification (MFA) : ").strip())
    # Connexion dans un dossier temporaire vide : d'anciens jetons seraient réutilisés à la place
    # des identifiants saisis, et ils ne sont remplacés que si la nouvelle connexion a réussi.
    with tempfile.TemporaryDirectory() as tmp:
        client.login(tmp)
        nouveaux = os.listdir(tmp)
        if not nouveaux:
            sys.exit("Connexion réussie mais les jetons n'ont pas pu être enregistrés.")
        os.makedirs(TOKENSTORE, exist_ok=True)
        for f in os.listdir(TOKENSTORE):
            if f.endswith(".json"):
                os.remove(os.path.join(TOKENSTORE, f))
        for f in nouveaux:
            shutil.copy2(os.path.join(tmp, f), os.path.join(TOKENSTORE, f))
    print(f"Connecté en tant que {client.get_full_name()}. Jetons enregistrés dans {TOKENSTORE}")


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        login()
    else:
        mcp.run()
