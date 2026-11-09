
import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

st.set_page_config(page_title='KAIROS LIVE V3.1', page_icon='🔴', layout='wide')
API = 'https://v3.football.api-sports.io'
TZ = ZoneInfo('America/Toronto')


def number(value, default=0.0):
    try:
        if value is None or str(value).strip() in ('', '-', 'None'):
            return default
        return float(str(value).replace('%', '').strip())
    except (ValueError, TypeError):
        return default


def api_get(path, params=None):
    key = st.secrets.get('API_FOOTBALL_KEY', '')
    if not key:
        raise RuntimeError('Ajoute API_FOOTBALL_KEY dans les Secrets Streamlit.')
    response = requests.get(API + path, headers={'x-apisports-key': key}, params=params or {}, timeout=25)
    if response.status_code != 200:
        raise RuntimeError(f'API HTTP {response.status_code}: {response.text[:180]}')
    data = response.json()
    if data.get('errors'):
        raise RuntimeError(f"API : {data['errors']}")
    remaining = response.headers.get('x-ratelimit-requests-remaining')
    if remaining is not None:
        st.session_state.remaining = remaining
    return data.get('response', [])


@st.cache_data(ttl=90, show_spinner=False)
def live_list():
    return api_get('/fixtures', {'live': 'all'})


@st.cache_data(ttl=120, show_spinner=False)
def statistics(fixture_id):
    return api_get('/fixtures/statistics', {'fixture': fixture_id})


@st.cache_data(ttl=120, show_spinner=False)
def odds_live(fixture_id):
    return api_get('/odds/live', {'fixture': fixture_id})


def match_minute(fixture):
    return int(number(fixture.get('fixture', {}).get('status', {}).get('elapsed')))


def match_label(fixture):
    teams = fixture.get('teams', {})
    return f"{teams.get('home', {}).get('name', '?')} vs {teams.get('away', {}).get('name', '?')} | {match_minute(fixture)}' | ID {fixture['fixture']['id']}"


def stat_values(data):
    out = []
    for team in data[:2]:
        stats = {str(s.get('type', '')).lower(): number(s.get('value')) for s in team.get('statistics', [])}
        out.append({
            'shots': stats.get('total shots', 0),
            'on': stats.get('shots on goal', 0),
            'corners': stats.get('corner kicks', 0),
            'danger': stats.get('shots insidebox', 0),
            'red': stats.get('red cards', 0),
        })
    return out if len(out) == 2 else None


def activity(stats, minute):
    total_shots = sum(x['shots'] for x in stats)
    on = sum(x['on'] for x in stats)
    corners = sum(x['corners'] for x in stats)
    pace = 45 / max(15, minute)
    return min(100, round((total_shots * 2.5 + on * 5 + corners * 2) * pace))


def projections(fixture, stats):
    minute = match_minute(fixture)
    left = max(0, 90 - minute)
    shots = sum(x['shots'] for x in stats)
    on = sum(x['on'] for x in stats)
    corners = sum(x['corners'] for x in stats)
    # Heuristiques volontairement prudentes, non calibrées sur historique.
    goals_rate = (0.018 * shots + 0.065 * on + 0.012 * sum(x['danger'] for x in stats)) / max(20, minute)
    corners_rate = (corners + 1.5) / (minute + 20)
    return max(0.05, min(3.0, goals_rate * left)), max(0.1, min(12.0, corners_rate * left))


def poisson_tail(lam, minimum):
    if minimum <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - sum(math.exp(-lam) * lam**k / math.factorial(k) for k in range(minimum))))


def outcome_probability(current, remaining_mean, side, line):
    # Pour lignes entières et quart de but, les règles de remboursement / demi-gain
    # nécessitent un calcul de règlement complet : on les exclut ici.
    if abs(line % 1 - 0.5) > 1e-6:
        return None
    needed = math.floor(line) + 1 - current
    p_over = poisson_tail(remaining_mean, needed)
    return p_over if side == 'Over' else 1 - p_over


