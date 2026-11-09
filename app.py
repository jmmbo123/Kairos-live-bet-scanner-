
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st

BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")

st.set_page_config(
    page_title="KAIROS LIVE V3.5",
    page_icon="🔴",
    layout="wide",
)

# =====================================
# SESSION
# =====================================

if "calls" not in st.session_state:
    st.session_state.calls = 0

if "remaining" not in st.session_state:
    st.session_state.remaining = None

if "results" not in st.session_state:
    st.session_state.results = []

if "live" not in st.session_state:
    st.session_state.live = []

if "last_scan" not in st.session_state:
    st.session_state.last_scan = None


# =====================================
# API + CACHE
# =====================================

@st.cache_data(ttl=180, show_spinner=False)
def api_get(endpoint, fixture=None, api_key=""):
    params = {"fixture": fixture} if fixture is not None else {}

    if endpoint == "fixtures":
        params = {"live": "all"}

    try:
        r = requests.get(
            f"{BASE}/{endpoint}",
            headers={"x-apisports-key": api_key},
            params=params,
            timeout=18,
        )
        r.raise_for_status()

        data = r.json()
        remaining = r.headers.get(
            "x-ratelimit-requests-remaining"
        )

        if data.get("errors"):
            return {
                "error": str(data["errors"]),
                "response": [],
                "remaining": remaining,
            }

        return {
            "response": data.get("response", []),
            "error": None,
            "remaining": remaining,
        }

    except (requests.RequestException, ValueError) as exc:
        return {
            "response": [],
            "error": str(exc),
            "remaining": None,
        }


def request_data(endpoint, fixture=None):
    key = st.secrets.get("API_FOOTBALL_KEY", "")

    if not key:
        return {
            "response": [],
            "error": "Clé API_FOOTBALL_KEY absente dans Streamlit Secrets.",
            "remaining": None,
        }

    result = api_get(
        endpoint=endpoint,
        fixture=fixture,
        api_key=key,
    )

    # Compteur conservateur : inclut les accès au cache.
    st.session_state.calls += 1

    if result["remaining"] is not None:
        st.session_state.remaining = result["remaining"]

    return result


# =====================================
# OUTILS
# =====================================

