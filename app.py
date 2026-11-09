
import csv
import io
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")

st.set_page_config(
    page_title="KAIROS LIVE V2",
    page_icon="⚽",
    layout="wide",
)

try:
    KEY = st.secrets["API_FOOTBALL_KEY"]
except (KeyError, FileNotFoundError):
    st.error("Ajoute API_FOOTBALL_KEY dans les Secrets Streamlit.")
    st.stop()

if not KEY:
    st.error("La clé API est vide.")
    st.stop()

TODAY = datetime.now(TZ).date().isoformat()

if st.session_state.get("quota_date") != TODAY:
    st.session_state.quota_date = TODAY
    st.session_state.calls = 0
    st.session_state.cache = {}
    st.session_state.history = []
    st.session_state.last_results = []
    st.session_state.manual_result = None


def api(path, params=None, ttl=90):
    """Cache et compteur local de requêtes."""
    params = params or {}

    cache_key = (
        path,
        tuple(sorted(
            (str(k), str(v))
            for k, v in params.items()
        )),
    )

    cached = st.session_state.cache.get(cache_key)

    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    if st.session_state.calls >= st.session_state.budget:
        raise RuntimeError(
            "Budget local atteint. Réduis le nombre de matchs "
            "ou attends demain."
        )

    st.session_state.calls += 1

    try:
        response = requests.get(
            BASE + path,
            headers={"x-apisports-key": KEY},
            params=params,
            timeout=18,
        )
        response.raise_for_status()
        data = response.json()

    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Erreur API : {exc}") from exc

    if data.get("errors"):
        raise RuntimeError(f"Réponse API : {data['errors']}")

    st.session_state.cache[cache_key] = (time.time(), data)

    return data


def live_fixtures():
    return api(
        "/fixtures",
        {"live": "all"},
        ttl=90,
    ).get("response", [])


def stat_value(items, name):
    for item in items or []:
        if str(item.get("type", "")).lower() == name.lower():
            value = item.get("value")

            if value is None:
                return None

            try:
                return float(str(value).replace("%", ""))
            except (ValueError, TypeError):
                return None

    return None


def fixture_details(fixture):
    fid = fixture["fixture"]["id"]

    data = api(
        "/fixtures/statistics",
        {"fixture": fid},
        ttl=120,
    ).get("response", [])

    teams = fixture["teams"]

    by_id = {
        entry.get("team", {}).get("id"):
        entry.get("statistics", [])
        for entry in data
    }

    home = by_id.get(teams["home"]["id"], [])
    away = by_id.get(teams["away"]["id"], [])

    names = [
        "Shots on Goal",
        "Total Shots",
        "Corner Kicks",
        "Red Cards",
    ]

    vals = {
        name: (
            stat_value(home, name),
            stat_value(away, name),
        )
        for name in names
    }

    sufficient = all(
        v is not None
        for name in names[:3]
        for v in vals[name]
    )

    minute = (
        fixture["fixture"]["status"].get("elapsed") or 0
    )

    score_home = fixture["goals"].get("home") or 0
    score_away = fixture["goals"].get("away") or 0

    on = vals["Shots on Goal"]
    shots = vals["Total Shots"]
    corners = vals["Corner Kicks"]

    if sufficient:
        total_on = sum(on)
        total_shots = sum(shots)
        total_corners = sum(corners)

        # Indice heuristique, pas une probabilité.
        activity = min(
            100,
            round(
                10 * total_on
                + 2 * total_shots
                + 2 * total_corners
            ),
        )

        remaining = max(0, 90 - minute)

        # Extrapolation expérimentale.
        expected_goals = round(
            (
                total_on * 0.13
                + max(0, total_shots - total_on) * 0.025
            )
            * remaining
            / max(20, minute),
            2,
        )

        expected_corners = round(
            total_corners
            * remaining
            / max(20, minute),
            2,
        )

    else:
        activity = None
        expected_goals = None
        expected_corners = None

    return {
        "id": fid,
        "fixture": fixture,
        "minute": minute,
        "home": teams["home"]["name"],
        "away": teams["away"]["name"],
        "league": fixture["league"]["name"],
        "score": f"{score_home}-{score_away}",
        "goals_total": score_home + score_away,
        "stats": vals,
        "sufficient": sufficient,
        "activity": activity,
        "goals_remaining_est": expected_goals,
        "corners_remaining_est": expected_corners,
    }


def get_live_odds(fid):
    data = api(
        "/odds/live",
        {"fixture": fid},
        ttl=90,
    )

    return data.get("response", [])


def classify_market(name):
    low = str(name).lower()

    if "corner" in low:
        return "corners"

    if any(
        word in low
        for word in ("goal", "over/under", "total")
    ):
        return "buts"

    return None


def extract_line(text):
    match = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)",
        str(text),
    )

    return (
        match.group(1).replace(",", ".")
        if match
        else None
    )


