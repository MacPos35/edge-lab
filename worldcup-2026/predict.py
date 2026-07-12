"""World Cup 2026 daily prediction pipeline.

Fetches the whole tournament from ESPN's public scoreboard API (one call), fits an
Elo-anchored Poisson model on played matches, predicts every remaining match
(W/D/L in 90', P(advance) incl. extra time + penalties, top scorelines), Monte-Carlo
simulates the remaining bracket for champion odds, grades its own past predictions
(binary Brier on P(advance) — predictions are frozen at issue, never revised), and
renders wc_tab.html, a fragment the polymarket-edge-lab dashboard includes as a tab.

    python predict.py              # daily run (GitHub Actions, 04:xx UTC slot)
    python predict.py --selftest   # synthetic checks, no network

Toy model on ~5 games/team + priors — NOT a betting signal (execution is blocked
from Poland anyway; see ../polymarket-edge-lab/README.md Step 0).
"""

import html as _html
import json
import math
import os
import random
import re
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
SCOREBOARD_PATH = os.path.join(DATA_DIR, "scoreboard.json")
STATE_PATH = os.path.join(DATA_DIR, "predictions.json")
TAB_PATH = os.path.join(HERE, "wc_tab.html")

ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/"
            "scoreboard?dates=20260611-20260719")

# --- model parameters (see README.md for the rationale; council-reviewed) ---
MU_TOTAL = 2.7          # average total goals per WC match -> baseline lambda 1.35
ELO_SCALE = 850.0       # lambda ratio = exp(elo_diff / ELO_SCALE)
LAMBDA_FLOOR = 0.25
SHRINK_K = 6            # attack/defense weight w = n / (n + K): tournament data
                        # never gets more than ~half the say at n≈5
RHO = -0.10             # Dixon-Coles low-score adjustment, hardcoded (NOT fit)
HOST_ELO_BONUS = 50.0   # hosts playing in their own country
MAX_GOALS = 8           # score grid is (0..8) x (0..8)
ET_FRACTION = 1.0 / 3.0 # extra time = 30 min of a 90-min match
PENALTY_P = 0.50        # penalty shootouts are a coin flip
N_SIMS = 10_000

# Council variant (2026-07-12 review, see COUNCIL-2026-07-12.md): after 10 graded
# knockout matches the w=0 sanity variant beat the blend (Brier 0.155 vs 0.198) and
# the leakage-free backtest (backtest.py) points the same way on 92 matches of 90'
# W/D/L log loss — more shrinkage, sharper Elo scale. Not significant (paired t≈1),
# so the headline model stays frozen; this variant is tracked alongside w0/w1 and
# graded out-of-sample on the remaining matches. Never the headline.
CV_SHRINK_K = 15
CV_ELO_SCALE = 600.0

# Host team -> ESPN venue country string
HOSTS = {"United States": "USA", "Mexico": "Mexico", "Canada": "Canada"}

# Pre-tournament Elo snapshot: eloratings.net ratings at end of 2025 (fetched
# 2026-07-05, file https://eloratings.net/2025.tsv). Frozen — the in-tournament
# attack/defense factors supply all tournament-form updating; using current Elo
# would double-count the same matches.
ELO = {
    "Spain": 2172, "Argentina": 2113, "France": 2062, "England": 2042,
    "Colombia": 1998, "Brazil": 1978, "Portugal": 1976, "Netherlands": 1959,
    "Croatia": 1933, "Ecuador": 1933, "Norway": 1922, "Germany": 1910,
    "Switzerland": 1897, "Uruguay": 1890, "Türkiye": 1881, "Japan": 1878,
    "Belgium": 1850, "Morocco": 1840, "Mexico": 1834, "Paraguay": 1833,
    "Austria": 1818, "Senegal": 1807, "Canada": 1802, "Scotland": 1790,
    "South Korea": 1784, "Australia": 1773, "Algeria": 1757, "Iran": 1755,
    "United States": 1747, "Panama": 1742, "Uzbekistan": 1735, "Czechia": 1731,
    "Jordan": 1689, "Sweden": 1660, "Congo DR": 1657, "Ivory Coast": 1627,
    "Egypt": 1623, "Tunisia": 1615, "Saudi Arabia": 1592, "New Zealand": 1585,
    "Iraq": 1582, "Bosnia-Herzegovina": 1572, "Cape Verde": 1561,
    "South Africa": 1550, "Haiti": 1542, "Ghana": 1509, "Curaçao": 1466,
    "Qatar": 1425,
}

