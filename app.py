
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(
    page_title="KAIROS LIVE V3.7",
    page_icon="🔴",
    layout="wide"
)

BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")

DAILY_BUDGET = 60
RESERVE = 5
CACHE_SECONDS = 300


# =====================================
# INITIALISATION
# =====================================

for k, default in {
    "running": False,
    "last_cycle": 0.0,
    "fixtures": [],
    "scanned": [],
    "odds_results": {},
    "manual": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = default


@st.cache_resource
def shared_state():
    import threading
    return {
        "lock": threading.RLock(),
        "day": "",
        "used": 0,
        "remaining": None,
        "cache": {},
    }


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def api_key():
    try:
        return str(
            st.secrets.get("API_FOOTBALL_KEY", "")
        ).strip()
    except Exception:
        return ""


def number(value, default=None):
    try:
        x = float(
            str(value).replace("%", "").replace(",", ".")
        )
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def budget_info():
    state = shared_state()
    with state["lock"]:
        if state["day"] != today():
            state["day"] = today()
            state["used"] = 0
            state["remaining"] = None

        return state["used"], state["remaining"]


# =====================================
# API AVEC PROTECTION DU QUOTA
# =====================================

def api(endpoint, fixture=None):
    key = api_key()

    if not key:
        return {
            "data": [],
            "error": "Clé API_FOOTBALL_KEY absente.",
            "http": None,
            "quota": None,
        }

    state = shared_state()
    token = (endpoint, fixture)
    now = time.monotonic()

    with state["lock"]:
        if state["day"] != today():
            state["day"] = today()
            state["used"] = 0
            state["remaining"] = None

        cached = state["cache"].get(token)

        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]

        if state["used"] >= DAILY_BUDGET:
            return {
                "data": [],
                "error": "Budget quotidien local atteint.",
                "http": None,
                "quota": state["remaining"],
            }

        remaining = number(state["remaining"])

        if remaining is not None and remaining <= RESERVE:
            return {
                "data": [],
                "error": "Réserve de quota atteinte.",
                "http": None,
                "quota": state["remaining"],
            }

        state["used"] += 1

        params = (
            {"live": "all"}
            if endpoint == "fixtures"
            else {"fixture": fixture}
        )

        try:
            response = requests.get(
                f"{BASE}/{endpoint}",
                headers={"x-apisports-key": key},
                params=params,
                timeout=22,
            )

            quota = response.headers.get(
                "x-ratelimit-requests-remaining"
            )

            response.raise_for_status()
            payload = response.json()

            result = {
                "data": payload.get("response") or [],
                "error": (
                    str(payload["errors"])
                    if payload.get("errors")
                    else ""
                ),
                "http": response.status_code,
                "quota": quota,
            }

            if quota is not None:
                state["remaining"] = quota

        except (requests.RequestException, ValueError) as exc:
            result = {
                "data": [],
                "error": str(exc),
                "http": None,
                "quota": state["remaining"],
            }

        state["cache"][token] = (now, result)
        return result


# =====================================
# MATCHS ET STATISTIQUES
# =====================================

def fixture_id(f):
    return (f.get("fixture") or {}).get("id")


def minute(f):
    status = (
        (f.get("fixture") or {}).get("status") or {}
    )
    return int(number(status.get("elapsed"), 0))


def match_name(f):
    teams = f.get("teams") or {}
    home = (teams.get("home") or {}).get("name", "?")
    away = (teams.get("away") or {}).get("name", "?")
    return f"{home} vs {away}"


def score(f):
    goals = f.get("goals") or {}
    home = goals.get("home")
    away = goals.get("away")

    return (
        f"{home if home is not None else '?'}-"
        f"{away if away is not None else '?'}"
    )


def statistics(data):
    totals = {
        "shots on goal": 0,
        "total shots": 0,
        "corner kicks": 0,
    }

    available = set()

    for team in data or []:
        for item in team.get("statistics") or []:
            field = str(
                item.get("type", "")
            ).lower().strip()

            if field in totals:
                value = number(item.get("value"))

                if value is not None:
                    totals[field] += max(0, value)
                    available.add(field)

    return totals, available


# =====================================
# RECONNAISSANCE DES MARCHÉS
# =====================================

def market_type(name):
    n = re.sub(
        r"\s+",
        " ",
        str(name).lower().replace("-", " ")
    ).strip()

    excluded = (
        "half", "1st", "2nd", "first",
        "second", "team", "home", "away",
        "asian", "race", "exact",
        "odd/even", "3 way", "3way",
        "period",
    )

    if any(word in n for word in excluded):
        return None

    corner_markets = {
        "total corners",
        "match corners",
        "corners over/under",
        "corner kicks over/under",
        "corners",
        "total corner kicks",
    }

    goal_markets = {
        "goals over/under",
        "match goals",
        "over/under",
        "total goals",
        "over/under line",
        "goals over under",
    }

    if n in corner_markets:
        return "Corners"

    if n in goal_markets:
        return "Buts"

    return None


