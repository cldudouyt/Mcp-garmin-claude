"""MCP Garmin : créer des séances structurées depuis Claude et les envoyer sur Garmin Connect,
et lire les activités récentes et l'état de forme pour adapter l'intensité des séances.

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
from datetime import timedelta
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
        "toute seule sur la montre à la prochaine synchronisation. "
        "Pour adapter l'intensité, lis d'abord dernieres_activites (allures et FC "
        "réellement tenues) et etat_de_forme (VFC, sommeil, charge) avant de créer la séance."
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


# ---------------------------------------------------------------- lecture (activités, forme)

TYPES_ACTIVITE = {"course": "running", "velo": "cycling", "natation": "swimming"}


def _mmss(secondes: float | None) -> str | None:
    """Durée lisible : '5:12' ou '1:32:37'."""
    if not secondes or secondes <= 0:
        return None
    s = round(secondes)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _arrondi(valeur: Any, decimales: int = 0) -> Any:
    if not isinstance(valeur, (int, float)):
        return None
    return round(valeur, decimales) if decimales else round(valeur)


def _sans_vides(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, {}, [])}


def _rythme(vitesse_ms: float | None, type_garmin: str) -> dict[str, Any]:
    """Vitesse en m/s -> allure min/km (course, marche), min/100 m (natation) ou km/h (reste)."""
    if not vitesse_ms or vitesse_ms <= 0:
        return {}
    if "swim" in type_garmin:
        return {"allure_100m": _mmss(100 / vitesse_ms)}
    if any(t in type_garmin for t in ("run", "walk", "hik")):
        return {"allure_km": _mmss(1000 / vitesse_ms)}
    return {"vitesse_kmh": round(vitesse_ms * 3.6, 1)}


def _resume_activite(a: dict[str, Any]) -> dict[str, Any]:
    type_garmin = (a.get("activityType") or {}).get("typeKey") or ""
    return _sans_vides(
        {
            "activite_id": a.get("activityId"),
            "date": a.get("startTimeLocal"),
            "nom": a.get("activityName"),
            "type": type_garmin,
            "distance_km": _arrondi((a.get("distance") or 0) / 1000, 2) or None,
            "duree": _mmss(a.get("duration")),
            **_rythme(a.get("averageSpeed"), type_garmin),
            "fc_moy": _arrondi(a.get("averageHR")),
            "fc_max": _arrondi(a.get("maxHR")),
            "puissance_moy_w": _arrondi(a.get("avgPower")),
            "puissance_normalisee_w": _arrondi(a.get("normPower")),
            "cadence_moy": _arrondi(
                a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute")
            ),
            "denivele_m": _arrondi(a.get("elevationGain")),
            "effet_aerobie": _arrondi(a.get("aerobicTrainingEffect"), 1),
            "effet_anaerobie": _arrondi(a.get("anaerobicTrainingEffect"), 1),
            "charge": _arrondi(a.get("activityTrainingLoad")),
            "vo2max": _arrondi(a.get("vO2MaxValue")),
        }
    )


def _tours(activite_id: int, type_garmin: str) -> list[dict[str, Any]]:
    tours = (_appel(lambda g: g.get_activity_splits(activite_id)) or {}).get("lapDTOs") or []
    return [
        _sans_vides(
            {
                "tour": t.get("lapIndex"),
                "phase": (t.get("intensityType") or "").lower() or None,
                "distance_m": _arrondi(t.get("distance")),
                "duree": _mmss(t.get("duration")),
                **_rythme(t.get("averageSpeed"), type_garmin),
                "fc_moy": _arrondi(t.get("averageHR")),
                "fc_max": _arrondi(t.get("maxHR")),
                "puissance_moy_w": _arrondi(t.get("averagePower")),
            }
        )
        for t in tours
    ]


@mcp.tool()
@_erreurs_lisibles
def dernieres_activites(
    jours: Annotated[int, Field(ge=1, le=90, description="Fenêtre en jours avant aujourd'hui")] = 14,
    sport: Annotated[
        Literal["course", "velo", "natation"] | None, Field(description="Filtre ; vide = tous les sports")
    ] = None,
    limite: Annotated[int, Field(ge=1, le=50)] = 20,
    avec_tours: Annotated[
        bool,
        Field(description="True = ajoute le détail tour par tour (allure et FC de chaque répétition) des 5 plus récentes"),
    ] = False,
) -> list[dict[str, Any]]:
    """Activités récentes enregistrées par la montre, de la plus récente à la plus ancienne :
    distance, durée, allure ou vitesse, FC, puissance, charge et effet d'entraînement.

    À lire avant de fixer les allures d'une séance. `avec_tours=True` montre ce qui a été
    réellement tenu sur chaque répétition d'un fractionné.
    """
    aujourdhui = _date.today()
    debut = (aujourdhui - timedelta(days=jours)).isoformat()
    type_garmin = TYPES_ACTIVITE[sport] if sport else None
    activites = _appel(lambda g: g.get_activities_by_date(debut, aujourdhui.isoformat(), type_garmin)) or []
    activites.sort(key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    resultat = [_resume_activite(a) for a in activites[:limite]]
    if avec_tours:
        for r in resultat[:5]:
            try:
                r["tours"] = _tours(r["activite_id"], r.get("type", ""))
            except Exception as exc:
                r["tours_indisponibles"] = f"{type(exc).__name__}: {exc}"
    return resultat


def _forme_vfc(g, jour: str) -> dict[str, Any]:
    v = (g.get_hrv_data(jour) or {}).get("hrvSummary") or {}
    base = v.get("baseline") or {}
    return {
        "statut": v.get("status"),
        "nuit_ms": v.get("lastNightAvg"),
        "moyenne_7j_ms": v.get("weeklyAvg"),
        "plage_normale_ms": f"{base['balancedLow']}-{base['balancedUpper']}" if base.get("balancedLow") else None,
    }


def _forme_sommeil(g, jour: str) -> dict[str, Any]:
    d = (g.get_sleep_data(jour) or {}).get("dailySleepDTO") or {}
    score = (d.get("sleepScores") or {}).get("overall") or {}
    return {
        "duree": _mmss(d.get("sleepTimeSeconds")),
        "score": score.get("value"),
        "qualite": score.get("qualifierKey"),
        "profond": _mmss(d.get("deepSleepSeconds")),
        "fc_moy": _arrondi(d.get("avgHeartRate")),
    }


def _forme_journee(g, jour: str) -> dict[str, Any]:
    s = g.get_stats(jour) or {}
    return {
        "body_battery": s.get("bodyBatteryMostRecentValue"),
        "body_battery_au_reveil": s.get("bodyBatteryAtWakeTime"),
        "fc_repos": s.get("restingHeartRate"),
        "fc_repos_moy_7j": s.get("lastSevenDaysAvgRestingHeartRate"),
        "stress_moyen": s.get("averageStressLevel"),
    }


def _forme_preparation(g, jour: str) -> dict[str, Any]:
    mesures = g.get_training_readiness(jour) or []
    p = mesures[0] if mesures else {}
    return {"score": p.get("score"), "niveau": p.get("level"), "recuperation_restante_min": p.get("recoveryTime")}


def _forme_entrainement(g, jour: str) -> dict[str, Any]:
    e = g.get_training_status(jour) or {}

    def principal(appareils: dict[str, Any] | None) -> dict[str, Any]:
        valeurs = list((appareils or {}).values())
        return next((v for v in valeurs if v.get("primaryTrainingDevice")), valeurs[0] if valeurs else {})

    statut = principal((e.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    charge = statut.get("acuteTrainingLoadDTO") or {}
    equilibre = principal((e.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap"))
    vo2 = e.get("mostRecentVO2Max") or {}
    return {
        "statut": statut.get("trainingStatusFeedbackPhrase"),
        "charge_aigue_7j": _arrondi(charge.get("dailyTrainingLoadAcute")),
        "charge_chronique_28j": _arrondi(charge.get("dailyTrainingLoadChronic")),
        "ratio_aigue_chronique": charge.get("dailyAcuteChronicWorkloadRatio"),
        "niveau_de_charge": charge.get("acwrStatus"),
        "equilibre_de_charge": equilibre.get("trainingBalanceFeedbackPhrase"),
        "vo2max_course": (vo2.get("generic") or {}).get("vo2MaxValue"),
        "vo2max_velo": (vo2.get("cycling") or {}).get("vo2MaxValue"),
    }


def _forme_seuil(g, jour: str) -> dict[str, Any]:
    seuil = g.get_lactate_threshold() or {}
    course = seuil.get("speed_and_heart_rate") or {}
    vitesse = course.get("speed")  # Garmin renvoie la vitesse en dixièmes de m/s
    return {
        "fc": course.get("heartRate"),
        "allure_km": _mmss(100 / vitesse) if vitesse else None,
        "mesure_le": (course.get("calendarDate") or "")[:10] or None,
        "puissance_w": (seuil.get("power") or {}).get("functionalThresholdPower"),
    }


def _forme_predictions(g, jour: str) -> dict[str, Any]:
    p = g.get_race_predictions() or {}
    return {
        "5_km": _mmss(p.get("time5K")),
        "10_km": _mmss(p.get("time10K")),
        "semi": _mmss(p.get("timeHalfMarathon")),
        "marathon": _mmss(p.get("timeMarathon")),
    }


RUBRIQUES_FORME = {
    "vfc": _forme_vfc,
    "sommeil": _forme_sommeil,
    "journee": _forme_journee,
    "preparation_entrainement": _forme_preparation,
    "entrainement": _forme_entrainement,
    "seuil_lactique": _forme_seuil,
    "predictions_course": _forme_predictions,
}


@mcp.tool()
@_erreurs_lisibles
def etat_de_forme(
    date: Annotated[str | None, Field(description="Jour AAAA-MM-JJ ; vide = aujourd'hui")] = None,
) -> dict[str, Any]:
    """État de forme du jour d'après Garmin : VFC nocturne, sommeil, Body Battery, FC de repos,
    charge d'entraînement (aiguë / chronique), VO2max, seuil lactique et prédictions de course.

    À lire avant de créer une séance pour décider de l'alléger (VFC basse, mauvais sommeil,
    charge élevée) ou de la durcir. Une rubrique absente de la montre est listée dans `indisponible`.
    """
    jour = date or _date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", jour):
        raise ValueError(f"Date illisible : {jour!r} (attendu AAAA-MM-JJ)")
    _date.fromisoformat(jour)
    resultat: dict[str, Any] = {"date": jour}
    indisponible: list[str] = []
    for nom, lire in RUBRIQUES_FORME.items():
        try:
            donnees = _sans_vides(_appel(lambda g: lire(g, jour)))
        except RuntimeError:
            raise  # pas connecté : inutile de continuer
        except Exception:
            donnees = {}
        if donnees:
            resultat[nom] = donnees
        else:
            indisponible.append(nom)
    if indisponible:
        resultat["indisponible"] = indisponible
    return resultat


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
