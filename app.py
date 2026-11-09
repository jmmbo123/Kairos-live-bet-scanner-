
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title="KAIROS LIVE V3.2", page_icon="🔴", layout="wide")
BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")


def api(path, params):
    key = st.secrets.get("API_FOOTBALL_KEY", "")
    if not key:
        raise RuntimeError("Clé API_FOOTBALL_KEY absente des Secrets Streamlit.")
    r = requests.get(BASE + path, headers={"x-apisports-key": key}, params=params, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"Erreur HTTP {r.status_code}")
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    remaining = r.headers.get("x-ratelimit-requests-remaining")
    if remaining is not None:
        st.session_state["remaining"] = remaining
    return data.get("response", [])


@st.cache_data(ttl=90, show_spinner=False)
def live_fixtures():
    return api("/fixtures", {"live": "all"})


@st.cache_data(ttl=120, show_spinner=False)
def fixture_stats(fid):
    return api("/fixtures/statistics", {"fixture": fid})


@st.cache_data(ttl=120, show_spinner=False)
def fixture_odds(fid):
    return api("/odds/live", {"fixture": fid})


def num(x, default=None):
    try:
        if x is None or str(x).strip() in ("", "-"):
            return default
        return float(str(x).replace("%", "").replace(",", "."))
    except (ValueError, TypeError):
        return default


def minute(f):
    return int(num(f.get("fixture", {}).get("status", {}).get("elapsed"), 0))


def label(f):
    return (f"{f['teams']['home']['name']} vs {f['teams']['away']['name']} "
            f"| {minute(f)}' | ID {f['fixture']['id']}")


def stats_parse(raw):
    if len(raw) < 2:
        return None
    result = []
    for t in raw[:2]:
        d = {str(s.get("type", "")).lower(): num(s.get("value")) for s in t.get("statistics", [])}
        required = ["total shots", "shots on goal", "corner kicks"]
        if any(d.get(k) is None for k in required):
            return None
        result.append({"shots": d["total shots"], "on": d["shots on goal"],
                       "corners": d["corner kicks"], "inside": d.get("shots insidebox") or 0})
    return result


def metrics(f, stats):
    m = max(1, minute(f))
    shots = sum(s["shots"] for s in stats)
    on = sum(s["on"] for s in stats)
    corners = sum(s["corners"] for s in stats)
    inside = sum(s["inside"] for s in stats)
    activity = min(100, round((shots * 2.5 + on * 5 + corners * 2) * 45 / max(15, m)))
    remaining = max(0, 90 - m)
    # Modèle de démonstration : taux heuristiques, NON calibrés.
    goal_rate = (0.018 * shots + 0.065 * on + 0.012 * inside) / max(20, m)
    corner_rate = (corners + 1.5) / (m + 20)
    return activity, min(3.0, max(0.0, goal_rate * remaining)), min(12.0, max(0.0, corner_rate * remaining))


def poisson_tail(lam, k):
    if k <= 0:
        return 1.0
    return max(0.0, min(1.0, 1 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))))


def probability(current, mean, side, line):
    # Lignes X.5 uniquement : pas de remboursement, demi-gain ou demi-perte.
    if not math.isclose(line % 1, 0.5, abs_tol=1e-7):
        return None
    over = poisson_tail(mean, math.floor(line) + 1 - current)
    return over if side == "Over" else 1 - over


def extract_odds(raw):
    picks = []
    markets = []
    for event in raw:
        for market in event.get("odds", []) or []:
            name = str(market.get("name", ""))
            markets.append(name)
            low = name.lower()
            if any(w in low for w in ("team", "half", "asian", "handicap", "1st", "2nd", "odd/even")):
                continue
            if "corner" in low:
                category = "Corners"
            elif any(w in low for w in ("goal", "over/under", "total")):
                category = "Buts"
            else:
                continue
            for selection in market.get("values", []) or []:
                value = str(selection.get("value", ""))
                handicap = str(selection.get("handicap") or "")
                match = re.search(r"\b(over|under)\s*(\d+(?:[.,]\d+)?)", value, re.I)
                if not match:
                    match = re.search(r"\b(over|under)\s*(\d+(?:[.,]\d+)?)", value + " " + handicap, re.I)
                if not match:
                    continue
                side = match.group(1).capitalize()
                line = float(match.group(2).replace(",", "."))
                odd = num(selection.get("odd"))
                if odd is None or odd <= 1:
                    continue
                picks.append({"category": category, "side": side, "line": line,
                              "odd": odd, "market": name})
    return picks, sorted(set(markets))


