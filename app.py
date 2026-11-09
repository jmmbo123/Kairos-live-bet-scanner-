
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(
    page_title="KAIROS LIVE",
    page_icon="⚽",
    layout="wide",
)

TZ = ZoneInfo("America/Toronto")
BASE = "https://v3.football.api-sports.io"

st.title("🔴 KAIROS LIVE SCANNER")
st.caption("Soccer live | Mode automatique + manuel | Heure du Québec")


def get_key():
    try:
        return st.secrets.get("API_FOOTBALL_KEY", "")
    except Exception:
        return ""


API_KEY = get_key()


@st.cache_data(ttl=45, show_spinner=False)
def api_get(endpoint, params, key):
    response = requests.get(
        BASE + endpoint,
        headers={"x-apisports-key": key},
        params=params,
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("errors"):
        raise ValueError(str(payload["errors"]))

    return payload.get("response", [])


def live_fixtures():
    return api_get("/fixtures", {"live": "all"}, API_KEY)


def fixture_statistics(fixture_id):
    return api_get(
        "/fixtures/statistics",
        {"fixture": fixture_id},
        API_KEY,
    )


def live_odds():
    return api_get("/odds/live", {}, API_KEY)


def number(value):
    if value is None:
        return 0
    try:
        return float(str(value).replace("%", "").strip())
    except (ValueError, TypeError):
        return 0


def get_stats(data):
    output = {}

    for team in data:
        team_id = team.get("team", {}).get("id")
        stats = {}

        for item in team.get("statistics", []):
            stats[item.get("type")] = number(item.get("value"))

        output[team_id] = stats

    return output


def value(stats, key):
    return stats.get(key, 0)


def analyze(fixture, stats):
    teams = fixture.get("teams", {})
    home = teams.get("home", {})
    away = teams.get("away", {})

    home_id = home.get("id")
    away_id = away.get("id")

    hs = stats.get(home_id, {})
    aw = stats.get(away_id, {})

    shots_h = value(hs, "Total Shots")
    shots_a = value(aw, "Total Shots")

    sot_h = value(hs, "Shots on Goal")
    sot_a = value(aw, "Shots on Goal")

    corners_h = value(hs, "Corner Kicks")
    corners_a = value(aw, "Corner Kicks")

    reds_h = value(hs, "Red Cards")
    reds_a = value(aw, "Red Cards")

    minute = fixture.get("fixture", {}).get("status", {}).get("elapsed") or 0
    goals = fixture.get("goals", {})

    total_shots = shots_h + shots_a
    total_sot = sot_h + sot_a
    total_corners = corners_h + corners_a

    # Indicateur descriptif expérimental, pas un xG.
    # La formule récompense l'activité offensive.
    activity = (
        total_sot * 7
        + total_shots * 1.7
        + total_corners * 2.5
    )

    # Comparaison du volume avec le temps écoulé.
    pace = min(1.5, 45 / max(15, minute))
    activity = min(100, round(activity * pace))

    if minute < 15:
        activity = min(activity, 49)

    if activity >= 75:
        label = "Activité offensive élevée"
    elif activity >= 50:
        label = "Activité offensive modérée"
    else:
        label = "Activité offensive faible"

    return {
        "id": fixture.get("fixture", {}).get("id"),
        "home": home.get("name", "?"),
        "away": away.get("name", "?"),
        "league": fixture.get("league", {}).get("name", "?"),
        "minute": minute,
        "status": fixture.get("fixture", {}).get("status", {}).get("short", ""),
        "score": f"{goals.get('home', 0)} - {goals.get('away', 0)}",
        "shots": f"{int(shots_h)} - {int(shots_a)}",
        "sot": f"{int(sot_h)} - {int(sot_a)}",
        "corners": f"{int(corners_h)} - {int(corners_a)}",
        "reds": f"{int(reds_h)} - {int(reds_a)}",
        "activity": activity,
        "label": label,
        "has_stats": bool(hs or aw),
    }


def render_match(match):
    with st.container(border=True):
        st.markdown(
            f"### {match['home']} vs {match['away']}"
        )

        st.caption(
            f"🏆 {match['league']} | "
            f"⏱️ {match['minute']}' | "
            f"{match['status']}"
        )

        st.markdown(f"## {match['score']}")

        if not match["has_stats"]:
            st.warning("Statistiques indisponibles pour ce match.")
            return

        a, b, c = st.columns(3)

        a.metric("Tirs cadrés", match["sot"])
        b.metric("Tirs totaux", match["shots"])
        c.metric("Corners", match["corners"])

        st.write(f"Cartons rouges : {match['reds']}")

        st.progress(match["activity"] / 100)
        st.write(
            f"**Indice d'activité : {match['activity']}/100**"
        )
        st.caption(match["label"])


def show_odds(fixture_id):
    try:
        data = live_odds()
    except Exception as exc:
        st.warning(f"Cotes live indisponibles : {exc}")
        return

    selected = []

    for entry in data:
        fid = entry.get("fixture", {}).get("id")
        if str(fid) == str(fixture_id):
            selected.append(entry)

    if not selected:
        st.info(
            "Aucune cote live disponible pour ce match "
            "dans la réponse de l'API."
        )
        return

    st.success("Cotes live reçues")

    for entry in selected:
        for market in entry.get("odds", []):
            name = market.get("name", "Marché")

            with st.expander(name):
                for selection in market.get("values", []):
                    label = selection.get("value", "?")
                    odd = selection.get("odd", "?")
                    st.write(f"{label} — {odd}")

                    try:
                        implied = 100 / float(odd)
                        st.caption(
                            f"Probabilité implicite brute : "
                            f"{implied:.1f}%"
                        )
                    except (ValueError, TypeError, ZeroDivisionError):
                        pass


if not API_KEY:
    st.error(
        "Clé API absente. Ajoute API_FOOTBALL_KEY "
        "dans les Secrets de Streamlit."
    )
    st.stop()


with st.sidebar:
    st.header("⚙️ Paramètres")

    min_activity = st.slider(
        "Seuil d'activité",
        min_value=0,
        max_value=100,
        value=65,
        step=5,
    )

    max_matches = st.slider(
        "Matchs à analyser par scan",
        min_value=1,
        max_value=30,
        value=10,
    )

    st.caption(
        "Chaque match analysé utilise une requête "
        "de statistiques. Respecte ton quota API."
    )


auto_tab, manual_tab = st.tabs([
    "📡 Scanner automatique",
    "🔍 Analyse manuelle",
])


with auto_tab:
    st.subheader("Scanner les matchs en direct")

    if st.button(
        "🔄 LANCER LE SCAN",
        type="primary",
        key="auto_scan",
        use_container_width=True,
    ):
        try:
            with st.spinner("Récupération des matchs..."):
                fixtures = live_fixtures()
                results = []

                for fixture in fixtures[:max_matches]:
                    fid = fixture["fixture"]["id"]

                    try:
                        raw = fixture_statistics(fid)
                        stats = get_stats(raw)
                        results.append(analyze(fixture, stats))
                    except Exception:
                        continue

                st.session_state["auto_results"] = results
                st.session_state["scan_time"] = (
                    datetime.now(TZ).strftime("%H:%M:%S")
                )

        except Exception as exc:
            st.error(f"Erreur API : {exc}")

    if "auto_results" in st.session_state:
        results = st.session_state["auto_results"]

        st.caption(
            "Dernier scan : "
            + st.session_state.get("scan_time", "?")
            + " (Québec)"
        )

        alerts = [
            m for m in results
            if m["has_stats"]
            and m["activity"] >= min_activity
        ]

        alerts.sort(
            key=lambda m: m["activity"],
            reverse=True,
        )

        c1, c2 = st.columns(2)
        c1.metric("Matchs analysés", len(results))
        c2.metric("Matchs à surveiller", len(alerts))

        if not alerts:
            st.info("Aucun match ne dépasse le seuil choisi.")

        for match in alerts:
            render_match(match)

        with st.expander("Voir tous les matchs analysés"):
            for match in results:
                st.write(
                    f"{match['home']} vs {match['away']} "
                    f"— {match['score']} "
                    f"— Activité {match['activity']}/100"
                )

    else:
        st.info("Appuie sur LANCER LE SCAN pour commencer.")


with manual_tab:
    st.subheader("Analyser un match précis")

    try:
        fixtures = live_fixtures()
    except Exception as exc:
        st.error(f"Impossible de récupérer les matchs : {exc}")
        fixtures = []

    if fixtures:
        choices = {
            (
                f"{m.get('teams', {}).get('home', {}).get('name', '?')}"
                f" vs "
                f"{m.get('teams', {}).get('away', {}).get('name', '?')}"
                f" | "
                f"{m.get('league', {}).get('name', '?')}"
                f" | ID {m['fixture']['id']}"
            ): m
            for m in fixtures
        }

        selected_name = st.selectbox(
            "Choisis un match live",
            list(choices.keys()),
        )

        if st.button(
            "🔎 ANALYSER CE MATCH",
            type="primary",
            use_container_width=True,
        ):
            fixture = choices[selected_name]
            fid = fixture["fixture"]["id"]

            try:
                raw = fixture_statistics(fid)
                stats = get_stats(raw)
                match = analyze(fixture, stats)

                st.session_state["manual_match"] = match

            except Exception as exc:
                st.error(f"Erreur : {exc}")

        if "manual_match" in st.session_state:
            match = st.session_state["manual_match"]

            render_match(match)

            if st.button(
                "💰 VOIR LES COTES LIVE",
                use_container_width=True,
            ):
                show_odds(match["id"])
    else:
        st.info("Aucun match live disponible actuellement.")


st.divider()
st.caption(
    "KAIROS LIVE V1 — Outil expérimental. "
    "Les statistiques peuvent être retardées ou manquantes. "
    "L'indice d'activité n'est ni un xG ni une probabilité "
    "de pari gagnant. Vérifie les cotes et les marchés."
)
