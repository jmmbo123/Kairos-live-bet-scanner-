
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title="KAIROS LIVE V3.4", page_icon="🔴", layout="wide")
BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")


def key():
    try:
        return str(st.secrets.get("API_FOOTBALL_KEY", "")).strip()
    except Exception:
        return ""


@st.cache_data(ttl=90, show_spinner=False)
def api_call(endpoint, params, api_key):
    try:
        r = requests.get(
            BASE + endpoint,
            params=dict(params),
            headers={"x-apisports-key": api_key},
            timeout=25,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            return {
                "items": [],
                "error": str(body["errors"]),
                "quota": r.headers.get("x-ratelimit-requests-remaining"),
            }
        return {
            "items": body.get("response") or [],
            "error": "",
            "quota": r.headers.get("x-ratelimit-requests-remaining"),
        }
    except (requests.RequestException, ValueError) as e:
        return {"items": [], "error": str(e), "quota": None}


def fetch(endpoint, **kwargs):
    params = tuple(sorted((k, str(v)) for k, v in kwargs.items()))
    signature = (endpoint, params)

    if signature not in st.session_state.seen:
        if st.session_state.calls >= st.session_state.budget:
            return {
                "items": [],
                "error": "Budget local atteint.",
                "quota": None,
            }
        st.session_state.seen.add(signature)
        st.session_state.calls += 1

    result = api_call(endpoint, params, key())

    if result["quota"] is not None:
        st.session_state.quota = result["quota"]

    return result


def numeric(value):
    try:
        return float(str(value).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def info(match):
    fixture = match.get("fixture") or {}
    teams = match.get("teams") or {}
    goals = match.get("goals") or {}
    status = fixture.get("status") or {}

    home = (teams.get("home") or {}).get("name", "Domicile")
    away = (teams.get("away") or {}).get("name", "Extérieur")

    h, a = goals.get("home"), goals.get("away")

    return {
        "id": fixture.get("id"),
        "name": f"{home} vs {away}",
        "minute": int(numeric(status.get("elapsed")) or 0),
        "score": f"{h if h is not None else '?'}-{a if a is not None else '?'}",
        "goals": h + a if isinstance(h, int) and isinstance(a, int) else None,
        "league": (match.get("league") or {}).get("name", ""),
    }


def stats(fixture_id):
    result = fetch("/fixtures/statistics", fixture=fixture_id)

    if result["error"] or len(result["items"]) < 2:
        return None, result["error"] or "Statistiques indisponibles"

    rows = [
        (x.get("statistics") or [])
        for x in result["items"][:2]
    ]

    def extract(row, name):
        for item in row:
            if str(item.get("type", "")).strip().lower() == name:
                return numeric(item.get("value"))
        return None

    output = {}

    for name, label in [
        ("corner kicks", "corners"),
        ("shots on goal", "target"),
        ("total shots", "shots"),
    ]:
        values = [extract(row, name) for row in rows]

        output[label] = (
            tuple(int(v) for v in values)
            if all(v is not None for v in values)
            else None
        )

    return output, ""


def market_kind(name):
    n = re.sub(
        r"\s+", " ", name.lower().replace("-", " ")
    ).strip()

    excluded = (
        "half", "1h", "2h", "period", "team",
        "home", "away", "asian", "race",
        "exact", "odd/even", "odd even",
        "first", "second",
    )

    if any(word in n for word in excluded):
        return None

    if "corner" in n and any(
        word in n
        for word in (
            "over", "under", "total",
            "match corner", "corner kicks",
        )
    ):
        return "corners"

    if "corner" in n:
        return None

    if any(
        word in n
        for word in (
            "over/under", "over under",
            "match goals", "total goals",
            "goals over", "goal line",
            "over/under line",
        )
    ):
        return "buts"

    return None


def parse_selection(value, market):
    raw = str(
        value.get("value")
        or value.get("label")
        or value.get("name")
        or ""
    )

    m = re.search(
        r"\b(over|under)\s*[:=\-]?\s*(\d+(?:[.,]\d+)?)\b",
        raw,
        re.I,
    )

    if m:
        side = m.group(1).title()
        line = numeric(m.group(2))
    else:
        side_match = re.search(
            r"\b(over|under)\b", raw, re.I
        )
        side = (
            side_match.group(1).title()
            if side_match else None
        )

        line = numeric(
            value.get("handicap") or value.get("line")
        )

        if line is None:
            line_match = re.search(
                r"\b(\d+(?:[.,]\d+)?)\b", market
            )
            line = (
                numeric(line_match.group(1))
                if line_match else None
            )

    odd = numeric(
        value.get("odd") or value.get("odds")
    )

    if (
        side is None
        or line is None
        or odd is None
        or odd <= 1
    ):
        return None

    return side, line, odd


def odds(fixture_id):
    result = fetch("/odds/live", fixture=fixture_id)

    if result["error"]:
        return [], [], result["error"]

    found = []
    names = set()

    for entry in result["items"]:
        for bookmaker in entry.get("bookmakers") or []:
            book = bookmaker.get(
                "name", "Source inconnue"
            )

            for bet in bookmaker.get("bets") or []:
                name = str(bet.get("name") or "")
                names.add(name)

                kind = market_kind(name)

                if not kind:
                    continue

                for value in bet.get("values") or []:
                    parsed = parse_selection(value, name)

                    if parsed:
                        side, line, odd = parsed

                        found.append({
                            "kind": kind,
                            "side": side,
                            "line": line,
                            "odd": odd,
                            "book": book,
                            "market": name,
                        })

    return found, sorted(names), ""


def status_text(selection, current):
    if current is None:
        return "Progression indisponible"

    threshold = math.floor(selection["line"]) + 1

    if selection["side"] == "Over":
        missing = max(0, threshold - current)

        if missing:
            return (
                f"{current} actuellement · "
                f"{missing} encore nécessaire(s)"
            )

        return (
            f"{current} actuellement · "
            "seuil déjà atteint"
        )

    if current >= threshold:
        return (
            f"{current} actuellement · "
            "seuil dépassé : Under perdant "
            "si total match complet"
        )

    return (
        f"{current} actuellement · "
        f"{threshold - current} supplémentaire(s) "
        "avant que l'Under soit dépassé"
    )


def show_match(match, origin):
    data = info(match)
    fid = data["id"]

    if fid is None:
        return

    with st.container(border=True):
        st.subheader(
            f"{data['name']} | "
            f"{data['minute']}' | "
            f"{data['score']}"
        )

        st.caption(
            f"{data['league']} · ID {fid}"
        )

        s, error = stats(fid)

        if error:
            st.warning(error)
            return

        corners = (
            sum(s["corners"])
            if s["corners"] is not None
            else None
        )

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "⚽ Buts",
            data["goals"]
            if data["goals"] is not None
            else "N/D",
        )

        c2.metric(
            "🚩 Corners",
            corners
            if corners is not None
            else "N/D",
        )

        c3.metric(
            "Tirs cadrés",
            " - ".join(map(str, s["target"]))
            if s["target"] else "N/D",
        )

        button_key = f"load_{origin}_{fid}"

        if st.button(
            "💰 Charger les cotes live",
            key=button_key,
        ):
            st.session_state[f"loaded_{fid}"] = True

        if not st.session_state.get(f"loaded_{fid}"):
            st.caption(
                "Les cotes ne sont demandées "
                "qu'après avoir appuyé sur le bouton."
            )
            return

        selections, names, odds_error = odds(fid)

        if odds_error:
            st.warning(odds_error)
            return

        markets = (
            ("buts", "⚽", data["goals"]),
            ("corners", "🚩", corners),
        )

        for kind, emoji, current in markets:
            group = [
                x for x in selections
                if x["kind"] == kind
            ]

            with st.expander(
                f"{emoji} TOTAL {kind.upper()} "
                f"— {len(group)} cote(s)",
                expanded=bool(group),
            ):
                if not group:
                    st.info(
                        "Aucune sélection compatible "
                        "trouvée pour ce marché."
                    )

                for x in sorted(
                    group,
                    key=lambda a: (
                        a["line"],
                        a["side"],
                        a["odd"],
                    ),
                ):
                    st.markdown(
                        f"**{emoji} {x['side'].upper()} "
                        f"{x['line']:g} {kind.upper()} "
                        f"— @{x['odd']:.2f}**"
                    )

                    st.caption(
                        f"{status_text(x, current)} · "
                        f"{x['book']} · "
                        f"Marché API : {x['market']}"
                    )

                    st.divider()

        with st.expander(
            f"🔧 Diagnostic des marchés reçus "
            f"({len(names)})"
        ):
            if names:
                st.code(
                    "\n".join(names),
                    language="text",
                )
            else:
                st.info(
                    "L'API n'a renvoyé aucun "
                    "marché pour ce match."
                )

        st.caption(
            "Attention : une cote affichée ne "
            "constitue pas une preuve de valeur. "
            "Vérifie sa période, son règlement "
            "et son actualité."
        )


for k, default in {
    "budget": 60,
    "calls": 0,
    "seen": set(),
    "quota": "Inconnu",
}.items():
    if k not in st.session_state:
        st.session_state[k] = default

today = datetime.now(TZ).date().isoformat()

if st.session_state.get("day") != today:
    st.session_state.day = today
    st.session_state.calls = 0
    st.session_state.seen = set()

st.title("🔴 KAIROS LIVE V3.4")

st.caption(
    "Soccer live · Buts et corners · "
    "Québec · Mode économe"
)

with st.sidebar:
    st.header("⚙️ Paramètres")

    st.slider(
        "Budget local d'appels / jour",
        5, 100,
        key="budget",
    )

    limit = st.slider(
        "Matchs analysés par scan",
        1, 10, 3,
    )

    minute_min, minute_max = st.slider(
        "Minutes du match",
        0, 120, (20, 85),
    )

    st.metric(
        "Appels estimés (session)",
        st.session_state.calls,
    )

    st.caption(
        "Budget local restant : "
        f"{max(0, st.session_state.budget - st.session_state.calls)}"
    )

    st.caption(
        f"Quota API annoncé : "
        f"{st.session_state.quota}"
    )

    st.warning(
        "Le compteur local est indicatif "
        "et ne mesure pas exactement "
        "le quota fournisseur."
    )

    if st.button(
        "Vider le cache API",
        key="clear_cache",
    ):
        api_call.clear()
        st.info(
            "Cache vidé. Les prochains "
            "chargements peuvent consommer "
            "des requêtes."
        )

if not key():
    st.error(
        'Ajoute API_FOOTBALL_KEY = "ta_cle" '
        "dans les Secrets Streamlit."
    )
    st.stop()

auto, manual, guide = st.tabs([
    "📡 Automatique",
    "🔎 Manuel",
    "📘 Aide",
])

with auto:
    st.subheader("Scanner les matchs live")

    if st.button(
        "🔄 LANCER LE SCAN",
        type="primary",
        key="scan_auto",
    ):
        result = fetch(
            "/fixtures",
            live="all",
        )

        if result["error"]:
            st.error(result["error"])
        else:
            eligible = [
                m for m in result["items"]
                if minute_min <= info(m)["minute"] <= minute_max
            ]

            st.session_state.auto_matches = eligible[:limit]
            st.session_state.auto_total = len(eligible)

    if "auto_matches" in st.session_state:
        st.caption(
            f"{len(st.session_state.auto_matches)} "
            f"match(s) sur "
            f"{st.session_state.get('auto_total', 0)} "
            "admissibles"
        )

        for match in st.session_state.auto_matches:
            show_match(match, "auto")

with manual:
    st.subheader("Analyser un match précis")

    if st.button(
        "🔍 Charger les matchs live",
        key="manual_refresh",
    ):
        result = fetch(
            "/fixtures",
            live="all",
        )

        if result["error"]:
            st.error(result["error"])
        else:
            st.session_state.manual_list = result["items"]

    if st.session_state.get("manual_list"):
        options = {
            (
                f"{info(m)['name']} | "
                f"{info(m)['minute']}' | "
                f"ID {info(m)['id']}"
            ): m
            for m in st.session_state.manual_list
        }

        selected = st.selectbox(
            "Match",
            list(options),
            key="manual_selection",
        )

        if st.button(
            "🔎 ANALYSER",
            key="manual_analyze",
        ):
            st.session_state.manual_match = options[selected]

    if st.session_state.get("manual_match"):
        show_match(
            st.session_state.manual_match,
            "manual",
        )

with guide:
    st.write(
        "⚽ OVER 3.5 BUTS : au moins "
        "4 buts au total. "
        "🚩 UNDER 10.5 CORNERS : "
        "au maximum 10 corners au total."
    )

    st.write(
        "Les marchés ambigus ou sans ligne "
        "explicite sont exclus. "
        "Le diagnostic montre les noms exacts "
        "renvoyés par l'API."
    )

    st.write(
        "Le cache de 90 secondes limite "
        "les appels réseau. "
        "Les estimations d'appels locales "
        "peuvent surestimer ou sous-estimer "
        "la consommation réelle."
    )
