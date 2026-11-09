
import csv
import io
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(
    page_title="KAIROS LIVE V3",
    page_icon="🎯",
    layout="wide"
)

BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

try:
    API_KEY = st.secrets["API_FOOTBALL_KEY"]
except (KeyError, FileNotFoundError):
    st.error("Ajoute API_FOOTBALL_KEY dans les Secrets Streamlit.")
    st.stop()

if not API_KEY:
    st.error("La clé API est vide.")
    st.stop()

DEFAULTS = {
    "cache": {},
    "calls": 0,
    "quota_day": TODAY,
    "api_remaining": None,
    "results": [],
    "ignored": [],
    "history": [],
    "manual_result": None,
    "coverage": {},
    "budget": 75,
}

for key, value in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value

if st.session_state.quota_day != TODAY:
    st.session_state.calls = 0
    st.session_state.quota_day = TODAY


# ==========================================
# API ET CACHE
# ==========================================

def fetch(endpoint, params=None, ttl=90):
    params = params or {}

    cache_key = (
        endpoint,
        tuple(sorted(
            (str(k), str(v))
            for k, v in params.items()
        ))
    )

    cached = st.session_state.cache.get(cache_key)

    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    if st.session_state.calls >= st.session_state.budget:
        raise RuntimeError("Budget local atteint.")

    if (
        st.session_state.api_remaining is not None
        and st.session_state.api_remaining <= 0
    ):
        raise RuntimeError("Quota API annoncé épuisé.")

    st.session_state.calls += 1

    try:
        response = requests.get(
            BASE + endpoint,
            headers={"x-apisports-key": API_KEY},
            params=params,
            timeout=20
        )

        remaining = response.headers.get(
            "x-ratelimit-requests-remaining"
        )

        if remaining is not None:
            try:
                st.session_state.api_remaining = int(remaining)
            except ValueError:
                pass

        response.raise_for_status()
        data = response.json()

    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(
            f"Erreur API : {exc}"
        ) from exc

    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

    st.session_state.cache[cache_key] = (
        time.time(),
        data
    )

    return data


def get_live_matches():
    return fetch(
        "/fixtures",
        {"live": "all"},
        ttl=90
    ).get("response", [])


# ==========================================
# STATISTIQUES
# ==========================================

def get_stat(items, name):
    for item in items:
        if str(item.get("type", "")).lower() == name.lower():
            value = item.get("value")

            if value is None:
                return None

            try:
                return float(
                    str(value).replace("%", "")
                )
            except (ValueError, TypeError):
                return None

    return None


def get_match_data(fixture):
    fixture_id = fixture["fixture"]["id"]

    response = fetch(
        "/fixtures/statistics",
        {"fixture": fixture_id},
        ttl=150
    ).get("response", [])

    by_team = {
        x.get("team", {}).get("id"): x.get("statistics", [])
        for x in response
    }

    home_id = fixture["teams"]["home"]["id"]
    away_id = fixture["teams"]["away"]["id"]

    home_stats = by_team.get(home_id, [])
    away_stats = by_team.get(away_id, [])

    names = [
        "Shots on Goal",
        "Total Shots",
        "Corner Kicks"
    ]

    stats = {
        name: (
            get_stat(home_stats, name),
            get_stat(away_stats, name)
        )
        for name in names
    }

    valid = all(
        value is not None
        for pair in stats.values()
        for value in pair
    )

    league_id = fixture["league"].get("id")

    observed = st.session_state.coverage.get(
        str(league_id),
        [0, 0]
    )

    observed[0] += int(valid)
    observed[1] += 1

    st.session_state.coverage[str(league_id)] = observed

    minute = fixture["fixture"]["status"].get("elapsed") or 0

    goals = fixture.get("goals") or {}

    home_goals = goals.get("home") or 0
    away_goals = goals.get("away") or 0

    result = {
        "id": fixture_id,
        "home": fixture["teams"]["home"]["name"],
        "away": fixture["teams"]["away"]["name"],
        "league": fixture["league"]["name"],
        "league_id": league_id,
        "minute": minute,
        "home_goals": home_goals,
        "away_goals": away_goals,
        "score": f"{home_goals}-{away_goals}",
        "stats": stats,
        "valid": valid,
        "activity": None,
        "current_corners": None,
        "expected_goals": None,
        "expected_corners": None,
        "picks": [],
        "best_pick": None,
        "odds_loaded": False,
        "odds_error": None,
    }

    if not valid:
        return result

    shots_on_target = sum(stats["Shots on Goal"])
    total_shots = sum(stats["Total Shots"])
    corners = sum(stats["Corner Kicks"])

    activity = min(
        100,
        round(
            10 * shots_on_target
            + 2 * total_shots
            + 2 * corners
        )
    )

    remaining_minutes = max(0, 90 - minute)

    # Projections heuristiques expérimentales.
    expected_goals = (
        shots_on_target * 0.13
        + max(0, total_shots - shots_on_target) * 0.025
    ) * remaining_minutes / max(20, minute)

    expected_corners = (
        corners * remaining_minutes / max(20, minute)
    )

    result.update({
        "activity": activity,
        "current_corners": int(corners),
        "expected_goals": expected_goals,
        "expected_corners": expected_corners
    })

    return result