def evaluate(f, stats, selection):
    activity, mg, mc = metrics(f, stats)
    current_goals = int(num(f.get("goals", {}).get("home"), 0) + num(f.get("goals", {}).get("away"), 0))
    current_corners = int(sum(s["corners"] for s in stats))
    current, mean = (current_goals, mg) if selection["category"] == "Buts" else (current_corners, mc)
    p = probability(current, mean, selection["side"], selection["line"])
    if p is None:
        return None
    return {**selection, "p": p, "ev": (p * selection["odd"] - 1) * 100,
            "activity": activity, "mg": mg, "mc": mc}


def pick_text(p):
    return f"{p['side']} {p['line']:g} {p['category'].lower()} @ {p['odd']:.2f}"


def show_picks(f, stats, odds, min_ev):
    parsed, market_names = extract_odds(odds)
    evaluated = [evaluate(f, stats, p) for p in parsed]
    evaluated = sorted((p for p in evaluated if p is not None), key=lambda p: p["ev"], reverse=True)
    st.caption(f"Marchés API : {len(market_names)} | Sélections compatibles : {len(evaluated)}")
    good = [p for p in evaluated if p["ev"] >= min_ev]
    if good:
        st.success(f"Meilleur candidat du modèle : {pick_text(good[0])} | EV théorique {good[0]['ev']:+.1f}%")
    elif evaluated:
        st.info("Aucun candidat au-dessus du seuil. Meilleur résultat calculé (non recommandé) : " + pick_text(evaluated[0]))
    else:
        st.warning("Aucune cote live compatible. Utilise la vérification manuelle ci-dessous.")
    for p in evaluated[:8]:
        st.write(f"**{pick_text(p)}** — probabilité modèle {p['p']:.1%} | EV théorique {p['ev']:+.1f}%")
    with st.expander("Diagnostic des marchés API"):
        st.write(market_names if market_names else "L'API a renvoyé zéro marché pour ce match.")
    return evaluated


def show_match(f, load_odds=False, min_ev=5):
    fid = f["fixture"]["id"]
    st.subheader(label(f))
    try:
        raw = fixture_stats(fid)
        stats = stats_parse(raw)
    except Exception as exc:
        st.error(f"Statistiques : {exc}")
        return
    if not stats:
        st.warning("Statistiques indisponibles ou incomplètes : aucune estimation.")
        return
    activity, mg, mc = metrics(f, stats)
    a, b, c = st.columns(3)
    a.metric("Tirs cadrés", f"{stats[0]['on']:g} - {stats[1]['on']:g}")
    b.metric("Tirs totaux", f"{stats[0]['shots']:g} - {stats[1]['shots']:g}")
    c.metric("Corners", f"{stats[0]['corners']:g} - {stats[1]['corners']:g}")
    st.progress(activity / 100, text=f"Activité {activity}/100")
    st.caption(f"Projection restante heuristique : {mg:.2f} buts et {mc:.2f} corners. Ce ne sont pas des xG officiels.")
    if load_odds:
        try:
            show_picks(f, stats, fixture_odds(fid), min_ev)
        except Exception as exc:
            st.warning(f"Cotes live indisponibles : {exc}")
    with st.expander("✍️ Vérifier une cote Bet365 et calculer son EV théorique"):
        st.caption("Entre uniquement une cote visible sur Bet365 pour CE match et CE marché. Aucun appel API supplémentaire.")
        category = st.selectbox("Marché", ["Buts", "Corners"], key=f"cat_{fid}")
        side = st.selectbox("Sélection", ["Over", "Under"], key=f"side_{fid}")
        current = int(sum(s["corners"] for s in stats)) if category == "Corners" else int(
            num(f.get("goals", {}).get("home"), 0) + num(f.get("goals", {}).get("away"), 0))
        default_line = float(current) + 0.5
        line = st.number_input("Ligne Bet365 (X.5)", min_value=0.5, max_value=40.5,
                               value=min(40.5, default_line), step=1.0, key=f"line_{fid}_{category}")
        odd = st.number_input("Cote décimale Bet365", min_value=1.01, max_value=100.0,
                              value=1.80, step=0.01, key=f"odd_{fid}")
        if st.button("📊 ÉVALUER CETTE COTE", key=f"eval_{fid}"):
            p = evaluate(f, stats, {"category": category, "side": side, "line": line,
                                    "odd": odd, "market": "Cote saisie manuellement"})
            if p is None:
                st.error("Seules les lignes X.5 sont évaluées dans cette version.")
            else:
                st.markdown(f"### {pick_text(p)}")
                st.metric("Probabilité estimée", f"{p['p']:.1%}")
                st.metric("EV théorique", f"{p['ev']:+.1f}%")
                if p["ev"] >= min_ev:
                    st.success("Candidat selon le seuil choisi — estimation non validée, pas une recommandation fiable.")
                else:
                    st.info("Ne dépasse pas le seuil d'EV choisi.")
                st.caption("La probabilité provient d'un modèle simplifié non calibré. Vérifie la minute, le score et la disponibilité de la cote.")