def num(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fixture_name(f):
    home = f.get("teams", {}).get("home", {}).get("name", "?")
    away = f.get("teams", {}).get("away", {}).get("name", "?")
    return f"{home} vs {away}"


def minute(f):
    return int(
        num(
            f.get("fixture", {})
            .get("status", {})
            .get("elapsed")
        )
    )


def stats_summary(items):
    out = {}

    for team in items or []:
        for item in team.get("statistics") or []:
            label = str(item.get("type", "")).lower()

            if label in (
                "shots on goal",
                "total shots",
                "corner kicks",
            ):
                out[label] = (
                    out.get(label, 0)
                    + num(item.get("value"))
                )

    return out


def score_goals(f):
    goals = f.get("goals") or {}

    return (
        int(num(goals.get("home")))
        + int(num(goals.get("away")))
    )


# =====================================
# MARCHÉS OVER / UNDER
# =====================================

def parse_market(market, home, away):
    name = str(market.get("name", "")).strip()
    low = name.lower()

    # Exclure les marchés hors match complet.
    excluded = (
        "1st half",
        "2nd half",
        "first half",
        "second half",
        "home",
        "away",
        "team",
        "asian",
        "race",
        "odd/even",
        "exact",
        "3way",
        "3-way",
    )

    if any(x in low for x in excluded):
        return None

    if low in (
        "goals over/under",
        "match goals",
        "over/under",
        "total goals",
        "over/under line",
    ):
        kind = "⚽ Buts"

    elif low in (
        "total corners",
        "match corners",
        "corners over/under",
        "corner kicks over/under",
        "corners",
    ):
        kind = "🚩 Corners"

    else:
        return None

    picks = []

    for val in market.get("values") or []:
        label = str(val.get("value", "")).strip()

        match = re.fullmatch(
            r"(Over|Under)\s+(\d+(?:\.\d+)?)",
            label,
            re.I,
        )

        if not match:
            continue

        odd = num(val.get("odd"))

        if odd <= 1 or not math.isfinite(odd):
            continue

        picks.append({
            "kind": kind,
            "pick": (
                f"{match.group(1).title()} "
                f"{match.group(2)}"
            ),
            "odd": odd,
            "market": name,
        })

    return picks


def parse_odds(response, f):
    if not response:
        return (
            [],
            0,
            "Aucun marché renvoyé par API-Football.",
        )

    home = (
        f.get("teams", {})
        .get("home", {})
        .get("name", "")
    )

    away = (
        f.get("teams", {})
        .get("away", {})
        .get("name", "")
    )

    markets = 0
    picks = []

    for block in response:
        for bookmaker in block.get("bookmakers") or []:
            for market in bookmaker.get("bets") or []:
                markets += 1

                parsed = parse_market(
                    market,
                    home,
                    away,
                )

                if parsed:
                    for p in parsed:
                        p["bookmaker"] = bookmaker.get(
                            "name",
                            "Inconnu",
                        )
                        picks.append(p)

    if markets == 0:
        return (
            [],
            0,
            "Réponse API présente, mais aucun marché disponible.",
        )

    if not picks:
        return (
            [],
            markets,
            "Marchés reçus, mais aucun Over/Under "
            "match complet reconnu avec ligne explicite.",
        )

    return picks, markets, None


# =====================================
# AFFICHAGE DES MATCHS
# =====================================

def fixture_card(
    f,
    stats=None,
    odds=None,
    markets=None,
    note=None,
):
    fid = f.get("fixture", {}).get("id", "?")
    title = fixture_name(f)
    goals = f.get("goals") or {}

    st.subheader(
        f"{title} | {minute(f)}' | "
        f"{goals.get('home', 0)}-"
        f"{goals.get('away', 0)}"
    )

    st.caption(
        f"ID {fid} · "
        f"{f.get('league', {}).get('name', '')}"
    )

    if stats is not None:
        summary = stats_summary(stats)

        a, b, c = st.columns(3)

        a.metric(
            "Tirs cadrés (total)",
            int(summary.get("shots on goal", 0)),
        )

        b.metric(
            "Tirs (total)",
            int(summary.get("total shots", 0)),
        )

        c.metric(
            "Corners (total)",
            int(summary.get("corner kicks", 0)),
        )

    if note:
        st.info(note)

    if odds is not None:
        st.caption(
            f"Marchés reçus : {markets or 0} | "
            f"Sélections reconnues : {len(odds)}"
        )

        if odds:
            for kind in ("⚽ Buts", "🚩 Corners"):
                selection = [
                    p for p in odds
                    if p["kind"] == kind
                ]

                with st.expander(
                    f"{kind} — {len(selection)} cotes"
                ):
                    for p in sorted(
                        selection,
                        key=lambda x: (
                            x["pick"],
                            x["bookmaker"],
                        ),
                    ):
                        label = kind.split(" ", 1)[1]

                        st.write(
                            f"**{p['pick']} {label} "
                            f"@ {p['odd']:.2f}** · "
                            f"{p['bookmaker']} · "
                            f"{p['market']}"
                        )

            st.warning(
                "Aucun pick validé automatiquement : "
                "une cote seule ne démontre pas "
                "une valeur positive."
            )

        else:
            st.warning(
                "Aucun pick vérifiable pour ce match."
            )


# =====================================
# DONNÉES DE SIMULATION
# =====================================

DEMO = [
    {
        "fixture": {
            "id": 1001,
            "status": {"elapsed": 37},
        },
        "teams": {
            "home": {"name": "KAIROS Démo A"},
            "away": {"name": "KAIROS Démo B"},
        },
        "goals": {
            "home": 1,
            "away": 0,
        },
        "league": {"name": "Simulation"},
    },
    {
        "fixture": {
            "id": 1002,
            "status": {"elapsed": 62},
        },
        "teams": {
            "home": {"name": "KAIROS Démo C"},
            "away": {"name": "KAIROS Démo D"},
        },
        "goals": {
            "home": 1,
            "away": 1,
        },
        "league": {"name": "Simulation"},
    },
]

DEMO_ODDS = [
    {
        "bookmakers": [
            {
                "name": "Bookmaker fictif",
                "bets": [
                    {
                        "name": "Goals Over/Under",
                        "values": [
                            {
                                "value": "Over 2.5",
                                "odd": "1.85",
                            },
                            {
                                "value": "Under 2.5",
                                "odd": "1.95",
                            },
                        ],
                    },
                    {
                        "name": "Total Corners",
                        "values": [
                            {
                                "value": "Over 8.5",
                                "odd": "1.80",
                            },
                            {
                                "value": "Under 8.5",
                                "odd": "2.00",
                            },
                        ],
                    },
                ],
            }
        ]
    }
]


# =====================================
# INTERFACE
# =====================================

st.title("🔴 KAIROS LIVE V3.5")

st.caption(
    "Football live · Over/Under buts et corners · "
    "Québec · Requêtes manuelles uniquement"
)

with st.sidebar:
    st.header("⚙️ Paramètres")

    demo = st.toggle(
        "Mode démo — 0 requête API",
        value=True,
    )

    limit = st.slider(
        "Matchs maximum par scan",
        1,
        10,
        3,
    )

    min_minute, max_minute = st.slider(
        "Minutes de match",
        0,
        120,
        (20, 85),
    )

    st.metric(
        "Appels déclenchés dans cette session "
        "(estimation haute)",
        st.session_state.calls,
    )

    st.write(
        "Quota restant annoncé :",
        (
            st.session_state.remaining
            if st.session_state.remaining is not None
            else "Inconnu"
        ),
    )

    st.caption(
        "Le compteur local n'est pas le quota officiel. "
        "Les données sont mises en cache 180 secondes."
    )

    if st.button(
        "Effacer le cache",
        help=(
            "Efface les réponses mémorisées. "
            "Le prochain scan pourra utiliser le quota API."
        ),
    ):
        api_get.clear()
        st.success("Cache vidé.")


auto, manual, guide = st.tabs([
    "📡 Automatique",
    "🔎 Manuel",
    "ℹ️ Diagnostic",
])


# =====================================
# MODE AUTOMATIQUE
# =====================================

with auto:
    st.write(
        "Un scan charge la liste live et les statistiques "
        "de quelques matchs. Les cotes ne sont chargées "
        "que sur demande."
    )

    if st.button(
        "🔄 LANCER LE SCAN",
        type="primary",
        key="scan",
    ):
        if demo:
            live = DEMO
            error = None
        else:
            res = request_data("fixtures")
            live = res["response"]
            error = res["error"]

        if error:
            st.error(error)

        else:
            candidates = [
                f for f in live
                if min_minute <= minute(f) <= max_minute
            ]

            st.session_state.live = live
            st.session_state.results = []

            for f in candidates[:limit]:
                if demo:
                    stat = [
                        {
                            "statistics": [
                                {
                                    "type": "Shots on Goal",
                                    "value": 3,
                                },
                                {
                                    "type": "Total Shots",
                                    "value": 8,
                                },
                                {
                                    "type": "Corner Kicks",
                                    "value": 4,
                                },
                            ]
                        }
                    ]
                    err = None

                else:
                    sr = request_data(
                        "fixtures/statistics",
                        f["fixture"]["id"],
                    )

                    stat = sr["response"]
                    err = sr["error"]

                st.session_state.results.append(
                    (f, stat, err)
                )

            st.session_state.last_scan = (
                datetime.now(TZ).strftime("%H:%M:%S")
            )

    if st.session_state.last_scan:
        st.caption(
            "Dernier scan : "
            + st.session_state.last_scan
            + " (heure du Québec)"
        )

    if not st.session_state.results:
        st.info(
            "Aucun résultat chargé. "
            "Clique sur LANCER LE SCAN."
        )

    for i, (f, stats, err) in enumerate(
        st.session_state.results
    ):
        fid = f["fixture"]["id"]

        with st.container(border=True):
            fixture_card(
                f,
                stats,
                note=err,
            )

            if st.button(
                "💰 Charger les cotes live",
                key=f"auto_odds_{fid}_{i}",
            ):
                if demo:
                    odds, markets, note = parse_odds(
                        DEMO_ODDS,
                        f,
                    )

                else:
                    response = request_data(
                        "odds/live",
                        fid,
                    )

                    odds, markets, note = parse_odds(
                        response["response"],
                        f,
                    )

                    if response["error"]:
                        note = response["error"]

                st.session_state[f"odds_{fid}"] = (
                    odds,
                    markets,
                    note,
                )

            saved = st.session_state.get(
                f"odds_{fid}"
            )

            if saved:
                odds, markets, note = saved

                st.caption(
                    f"Marchés reçus : {markets} | "
                    f"Sélections compatibles : {len(odds)}"
                )

                if note:
                    st.warning(note)

                for kind in ("⚽ Buts", "🚩 Corners"):
                    selected = [
                        p for p in odds
                        if p["kind"] == kind
                    ]

                    with st.expander(
                        f"{kind} ({len(selected)})"
                    ):
                        for p in selected:
                            label = kind.split(" ", 1)[1]

                            st.write(
                                f"{p['pick']} {label} "
                                f"@ {p['odd']:.2f} · "
                                f"{p['bookmaker']}"
                            )

                if odds:
                    st.info(
                        "Lignes et cotes observées, "
                        "pas des picks validés."
                    )


# =====================================
# MODE MANUEL
# =====================================

with manual:
    if st.button(
        "📋 Charger la liste des matchs",
        key="manual_list",
    ):
        if demo:
            st.session_state.live = DEMO

        else:
            res = request_data("fixtures")

            if res["error"]:
                st.error(res["error"])
            else:
                st.session_state.live = res["response"]

    live = st.session_state.live

    if live:
        choices = {
            (
                f"{fixture_name(f)} | "
                f"{minute(f)}' | "
                f"ID {f['fixture']['id']}"
            ): f
            for f in live
        }

        chosen = st.selectbox(
            "Choisir un match",
            list(choices),
        )

        f = choices[chosen]

        if st.button(
            "🔎 ANALYSER CE MATCH",
            key="manual_stats",
        ):
            if demo:
                stat = []
                err = None

            else:
                sr = request_data(
                    "fixtures/statistics",
                    f["fixture"]["id"],
                )

                stat = sr["response"]
                err = sr["error"]

            st.session_state["manual_result"] = (
                f,
                stat,
                err,
            )

    if st.session_state.get("manual_result"):
        f, stat, err = st.session_state.manual_result
        fid = f["fixture"]["id"]

        with st.container(border=True):
            fixture_card(
                f,
                stat,
                note=err,
            )

            if st.button(
                "💰 Charger les cotes de ce match",
                key="manual_odds",
            ):
                if demo:
                    odds, markets, note = parse_odds(
                        DEMO_ODDS,
                        f,
                    )

                else:
                    res = request_data(
                        "odds/live",
                        fid,
                    )

                    odds, markets, note = parse_odds(
                        res["response"],
                        f,
                    )

                    if res["error"]:
                        note = res["error"]

                st.session_state[
                    "manual_odds_result"
                ] = (
                    fid,
                    odds,
                    markets,
                    note,
                )

            record = st.session_state.get(
                "manual_odds_result"
            )

            if record and record[0] == fid:
                _, odds, markets, note = record

                st.caption(
                    f"Marchés API : {markets} | "
                    f"Sélections : {len(odds)}"
                )

                if note:
                    st.warning(note)

                for p in odds:
                    st.write(
                        f"{p['kind']} · "
                        f"**{p['pick']} @ {p['odd']:.2f}** "
                        f"· {p['bookmaker']}"
                    )

                if odds:
                    st.info(
                        "Cotes affichées à titre informatif, "
                        "pas de valeur prédictive validée."
                    )


# =====================================
# DIAGNOSTIC
# =====================================

with guide:
    st.write(
        "**0 marché** : le fournisseur ne renvoie "
        "pas de cotes pour le match."
    )

    st.write(
        "**Marchés reçus mais 0 sélection** : "
        "les noms ou formats ne correspondent pas "
        "aux marchés de match complet acceptés."
    )

    st.write(
        "**Sélections disponibles** : "
        "affichage de la ligne et de la cote, "
        "sans inventer de probabilité de gain."
    )

    st.write(
        "La simulation utilise des matchs et "
        "des cotes fictifs."
    )

    st.write(
        "Les requêtes réelles sont uniquement "
        "déclenchées par les boutons."
    )

    st.write(
        "Le cache de 180 secondes évite certains "
        "appels répétés, mais le compteur local "
        "reste une estimation haute."
    )

    st.warning(
        "Ne jamais interpréter une cote ou un "
        "score d'activité comme une garantie "
        "de rentabilité."
    )