# ==========================================
# COTES LIVE
# ==========================================

def parse_selection(value, market_name):
    label = str(value.get("value", ""))

    match = re.search(
        r"\b(over|under)\s*([+-]?\d+(?:[.,]\d+)?)",
        label,
        re.IGNORECASE
    )

    if not match:
        return None

    try:
        odd = float(value.get("odd"))

        if not math.isfinite(odd) or odd <= 1:
            return None

        line = float(
            match.group(2).replace(",", ".")
        )

        if not math.isfinite(line) or line < 0:
            return None

    except (ValueError, TypeError):
        return None

    return {
        "side": match.group(1).title(),
        "line": line,
        "odd": odd,
        "market_name": market_name
    }


def get_live_odds(fixture_id):
    response = fetch(
        "/odds/live",
        {"fixture": fixture_id},
        ttl=120
    ).get("response", [])

    markets = {
        "Buts": [],
        "Corners": []
    }

    seen = set()

    for block in response:
        for market in block.get(
            "odds",
            block.get("bets", [])
        ):
            name = str(market.get("name", ""))
            low = name.lower().strip()

            # Match complet uniquement.
            # Exclut les totaux individuels.
            if any(x in low for x in [
                "team",
                "home",
                "away",
                "1st half",
                "2nd half",
                "first half",
                "second half",
                "half time",
                "halftime"
            ]):
                continue

            if low in (
                "match goals",
                "over/under",
                "over/under line",
                "total goals"
            ):
                category = "Buts"

            elif low in (
                "match corners",
                "total corners",
                "asian corners",
                "corners over/under"
            ):
                category = "Corners"

            else:
                continue

            for value in market.get("values", []):
                selection = parse_selection(value, name)

                if selection is None:
                    continue

                identity = (
                    category,
                    selection["side"],
                    selection["line"],
                    selection["odd"]
                )

                if identity in seen:
                    continue

                seen.add(identity)

                markets[category].append(selection)

    return markets


# ==========================================
# PROBABILITES POISSON
# ==========================================

def poisson_cdf(k, lam):
    if k < 0:
        return 0.0

    if lam <= 0:
        return 1.0

    probability = math.exp(-lam)
    total = probability

    for n in range(1, k + 1):
        probability *= lam / n
        total += probability

    return min(1.0, max(0.0, total))


def settlement_probabilities(
    current,
    line,
    lam,
    side
):
    """
    Retourne :
    p_win, p_push, p_loss

    Pour les lignes entières, un push est possible.
    Les lignes .25 et .75 sont volontairement exclues
    du classement V3 pour éviter une évaluation
    incorrecte des demi-gains et demi-pertes.
    """

    fractional = round(line % 1, 4)

    if fractional not in (0.0, 0.5):
        return None

    if fractional == 0.5:
        threshold = math.floor(line)

        p_under = poisson_cdf(
            threshold - current,
            lam
        )

        if side == "Under":
            return p_under, 0.0, 1.0 - p_under

        return 1.0 - p_under, 0.0, p_under

    threshold = int(line)

    p_less = poisson_cdf(
        threshold - current - 1,
        lam
    )

    p_equal = (
        poisson_cdf(threshold - current, lam)
        - p_less
    )

    p_equal = max(0.0, min(1.0, p_equal))

    if side == "Under":
        p_win = p_less
        p_push = p_equal
        p_loss = max(0.0, 1 - p_win - p_push)

    else:
        p_loss = p_less
        p_push = p_equal
        p_win = max(0.0, 1 - p_loss - p_push)

    return p_win, p_push, p_loss