st.title("🔴 KAIROS LIVE V3.2")
st.caption("Football live · Over/Under buts et corners · Mode automatique et manuel · Heure du Québec")
with st.sidebar:
    st.header("⚙️ Paramètres")
    max_matches = st.slider("Matchs analysés par scan", 1, 8, 3)
    start, end = st.slider("Minutes du match", 0, 100, (20, 85))
    min_activity = st.slider("Activité minimale", 0, 100, 65)
    min_ev = st.slider("EV théorique minimale (%)", 0, 30, 5)
    st.caption("Cache : matchs 90 s, statistiques et cotes 120 s. Les scans répétés après expiration consomment le quota API.")
    if "remaining" in st.session_state:
        st.metric("Quota restant annoncé par API", st.session_state["remaining"])
    st.warning("Les estimations ne sont pas des probabilités validées. Ne mise pas sur la seule base du scanner.")

auto, manual = st.tabs(["📡 Automatique", "🔎 Manuel"])
with auto:
    st.write("Scanne les matchs en direct. Les cotes ne sont chargées que pour les matchs dont les statistiques dépassent le seuil.")
    if st.button("🔄 LANCER LE SCAN", type="primary"):
        try:
            live = [f for f in live_fixtures() if start <= minute(f) <= end]
            # Le tri est stable et reproductible. Le quota est limité au nombre sélectionné.
            st.session_state["auto_fixtures"] = live[:max_matches]
            st.session_state["auto_total"] = len(live)
        except Exception as exc:
            st.error(str(exc))
    if "auto_fixtures" in st.session_state:
        st.caption(f"{len(st.session_state['auto_fixtures'])} matchs examinés sur {st.session_state['auto_total']} dans la plage horaire.")
        for f in st.session_state["auto_fixtures"]:
            with st.container(border=True):
                fid = f["fixture"]["id"]
                try:
                    raw = fixture_stats(fid)
                    stats = stats_parse(raw)
                    if not stats:
                        st.subheader(label(f))
                        st.warning("Statistiques insuffisantes : aucune cote chargée.")
                    elif metrics(f, stats)[0] < min_activity:
                        st.subheader(label(f))
                        st.info("Activité sous le seuil : aucune cote chargée.")
                    else:
                        show_match(f, load_odds=True, min_ev=min_ev)
                except Exception as exc:
                    st.error(f"{label(f)} : {exc}")
with manual:
    if st.button("📋 Charger les matchs live"):
        try:
            st.session_state["manual_fixtures"] = live_fixtures()
        except Exception as exc:
            st.error(str(exc))
    fixtures = st.session_state.get("manual_fixtures", [])
    if fixtures:
        selected = st.selectbox("Choisir un match", fixtures, format_func=label,
                                key="manual_selection")
        if st.button("🔍 ANALYSER LE MATCH", type="primary"):
            st.session_state["manual_id"] = selected["fixture"]["id"]
        chosen = next((f for f in fixtures if f["fixture"]["id"] == st.session_state.get("manual_id")), None)
        if chosen:
            with st.container(border=True):
                show_match(chosen, load_odds=True, min_ev=min_ev)
    else:
        st.info("Charge la liste live pour commencer.")

st.divider()
st.caption("KAIROS LIVE V3.2 · Modèle expérimental non calibré. Les cotes API peuvent être absentes, décalées ou périmées. Les EV calculées ne démontrent pas une rentabilité réelle.")