def parse_side_and_line(name):
    name = str(name).strip()
    match = re.search(r'\b(over|under)\s*\(?\s*(\d+(?:[.,]\d+)?)', name, re.I)
    if match:
        return match.group(1).capitalize(), float(match.group(2).replace(',', '.'))
    return None, None


def parse_markets(data):
    # Format documenté API-FOOTBALL /odds/live : response[].odds[].values[]
    picks = []
    for item in data:
        for market in item.get('odds', []) or []:
            market_name = str(market.get('name', ''))
            low = market_name.lower()
            if any(x in low for x in ('team', 'half', '1st', '2nd', 'first', 'second', 'asian', 'handicap', 'odd/even')):
                continue
            if 'corner' in low:
                category = 'Corners'
            elif any(x in low for x in ('goal', 'over/under', 'total')):
                category = 'Buts'
            else:
                continue
            for selection in market.get('values', []) or []:
                label = str(selection.get('value', ''))
                side, line = parse_side_and_line(label)
                if side is None:
                    side, line = parse_side_and_line(f"{selection.get('value', '')} {selection.get('handicap', '')}")
                if side is None or line is None:
                    continue
                odd = number(selection.get('odd'))
                if odd <= 1.0:
                    continue
                picks.append({'category': category, 'market': market_name, 'side': side, 'line': line, 'odd': odd})
    return picks


def analyze(fixture, raw_stats, raw_odds, ev_threshold):
    stats = stat_values(raw_stats)
    if not stats:
        return {'fixture': fixture, 'error': 'Statistiques absentes ou incomplètes.'}
    minute = match_minute(fixture)
    score = fixture.get('goals', {})
    goals = int(number(score.get('home'))) + int(number(score.get('away')))
    corners = int(sum(x['corners'] for x in stats))
    mu_goals, mu_corners = projections(fixture, stats)
    candidates = []
    excluded = 0
    for selection in parse_markets(raw_odds):
        if selection['category'] == 'Buts':
            current, mean = goals, mu_goals
        else:
            current, mean = corners, mu_corners
        prob = outcome_probability(current, mean, selection['side'], selection['line'])
        if prob is None:
            excluded += 1
            continue
        ev = (prob * selection['odd'] - 1) * 100
        candidates.append({**selection, 'prob': prob, 'ev': ev})
    candidates.sort(key=lambda x: x['ev'], reverse=True)
    return {
        'fixture': fixture, 'stats': stats, 'activity': activity(stats, minute),
        'mu_goals': mu_goals, 'mu_corners': mu_corners,
        'candidates': candidates, 'selected': [x for x in candidates if x['ev'] >= ev_threshold],
        'raw_markets': sum(len(x.get('odds', [])) for x in raw_odds),
        'excluded': excluded,
    }


def render_result(result, threshold):
    fixture = result['fixture']
    st.subheader(match_label(fixture))
    if 'error' in result:
        st.warning(result['error'])
        return
    s = result['stats']
    a, b, c = st.columns(3)
    a.metric('Tirs cadrés', f"{int(s[0]['on'])} - {int(s[1]['on'])}")
    b.metric('Tirs totaux', f"{int(s[0]['shots'])} - {int(s[1]['shots'])}")
    c.metric('Corners', f"{int(s[0]['corners'])} - {int(s[1]['corners'])}")
    st.progress(result['activity'] / 100, text=f"Activité : {result['activity']}/100")
    st.caption(f"Projection heuristique restante : {result['mu_goals']:.2f} buts, {result['mu_corners']:.2f} corners. Ce ne sont pas des xG officiels.")
    picks = result['candidates']
    if not picks:
        st.warning(f"Aucun marché Over/Under compatible trouvé ({result['raw_markets']} marchés bruts). Vérifier la disponibilité de /odds/live et les noms des marchés.")
        return
    if result['selected']:
        st.success(f"{len(result['selected'])} candidat(s) avec EV théorique ≥ {threshold:.0f} %")
    else:
        st.info(f"Aucun pick ne dépasse {threshold:.0f} % d'EV théorique. Voici le meilleur candidat, non recommandé.")
    for i, pick in enumerate(picks[:5]):
        prefix = '🥇 Meilleur candidat' if i == 0 else f'#{i + 1}'
        st.markdown(f"**{prefix} : {pick['side']} {pick['line']:g} {pick['category'].lower()} — cote {pick['odd']:.2f}**")
        st.write(f"Probabilité modèle : {pick['prob']:.1%} | EV théorique : {pick['ev']:+.1f}% | Marché : {pick['market']}")
        if i == 0:
            st.caption('Calcul fondé sur le rythme des tirs/corners, le temps restant et une loi de Poisson. Forte incertitude : ne pas interpréter comme une valeur prouvée.')
    with st.expander(f"Diagnostic : {len(picks)} sélections évaluées, {result['excluded']} lignes exclues"):
        st.write('Les lignes entières et asiatiques à quart sont exclues pour éviter un calcul de gain erroné. Les marchés de mi-temps et les totaux par équipe sont exclus.')


