
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title="KAIROS LIVE V3.3", page_icon="🔴", layout="wide")
TZ = ZoneInfo("America/Toronto")
BASE = "https://v3.football.api-sports.io"


def secret_key():
    try:
        return str(st.secrets.get("API_FOOTBALL_KEY", "")).strip()
    except Exception:
        return ""


@st.cache_data(ttl=90, show_spinner=False)
def api_get(endpoint, key, params=()):
    try:
        response = requests.get(
            BASE + endpoint,
            headers={"x-apisports-key": key},
            params=dict(params),
            timeout=22,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return {"error": str(payload["errors"]), "data": [], "quota": {}}
        return {
            "error": "",
            "data": payload.get("response") or [],
            "quota": {
                "remaining": response.headers.get("x-ratelimit-requests-remaining"),
                "limit": response.headers.get("x-ratelimit-requests-limit"),
            },
        }
    except (requests.RequestException, ValueError) as exc:
        return {"error": str(exc), "data": [], "quota": {}}


def request(endpoint, **params):
    key = secret_key()
    if not key:
        return {"error": "Clé API_FOOTBALL_KEY absente des secrets Streamlit.", "data": [], "quota": {}}
    frozen = tuple(sorted((k, str(v)) for k, v in params.items()))
    # st.cache_data ne compte pas un appel réseau lorsque les arguments sont identiques.
    if "seen_calls" not in st.session_state:
        st.session_state.seen_calls = set()
    if "local_calls" not in st.session_state:
        st.session_state.local_calls = 0
    today = datetime.now(TZ).date().isoformat()
    if st.session_state.get("counter_day") != today:
        st.session_state.counter_day = today
        st.session_state.local_calls = 0
        st.session_state.seen_calls = set()
    token = (endpoint, frozen)
    if token not in st.session_state.seen_calls:
        if st.session_state.local_calls >= st.session_state.budget:
            return {"error": "Budget local atteint. Réessaie demain ou augmente le budget.", "data": [], "quota": {}}
        st.session_state.seen_calls.add(token)
        st.session_state.local_calls += 1
    result = api_get(endpoint, key, frozen)
    if result["quota"].get("remaining") is not None:
        st.session_state.remaining = result["quota"]["remaining"]
    return result


def number(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def stat_value(stats, names):
    for item in stats or []:
        if str(item.get("type", "")).strip().lower() in names:
            val = item.get("value")
            if isinstance(val, str):
                val = val.replace("%", "").strip()
            return number(val)
    return None


def fixture_info(item):
    f = item.get("fixture") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    score = item.get("score") or {}
    elapsed = number((f.get("status") or {}).get("elapsed")) or 0
    home = (teams.get("home") or {}).get("name", "Domicile")
    away = (teams.get("away") or {}).get("name", "Extérieur")
    gh = goals.get("home")
    ga = goals.get("away")
    return {
        "id": f.get("id"), "name": f"{home} vs {away}",
        "league": (item.get("league") or {}).get("name", ""),
        "minute": int(elapsed), "score": f"{gh if gh is not None else '?'}-{ga if ga is not None else '?'}",
        "goals": (gh + ga) if isinstance(gh, int) and isinstance(ga, int) else None,
        "halftime": (score.get("halftime") or {}),
        "status": (f.get("status") or {}).get("short", ""),
    }


def stats_for(fixture_id):
    res = request("/fixtures/statistics", fixture=fixture_id)
    if res["error"] or len(res["data"]) < 2:
        return None, res["error"] or "Statistiques absentes"
    a = res["data"][0].get("statistics") or []
    b = res["data"][1].get("statistics") or []
    fields = {
        "corners": {"corner kicks"},
        "shots": {"total shots"},
        "on_target": {"shots on goal"},
    }
    out = {}
    for field, aliases in fields.items():
        x, y = stat_value(a, aliases), stat_value(b, aliases)
        out[field] = (int(x), int(y)) if x is not None and y is not None else None
    return out, ""


def extract_live_odds(fixture_id):
    res = request("/odds/live", fixture=fixture_id)
    if res["error"]:
        return [], res["error"]
    selections = []
    for match in res["data"]:
        for bookmaker in match.get("bookmakers") or []:
            book_name = bookmaker.get("name", "Source inconnue")
            for bet in bookmaker.get("bets") or []:
                market = str(bet.get("name", ""))
                low = market.lower()
                # Exclure les périodes et marchés spécifiques aux équipes.
                if any(x in low for x in ("1st half", "2nd half", "first half", "second half", "half time", "halftime", "team", "home", "away", "asian", "race", "exact", "odd/even")):
                    continue
                if "corner" in low and any(x in low for x in ("total", "over/under", "match corners")):
                    kind = "corners"
                elif ("goal" in low or "over/under" in low) and "corner" not in low:
                    kind = "buts"
                else:
                    continue
                for val in bet.get("values") or []:
                    raw = str(val.get("value") or val.get("label") or "")
                    m = re.search(r"\b(over|under)\s*([0-9]+(?:[.,][0-9]+)?)\b", raw, re.I)
                    if not m:
                        continue
                    side, line = m.group(1).title(), float(m.group(2).replace(",", "."))
                    odd = number(val.get("odd") or val.get("odds"))
                    if odd is None or odd <= 1:
                        continue
                    selections.append({"kind": kind, "side": side, "line": line, "odd": odd, "book": book_name, "market": market})
    return selections, ""


def fmt(x):
    return f"{x:g}"


def progress(selection, goals, corners):
    total = goals if selection["kind"] == "buts" else corners
    if total is None:
        return "Progression indisponible : statistique manquante", False
    line = selection["line"]
    # Nombre entier minimal qui dépasse la ligne.
    needed_to_over = math.floor(line) + 1
    if selection["side"] == "Over":
        missing = max(0, needed_to_over - total)
        return (f"{total} actuellement · {missing} supplémentaire(s) nécessaire(s)" if missing else f"{total} actuellement · seuil déjà franchi"), missing == 0
    remaining = max(0, needed_to_over - total)
    if remaining == 0:
        return f"{total} actuellement · seuil déjà franchi (Under compromis)", True
    return f"{total} actuellement · {remaining} supplémentaire(s) avant de dépasser la ligne", False


def render_match(item, with_odds):
    info = fixture_info(item)
    with st.container(border=True):
        st.subheader(f"{info['name']} | {info['minute']}' | {info['score']}")
        st.caption(f"{info['league']} · ID {info['id']} · {info['status']}")
        stats, err = stats_for(info["id"])
        if stats is None:
            st.warning(f"Statistiques indisponibles : {err}")
            return
        corners = sum(stats["corners"]) if stats["corners"] is not None else None
        a, b, c = st.columns(3)
        a.metric("⚽ Buts", info["goals"] if info["goals"] is not None else "?")
        b.metric("🚩 Corners", corners if corners is not None else "?")
        c.metric("Tirs cadrés", " - ".join(map(str, stats["on_target"])) if stats["on_target"] else "N/D")
        if not with_odds:
            st.caption("Active « Charger les cotes » pour consulter les marchés, sans appel API supplémentaire avant cette action.")
            if st.button("💰 Charger les cotes live", key=f"odds_{info['id']}"):
                st.session_state[f"show_{info['id']}"] = True
        if with_odds or st.session_state.get(f"show_{info['id']}"):
            odds, odds_error = extract_live_odds(info["id"])
            if odds_error:
                st.warning(f"Cotes indisponibles : {odds_error}")
                return
            if not odds:
                st.info("Aucun marché Over/Under match complet avec ligne explicite n'a été trouvé.")
                return
            for kind, emoji in (("buts", "⚽"), ("corners", "🚩")):
                group = [x for x in odds if x["kind"] == kind]
                with st.expander(f"{emoji} TOTAL {kind.upper()} — {len(group)} sélections", expanded=bool(group)):
                    if not group:
                        st.caption("Aucune cote compatible.")
                    for s in sorted(group, key=lambda x: (x["line"], x["side"], x["book"])):
                        desc, crossed = progress(s, info["goals"], corners)
                        st.markdown(f"**{emoji} {s['side'].upper()} {fmt(s['line'])} {kind.upper()} — @{s['odd']:.2f}**")
                        st.caption(f"Match complet (selon marché API : {s['market']}) · {s['book']} · {desc}")
                        if crossed:
                            st.caption("⚠️ Ligne déjà franchie : vérifier si le marché est encore ouvert ou s'il s'agit d'un autre règlement.")
                        st.divider()
            st.info("Ces cotes sont des marchés à examiner, pas des picks à valeur prouvée. Une EV fiable exige un modèle probabiliste validé et des cotes à jour.")


st.title("🔴 KAIROS LIVE V3.3")
st.caption("Soccer live · Over/Under buts et corners · Heure du Québec · Mode économe")
if "budget" not in st.session_state:
    st.session_state.budget = 60
if "remaining" not in st.session_state:
    st.session_state.remaining = "Inconnu"

with st.sidebar:
    st.header("⚙️ Paramètres")
    st.slider("Budget local d'appels / jour", 5, 100, key="budget")
    match_limit = st.slider("Matchs analysés par scan", 1, 10, 3)
    min_minute, max_minute = st.slider("Minutes du match", 0, 120, (20, 85))
    st.metric("Appels estimés dans cette session", st.session_state.get("local_calls", 0))
    st.caption(f"Budget local restant : {max(0, st.session_state.budget - st.session_state.get('local_calls', 0))}")
    st.caption(f"Quota restant annoncé par l'API : {st.session_state.remaining}")
    st.warning("Le compteur local est indicatif : il peut se réinitialiser au redémarrage et n'inclut pas les autres applications. Seul le fournisseur connaît le quota réel.")
    if st.button("Vider le cache API"):
        api_get.clear()
        st.session_state.seen_calls = set()
        st.info("Cache vidé. Les prochains chargements pourront consommer des requêtes.")

if not secret_key():
    st.error('Ajoute ta clé dans Streamlit → Manage app → Settings → Secrets, au format : API_FOOTBALL_KEY = "ta_cle"')
    st.stop()

auto_tab, manual_tab, help_tab = st.tabs(["📡 Automatique", "🔎 Manuel", "📘 Fonctionnement"])
with auto_tab:
    st.subheader("Scanner les matchs en direct")
    st.caption("La liste live est mise en cache 90 secondes. Les statistiques et les cotes sont mises en cache 90 secondes également. Les cotes ne sont chargées qu'à la demande.")
    if st.button("🔄 LANCER LE SCAN", type="primary", use_container_width=True):
        live = request("/fixtures", live="all")
        if live["error"]:
            st.error(live["error"])
        else:
            pool = [x for x in live["data"] if min_minute <= fixture_info(x)["minute"] <= max_minute]
            st.session_state.auto_matches = pool[:match_limit]
            st.session_state.auto_total = len(pool)
    if "auto_matches" in st.session_state:
        st.caption(f"{len(st.session_state.auto_matches)} match(s) sélectionné(s) sur {st.session_state.get('auto_total', 0)} dans la plage choisie.")
        if not st.session_state.auto_matches:
            st.info("Aucun match dans cette plage de minutes.")
        for match in st.session_state.auto_matches:
            render_match(match, False)

with manual_tab:
    st.subheader("Analyser un match live précis")
    if st.button("🔍 Actualiser la liste des matchs"):
        live = request("/fixtures", live="all")
        if live["error"]:
            st.error(live["error"])
        else:
            st.session_state.live_list = live["data"]
    if st.session_state.get("live_list"):
        matches = st.session_state.live_list
        choices = {f"{fixture_info(x)['name']} | {fixture_info(x)['minute']}' | ID {fixture_info(x)['id']}": x for x in matches}
        chosen = st.selectbox("Match", list(choices))
        if st.button("🔎 ANALYSER CE MATCH", type="primary"):
            st.session_state.manual_match = choices[chosen]
    if st.session_state.get("manual_match"):
        render_match(st.session_state.manual_match, False)

with help_tab:
    st.markdown("""
**Lecture des marchés :** « ⚽ OVER 3.5 BUTS » signifie au moins 4 buts dans le match. « 🚩 UNDER 10.5 CORNERS » signifie au maximum 10 corners dans le match.

**Limites :** l'API peut fournir des statistiques manquantes, des cotes retardées, des marchés sans ligne explicite ou des marchés de période différente. Les marchés ambigus sont exclus. Aucune probabilité de victoire ni EV n'est inventée à partir des seules statistiques live.

**Économie d'API :** lance peu de scans, limite le nombre de matchs et charge les cotes seulement pour les matchs intéressants. Le cache est partagé côté serveur, mais le compteur local n'est pas un compteur de facturation exact.
""")