def parse_odds(raw):
    picks = []
    names = set()
    bookmakers = 0

    for block in raw or []:
        for book in block.get("bookmakers") or []:
            bookmakers += 1

            for market in book.get("bets") or []:
                market_name = str(
                    market.get("name") or ""
                )

                names.add(market_name)
                kind = market_type(market_name)

                if not kind:
                    continue

                for item in market.get("values") or []:
                    label = str(
                        item.get("value") or ""
                    ).strip()

                    match = re.fullmatch(
                        r"(Over|Under)\s+(\d+(?:[.,]\d+)?)",
                        label,
                        re.I,
                    )

                    odd = number(item.get("odd"))

                    if (
                        not match
                        or odd is None
                        or odd <= 1
                    ):
                        continue

                    picks.append({
                        "kind": kind,
                        "side": match.group(1).title(),
                        "line": float(
                            match.group(2).replace(",", ".")
                        ),
                        "odd": odd,
                        "bookmaker": (
                            book.get("name") or "Inconnu"
                        ),
                        "market": market_name,
                    })

    return picks, sorted(names), bookmakers


# =====================================
# PROGRESSION DES PICKS
# =====================================

def pick_progress(p, f, raw_stats):
    if p["kind"] == "Buts":
        goals = f.get("goals") or {}

        if (
            goals.get("home") is None
            or goals.get("away") is None
        ):
            return "Score indisponible"

        current = (
            int(goals["home"])
            + int(goals["away"])
        )

    else:
        totals, available = statistics(raw_stats)

        if "corner kicks" not in available:
            return "Corners indisponibles"

        current = int(totals["corner kicks"])

    threshold = math.floor(p["line"]) + 1

    if p["side"] == "Over":
        missing = max(0, threshold - current)

        if missing == 0:
            return f"{current} actuellement · seuil atteint"

        return (
            f"{current} actuellement · "
            f"{missing} supplémentaire(s) nécessaire(s)"
        )

    if current >= threshold:
        return (
            f"{current} actuellement · "
            "seuil Under dépassé"
        )

    return (
        f"{current} actuellement · "
        f"{threshold - current} avant dépassement"
    )


# =====================================
# AFFICHAGE D'UN MATCH
# =====================================

def show_match(f, stats_result, origin):
    fid = fixture_id(f)

    with st.container(border=True):
        st.subheader(
            f"{match_name(f)} | "
            f"{minute(f)}' | {score(f)}"
        )

        st.caption(
            f"{(f.get('league') or {}).get('name', '')}"
            f" · ID {fid}"
        )

        if stats_result.get("error"):
            st.warning(
                "Statistiques : "
                + stats_result["error"]
            )

        raw_stats = stats_result.get("data") or []
        totals, available = statistics(raw_stats)

        cols = st.columns(3)

        fields = (
            ("shots on goal", "Tirs cadrés"),
            ("total shots", "Tirs"),
            ("corner kicks", "Corners"),
        )

        for col, (field, title) in zip(cols, fields):
            col.metric(
                title,
                (
                    int(totals[field])
                    if field in available
                    else "N/D"
                ),
            )

        if st.button(
            "💰 Vérifier les cotes live",
            key=f"odds_{origin}_{fid}",
        ):
            st.session_state.odds_results[str(fid)] = api(
                "odds/live",
                fid,
            )

        result = st.session_state.odds_results.get(
            str(fid)
        )

        if result is None:
            st.caption(
                "Cotes non demandées : "
                "aucun appel de cotes effectué."
            )
            return

        if result["error"]:
            st.error(
                "Diagnostic API : "
                + result["error"]
            )

        picks, markets, books = parse_odds(
            result["data"]
        )

        st.caption(
            f"HTTP : {result['http'] or 'N/D'} · "
            f"Événements : {len(result['data'])} · "
            f"Bookmakers : {books} · "
            f"Marchés : {len(markets)} · "
            f"Sélections : {len(picks)}"
        )

        if not result["data"]:
            st.warning(
                "Aucune cote fournie par l'API "
                "pour ce match à cet instant."
            )

        elif not picks:
            st.warning(
                "Des données ont été reçues, "
                "mais aucun Over/Under de match "
                "complet n'a été reconnu."
            )

        for kind, emoji in (
            ("Buts", "⚽"),
            ("Corners", "🚩"),
        ):
            group = [
                p for p in picks
                if p["kind"] == kind
            ]

            with st.expander(
                f"{emoji} TOTAL {kind.upper()} "
                f"— {len(group)} sélection(s)"
            ):
                for p in group:
                    st.markdown(
                        f"**{emoji} {p['side'].upper()} "
                        f"{p['line']:g} {kind.upper()} "
                        f"@ {p['odd']:.2f}**"
                    )

                    st.caption(
                        f"{p['bookmaker']} · "
                        f"{pick_progress(p, f, raw_stats)}"
                    )

                    st.divider()

        with st.expander(
            "🔧 Diagnostic des marchés reçus"
        ):
            if markets:
                st.code(
                    "\n".join(markets),
                    language="text",
                )
            else:
                st.info("Aucun marché reçu.")

            st.caption(
                "Ce diagnostic ne déclenche "
                "aucun appel API supplémentaire."
            )

        st.info(
            "Les cotes affichées sont des marchés "
            "à examiner, pas des picks dont "
            "la valeur a été démontrée."
        )


