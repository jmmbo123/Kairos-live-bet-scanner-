
import csv
import io
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title="KAIROS LIVE V2.1", page_icon="⚽", layout="wide")
BASE = "https://v3.football.api-sports.io"
TZ = ZoneInfo("America/Toronto")
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")

try:
    KEY = st.secrets["API_FOOTBALL_KEY"]
except (KeyError, FileNotFoundError):
    st.error("Ajoute API_FOOTBALL_KEY dans les Secrets Streamlit.")
    st.stop()

if not KEY:
    st.error("La clé API est vide.")
    st.stop()

if "cache" not in st.session_state:
    st.session_state.cache = {}
if "calls" not in st.session_state:
    st.session_state.calls = 0
if "quota_day" not in st.session_state:
    st.session_state.quota_day = TODAY
if st.session_state.quota_day != TODAY:
    st.session_state.calls = 0
    st.session_state.quota_day = TODAY
if "api_remaining" not in st.session_state:
    st.session_state.api_remaining = None
if "coverage" not in st.session_state:
    st.session_state.coverage = {}
if "results" not in st.session_state:
    st.session_state.results = []
if "ignored" not in st.session_state:
    st.session_state.ignored = []
if "history" not in st.session_state:
    st.session_state.history = []
if "manual" not in st.session_state:
    st.session_state.manual = None
if "odds" not in st.session_state:
    st.session_state.odds = {}


