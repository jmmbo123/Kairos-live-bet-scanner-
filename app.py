
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")
st.set_page_config(page_title="KAIROS LIVE V3.6", page_icon="🔴", layout="wide")

for key, default in {
    "calls": 0, "remaining": None, "live": [], "results": [],
    "last_scan": None, "manual_result": None, "odds_saved": {},
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def number(x, default=None):
    try:
        if isinstance(x, str):
            x = x.replace("%", "").strip()
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


@st.cache_data(ttl=180, show_spinner=False)
def api_get(endpoint, fixture, api_key):
    params = {"live": "all"} if endpoint == "fixtures" else {"fixture": fixture}
    try:
        response = requests.get(
            f"{BASE}/{endpoint}",
            headers={"x-apisports-key": api_key},
            params=params, timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        return {
            "response": payload.get("response") or [],
            "error": str(payload.get("errors")) if payload.get("errors") else None,
            "remaining": response.headers.get("x-ratelimit-requests-remaining"),
        }
    except (requests.RequestException, ValueError) as exc:
        return {"response": [], "error": str(exc), "remaining": None}


def request_data(endpoint, fixture=None):
    key = st.secrets.get("API_FOOTBALL_KEY", "")
    if not key:
        return {"response": [], "error": "Clé API_FOOTBALL_KEY absente dans Secrets.", "remaining": None}
    # Cache_data évite un nouvel appel HTTP pour une requête identique durant 180 s.
    # Le compteur est volontairement une borne haute, pas le quota officiel.
    result = api_get(endpoint, fixture, key)
    st.session_state.calls += 1
    if result["remaining"] is not None:
        st.session_state.remaining = result["remaining"]
    return result


def fixture_id(f):
    return f.get("fixture", {}).get("id")


def fixture_name(f):
    teams = f.get("teams") or {}
    return f"{(teams.get('home') or {}).get('name', '?')} vs {(teams.get('away') or {}).get('name', '?')}"


def minute(f):
    return int(number(((f.get("fixture") or {}).get("status") or {}).get("elapsed"), 0))


def score(f):
    g = f.get("goals") or {}
    return f"{int(number(g.get('home'), 0))}-{int(number(g.get('away'), 0))}"


def summary_stats(stats):
    data = {"shots on goal": 0, "total shots": 0, "corner kicks": 0}
    present = set()
    for team in stats or []:
        for item in team.get("statistics") or []:
            name = str(item.get("type", "")).strip().lower()
            if name in data:
                value = number(item.get("value"))
                if value is not None:
                    data[name] += max(0, value)
                    present.add(name)
    return data, present


def classify_market(name):
    low = re.sub(r"\s+", " ", str(name).lower()).strip()
    excluded = ("1st half", "2nd half", "first half", "second half", "half time",
                "halftime", "home", "away", "team", "asian", "race", "odd/even",
                "exact", "3way", "3-way", "3 way", "1h", "2h")
    if any(x in low for x in excluded):
        return None
    goals = {"goals over/under", "match goals", "over/under", "total goals",
             "over/under line", "goals over under"}
    corners = {"total corners", "match corners", "corners over/under",
               "corner kicks over/under", "corners", "total corner kicks"}
    if low in goals:
        return "Buts"
    if low in corners:
        return "Corners"
    return None


def parse_odds(response):
    output, market_count = [], 0
    for block in response or []:
        for book in block.get("bookmakers") or []:
            bookmaker = book.get("name") or "Bookmaker inconnu"
            for market in book.get("bets") or []:
                market_count += 1
                kind = classify_market(market.get("name", ""))
                if not kind:
                    continue
                for item in market.get("values") or []:
                    raw = str(item.get("value", "")).strip()
                    match = re.fullmatch(r"(Over|Under)\s+(\d+(?:[.,]\d+)?)", raw, re.I)
                    odd = number(item.get("odd"))
                    if not match or odd is None or odd <= 1:
                        continue
                    line = float(match.group(2).replace(",", "."))
                    # Les lignes entières peuvent être remboursées (push) :
                    # le modèle binaire ci-dessous ne les évalue pas.
                    output.append({
                        "kind": kind, "side": match.group(1).title(),
                        "line": line, "odd": odd, "bookmaker": bookmaker,
                        "market": market.get("name", ""),
                    })
    unique = {}
    for p in output:
        key = (p["kind"], p["side"], p["line"], p["bookmaker"], p["odd"])
        unique[key] = p
    return list(unique.values()), market_count


def poisson_cdf(k, lam):
    if k < 0:
        return 0.0
    lam = max(0.01, min(30.0, lam))
    term = math.exp(-lam)
    total = term
    for i in range(1, min(k, 100) + 1):
        term *= lam / i
        total += term
    return max(0.0, min(1.0, total))


def experimental_rates(f, stats):
    values, present = summary_stats(stats)
    t = minute(f)
    if t < 20 or t > 85 or len(present) < 3:
        return None
    # Heuristiques expérimentales volontairement prudentes, NON calibrées.
    # Taux futurs dépendants du temps et de l'activité; pas des xG.
    time_left = max(0, 95 - t)
    shots = values["total shots"]
    on_target = values["shots on goal"]
    corners = values["corner kicks"]
    pace_goals = (0.045 * shots + 0.16 * on_target) / max(t, 25)
    pace_corners = corners / max(t, 25)
    goals_remaining = time_left * (0.45 * (2.5 / 95) + 0.55 * pace_goals)
    corners_remaining = time_left * (0.45 * (9.0 / 95) + 0.55 * pace_corners)
    return {"Buts": min(4.0, goals_remaining), "Corners": min(15.0, corners_remaining)}


def candidate_rows(f, stats, odds):
    rates = experimental_rates(f, stats)
    if rates is None:
        return [], "Statistiques insuffisantes pour une estimation expérimentale."
    values, present = summary_stats(stats)
    goals = sum(int(number((f.get("goals") or {}).get(k), 0)) for k in ("home", "away"))
    current = {"Buts": goals, "Corners": int(values["corner kicks"])}
    rows = []
    for p in odds:
        # Lignes .5 seulement : pas de push, pas de confusion avec lignes asiatiques.
        if abs(p["line"] % 1 - 0.5) > 0.0001:
            continue
        remaining_needed = math.floor(p["line"] - current[p["kind"]])
        prob_under = poisson_cdf(remaining_needed, rates[p["kind"]])
        probability = prob_under if p["side"] == "Under" else 1 - prob_under
        probability = max(0, min(1, probability))
        ev = probability * p["odd"] - 1
        rows.append({**p, "prob": probability, "ev": ev})
    rows.sort(key=lambda x: x["ev"], reverse=True)
    return rows, None


def render_fixture(f, stats=None, error=None, show_odds=True, suffix=""):
    fid = fixture_id(f)
    st.subheader(f"{fixture_name(f)} | {minute(f)}' | {score(f)}")
    st.caption(f"{(f.get('league') or {}).get('name', '')} · ID {fid}")
    if error:
        st.warning(error)
    if stats is not None:
        values, present = summary_stats(stats)
        cols = st.columns(3)
        for col, name, label in zip(cols,
                                    ("shots on goal", "total shots", "corner kicks"),
                                    ("Tirs cadrés", "Tirs", "Corners")):
            col.metric(label, int(values[name]) if name in present else "N/D")
    if not show_odds:
        return
    if st.button("💰 Charger les cotes live (1 appel si hors cache)", key=f"odds_button_{suffix}_{fid}"):
        res = request_data("odds/live", fid)
        picks, count = parse_odds(res["response"])
        st.session_state.odds_saved[str(fid)] = (picks, count, res["error"], datetime.now(TZ).strftime("%H:%M:%S"))
    saved = st.session_state.odds_saved.get(str(fid))
    if not saved:
        st.caption("Cotes non chargées : aucun appel supplémentaire effectué.")
        return
    odds, markets, odds_error, fetched = saved
    st.caption(f"Cotes chargées à {fetched} · {markets} marchés reçus · {len(odds)} sélections reconnues")
    if odds_error:
        st.warning(odds_error)
    if not odds:
        st.info("Aucune cote Over/Under de match complet reconnue. Ce n'est pas une preuve d'absence de marché chez les bookmakers.")
        return
    rows, reason = candidate_rows(f, stats, odds)
    if reason:
        st.info(reason)
    else:
        # Ce classement est purement heuristique : pas de promesse de valeur réelle.
        filtered = [r for r in rows if r["ev"] >= min_ev / 100 and r["prob"] >= 0.08 and r["prob"] <= 0.92]
        if filtered:
            top = filtered[0]
            st.success(
                f"🧪 MEILLEUR CANDIDAT EXPÉRIMENTAL : {top['side']} {top['line']:g} "
                f"{top['kind']} @ {top['odd']:.2f} · {top['bookmaker']} · "
                f"EV du modèle {top['ev']:+.1%}"
            )
            st.caption("Ce n'est PAS un pick validé : probabilités non calibrées et cotes potentiellement différées.")
        else:
            st.info("Aucun candidat ne passe les filtres du modèle expérimental.")
        with st.expander("📊 Classement expérimental détaillé"):
            for r in rows[:20]:
                st.write(f"{r['side']} {r['line']:g} {r['kind']} @ {r['odd']:.2f} · "
                         f"{r['bookmaker']} · P modèle {r['prob']:.1%} · EV modèle {r['ev']:+.1%}")
    for kind, emoji in (("Buts", "⚽"), ("Corners", "🚩")):
        group = [p for p in odds if p["kind"] == kind]
        with st.expander(f"{emoji} {kind} — {len(group)} cotes réelles reçues"):
            for p in sorted(group, key=lambda x: (x["line"], x["side"], x["bookmaker"])):
                st.write(f"{p['side']} {p['line']:g} {kind} @ {p['odd']:.2f} · {p['bookmaker']} · {p['market']}")


st.title("🔴 KAIROS LIVE V3.6")
st.caption("Matchs réels · API-Football · Québec · Aucun appel automatique · Modèle expérimental")
with st.sidebar:
    st.header("⚙️ Paramètres")
    limit = st.slider("Matchs maximum par scan", 1, 8, 2)
    min_minute, max_minute = st.slider("Minutes du match", 0, 120, (20, 85))
    min_ev = st.slider("EV expérimentale minimale (%)", 0, 40, 10, 5)
    st.metric("Appels demandés (session, cache inclus)", st.session_state.calls)
    st.write("Quota API annoncé :", st.session_state.remaining or "Inconnu")
    st.caption("Le compteur local inclut les accès au cache et n'est pas le quota officiel. Cache de 180 secondes.")
    st.warning("N'efface pas le cache inutilement : le prochain appel pourra consommer ton quota.")

auto, manual, guide = st.tabs(["📡 Automatique", "🔎 Manuel", "ℹ️ Diagnostic"])

with auto:
    st.write("Le scan demande la liste des matchs en direct, puis les statistiques des matchs sélectionnés. Les cotes ne sont chargées que sur clic.")
    st.caption(f"Budget maximal théorique d'un nouveau scan : {1 + limit} appels API, hors cache. Les cotes coûtent jusqu'à 1 appel supplémentaire par match.")
    if st.button("🔄 SCANNER LES VRAIS MATCHS", type="primary", key="scan"):
        res = request_data("fixtures")
        if res["error"]:
            st.error(res["error"])
        else:
            st.session_state.live = res["response"]
            matches = [f for f in res["response"] if min_minute <= minute(f) <= max_minute]
            st.session_state.results = []
            for f in matches[:limit]:
                stats_res = request_data("fixtures/statistics", fixture_id(f))
                st.session_state.results.append((f, stats_res["response"], stats_res["error"]))
            st.session_state.last_scan = datetime.now(TZ).strftime("%H:%M:%S")
            if not matches:
                st.info("Aucun match dans la plage de minutes choisie.")
    if st.session_state.last_scan:
        st.caption(f"Dernier scan : {st.session_state.last_scan} (Québec)")
    for i, (f, stats, error) in enumerate(st.session_state.results):
        with st.container(border=True):
            render_fixture(f, stats, error, suffix=f"auto_{i}")

with manual:
    st.write("Utilise la liste déjà chargée, ou récupère-la une seule fois. L'analyse d'un match déclenche au maximum un appel statistiques, puis les cotes sur demande.")
    if st.button("📋 Charger / actualiser les matchs live", key="manual_list"):
        res = request_data("fixtures")
        if res["error"]:
            st.error(res["error"])
        else:
            st.session_state.live = res["response"]
    choices = {f"{fixture_name(f)} | {minute(f)}' | ID {fixture_id(f)}": f for f in st.session_state.live}
    if choices:
        selected = st.selectbox("Match", list(choices), key="manual_select")
        f = choices[selected]
        if st.button("🔎 ANALYSER CE MATCH", key="manual_stats"):
            res = request_data("fixtures/statistics", fixture_id(f))
            st.session_state.manual_result = (f, res["response"], res["error"])
    if st.session_state.manual_result:
        f, stats, error = st.session_state.manual_result
        with st.container(border=True):
            render_fixture(f, stats, error, suffix="manual")

with guide:
    st.markdown("""
    **Ce que la V3.6 fait réellement :** affiche les cotes live fournies par API-Football,
    distingue clairement les buts des corners et classe des candidats avec un modèle
    Poisson simplifié fondé sur les statistiques du match.

    **Limites importantes :** les taux et probabilités sont expérimentaux, non calibrés
    sur des résultats historiques. Une EV positive calculée n'est **pas** une preuve
    de value bet. Les cotes live peuvent être absentes, retardées ou indisponibles
    selon les compétitions et le forfait API. Les lignes entières et asiatiques
    ne sont pas évaluées par ce modèle.

    **Économie d'appels :** aucun rafraîchissement automatique. Le cache évite les
    requêtes HTTP identiques pendant 180 secondes. Le quota exact est celui du fournisseur.
    """)