st.title('🔴 KAIROS LIVE V3.1')
st.caption('Football live · Over/Under buts et corners · Classement expérimental · Québec')
with st.sidebar:
    st.header('⚙️ Paramètres')
    max_matches = st.slider('Matchs à analyser par scan', 1, 10, 3)
    min_minute, max_minute = st.slider('Minutes du match', 0, 100, (20, 85))
    ev_threshold = st.slider('EV théorique minimale (%)', 0, 30, 5)
    min_activity = st.slider('Activité minimale /100', 0, 100, 65)
    st.caption('Cache : liste live 90 s, statistiques et cotes 120 s. Chaque requête non cachée compte dans le quota réel.')
    if 'remaining' in st.session_state:
        st.metric('Requêtes restantes annoncées par API', st.session_state.remaining)

auto, manual = st.tabs(['📡 Automatique', '🔎 Manuel'])
with auto:
    st.write('Le scan classe les matchs live selon leur activité puis analyse au maximum le nombre choisi.')
    if st.button('🔄 LANCER LE SCAN', type='primary', use_container_width=True):
        try:
            live = [f for f in live_list() if min_minute <= match_minute(f) <= max_minute]
            # Ne récupère les statistiques que pour les N premiers matchs pour économiser le quota.
            results = []
            for fixture in live[:max_matches]:
                fid = fixture['fixture']['id']
                stats = statistics(fid)
                parsed = stat_values(stats)
                if not parsed:
                    results.append({'fixture': fixture, 'error': 'Statistiques indisponibles.'})
                    continue
                if activity(parsed, match_minute(fixture)) < min_activity:
                    results.append({'fixture': fixture, 'error': 'Activité sous le seuil défini.'})
                    continue
                raw_odds = odds_live(fid)
                results.append(analyze(fixture, stats, raw_odds, ev_threshold))
            st.session_state.auto_results = results
            st.session_state.auto_total = len(live)
        except Exception as exc:
            st.error(str(exc))
    if 'auto_results' in st.session_state:
        st.caption(f"{len(st.session_state.auto_results)} matchs examinés sur {st.session_state.auto_total} matchs dans la plage de minutes.")
        for result in st.session_state.auto_results:
            with st.container(border=True):
                render_result(result, ev_threshold)
with manual:
    st.write('Choisis un match précis pour obtenir son classement des sélections.')
    if st.button('📋 Charger / actualiser la liste live'):
        try:
            st.session_state.manual_live = live_list()
        except Exception as exc:
            st.error(str(exc))
    live = st.session_state.get('manual_live', [])
    if live:
        fixture = st.selectbox('Match en direct', live, format_func=match_label)
        if st.button('🔍 ANALYSER CE MATCH', type='primary'):
            try:
                fid = fixture['fixture']['id']
                st.session_state.manual_result = analyze(fixture, statistics(fid), odds_live(fid), ev_threshold)
            except Exception as exc:
                st.error(str(exc))
    else:
        st.info('Charge la liste live pour sélectionner un match.')
    if 'manual_result' in st.session_state:
        with st.container(border=True):
            render_result(st.session_state.manual_result, ev_threshold)

st.divider()
st.caption('KAIROS LIVE V3.1 · Prototype non calibré et non validé. Les cotes peuvent être absentes, périmées ou différentes de celles du bookmaker. Aucun rendement garanti.')