def evaluate_picks(match, markets, min_ev, min_activity):
    if not match["valid"]:
        return []

    if match["activity"] < min_activity:
        return []

    picks = []

    for category, selections in markets.items():
        if category == "Buts":
            current = (
                match["home_goals"]
                + match["away_goals"]
            )
            expected = match["expected_goals"]

        else:
            current = match["current_corners"]
            expected = match["expected_corners"]

        if expected is None:
            continue

        for selection in selections:
            probs = settlement_probabilities(
                current,
                selection["line"],
                expected,
                selection["side"]
            )

            if probs is None:
                continue

            p_win, p_push, p_loss = probs
            odd = selection["odd"]

            # EV par unité misée, en tenant compte
            # d'un éventuel remboursement (push).
            ev = p_win * (odd - 1) - p_loss

            # Écart entre probabilité du modèle et
            # seuil de rentabilité de la cote.
            effective_probability = (
                p_win / (p_win + p_loss)
                if p_win + p_loss > 0
                else 0
            )

            implied_probability = 1 / odd

            pick = {
                "category": category,
                "side": selection["side"],
                "line": selection["line"],
                "odd": odd,
                "p_win": p_win,
                "p_push": p_push,
                "p_loss": p_loss,
                "model_probability": effective_probability,
                "implied_probability": implied_probability,
                "edge": (
                    effective_probability
                    - implied_probability
                ),
                "ev": ev,
                "market": selection["market_name"]
            }

            if ev >= min_ev:
                picks.append(pick)

    picks.sort(
        key=lambda x: x["ev"],
        reverse=True
    )

    return picks


# ==========================================
# ANALYSE COMPLETE
# ==========================================

def analyze_match(fixture, load_odds, min_ev, threshold):
    match = get_match_data(fixture)

    if not match["valid"]:
        return match

    if not load_odds:
        return match

    try:
        markets = get_live_odds(match["id"])
        match["odds_loaded"] = True

        picks = evaluate_picks(
            match,
            markets,
            min_ev,
            threshold
        )

        match["picks"] = picks

        if picks:
            match["best_pick"] = picks[0]

    except RuntimeError as exc:
        match["odds_error"] = str(exc)

    return match


# ==========================================
# AFFICHAGE
# ==========================================

def show_pick(pick):
    with st.container(border=True):
        st.markdown("### 🎯 MEILLEUR PICK POTENTIEL")

        st.markdown(
            f"## {pick['side']} {pick['line']:g} "
            f"{pick['category'].lower()}"
        )

        st.markdown(
            f"### Cote : {pick['odd']:.2f}"
        )

        a, b, c = st.columns(3)

        a.metric(
            "Probabilité modèle",
            f"{pick['model_probability'] * 100:.1f}%"
        )

        b.metric(
            "Seuil de la cote",
            f"{pick['implied_probability'] * 100:.1f}%"
        )

        c.metric(
            "EV théorique",
            f"{pick['ev'] * 100:+.1f}%"
        )

        st.caption(
            f"Écart : {pick['edge'] * 100:+.1f} points "
            f"| Marché : {pick['market']}"
        )

        if pick["p_push"] > 0.001:
            st.caption(
                f"Probabilité de remboursement estimée : "
                f"{pick['p_push'] * 100:.1f}%"
            )

        st.warning(
            "Candidat expérimental : le modèle n'est "
            "pas calibré et peut fortement surestimer "
            "la valeur réelle de ce pari."
        )