def parse_ou(value, market_name):
    label = str(value.get("value", "")).strip()

    match = re.search(
        r"\b(over|under)\b",
        label,
        re.I,
    )

    if not match:
        return None

    side = match.group(1).capitalize()

    line = extract_line(label)

    if line is None:
        for field in ("handicap", "line", "point"):
            if value.get(field) is not None:
                line = extract_line(value[field])

                if line:
                    break

    if line is None:
        line = extract_line(market_name)

    # Ne pas afficher une sélection sans ligne.
    if line is None:
        return None

    try:
        odd = float(value.get("odd"))

        if odd <= 1:
            return None

    except (TypeError, ValueError):
        return None

    return {
        "type": side,
        "line": line,
        "odd": odd,
        "market": market_name,
        "raw_label": label,
    }


def markets_from_response(data):
    result = {
        "buts": [],
        "corners": [],
    }

    for block in data:
        # Certaines réponses utilisent "odds",
        # d'autres peuvent utiliser "bets".
        markets = block.get("odds", block.get("bets", []))

        for bet in markets:
            market = bet.get("name", "")
            category = classify_market(market)

            if not category:
                continue

            for value in bet.get("values", []):
                selection = parse_ou(value, market)

                if selection:
                    result[category].append(selection)

    for category in result:
        seen = set()
        unique = []

        for item in result[category]:
            key = (
                item["market"],
                item["type"],
                item["line"],
                item["odd"],
            )

            if key not in seen:
                seen.add(key)
                unique.append(item)

        result[category] = unique

    return result


def show_card(detail, show_odds=False):
    with st.container(border=True):
        st.subheader(
            f"{detail['home']} vs {detail['away']}"
        )

        st.caption(
            f"{detail['league']} · "
            f"{detail['minute']}' · "
            f"{detail['score']} · "
            f"ID {detail['id']}"
        )

        if not detail["sufficient"]:
            st.warning(
                "Statistiques insuffisantes : "
                "aucune estimation ni alerte."
            )

        else:
            a, b, c = st.columns(3)

            a.metric(
                "Tirs cadrés",
                " - ".join(
                    str(int(v))
                    for v in detail["stats"]["Shots on Goal"]
                ),
            )

            b.metric(
                "Tirs totaux",
                " - ".join(
                    str(int(v))
                    for v in detail["stats"]["Total Shots"]
                ),
            )

            c.metric(
                "Corners",
                " - ".join(
                    str(int(v))
                    for v in detail["stats"]["Corner Kicks"]
                ),
            )

            st.progress(
                detail["activity"] / 100,
                text=(
                    f"Activité : {detail['activity']}/100"
                ),
            )

            st.caption(
                "Estimation expérimentale sur le temps restant : "
                f"{detail['goals_remaining_est']} but(s), "
                f"{detail['corners_remaining_est']} corner(s). "
                "Ce ne sont ni des xG officiels "
                "ni des probabilités de gain."
            )

        if show_odds:
            if st.button(
                "💰 CHARGER LES COTES LIVE",
                key=f"odds_{detail['id']}",
            ):
                try:
                    raw = get_live_odds(detail["id"])

                    st.session_state[
                        f"markets_{detail['id']}"
                    ] = markets_from_response(raw)

                except RuntimeError as exc:
                    st.error(str(exc))

            markets = st.session_state.get(
                f"markets_{detail['id']}"
            )

            if markets is not None:
                for category in ("buts", "corners"):
                    st.markdown(
                        f"**Over/Under {category}**"
                    )

                    selections = markets[category]

                    if not selections:
                        st.info(
                            "Aucune sélection avec ligne "
                            "identifiable dans la réponse API."
                        )

                    else:
                        for item in selections[:40]:
                            st.write(
                                f"{item['market']} · "
                                f"{item['type']} "
                                f"{item['line']} · "
                                f"Cote {item['odd']:.3f}"
                            )

                        if len(selections) > 40:
                            st.caption(
                                f"{len(selections) - 40} "
                                "autres sélections masquées."
                            )


st.title("🔴 KAIROS LIVE V2")

st.caption(
    "Soccer live · Buts et corners · "
    "Québec · Scanner expérimental"
)

with st.sidebar:
    st.header("⚙️ Paramètres")

    st.session_state.budget = st.slider(
        "Budget maximal local / jour",
        5,
        95,
        80,
    )

    threshold = st.slider(
        "Seuil d'activité",
        0,
        100,
        65,
    )

    max_matches = st.slider(
        "Matchs analysés par scan",
        1,
        15,
        3,
    )

    min_minute, max_minute = st.slider(
        "Minutes de match",
        0,
        120,
        (20, 85),
    )

    st.metric(
        "Appels API comptés dans cette session",
        st.session_state.calls,
    )

    remaining_budget = max(
        0,
        st.session_state.budget - st.session_state.calls,
    )

    st.caption(
        f"Budget restant local : {remaining_budget}"
    )

    st.warning(
        "Le compteur se réinitialise si Streamlit redémarre. "
        "Vérifie le quota réel sur API-Football."
    )

    if st.button("Vider le cache de session"):
        st.session_state.cache = {}
        st.rerun()