# =====================================
# SCANNER AUTOMATIQUE
# =====================================

def scan_cycle():
    result = api("fixtures")

    if result["error"]:
        st.error(result["error"])
        return

    st.session_state.fixtures = result["data"]

    eligible = [
        f for f in result["data"]
        if low_minute <= minute(f) <= high_minute
    ]

    st.session_state.scanned = []

    for f in eligible[:match_limit]:
        stats_result = api(
            "fixtures/statistics",
            fixture_id(f),
        )

        st.session_state.scanned.append(
            (f, stats_result)
        )

    st.session_state.last_cycle = time.time()


# =====================================
# INTERFACE
# =====================================

st.title("🔴 KAIROS LIVE V3.7")

st.caption(
    "Soccer live · Scanner automatique · "
    "API-Football · Heure du Québec"
)

used, remaining = budget_info()

with st.sidebar:
    st.header("⚙️ AUTO SCANNER")

    st.session_state.running = st.toggle(
        "Activer le scanner automatique",
        value=st.session_state.running,
    )

    interval = st.selectbox(
        "Intervalle entre les scans",
        [5, 10, 15],
        index=1,
        format_func=lambda x: f"{x} minutes",
    )

    match_limit = st.slider(
        "Matchs maximum par cycle",
        1, 2, 2,
    )

    low_minute, high_minute = st.slider(
        "Minutes du match",
        0, 120, (20, 85),
    )

    st.divider()

    st.metric(
        "Appels comptabilisés aujourd'hui",
        used,
    )

    st.metric(
        "Quota restant annoncé par l'API",
        remaining if remaining is not None else "Inconnu",
    )

    st.caption(
        "Budget local : 60 appels par jour. "
        "Réserve : 5 appels."
    )

    st.warning(
        "Le compteur local peut se réinitialiser "
        "au redémarrage du serveur. "
        "Le quota officiel reste celui d'API-Football."
    )


auto, manual, diagnostic = st.tabs([
    "📡 Automatique",
    "🔎 Manuel",
    "ℹ️ Diagnostic",
])


with auto:
    st.subheader("Scanner les matchs live")

    st.caption(
        "Maximum 3 appels hors cache par cycle : "
        "1 liste live et 2 statistiques. "
        "Les cotes sont chargées séparément."
    )

    if st.button(
        "▶️ SCANNER MAINTENANT",
        type="primary",
        key="scan_now",
    ):
        scan_cycle()

    if st.session_state.running:

        @st.fragment(run_every=f"{interval}m")
        def auto_refresh():
            elapsed = (
                time.time()
                - st.session_state.last_cycle
            )

            if (
                st.session_state.running
                and elapsed >= interval * 60
            ):
                scan_cycle()

            if st.session_state.last_cycle:
                last = datetime.fromtimestamp(
                    st.session_state.last_cycle,
                    TZ,
                ).strftime("%H:%M:%S")

                st.caption(
                    f"Dernier cycle automatique : {last}"
                )

        auto_refresh()

    for i, (f, stats_result) in enumerate(
        st.session_state.scanned
    ):
        show_match(
            f,
            stats_result,
            f"auto_{i}",
        )


with manual:
    st.subheader("Analyser un match précis")

    if st.button(
        "📋 Charger les matchs live",
        key="load_fixtures",
    ):
        result = api("fixtures")

        if result["error"]:
            st.error(result["error"])
        else:
            st.session_state.fixtures = result["data"]

    choices = {
        (
            f"{match_name(f)} | "
            f"{minute(f)}' | "
            f"ID {fixture_id(f)}"
        ): f
        for f in st.session_state.fixtures
    }

    if choices:
        selected = st.selectbox(
            "Choisir un match",
            list(choices),
            key="manual_choice",
        )

        if st.button(
            "🔎 ANALYSER CE MATCH",
            type="primary",
            key="manual_analyze",
        ):
            f = choices[selected]

            stats_result = api(
                "fixtures/statistics",
                fixture_id(f),
            )

            st.session_state.manual = (
                f,
                stats_result,
            )

    if st.session_state.manual:
        f, stats_result = st.session_state.manual

        show_match(
            f,
            stats_result,
            "manual",
        )


with diagnostic:
    st.subheader("Diagnostic API-Football")

    st.write(
        "Cette version distingue les erreurs API, "
        "les réponses vides, les marchés non reconnus "
        "et les sélections Over/Under exploitables."
    )

    st.write(
        "Les cotes ne sont récupérées que lorsque "
        "tu cliques sur « Vérifier les cotes live »."
    )

    st.write(
        "Le scanner automatique fonctionne pendant "
        "que l'application est ouverte et active. "
        "Il ne garantit pas une surveillance continue "
        "lorsque Streamlit met l'application en veille."
    )

    st.warning(
        "Les statistiques ne suffisent pas à "
        "calculer une EV fiable. Aucun xG officiel "
        "ni pick rentable garanti n'est produit "
        "dans cette version."
    )