def show_match(match):
    with st.container(border=True):
        st.subheader(
            f"{match['home']} vs {match['away']}"
        )

        st.caption(
            f"{match['league']} | "
            f"{match['minute']}' | "
            f"{match['score']} | "
            f"ID {match['id']}"
        )

        if not match["valid"]:
            st.warning(
                "Statistiques insuffisantes. "
                "Aucun pick calculé."
            )
            return

        a, b, c = st.columns(3)

        for column, label, key in [
            (a, "Tirs cadrés", "Shots on Goal"),
            (b, "Tirs totaux", "Total Shots"),
            (c, "Corners", "Corner Kicks")
        ]:
            x, y = match["stats"][key]

            column.metric(
                label,
                f"{int(x)} - {int(y)}"
            )

        st.progress(
            match["activity"] / 100,
            text=(
                f"Activité : "
                f"{match['activity']}/100"
            )
        )

        st.caption(
            f"Buts restants estimés : "
            f"{match['expected_goals']:.2f} | "
            f"Corners restants estimés : "
            f"{match['expected_corners']:.2f}"
        )

        if match["best_pick"]:
            show_pick(match["best_pick"])

            if len(match["picks"]) > 1:
                with st.expander(
                    "Voir les autres candidats"
                ):
                    for pick in match["picks"][1:11]:
                        st.write(
                            f"{pick['side']} "
                            f"{pick['line']:g} "
                            f"{pick['category']} "
                            f"@ {pick['odd']:.2f} | "
                            f"EV modèle "
                            f"{pick['ev'] * 100:+.1f}%"
                        )

        elif match["odds_error"]:
            st.warning(
                "Impossible de charger les cotes : "
                + match["odds_error"]
            )

        elif match["odds_loaded"]:
            st.info(
                "Aucun pick ne dépasse les filtres "
                "du modèle actuel."
            )

        else:
            st.info(
                "Cotes non chargées. "
                "Aucun pick évalué."
            )


# ==========================================
# INTERFACE
# ==========================================

st.title("🔴 KAIROS LIVE V3")

st.caption(
    "Scanner expérimental Over/Under buts et corners "
    "| Mode automatique + manuel | Québec"
)

with st.sidebar:
    st.header("⚙️ Paramètres")

    st.session_state.budget = st.slider(
        "Budget local d'appels / jour",
        5, 95, 75
    )

    threshold = st.slider(
        "Seuil d'activité",
        0, 100, 65
    )

    min_ev_percent = st.slider(
        "EV théorique minimale (%)",
        0, 30, 5
    )

    max_matches = st.slider(
        "Matchs valides recherchés",
        1, 10, 3
    )

    attempts = st.slider(
        "Maximum de matchs testés",
        1, 15, 6
    )

    start, end = st.slider(
        "Minutes de match",
        0, 120, (20, 85)
    )

    st.metric(
        "Appels locaux",
        st.session_state.calls
    )

    st.caption(
        "Budget local restant : "
        f"{max(0, st.session_state.budget - st.session_state.calls)}"
    )

    if st.session_state.api_remaining is not None:
        st.metric(
            "Requêtes restantes annoncées par l'API",
            st.session_state.api_remaining
        )

    st.warning(
        "Le compteur local se réinitialise si "
        "Streamlit redémarre. Vérifie le quota "
        "réel sur API-Football."
    )

min_ev = min_ev_percent / 100

auto, manual, history = st.tabs([
    "📡 Automatique",
    "🔎 Manuel",
    "📋 Historique"
])


# ==========================================
# MODE AUTOMATIQUE
# ==========================================