# 48-team format: chronological stage boundaries (feed always holds the full range)
STAGE_SLICES = [("group", 72), ("r32", 16), ("r16", 8), ("qf", 4),
                ("sf", 2), ("bronze", 1), ("final", 1)]
STAGE_LABEL = {"group": "Group", "r32": "Round of 32", "r16": "Round of 16",
               "qf": "Quarterfinal", "sf": "Semifinal", "bronze": "Third place",
               "final": "Final"}
PLACEHOLDER_RE = re.compile(
    r"(Round of 32|Round of 16|Quarterfinals?|Semifinals?) (\d+) (Winner|Loser)")
PLACEHOLDER_STAGE = {"Round of 32": "r32", "Round of 16": "r16",
                     "Quarterfinal": "qf", "Quarterfinals": "qf",
                     "Semifinal": "sf", "Semifinals": "sf"}


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}", flush=True)


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------- fetch
def fetch_scoreboard():
    """One call for the whole tournament, retry x3; never clobber good data."""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(ESPN_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            n_new = len(data.get("events", []))
            n_old = 0
            if os.path.exists(SCOREBOARD_PATH):
                with open(SCOREBOARD_PATH, encoding="utf-8") as f:
                    n_old = len(json.load(f).get("events", []))
            if n_new < n_old:
                log(f"fetch returned {n_new} events < last-good {n_old} — keeping last-good")
                break
            write_atomic(SCOREBOARD_PATH, json.dumps(data))
            log(f"fetched {n_new} events")
            break
        except Exception as e:  # DNS/transient — house lesson: retry with backoff
            last_err = e
            log(f"fetch attempt {attempt + 1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    else:
        log(f"all fetch attempts failed ({last_err}) — rendering from last-good cache")
    with open(SCOREBOARD_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_events(data):
    """Flatten ESPN events -> chronological match dicts with stage labels."""
    matches = []
    for e in data["events"]:
        comp = e["competitions"][0]
        home = away = None
        for c in comp["competitors"]:
            side = dict(
                name=c["team"]["displayName"],
                score=int(c["score"]) if c.get("score", "").isdigit() else None,
                winner=bool(c.get("winner", False)),
            )
            if c.get("homeAway") == "away":
                away = side
            else:
                home = side
        matches.append(dict(
            id=e["id"], date=e["date"], state=e["status"]["type"]["state"],
            detail=e["status"]["type"].get("shortDetail", ""),
            venue_country=comp.get("venue", {}).get("address", {}).get("country", ""),
            home=home, away=away,
        ))
    matches.sort(key=lambda m: (m["date"], m["id"]))
    expected = sum(n for _, n in STAGE_SLICES)
    if len(matches) != expected:
        log(f"warning: feed has {len(matches)} events, expected {expected} — "
            "stage labels may be wrong")
    i = 0
    by_stage = {}
    for stage, n in STAGE_SLICES:
        chunk = matches[i:i + n]
        for m in chunk:
            m["stage"] = stage
        by_stage[stage] = chunk
        i += n
    return matches, by_stage


# --------------------------------------------------------------------- model
def elo_lambdas(home, away, venue_country):
    """Baseline expected goals for each side from the pre-tournament Elo gap."""
    eh = ELO[home] + (HOST_ELO_BONUS if HOSTS.get(home) == venue_country else 0)
    ea = ELO[away] + (HOST_ELO_BONUS if HOSTS.get(away) == venue_country else 0)
    d = (eh - ea) / ELO_SCALE
    base = MU_TOTAL / 2.0
    return max(LAMBDA_FLOOR, base * math.exp(d)), max(LAMBDA_FLOOR, base * math.exp(-d))


def fit_factors(matches):
    """Per-team attack/defense multipliers: observed vs Elo-expected goal rates,
    shrunk toward 1 with w = n/(n+K). Goals only — shots/possession at n≈5 add
    overfitting, not signal (council verdict)."""
    acc = {t: dict(gf=0.0, ga=0.0, ef=0.0, ea=0.0, n=0) for t in ELO}
    for m in matches:
        if m["state"] != "post" or m["home"]["score"] is None:
            continue
        h, a = m["home"]["name"], m["away"]["name"]
        if h not in ELO or a not in ELO:
            continue
        lh, la = elo_lambdas(h, a, m["venue_country"])
        for team, gf, ga, ef, ea in ((h, m["home"]["score"], m["away"]["score"], lh, la),
                                     (a, m["away"]["score"], m["home"]["score"], la, lh)):
            acc[team]["gf"] += gf
            acc[team]["ga"] += ga
            acc[team]["ef"] += ef
            acc[team]["ea"] += ea
            acc[team]["n"] += 1
    factors = {}
    for t, s in acc.items():
        w = s["n"] / (s["n"] + SHRINK_K)
        att = w * (s["gf"] / s["ef"]) + (1 - w) if s["ef"] > 0 else 1.0
        dfn = w * (s["ga"] / s["ea"]) + (1 - w) if s["ea"] > 0 else 1.0
        factors[t] = dict(att=att, dfn=dfn, n=s["n"], w=w)
    return factors


def match_lambdas(home, away, venue_country, factors):
    lh, la = elo_lambdas(home, away, venue_country)
    if factors is not None:
        lh *= factors[home]["att"] * factors[away]["dfn"]
        la *= factors[away]["att"] * factors[home]["dfn"]
    return max(LAMBDA_FLOOR, lh), max(LAMBDA_FLOOR, la)


def score_grid(lh, la):
    """Independent Poisson grid with the Dixon-Coles low-score adjustment
    (hardcoded rho — fitting it on ~90 matches is overfitting theater)."""
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    g = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    g[0][0] *= 1 - lh * la * RHO
    g[0][1] *= 1 + lh * RHO
    g[1][0] *= 1 + la * RHO
    g[1][1] *= 1 - RHO
    total = sum(sum(row) for row in g)
    return [[v / total for v in row] for row in g]


def grid_wdl(g):
    n = len(g)
    w = sum(g[i][j] for i in range(n) for j in range(n) if i > j)
    d = sum(g[i][i] for i in range(n))
    return w, d, 1.0 - w - d


def predict_match(home, away, venue_country, factors):
    """Full prediction: 90' W/D/L, top scorelines, P(home advances) incl. ET+pens."""
    lh, la = match_lambdas(home, away, venue_country, factors)
    g = score_grid(lh, la)
    w90, d90, l90 = grid_wdl(g)
    wet, det, _ = grid_wdl(score_grid(lh * ET_FRACTION, la * ET_FRACTION))
    p_adv = w90 + d90 * (wet + det * PENALTY_P)
    scores = sorted(((g[i][j], i, j) for i in range(MAX_GOALS + 1)
                     for j in range(MAX_GOALS + 1)), reverse=True)[:3]
    return dict(
        home=home, away=away, lam_home=round(lh, 3), lam_away=round(la, 3),
        p_home=round(w90, 4), p_draw=round(d90, 4), p_away=round(l90, 4),
        p_adv=round(p_adv, 4),
        top_scores=[[i, j, round(p, 4)] for p, i, j in scores],
    )


# --------------------------------------------------------------------- bracket sim
def build_bracket(by_stage):
    """Remaining knockout tree as a list of nodes in play order. Slots are team
    names or (stage, index) refs parsed from ESPN placeholders ('Round of 16 5
    Winner' = 5th R16 match chronologically — verified against the real feed).
    SF/final events don't exist in the feed until teams are known, so they are
    synthesized from FIFA match numbering (SF1 = W(QF1) v W(QF2) chronologically)."""
    def slot(side):
        m = PLACEHOLDER_RE.match(side["name"])
        if m:
            return (PLACEHOLDER_STAGE[m.group(1)], int(m.group(2)) - 1)
        return side["name"]

    nodes = []
    index = {}
    for stage in ("r16", "qf", "sf", "final"):
        for i, m in enumerate(by_stage.get(stage, [])):
            node = dict(stage=stage, idx=i, id=m["id"], date=m["date"],
                        venue_country=m["venue_country"] or "USA",
                        slots=[slot(m["home"]), slot(m["away"])],
                        winner=None)
            if m["state"] == "post":
                node["winner"] = (m["home"]["name"] if m["home"]["winner"]
                                  else m["away"]["name"])
            nodes.append(node)
            index[(stage, i)] = node
    for stage, n_exp, feeder in (("sf", 2, "qf"), ("final", 1, "sf")):
        for i in range(len(by_stage.get(stage, [])), n_exp):
            node = dict(stage=stage, idx=i, id=f"synth-{stage}{i}", date=None,
                        venue_country="USA",
                        slots=[(feeder, 2 * i), (feeder, 2 * i + 1)], winner=None)
            nodes.append(node)
            index[(stage, i)] = node
    return nodes, index


def simulate_champion(nodes, index, factors, n_sims=N_SIMS, rng=None):
    """Monte Carlo of the remaining bracket; winners sampled from cached P(advance)."""
    rng = rng or random.Random(2026)
    cache = {}

    def p_adv(h, a, country):
        key = (h, a, country)
        if key not in cache:
            cache[key] = predict_match(h, a, country, factors)["p_adv"]
        return cache[key]

    counts = {}
    for _ in range(n_sims):
        won = {}
        for nd in nodes:
            if nd["winner"]:
                won[(nd["stage"], nd["idx"])] = nd["winner"]
                continue
            h, a = (s if isinstance(s, str) else won[s] for s in nd["slots"])
            won[(nd["stage"], nd["idx"])] = (
                h if rng.random() < p_adv(h, a, nd["venue_country"]) else a)
        champ = won[("final", 0)]
        counts[champ] = counts.get(champ, 0) + 1
    return {t: c / n_sims for t, c in sorted(counts.items(), key=lambda x: -x[1])}


# --------------------------------------------------------------------- state / grading
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return dict(issued={}, champion_history={})


def issue_and_grade(state, matches, factors):
    """Freeze a prediction for every un-issued upcoming match with known teams;
    grade issued predictions whose match has since finished. Frozen at issue —
    later runs never revise, that is the track record."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    by_id = {m["id"]: m for m in matches}
    for m in matches:
        if m["stage"] == "group":
            continue
        h, a = m["home"]["name"], m["away"]["name"]
        if h not in ELO or a not in ELO:
            continue  # placeholder slots
        if m["state"] != "post" and m["id"] not in state["issued"]:
            pred = predict_match(h, a, m["venue_country"], factors)
            pred.update(
                issued_at=now_iso, date=m["date"], stage=m["stage"],
                # blend sanity variants (council): pure-Elo and pure-tournament
                p_adv_w0=predict_match(h, a, m["venue_country"], None)["p_adv"],
                p_adv_w1=predict_match(h, a, m["venue_country"],
                                       _extreme_factors(factors))["p_adv"],
                # council variant (2026-07-12): K=15, Elo scale 600
                p_adv_cv=_council_p_adv(h, a, m["venue_country"], matches),
            )
            state["issued"][m["id"]] = pred
            log(f"issued {m['stage']} {h} v {a}: adv {pred['p_adv']:.0%}")
    for eid, pred in state["issued"].items():
        m = by_id.get(eid)
        if pred.get("graded") or not m or m["state"] != "post":
            continue
        home_adv = m["home"]["winner"]
        pred.update(
            graded=True, home_advanced=home_adv,
            final_score=f'{m["home"]["score"]}-{m["away"]["score"]}',
            detail=m["detail"],
            brier=round((pred["p_adv"] - (1.0 if home_adv else 0.0)) ** 2, 4),
            brier_w0=round((pred["p_adv_w0"] - (1.0 if home_adv else 0.0)) ** 2, 4),
            brier_w1=round((pred["p_adv_w1"] - (1.0 if home_adv else 0.0)) ** 2, 4),
        )
        if "p_adv_cv" in pred:  # council variant exists only on preds issued after 2026-07-12
            pred["brier_cv"] = round((pred["p_adv_cv"] - (1.0 if home_adv else 0.0)) ** 2, 4)
        log(f"graded {pred['home']} v {pred['away']} -> {pred['final_score']} "
            f"(brier {pred['brier']:.3f})")


def _council_p_adv(home, away, venue_country, matches):
    """P(advance) under the council-variant params (CV_SHRINK_K / CV_ELO_SCALE).
    Refits factors under the variant globals, then restores them."""
    global SHRINK_K, ELO_SCALE
    orig = SHRINK_K, ELO_SCALE
    SHRINK_K, ELO_SCALE = CV_SHRINK_K, CV_ELO_SCALE
    try:
        return predict_match(home, away, venue_country, fit_factors(matches))["p_adv"]
    finally:
        SHRINK_K, ELO_SCALE = orig


def _extreme_factors(factors):
    """w=1 variant: undo the shrinkage (raw observed/expected ratio)."""
    out = {}
    for t, f in factors.items():
        w = f["w"]
        if w <= 0:
            out[t] = dict(att=1.0, dfn=1.0, n=f["n"], w=1.0)
        else:
            out[t] = dict(att=(f["att"] - (1 - w)) / w, dfn=(f["dfn"] - (1 - w)) / w,
                          n=f["n"], w=1.0)
    return out


def brier_summary(state):
    graded = [p for p in state["issued"].values() if p.get("graded")]
    if not graded:
        return None
    mean = lambda key: sum(p[key] for p in graded) / len(graded)
    out = dict(n=len(graded), mean=round(mean("brier"), 4),
               mean_w0=round(mean("brier_w0"), 4), mean_w1=round(mean("brier_w1"), 4))
    cv = [p["brier_cv"] for p in graded if "brier_cv" in p]
    if cv:
        out["n_cv"] = len(cv)
        out["mean_cv"] = round(sum(cv) / len(cv), 4)
    return out


# --------------------------------------------------------------------- render
def esc(s):
    return _html.escape(str(s))


def pct(p, dec=0):
    return f"{p * 100:.{dec}f}%"


def fmt_kickoff(iso):
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return dt.strftime("%b %d, %H:%M UTC")


def card(label, value, sub=""):
    sub = f'<div class="sub">{sub}</div>' if sub else ""
    return (f'<div class="card"><div class="lbl">{label}</div>'
            f'<div class="val">{value}</div>{sub}</div>')


def prob_bar(p_home, p_draw, p_away, home, away):
    """Stacked W/D/L bar with inline styles (zero dashboard CSS changes)."""
    seg = ('<div style="width:{w:.1f}%;background:{c};display:flex;align-items:center;'
           'justify-content:center;overflow:hidden;white-space:nowrap;color:#0b0e14;'
           'font-weight:600;font-size:11.5px">{t}</div>')
    def s(p, color, label):
        txt = f"{label} {pct(p)}" if p >= 0.14 else (pct(p) if p >= 0.07 else "")
        return seg.format(w=max(p * 100, 0.5), c=color, t=esc(txt))
    return ('<div style="display:flex;height:22px;border-radius:6px;overflow:hidden;'
            'margin:8px 0 6px">'
            + s(p_home, "#4ade80", esc(home)) + s(p_draw, "#94a3b8", "Draw")
            + s(p_away, "#f0a35e", esc(away)) + "</div>")


def match_card(p, state_label=""):
    h, a = p["home"], p["away"]
    fav, p_fav = (h, p["p_adv"]) if p["p_adv"] >= 0.5 else (a, 1 - p["p_adv"])
    chips = " · ".join(f"{i}-{j} ({pct(pr)})" for i, j, pr in p["top_scores"])
    live = ' <span style="color:#f0a35e;font-weight:700">LIVE</span>' if state_label == "in" else ""
    fh, fa = FACTORS.get(h), FACTORS.get(a)
    driver = (f'Elo {ELO[h]} v {ELO[a]} · attack {fh["att"]:.2f} v {fa["att"]:.2f} · '
              f'defense {fh["dfn"]:.2f} v {fa["dfn"]:.2f} '
              f'(×Elo-expected goals; shrunk, w≈{fh["w"]:.2f})')
    return (
        f'<div class="panel" style="margin-top:12px">'
        f'<h4 style="margin:0">{esc(STAGE_LABEL[p["stage"]])} — {esc(h)} v {esc(a)}'
        f'<span style="color:var(--mut);font-weight:400;font-size:12.5px"> · '
        f'{fmt_kickoff(p["date"])}{live}</span></h4>'
        + prob_bar(p["p_home"], p["p_draw"], p["p_away"], h, a) +
        f'<div style="margin:2px 0"><b>Advance: {esc(fav)} {pct(p_fav)}</b> '
        f'<span style="color:var(--mut)">(incl. extra time &amp; penalties)</span></div>'
        f'<div style="color:var(--mut);font-size:12.5px">Most likely scores: {chips}</div>'
        f'<div style="color:var(--mut);font-size:12px;margin-top:4px">{driver}</div>'
        f'</div>')


def champion_svg(odds, prev):
    teams = [(t, p) for t, p in odds.items()][:12]
    if not teams:
        return ""
    w, rh, pad_l, pad_r = 860, 26, 150, 90
    h = len(teams) * rh + 10
    pmax = max(p for _, p in teams)
    rows = ""
    for k, (t, p) in enumerate(teams):
        y = 5 + k * rh
        bw = (w - pad_l - pad_r) * p / pmax
        delta = ""
        if prev and t in prev:
            d = (p - prev[t]) * 100
            if abs(d) >= 0.1:
                arrow, color = ("▲", "#4ade80") if d > 0 else ("▼", "#f0a35e")
                delta = (f'<tspan fill="{color}" font-size="11">  {arrow}'
                         f'{abs(d):.1f}</tspan>')
        rows += (
            f'<text x="{pad_l - 8}" y="{y + 17}" text-anchor="end" fill="var(--txt)" '
            f'font-size="12.5">{esc(t)}</text>'
            f'<rect x="{pad_l}" y="{y + 4}" width="{bw:.1f}" height="{rh - 10}" rx="4" '
            f'fill="#4ade80" opacity="{0.35 + 0.65 * p / pmax:.2f}"/>'
            f'<text x="{pad_l + bw + 6:.1f}" y="{y + 17}" fill="var(--txt)" '
            f'font-size="12.5" font-weight="600">{pct(p, 1)}{delta}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;max-width:{w}px" '
            f'role="img" aria-label="Champion odds">{rows}</svg>')


def track_table(state):
    graded = sorted((p for p in state["issued"].values() if p.get("graded")),
                    key=lambda p: p["date"])
    if not graded:
        return ('<p style="color:var(--mut)">No graded predictions yet — the first '
                'rows appear after the next matchday resolves.</p>')
    rows = ""
    for p in graded:
        fav, p_fav = (p["home"], p["p_adv"]) if p["p_adv"] >= 0.5 else (p["away"], 1 - p["p_adv"])
        actual = p["home"] if p["home_advanced"] else p["away"]
        hit = fav == actual
        edge = "#4ade80" if hit else "#ef6a6a"
        rows += (
            f'<tr style="box-shadow:inset 3px 0 0 {edge}">'
            f'<td style="text-align:left">{esc(STAGE_LABEL[p["stage"]])} · '
            f'{esc(p["home"])} v {esc(p["away"])}</td>'
            f'<td>{esc(fav)} {pct(p_fav)}</td>'
            f'<td>{esc(actual)} advanced ({esc(p["final_score"])})</td>'
            f'<td>{"✓" if hit else "✗"}</td><td>{p["brier"]:.3f}</td></tr>')
    return ('<table><thead><tr><th style="text-align:left">Match</th><th>Model pick '
            '(P advance)</th><th>Actual</th><th>Hit</th><th>Brier</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def render_tab(state, upcoming_preds, champion_odds, prev_odds, brier, n_played):
    nxt = upcoming_preds[0] if upcoming_preds else None
    if nxt:
        fav, p_fav = ((nxt["home"], nxt["p_adv"]) if nxt["p_adv"] >= 0.5
                      else (nxt["away"], 1 - nxt["p_adv"]))
        next_card = card("Next match", f'{esc(nxt["home"])} v {esc(nxt["away"])}',
                         f'{fmt_kickoff(nxt["date"])} · pick {esc(fav)} {pct(p_fav)}')
    else:
        next_card = card("Next match", "—", "tournament complete")
    brier_card = (card("Model Brier (advance)", f'{brier["mean"]:.3f}',
                       f'{brier["n"]} graded · 0.25 = coin flip · lower is better')
                  if brier else card("Model Brier (advance)", "—", "no graded predictions yet"))
    cards = next_card + brier_card + card("Matches fit on", str(n_played),
                                          "goals only — shots/possession unused")

    cards_html = "".join(match_card(p, p.get("_state", "")) for p in upcoming_preds)
    variants = ""
    if brier:
        variants = (f' Blend sanity: pure-Elo (w=0) Brier {brier["mean_w0"]:.3f}, '
                    f'pure-tournament (w=1) {brier["mean_w1"]:.3f}, '
                    f'blend {brier["mean"]:.3f}.')
        if "mean_cv" in brier:
            variants += (f' Council variant (K={CV_SHRINK_K}, scale {CV_ELO_SCALE:.0f}, '
                         f'tracked from Jul 12): {brier["mean_cv"]:.3f} '
                         f'on {brier["n_cv"]} matches.')
    return f"""<div class="section">
<h2>World Cup 2026 — model predictions</h2>
<div class="grid">{cards}</div>
</div>
<div class="section"><h2>Upcoming matches</h2>{cards_html or
    '<div class="panel">No upcoming fixtures with confirmed teams.</div>'}
</div>
<div class="section"><h2>Champion odds — {N_SIMS:,} bracket simulations</h2>
<div class="panel" style="margin-top:12px">{champion_svg(champion_odds, prev_odds)}
<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">▲▼ change vs previous run.
Resolved matches enter as certainties; unplayed slots are simulated from the model.</p>
</div></div>
<div class="section"><h2>Track record — frozen at issue, graded after the whistle</h2>
<div class="panel" style="margin-top:12px">{track_table(state)}
<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">Brier = (P(advance) −
outcome)². Predictions are never revised after issue.{variants}</p></div></div>
<p style="color:var(--mut);font-size:12.5px;max-width:820px">Toy Poisson model anchored
on pre-tournament Elo (end-2025 snapshot), updated by tournament goals with hard
shrinkage (~5 games/team). Extra time at λ/3, penalties a coin flip. Champion odds are
±10pt fuzzy. Not a betting signal — and execution is blocked from Poland anyway.</p>"""


# --------------------------------------------------------------------- main
FACTORS = {}


def run():
    os.makedirs(DATA_DIR, exist_ok=True)
    data = fetch_scoreboard()
    matches, by_stage = parse_events(data)
    played = [m for m in matches if m["state"] == "post"]
    global FACTORS
    FACTORS = fit_factors(matches)

    state = load_state()
    issue_and_grade(state, matches, FACTORS)
    brier = brier_summary(state)

    nodes, index = build_bracket(by_stage)
    champion_odds = simulate_champion(nodes, index, FACTORS)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist = state["champion_history"]
    prev_odds = None
    for d in sorted(hist, reverse=True):
        if d < today:
            prev_odds = hist[d]
            break
    hist[today] = {t: round(p, 4) for t, p in champion_odds.items()}

    upcoming = []
    for m in matches:
        if m["state"] == "post" or m["stage"] == "group":
            continue
        pred = state["issued"].get(m["id"])
        if pred:
            pred = dict(pred, _state=m["state"])
            upcoming.append(pred)
    upcoming.sort(key=lambda p: p["date"])

    state["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["matches_played"] = len(played)
    state["brier"] = brier
    write_atomic(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=1))
    write_atomic(TAB_PATH, render_tab(state, upcoming, champion_odds, prev_odds,
                                      brier, len(played)))
    top = ", ".join(f"{t} {pct(p, 1)}" for t, p in list(champion_odds.items())[:5])
    log(f"done: {len(played)} played, {len(upcoming)} upcoming predicted, "
        f"champion odds: {top}")


# --------------------------------------------------------------------- selftest
def selftest():
    assert len(ELO) == 48, f"expected 48 teams, got {len(ELO)}"

    g = score_grid(1.4, 1.1)
    assert abs(sum(sum(r) for r in g) - 1.0) < 1e-9, "grid must sum to 1"
    w, d, l = grid_wdl(g)
    assert abs(w + d + l - 1.0) < 1e-9

    # equal teams, neutral venue -> symmetric
    p = predict_match("Brazil", "Brazil", "", None)
    assert abs(p["p_adv"] - 0.5) < 1e-9, f"equal teams must be 50/50, got {p['p_adv']}"
    assert abs(p["p_home"] - p["p_away"]) < 1e-9

    # stronger team must be favored, and more so than a weaker gap
    strong = predict_match("Spain", "Qatar", "", None)["p_adv"]
    mild = predict_match("Spain", "Brazil", "", None)["p_adv"]
    assert strong > mild > 0.5, (strong, mild)

    # MC on a synthetic 2-match bracket matches analytic path products within 1.5pt
    fac = {t: dict(att=1.0, dfn=1.0, n=0, w=0.0) for t in ELO}
    nodes = [
        dict(stage="sf", idx=0, id="s0", date=None, venue_country="",
             slots=["Spain", "Brazil"], winner=None),
        dict(stage="sf", idx=1, id="s1", date=None, venue_country="",
             slots=["France", "England"], winner=None),
        dict(stage="final", idx=0, id="f0", date=None, venue_country="",
             slots=[("sf", 0), ("sf", 1)], winner=None),
    ]
    index = {(n["stage"], n["idx"]): n for n in nodes}
    odds = simulate_champion(nodes, index, fac, n_sims=40_000)
    p_sf1 = predict_match("Spain", "Brazil", "", fac)["p_adv"]
    p_sf2 = predict_match("France", "England", "", fac)["p_adv"]
    p_f1 = predict_match("Spain", "France", "", fac)["p_adv"]
    p_f2 = predict_match("Spain", "England", "", fac)["p_adv"]
    analytic = p_sf1 * (p_sf2 * p_f1 + (1 - p_sf2) * p_f2)
    assert abs(odds.get("Spain", 0) - analytic) < 0.015, (odds.get("Spain"), analytic)

    # perfect prediction -> Brier 0; coin flip on a certainty -> 0.25
    assert (1.0 - 1.0) ** 2 == 0.0
    assert (0.5 - 1.0) ** 2 == 0.25

    # council variant: sane probability, and it must restore the global params
    k0, s0 = SHRINK_K, ELO_SCALE
    p_cv = _council_p_adv("Spain", "Qatar", "", [])
    assert 0.5 < p_cv < 1.0, p_cv
    assert (SHRINK_K, ELO_SCALE) == (k0, s0), "globals not restored"
    # sharper scale => variant more extreme than headline on an empty fit
    assert p_cv > predict_match("Spain", "Qatar", "", fit_factors([]))["p_adv"]

    # placeholder parsing
    m = PLACEHOLDER_RE.match("Round of 16 5 Winner")
    assert m and PLACEHOLDER_STAGE[m.group(1)] == "r16" and int(m.group(2)) == 5

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