tab_auto, tab_manual, tab_history = st.tabs([
    "📡 Automatique",
    "🔎 Manuel",
    "📋 Historique",
])


with tab_auto:
    st.subheader("Scanner les matchs en direct")

    st.caption(
        "Un scan utilise une requête pour la liste live "
        "(sauf cache), puis jusqu'au nombre de matchs "
        "choisi pour les statistiques."
    )

    if st.button(
        "🔄 LANCER LE SCAN",
        type="primary",
        use_container_width=True,
    ):
        try:
            fixtures = live_fixtures()

            eligible = [
                f
                for f in fixtures
                if min_minute
                <= (
                    f["fixture"]["status"].get("elapsed") or 0
                )
                <= max_minute
            ]

            eligible.sort(
                key=lambda f: (
                    f["fixture"]["status"].get("elapsed") or 0
                ),
                reverse=True,
            )

            results = []

            for fixture in eligible[:max_matches]:
                try:
                    results.append(
                        fixture_details(fixture)
                    )

                except RuntimeError as exc:
                    st.warning(
                        f"Match {fixture['fixture']['id']} : {exc}"
                    )
                    break

            st.session_state.last_results = results

            for item in results:
                if (
                    item["sufficient"]
                    and item["activity"] >= threshold
                ):
                    st.session_state.history.append({
                        "heure_qc": datetime.now(TZ).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "match": (
                            item["home"]
                            + " vs "
                            + item["away"]
                        ),
                        "minute": item["minute"],
                        "score": item["score"],
                        "activite": item["activity"],
                        "fixture_id": item["id"],
                    })

            st.session_state.last_total_live = len(fixtures)

        except RuntimeError as exc:
            st.error(str(exc))

    results = st.session_state.last_results

    if results:
        valid = [
            x for x in results
            if x["sufficient"]
        ]

        watch = [
            x for x in valid
            if x["activity"] >= threshold
        ]

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "Matchs analysés",
            len(results),
        )

        c2.metric(
            "Avec statistiques",
            len(valid),
        )

        c3.metric(
            "À surveiller",
            len(watch),
        )

        for item in sorted(
            watch,
            key=lambda x: x["activity"],
            reverse=True,
        ):
            show_card(
                item,
                show_odds=True,
            )

        with st.expander(
            "Voir tous les matchs analysés"
        ):
            for item in results:
                show_card(item)

    else:
        st.info(
            "Appuie sur LANCER LE SCAN pour commencer."
        )


with tab_manual:
    st.subheader("Analyser un match précis")

    if st.button(
        "Actualiser la liste des matchs live"
    ):
        st.session_state.cache = {
            k: v
            for k, v in st.session_state.cache.items()
            if k[0] != "/fixtures"
        }

    try:
        fixtures = live_fixtures()

        if not fixtures:
            st.info(
                "Aucun match live dans la réponse API."
            )

        else:
            options = {
                (
                    f"{f['teams']['home']['name']} vs "
                    f"{f['teams']['away']['name']} | "
                    f"{f['league']['name']} | "
                    f"ID {f['fixture']['id']}"
                ): f
                for f in fixtures
            }

            selected = st.selectbox(
                "Choisis un match live",
                list(options),
            )

            if st.button(
                "🔎 ANALYSER CE MATCH",
                type="primary",
                use_container_width=True,
            ):
                try:
                    st.session_state.manual_result = (
                        fixture_details(options[selected])
                    )

                except RuntimeError as exc:
                    st.error(str(exc))

            if st.session_state.manual_result:
                show_card(
                    st.session_state.manual_result,
                    show_odds=True,
                )

    except RuntimeError as exc:
        st.error(str(exc))


with tab_history:
    st.subheader(
        "Historique de cette session"
    )

    st.caption(
        "Historique temporaire : perdu au redémarrage "
        "du serveur. Les alertes ne sont pas "
        "des recommandations de mise."
    )

    rows = st.session_state.history

    if rows:
        st.dataframe(
            rows,
            use_container_width=True,
        )

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fieldnames=list(rows[0]),
        )

        writer.writeheader()
        writer.writerows(rows)

        st.download_button(
            "📥 Télécharger CSV",
            output.getvalue(),
            "kairos_live_alertes.csv",
            "text/csv",
        )

    else:
        st.info(
            "Aucune alerte enregistrée dans cette session."
        )


st.divider()

st.caption(
    "KAIROS LIVE V2 · Données potentiellement retardées. "
    "Couverture variable. Les indices heuristiques "
    "ne prouvent pas qu'un pari présente de la valeur."
)