with auto:
    st.subheader(
        "Scanner les meilleurs candidats live"
    )

    st.caption(
        "Recherche les matchs avec statistiques, "
        "puis analyse leurs lignes Over/Under "
        "buts et corners."
    )

    if st.button(
        "🎯 CHERCHER LES PICKS",
        type="primary",
        width="stretch"
    ):
        results = []
        ignored = []

        try:
            fixtures = get_live_matches()

            eligible = [
                f for f in fixtures
                if start <= (
                    f["fixture"]["status"].get("elapsed") or 0
                ) <= end
            ]

            def ranking(f):
                league_id = str(
                    f["league"].get("id")
                )

                wins, tests = (
                    st.session_state.coverage.get(
                        league_id,
                        [0, 0]
                    )
                )

                coverage_score = (
                    wins / tests
                    if tests
                    else 0.5
                )

                return (
                    coverage_score,
                    f["fixture"]["status"].get("elapsed") or 0
                )

            eligible.sort(
                key=ranking,
                reverse=True
            )

            for fixture in eligible[:attempts]:
                if len(results) >= max_matches:
                    break

                try:
                    match = analyze_match(
                        fixture,
                        load_odds=True,
                        min_ev=min_ev,
                        threshold=threshold
                    )

                except RuntimeError as exc:
                    st.warning(str(exc))
                    break

                if match["valid"]:
                    results.append(match)

                    if match["best_pick"]:
                        pick = match["best_pick"]

                        st.session_state.history.append({
                            "Heure": datetime.now(TZ).strftime(
                                "%Y-%m-%d %H:%M"
                            ),
                            "Match": (
                                match["home"]
                                + " vs "
                                + match["away"]
                            ),
                            "Minute": match["minute"],
                            "Score": match["score"],
                            "Pick": (
                                f"{pick['side']} "
                                f"{pick['line']:g} "
                                f"{pick['category']}"
                            ),
                            "Cote": pick["odd"],
                            "EV_modele": round(
                                pick["ev"] * 100, 2
                            )
                        })

                else:
                    ignored.append(match)

            st.session_state.results = results
            st.session_state.ignored = ignored

        except RuntimeError as exc:
            st.error(str(exc))

    results = st.session_state.results
    ignored = st.session_state.ignored

    a, b, c = st.columns(3)

    a.metric(
        "Matchs analysés",
        len(results)
    )

    b.metric(
        "Matchs ignorés",
        len(ignored)
    )

    c.metric(
        "Matchs avec candidats",
        sum(
            bool(m["best_pick"])
            for m in results
        )
    )

    results_sorted = sorted(
        results,
        key=lambda m: (
            m["best_pick"] is not None,
            m["best_pick"]["ev"]
            if m["best_pick"]
            else -999
        ),
        reverse=True
    )

    for match in results_sorted:
        show_match(match)

    if ignored:
        with st.expander(
            "Voir les matchs ignorés"
        ):
            for match in ignored:
                st.write(
                    f"{match['home']} vs "
                    f"{match['away']} — "
                    f"{match['league']}"
                )

    if not results and not ignored:
        st.info(
            "Clique sur CHERCHER LES PICKS "
            "pour commencer."
        )


# ==========================================
# MODE MANUEL
# ==========================================

with manual:
    st.subheader(
        "Analyser un match précis"
    )

    if st.button(
        "🔄 Actualiser la liste des matchs"
    ):
        st.session_state.cache.pop(
            (
                "/fixtures",
                (("live", "all"),)
            ),
            None
        )

    try:
        fixtures = get_live_matches()

        options = {
            (
                f"{f['teams']['home']['name']} vs "
                f"{f['teams']['away']['name']} | "
                f"{f['league']['name']} | "
                f"ID {f['fixture']['id']}"
            ): f
            for f in fixtures
        }

        if options:
            selected = st.selectbox(
                "Choisis un match live",
                list(options)
            )

            if st.button(
                "🎯 ANALYSER ET TROUVER UN PICK",
                type="primary"
            ):
                try:
                    st.session_state.manual_result = (
                        analyze_match(
                            options[selected],
                            load_odds=True,
                            min_ev=min_ev,
                            threshold=threshold
                        )
                    )

                except RuntimeError as exc:
                    st.error(str(exc))

            if st.session_state.manual_result:
                show_match(
                    st.session_state.manual_result
                )

        else:
            st.info(
                "Aucun match en direct actuellement."
            )

    except RuntimeError as exc:
        st.error(str(exc))


# ==========================================
# HISTORIQUE
# ==========================================

with history:
    st.subheader(
        "Historique des candidats détectés"
    )

    rows = st.session_state.history

    if rows:
        st.dataframe(
            rows,
            width="stretch"
        )

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fieldnames=list(rows[0])
        )

        writer.writeheader()
        writer.writerows(rows)

        st.download_button(
            "📥 Télécharger CSV",
            output.getvalue(),
            "kairos_live_v3.csv",
            "text/csv"
        )

    else:
        st.info(
            "Aucun candidat enregistré."
        )

    st.caption(
        "Historique temporaire de la session. "
        "Les résultats des paris ne sont pas "
        "encore vérifiés automatiquement."
    )


st.divider()

st.caption(
    "KAIROS LIVE V3 — Modèle Poisson simplifié, "
    "non calibré. Les projections ne sont pas "
    "des xG officiels. Les données et les cotes "
    "peuvent être retardées. Une EV positive "
    "calculée ne garantit pas une valeur réelle."
)