def fetch(endpoint, params=None, ttl=90):
    params = params or {}
    ck = (endpoint, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    cached = st.session_state.cache.get(ck)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    if st.session_state.calls >= st.session_state.budget:
        raise RuntimeError("Budget local atteint. Aucun nouvel appel envoyé.")
    if st.session_state.api_remaining is not None and st.session_state.api_remaining <= 0:
        raise RuntimeError("Quota API annoncé épuisé. Aucun nouvel appel envoyé.")
    # Count the request even if the network fails; it may reach the provider.
    st.session_state.calls += 1
    try:
        r = requests.get(BASE + endpoint, headers={"x-apisports-key": KEY}, params=params, timeout=20)
        for name in ("x-ratelimit-requests-remaining", "X-RateLimit-Requests-Remaining"):
            if name in r.headers:
                try:
                    st.session_state.api_remaining = int(r.headers[name])
                except (ValueError, TypeError):
                    pass
                break
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Appel API impossible : {exc}") from exc
    if data.get("errors"):
        raise RuntimeError(f"API-Football : {data['errors']}")
    st.session_state.cache[ck] = (time.time(), data)
    return data


def fixtures_live():
    return fetch("/fixtures", {"live": "all"}, 90).get("response", [])


def stat(items, name):
    for item in items:
        if item.get("type", "").lower() == name.lower():
            val = item.get("value")
            if val is None:
                return None
            try:
                return float(str(val).replace("%", ""))
            except (ValueError, TypeError):
                return None
    return None


def analyze(f):
    fid = f["fixture"]["id"]
    league = f["league"].get("id")
    response = fetch("/fixtures/statistics", {"fixture": fid}, 150).get("response", [])
    by_id = {x.get("team", {}).get("id"): x.get("statistics", []) for x in response}
    home = by_id.get(f["teams"]["home"]["id"], [])
    away = by_id.get(f["teams"]["away"]["id"], [])
    names = ("Shots on Goal", "Total Shots", "Corner Kicks")
    stats = {name: (stat(home, name), stat(away, name)) for name in names}
    valid = all(x is not None for pair in stats.values() for x in pair)
    # A league's coverage is learned from actual API responses, not assumed.
    observed = st.session_state.coverage.get(str(league), [0, 0])
    observed[0] += int(valid)
    observed[1] += 1
    st.session_state.coverage[str(league)] = observed
    minute = f["fixture"]["status"].get("elapsed") or 0
    goals = f.get("goals") or {}
    total_goals = (goals.get("home") or 0) + (goals.get("away") or 0)
    score = f"{goals.get('home') or 0}-{goals.get('away') or 0}"
    activity = eg = ec = None
    if valid:
        sot = sum(stats["Shots on Goal"])
        shots = sum(stats["Total Shots"])
        corners = sum(stats["Corner Kicks"])
        activity = min(100, round(10 * sot + 2 * shots + 2 * corners))
        remaining = max(0, 90 - minute)
        eg = round((sot * .13 + max(0, shots - sot) * .025) * remaining / max(20, minute), 2)
        ec = round(corners * remaining / max(20, minute), 2)
    return dict(id=fid, home=f["teams"]["home"]["name"], away=f["teams"]["away"]["name"],
                league=f["league"]["name"], league_id=league, minute=minute, score=score,
                total_goals=total_goals, stats=stats, valid=valid, activity=activity,
                goals_est=eg, corners_est=ec)


def line_from_value(v, market):
    label = str(v.get("value", ""))
    side_match = re.search(r"\b(over|under)\b", label, re.I)
    if not side_match:
        return None
    side = side_match.group(1).title()
    # Use the numeric threshold in the selection or explicit line fields only.
    number = re.search(r"\b(?:over|under)\s*([+-]?\d+(?:[.,]\d+)?)\b", label, re.I)
    threshold = number.group(1).replace(",", ".") if number else None
    if threshold is None:
        for field in ("handicap", "line", "point"):
            if v.get(field) is not None:
                raw = str(v[field])
                if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", raw):
                    threshold = raw.replace(",", ".")
                    break
    if threshold is None:
        return None
    try:
        odd = float(v["odd"])
        if odd <= 1:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    return dict(market=market, side=side, line=threshold, odd=odd)


def get_markets(fid):
    response = fetch("/odds/live", {"fixture": fid}, 120).get("response", [])
    markets = {"Buts": [], "Corners": []}
    seen = set()
    for block in response:
        for m in block.get("odds", block.get("bets", [])):
            name = str(m.get("name", ""))
            low = name.lower()
            if "corner" in low:
                category = "Corners"
            elif any(term in low for term in ("goal", "over/under", "total")):
                category = "Buts"
            else:
                continue
            for v in m.get("values", []):
                item = line_from_value(v, name)
                if item:
                    identity = (category, name, item["side"], item["line"], item["odd"])
                    if identity not in seen:
                        seen.add(identity)
                        markets[category].append(item)
    return markets


def display_match(d, allow_odds=False):
    with st.container(border=True):
        st.subheader(f"{d['home']} vs {d['away']}")
        st.caption(f"{d['league']} · {d['minute']}' · {d['score']} · ID {d['id']}")
        if not d["valid"]:
            st.warning("Statistiques insuffisantes : match ignoré pour les alertes.")
        else:
            a, b, c = st.columns(3)
            for col, title, key in ((a, "Tirs cadrés", "Shots on Goal"),
                                    (b, "Tirs totaux", "Total Shots"),
                                    (c, "Corners", "Corner Kicks")):
                x, y = d["stats"][key]
                col.metric(title, f"{int(x)} - {int(y)}")
            st.progress(d["activity"] / 100, text=f"Indice d'activité : {d['activity']}/100")
            st.caption(f"Projection heuristique restante : {d['goals_est']} buts et {d['corners_est']} corners. Ce ne sont pas des xG officiels ni des probabilités de gain.")
        if allow_odds:
            if st.button("💰 Charger les Over/Under live", key=f"load_{d['id']}"):
                try:
                    st.session_state.odds[str(d["id"])] = get_markets(d["id"])
                except RuntimeError as exc:
                    st.error(str(exc))
            odds = st.session_state.odds.get(str(d["id"]))
            if odds is not None:
                for category, items in odds.items():
                    with st.expander(f"{category} ({len(items)} sélections)", expanded=False):
                        if not items:
                            st.info("Aucune cote avec ligne explicite disponible.")
                        for x in items[:50]:
                            st.write(f"{x['market']} | {x['side']} {x['line']} | {x['odd']:.3f}")


st.title("🔴 KAIROS LIVE V2.1")
st.caption("Buts + corners · Scanner manuel et automatique · Heure du Québec")

with st.sidebar:
    st.header("⚙️ Paramètres")
    st.session_state.budget = st.slider("Budget local d'appels / jour", 5, 95, 75)
    threshold = st.slider("Seuil d'activité", 0, 100, 65)
    max_matches = st.slider("Matchs avec statistiques recherchés", 1, 10, 3)
    attempts = st.slider("Maximum de matchs testés par scan", 1, 15, 6)
    start, end = st.slider("Minutes de match", 0, 120, (20, 85))
    st.metric("Appels comptés dans cette session", st.session_state.calls)
    st.caption(f"Budget local restant : {max(0, st.session_state.budget - st.session_state.calls)}")
    if st.session_state.api_remaining is not None:
        st.metric("Requêtes restantes annoncées par l'API", st.session_state.api_remaining)
    st.warning("Le budget local ne se synchronise pas entre appareils ou redémarrages. Le quota officiel est celui d'API-Football.")


auto, manual, history = st.tabs(["📡 Automatique", "🔎 Manuel", "📋 Historique"])

with auto:
    st.subheader("Scan avec recherche de statistiques")
    st.caption("Le scanner privilégie les ligues ayant déjà fourni des statistiques dans cette session. Il s'arrête après le maximum d'essais ou dès que le nombre de matchs valides est atteint.")
    if st.button("🔄 LANCER LE SCAN", type="primary", use_container_width=True):
        try:
            fixtures = fixtures_live()
            eligible = [f for f in fixtures if start <= (f["fixture"]["status"].get("elapsed") or 0) <= end]
            def rank(f):
                wins, tests = st.session_state.coverage.get(str(f["league"].get("id")), [0, 0])
                return (wins / tests if tests else 0.5, f["fixture"]["status"].get("elapsed") or 0)
            eligible.sort(key=rank, reverse=True)
            results, ignored = [], []
            for f in eligible[:attempts]:
                if len(results) >= max_matches:
                    break
                try:
                    d = analyze(f)
                except RuntimeError as exc:
                    st.warning(str(exc))
                    break
                if d["valid"]:
                    results.append(d)
                    if d["activity"] >= threshold:
                        st.session_state.history.append(dict(
                            heure=datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                            match=f"{d['home']} vs {d['away']}", minute=d["minute"],
                            score=d["score"], activite=d["activity"], id=d["id"]))
                else:
                    ignored.append(d)
            st.session_state.results = results
            st.session_state.ignored = ignored
        except RuntimeError as exc:
            st.error(str(exc))
    results = st.session_state.results
    ignored = st.session_state.ignored
    a, b, c = st.columns(3)
    a.metric("Matchs avec statistiques", len(results))
    b.metric("Matchs ignorés", len(ignored))
    c.metric("Au-dessus du seuil", sum(x["activity"] >= threshold for x in results))
    for d in sorted(results, key=lambda x: x["activity"], reverse=True):
        display_match(d, allow_odds=True)
    if ignored:
        with st.expander("Voir les matchs ignorés"):
            for d in ignored:
                st.write(f"{d['home']} vs {d['away']} — {d['league']} — statistiques insuffisantes")
    if not results and not ignored:
        st.info("Lance un scan pour commencer.")

with manual:
    st.subheader("Analyser un match précis")
    try:
        fixtures = fixtures_live()
        options = {f"{f['teams']['home']['name']} vs {f['teams']['away']['name']} | {f['league']['name']} | {f['fixture']['id']}": f for f in fixtures}
        if options:
            selected = st.selectbox("Match en direct", list(options))
            if st.button("🔎 ANALYSER", type="primary"):
                try:
                    st.session_state.manual = analyze(options[selected])
                except RuntimeError as exc:
                    st.error(str(exc))
            if st.session_state.manual:
                display_match(st.session_state.manual, allow_odds=True)
        else:
            st.info("Aucun match live disponible actuellement.")
    except RuntimeError as exc:
        st.error(str(exc))

with history:
    st.subheader("Historique des alertes de cette session")
    rows = st.session_state.history
    if rows:
        st.dataframe(rows, use_container_width=True)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        st.download_button("📥 Télécharger CSV", out.getvalue(), "kairos_live_v21.csv", "text/csv")
    else:
        st.info("Aucune alerte enregistrée.")
    st.caption("Historique temporaire : perdu au redémarrage de l'application.")

st.divider()
st.caption("KAIROS LIVE V2.1 — Outil expérimental. Données retardées ou manquantes possibles. Les indices ne démontrent pas qu'un pari est rentable.")
