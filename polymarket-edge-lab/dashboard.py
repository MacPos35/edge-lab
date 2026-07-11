"""Generate a self-contained HTML dashboard for the Polymarket edge lab.

Reads the live SQLite data + config and writes an offline HTML file (default: the user's
Desktop). Reuses analyze.py so the numbers always match the real pre-registered test. Meant
to run hourly so the dashboard stays current; the page also auto-refreshes every 15 minutes.

    python dashboard.py [output.html]
"""

import html as _html
import json
import math
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone

import config
import db
import analyze

HERE = os.path.dirname(os.path.abspath(__file__))
DB_ABS = os.path.join(HERE, config.DB_PATH)
TARGET_WINDOW_DAYS = 49          # 7 weeks (midpoint of the 6-8 week collection window)

# Presentation-layer copies of the KILL_CRITERIA.md contract dates (display only —
# the contract file is authoritative; changing these changes nothing about the test).
READOUT_GATE_TS = datetime(2026, 8, 19, tzinfo=timezone.utc).timestamp()
HARD_STOP_TS = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()

# --- Paper-trading simulation (illustrative ONLY -- NOT part of the pre-registration).
# "What if execution were legal": bet against every longshot-band market the real test
# uses, at the same T-24h snapshot, settled at event time, with the frozen cost model
# from config.py. These sizing constants live here, not in config.py, because they are
# presentation-layer assumptions -- changing them does not touch the frozen test.
PAPER_START_EUR = 200.0
PAPER_STAKE_FRAC = 0.05          # 5% of the running bankroll per trade
DAYS_PER_MONTH = 30.4375         # for the monthly-return extrapolation


# --------------------------------------------------------------------- data
def gather():
    conn = sqlite3.connect(DB_ABS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    one = lambda q, *a: conn.execute(q, a).fetchone()

    markets = one("SELECT COUNT(*) n FROM markets")["n"]
    snaps = one("SELECT COUNT(*) n FROM snapshots")["n"]
    resolved = one("SELECT COUNT(*) n FROM markets WHERE outcome IS NOT NULL")["n"]
    first_ts = one("SELECT MIN(ts) t FROM snapshots")["t"]
    last_ts = one("SELECT MAX(ts) t FROM snapshots")["t"]
    lo, hi = config.LONGSHOT_BAND
    band_markets = one(
        "SELECT COUNT(DISTINCT market_id) n FROM snapshots WHERE implied_prob>=? AND implied_prob<?",
        lo, hi,
    )["n"]

    now = datetime.now(timezone.utc).timestamp()
    days = (now - first_ts) / 86400 if first_ts else 0.0

    # calibration + verdict via the real analyzer
    rows = analyze.load_rows(conn)
    rows.sort(key=lambda r: r[0])
    half = len(rows) // 2
    holdout = rows[half:]

    # paper-trading what-if (must select the exact same band markets as the analyzer).
    # On drift, degrade the Paper tab only -- an illustrative simulation must never
    # take down the real research page.
    trades = paper_trades(conn)
    n_band_analyze = sum(1 for r in rows if lo <= r[1] < hi)
    paper_error = None
    if len(trades) != n_band_analyze:
        paper_error = (f"paper_trades drift: {len(trades)} trades vs "
                       f"{n_band_analyze} analyzer band markets")
        print(f"WARNING: {paper_error}", file=sys.stderr)
        paper = None
    else:
        paper = simulate_paper(trades, first_ts, now) if first_ts else None

    market_rows = collect_market_rows(conn)
    health = pipeline_health(conn, first_ts, last_ts, now) if first_ts else None
    funnel = funnel_universe(conn, first_ts) if first_ts else None
    traj = trajectory_power(rows, now, first_ts)
    conn.close()

    return dict(
        markets=markets, snaps=snaps, resolved=resolved,
        first_ts=first_ts, last_ts=last_ts, days=days,
        band_markets=band_markets, usable=len(rows),
        health=health, funnel=funnel, traj=traj,
        rows=rows, holdout=holdout, market_rows=market_rows, paper=paper,
        paper_error=paper_error,
        # progress = USABLE markets (resolved + valid T-24h snapshot), matching the
        # analyzer's n>=MIN_RESOLVED_MARKETS gate -- raw resolved overstates it
        pct_sample=min(100.0, 100.0 * len(rows) / config.MIN_RESOLVED_MARKETS),
        pct_time=min(100.0, 100.0 * days / TARGET_WINDOW_DAYS),
    )


def collect_market_rows(conn):
    """Every tracked market with its latest snapshot and full implied-prob series.

    Sorted for the dashboard: active markets first (by liquidity desc), then resolved
    markets (by resolution date desc). The series drives the on-demand sparkline.
    """
    series = {}
    for r in conn.execute(
        "SELECT market_id, ts, implied_prob, liquidity FROM snapshots ORDER BY market_id, ts"
    ):
        series.setdefault(r["market_id"], []).append(
            (r["ts"], r["implied_prob"], r["liquidity"]))

    out = []
    for m in conn.execute(
        "SELECT id, question, slug, resolves_ts, outcome, active, closed FROM markets"
    ):
        s = series.get(m["id"], [])
        latest = s[-1] if s else None
        out.append(dict(
            id=m["id"],
            question=(m["question"] or "(untitled market)").strip(),
            slug=m["slug"],
            resolves_ts=m["resolves_ts"],
            outcome=m["outcome"],
            prob=latest[1] if latest else None,       # (ts, implied_prob, liquidity)
            liquidity=latest[2] if latest else None,
            last_ts=latest[0] if latest else None,
            hist=[(t, p) for (t, p, _liq) in s],
        ))

    active = [r for r in out if r["outcome"] is None]
    resolved = [r for r in out if r["outcome"] is not None]
    active.sort(key=lambda r: (r["liquidity"] or 0), reverse=True)
    resolved.sort(key=lambda r: (r["resolves_ts"] or 0), reverse=True)
    return active + resolved


def calibration_rows(rows):
    edges = config.PROB_BUCKETS
    out = []
    for i in range(len(edges) - 1):
        b = [r for r in rows if analyze.bucket_of(r[1]) == i]
        n = len(b)
        if n == 0:
            continue
        mean_impl = sum(r[1] for r in b) / n
        yes = sum(1 for r in b if r[2] >= 0.5)
        realized = yes / n
        lo, hi = analyze.wilson(yes, n)
        out.append(dict(band=f"{edges[i]:.2f}-{edges[i+1]:.2f}", n=n,
                        mean_impl=mean_impl, realized=realized,
                        gap=mean_impl - realized, lo=lo, hi=hi))
    return out


def longshot_edge(rows):
    lo_b, hi_b = config.LONGSHOT_BAND
    band = [r for r in rows if lo_b <= r[1] < hi_b]
    if not band:
        return None
    gross, glo, ghi = analyze.mean_ci([r[1] - r[2] for r in band])
    net = gross - config.EST_SPREAD_SLIPPAGE
    survives = (glo - config.EST_SPREAD_SLIPPAGE) > 0
    return dict(n=len(band), gross=gross, glo=glo, ghi=ghi, net=net, survives=survives)


# --------------------------------------------------------------------- health / trajectory
def pipeline_health(conn, first_ts, last_ts, now):
    """Live pipeline diagnostics: snapshot coverage, gaps, logs, resolver queue."""
    hours = [r[0] for r in conn.execute(
        "SELECT DISTINCT CAST(ts/3600 AS INT) h FROM snapshots ORDER BY h")]
    expected = (int(last_ts // 3600) - int(first_ts // 3600) + 1) if hours else 0
    gaps = []
    for a, b in zip(hours, hours[1:]):
        if b - a > 1:
            gaps.append((a * 3600, b - a - 1))   # (last good hour, hours missing)
    per_run = conn.execute(
        "SELECT MIN(c), AVG(c), MAX(c) FROM "
        "(SELECT COUNT(*) c FROM snapshots GROUP BY CAST(ts/3600 AS INT))").fetchone()

    pending = db.markets_needing_resolution(conn, now)
    oldest_pending_h = None
    if pending:
        oldest = conn.execute(
            "SELECT MIN(COALESCE(game_start_ts, resolves_ts)) FROM markets "
            "WHERE outcome IS NULL AND COALESCE(game_start_ts, resolves_ts) < ? "
            "AND COALESCE(game_start_ts, resolves_ts) >= ?",
            (now, now - db.RESOLVE_MAX_AGE_DAYS * 86400)).fetchone()[0]
        if oldest:
            oldest_pending_h = (now - oldest) / 3600

    db_bytes = os.path.getsize(DB_ABS) if os.path.exists(DB_ABS) else 0
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return dict(
        hour_buckets=len(hours), hours_expected=expected,
        hours_lost=max(0, expected - len(hours)), gaps=gaps[-8:],
        per_run=per_run, pending=len(pending), oldest_pending_h=oldest_pending_h,
        db_bytes=db_bytes, wal=wal,
        snap_age_h=(now - last_ts) / 3600 if last_ts else None,
    )


def funnel_universe(conn, first_ts):
    """Why resolved markets are / are not usable, + when markets first become liquid.

    One set-based pass (no N+1): per resolved market, its first snapshot and the
    nearest liquid snapshot's distance from the T-24h target.
    """
    target = config.SNAPSHOT_HORIZON_HOURS * 3600
    tol = config.SNAPSHOT_TOLERANCE_HOURS * 3600
    rows = conn.execute(
        """
        SELECT COALESCE(m.game_start_ts, m.resolves_ts) AS anchor,
               MIN(s.ts) AS first_snap,
               MIN(CASE WHEN s.liquidity >= :liq
                        THEN ABS(s.ts - (COALESCE(m.game_start_ts, m.resolves_ts) - :tgt))
                   END) AS best_dist
        FROM markets m LEFT JOIN snapshots s ON s.market_id = m.id
        WHERE m.outcome IS NOT NULL
          AND COALESCE(m.game_start_ts, m.resolves_ts) IS NOT NULL
        GROUP BY m.id
        """,
        dict(liq=config.MIN_LIQUIDITY_USD, tgt=target)).fetchall()

    cats = dict(usable=0, pre_coverage=0, entered_late=0, other=0)
    leads = []
    for r in rows:
        if r["first_snap"] is not None:
            leads.append((r["anchor"] - r["first_snap"]) / 3600)
        if r["best_dist"] is not None and r["best_dist"] <= tol:
            cats["usable"] += 1
        elif (r["anchor"] - target) + tol < first_ts:
            cats["pre_coverage"] += 1
        elif r["first_snap"] is not None and r["first_snap"] > (r["anchor"] - target) + tol:
            cats["entered_late"] += 1
        else:
            cats["other"] += 1
    leads.sort()
    n = len(leads)
    pct = lambda q: leads[min(n - 1, int(q * n))] if n else None
    return dict(total=len(rows), cats=cats,
                lead_p25=pct(0.25), lead_med=pct(0.50), lead_p75=pct(0.75))


def trajectory_power(rows, now, first_ts):
    """Accrual rates from steady-state UTC days -> projections to the contract dates,
    and the minimum gross edge the pre-registered test can detect there."""
    lo_b, hi_b = config.LONGSHOT_BAND
    by_day, band_by_day = {}, {}
    for anchor_ts, implied, _outcome in rows:
        d = datetime.fromtimestamp(anchor_ts, timezone.utc).date()
        by_day[d] = by_day.get(d, 0) + 1
        if lo_b <= implied < hi_b:
            band_by_day[d] = band_by_day.get(d, 0) + 1
    today = datetime.fromtimestamp(now, timezone.utc).date()
    # steady-state day = complete (before today, since today's markets are still
    # resolving) AND began >= 12h after collection started -- an anchor is coverable
    # iff anchor - horizon + tolerance >= first snapshot, i.e. anchor >= first_ts + 12h
    full = [d for d in by_day
            if d < today
            and datetime(d.year, d.month, d.day,
                         tzinfo=timezone.utc).timestamp() >= (first_ts or now) + 12 * 3600]
    days = len(full)
    rate = sum(by_day[d] for d in full) / days if days else None
    band_rate = sum(band_by_day.get(d, 0) for d in full) / days if days else None

    usable_now = len(rows)
    band_now = sum(1 for _t, p, _o in rows if lo_b <= p < hi_b)
    sd = math.sqrt(0.5 * (lo_b + hi_b) * (1 - 0.5 * (lo_b + hi_b)))  # Bernoulli SD at band mid

    def project(date_ts):
        d_days = max(0.0, (date_ts - now) / 86400)
        usable = usable_now + (rate or 0) * d_days
        band_holdout = (band_now + (band_rate or 0) * d_days) / 2  # analyzer holds out half
        min_edge = (1.96 * sd / math.sqrt(band_holdout) + config.EST_SPREAD_SLIPPAGE
                    if band_holdout >= 2 else None)
        return dict(days=d_days, usable=usable, band_holdout=band_holdout, min_edge=min_edge)

    cross_200 = None
    if usable_now >= config.MIN_RESOLVED_MARKETS:
        cross_200 = "reached"
    elif rate:
        cross_200 = now + (config.MIN_RESOLVED_MARKETS - usable_now) / rate * 86400
    return dict(rate=rate, band_rate=band_rate, days_measured=days,
                usable_now=usable_now, band_now=band_now, sd=sd, cross_200=cross_200,
                readout=project(READOUT_GATE_TS), hard_stop=project(HARD_STOP_TS))


# --------------------------------------------------------------------- paper P&L
def paper_trades(conn):
    """Longshot-band markets the strategy would have traded, with table metadata.

    Snapshot selection MUST mirror analyze.load_rows exactly (same anchor, T-24h
    target, tolerance, liquidity filter) -- gather() cross-checks the count against
    the real analyzer and refuses to render on drift.
    """
    target = config.SNAPSHOT_HORIZON_HOURS * 3600
    tol = config.SNAPSHOT_TOLERANCE_HOURS * 3600
    lo_b, hi_b = config.LONGSHOT_BAND
    out = []
    markets = conn.execute(
        "SELECT id, question, slug, COALESCE(game_start_ts, resolves_ts) AS anchor_ts, "
        "outcome FROM markets WHERE outcome IS NOT NULL "
        "AND COALESCE(game_start_ts, resolves_ts) IS NOT NULL"
    ).fetchall()
    for mk in markets:
        want_ts = mk["anchor_ts"] - target
        snap = conn.execute(
            """
            SELECT implied_prob, ABS(ts - ?) AS dist
            FROM snapshots
            WHERE market_id = ? AND liquidity >= ?
            ORDER BY dist ASC LIMIT 1
            """,
            (want_ts, mk["id"], config.MIN_LIQUIDITY_USD),
        ).fetchone()
        if snap and snap["dist"] <= tol and lo_b <= snap["implied_prob"] < hi_b:
            out.append(dict(
                ts=float(mk["anchor_ts"]),
                question=(mk["question"] or "(untitled market)").strip(),
                slug=mk["slug"],
                p=float(snap["implied_prob"]),
                outcome=float(mk["outcome"]),
            ))
    out.sort(key=lambda t: t["ts"])
    return out


def simulate_paper(trades, first_ts, now_ts):
    """Fixed-fractional paper bankroll: bet AGAINST each band longshot (buy the
    opposite side at 1-p), P&L booked at event time, frozen cost model applied.

    Per share: buying the NO side at (1-p) pays 1 if NO occurs, so profit/share
    = (p - outcome) minus EST_SPREAD_SLIPPAGE, plus GAS_USD flat per trade.
    EUR and USDC are treated 1:1 (stated on the page).
    """
    bank = PAPER_START_EUR
    curve = [(first_ts, bank)]
    wins = 0
    for t in trades:
        stake = bank * PAPER_STAKE_FRAC
        shares = stake / (1.0 - t["p"])
        pnl = shares * ((t["p"] - t["outcome"]) - config.EST_SPREAD_SLIPPAGE) - config.GAS_USD
        bank += pnl
        wins += 1 if t["outcome"] < 0.5 else 0
        t.update(stake=stake, shares=shares, pnl=pnl, bank=bank)
        curve.append((t["ts"], bank))
    curve.append((now_ts, bank))

    days = max((now_ts - first_ts) / 86400.0, 1e-9)
    n = len(trades)
    ret_total = bank / PAPER_START_EUR - 1.0
    monthly = (bank / PAPER_START_EUR) ** (DAYS_PER_MONTH / days) - 1.0 if n else None
    trades_per_month = n / days * DAYS_PER_MONTH

    # honesty anchors: how surprising is the current run, and what do costs alone do?
    p_clean = None
    if n and wins == n:  # probability of an all-win run IF prices were perfectly right
        p_clean = 1.0
        for t in trades:
            p_clean *= (1.0 - t["p"])
    null_monthly = None
    if n:
        pbar = sum(t["p"] for t in trades) / n
        r_null = (-config.EST_SPREAD_SLIPPAGE) / (1.0 - pbar) \
            - config.GAS_USD / (PAPER_START_EUR * PAPER_STAKE_FRAC)
        null_monthly = (1.0 + r_null * PAPER_STAKE_FRAC) ** trades_per_month - 1.0

    return dict(
        trades=trades, curve=curve, bank=bank, pnl=bank - PAPER_START_EUR,
        ret_total=ret_total, days=days, monthly=monthly,
        trades_per_month=trades_per_month, wins=wins, losses=n - wins,
        p_clean=p_clean, null_monthly=null_monthly,
    )


# --------------------------------------------------------------------- render helpers
def ts_str(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "—"


def bar(pct, cls=""):
    return (f'<div class="bar"><div class="fill {cls}" style="width:{max(2,min(100,pct)):.1f}%">'
            f'</div></div>')


def stat(label, value, sub=""):
    sub = f'<div class="sub">{sub}</div>' if sub else ""
    return f'<div class="card"><div class="lbl">{label}</div><div class="val">{value}</div>{sub}</div>'


# --------------------------------------------------------------------- markets tab
def fmt_pct(p):
    """Whole-percent like Polymarket, but keep near-certain longshots/favorites honest."""
    if p is None:
        return "—"
    if p <= 0:
        return "0%"
    if p >= 1:
        return "100%"
    if p < 0.005:
        return "&lt;1%"
    if p > 0.995:
        return "&gt;99%"
    return f"{p * 100:.0f}%"


def fmt_liq(v):
    if v is None:
        return "—"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}k"
    return f"${v:.0f}"


def fmt_short_date(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%b %d") if t else "—"


def fmt_long_date(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%b %d, %Y %H:%M UTC") if t else "—"


def market_url(r):
    """Exact Polymarket event URL when we have a slug, else a search on the question."""
    if r["slug"]:
        return f"https://polymarket.com/event/{r['slug']}"
    return "https://polymarket.com/markets?_q=" + urllib.parse.quote(r["question"])


def status_badge(r):
    if r["outcome"] is None:
        return '<span class="mb act">● Active</span>'
    if r["outcome"] >= 0.5:
        return '<span class="mb yes">✓ Resolved YES</span>'
    return '<span class="mb no">✗ Resolved NO</span>'


def sparkline(hist, w=560, h=104):
    """Self-contained SVG of implied probability over time (y fixed to 0–100%)."""
    pts = [(t, p) for (t, p) in hist if p is not None]
    L, R, T, B = 34, 10, 12, 14

    def Y(p):
        return T + (1 - p) * (h - T - B)

    if not pts:
        return '<div class="sparknote">No snapshots recorded yet.</div>'

    grid = ""
    for gp in (0.0, 0.5, 1.0):
        gy = Y(gp)
        grid += (f'<line x1="{L}" y1="{gy:.1f}" x2="{w - R}" y2="{gy:.1f}" class="grid"/>'
                 f'<text x="0" y="{gy + 3:.1f}" class="gtxt">{int(gp * 100)}%</text>')

    if len(pts) == 1:
        cx, cy = (L + w - R) / 2, Y(pts[0][1])
        return (f'<svg class="spark" viewBox="0 0 {w} {h}" role="img">{grid}'
                f'<circle cx="{cx:.0f}" cy="{cy:.1f}" r="3.5" class="dot"/></svg>'
                '<div class="sparknote">Only one snapshot so far — the line fills in as data '
                'collects.</div>')

    tmin = min(t for t, _ in pts)
    tmax = max(t for t, _ in pts)
    span = (tmax - tmin) or 1

    def X(t):
        return L + (t - tmin) / span * (w - L - R)

    line = " ".join(f"{X(t):.1f},{Y(p):.1f}" for t, p in pts)
    area = f"{X(tmin):.1f},{h - B:.1f} {line} {X(tmax):.1f},{h - B:.1f}"
    cls = "up" if pts[-1][1] >= pts[0][1] else "down"
    lx, ly = X(pts[-1][0]), Y(pts[-1][1])
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" role="img">{grid}'
            f'<polygon points="{area}" class="fill"/>'
            f'<polyline points="{line}" class="ln" pathLength="1"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.5" class="dot"/></svg>')


def market_detail(r):
    spark = sparkline(r["hist"])
    if r["hist"]:
        probs = [p for _, p in r["hist"] if p is not None]
        lo, hi = (min(probs), max(probs)) if probs else (None, None)
        rng = "—"
        if probs:
            rng = fmt_pct(lo) if fmt_pct(lo) == fmt_pct(hi) else f"{fmt_pct(lo)}–{fmt_pct(hi)}"
        cells = [
            ("First", fmt_pct(r["hist"][0][1])),
            ("Latest", fmt_pct(r["prob"])),
            ("Range", rng),
            ("Liquidity", fmt_liq(r["liquidity"])),
            ("Snapshots", str(len(r["hist"]))),
            ("Resolves", fmt_long_date(r["resolves_ts"])),
        ]
        stats = '<div class="mstats">' + "".join(
            f"<div><span>{lbl}</span>{val}</div>" for lbl, val in cells) + "</div>"
    else:
        stats = ""
    link = (f'<a class="mkt-link" href="{market_url(r)}" target="_blank" rel="noopener">'
            f'Open on Polymarket ↗</a>')
    return f'<div class="mkt-detail-in">{spark}{stats}{link}</div>'


def market_row(r):
    q = _html.escape(r["question"])
    qattr = _html.escape(r["question"].lower(), quote=True)
    barw = max(0, min(100, (r["prob"] or 0) * 100))
    return (
        f'<div class="mkt" data-q="{qattr}">'
        f'<div class="mkt-row">'
        f'<a class="m-q" href="{market_url(r)}" target="_blank" rel="noopener" title="{q}">'
        f'{q} <span class="ext">↗</span></a>'
        f'<span class="m-chance">{fmt_pct(r["prob"])}'
        f'<span class="cbar"><i style="width:{barw:.0f}%"></i></span></span>'
        f'<span class="m-status">{status_badge(r)}</span>'
        f'<span class="m-res">{fmt_short_date(r["resolves_ts"])}</span>'
        f'<span class="m-liq">{fmt_liq(r["liquidity"])}</span>'
        f'<button class="morebtn" type="button" aria-expanded="false">More ▾</button>'
        f'</div>'
        f'<div class="mkt-detail" hidden>{market_detail(r)}</div>'
        f'</div>')


def markets_tab(rows):
    n = len(rows)
    n_active = sum(1 for r in rows if r["outcome"] is None)
    head = ('<div class="mkt-head"><span>Market</span><span class="r">Chance</span>'
            '<span>Status</span><span class="r">Resolves</span><span class="r">Liquidity</span>'
            '<span></span></div>')
    body = "".join(market_row(r) for r in rows)
    return (
        '<div class="section"><h2>All tracked markets</h2>'
        '<p style="color:var(--mut);margin:-4px 0 0;max-width:820px">Every market in the research '
        'universe with its latest snapshot — the implied probability of the <b>reference '
        '(first-listed) outcome</b>, the same number the test uses. Percentages update when the '
        'dashboard regenerates (hourly) and the page auto-reloads (every 15 min). Click '
        '<b>More</b> on any row for its probability history.</p>'
        '<div class="mkt-toolbar">'
        '<input id="mkt-filter" type="text" placeholder="Filter markets by name…" '
        'autocomplete="off">'
        f'<span class="mkt-meta"><b id="mkt-count">{n}</b> shown · {n_active} active · '
        f'{n - n_active} resolved</span></div>'
        f'<div class="mkt-list">{head}{body}</div></div>')


# --------------------------------------------------------------------- paper P&L tab
def eur(v, signed=False):
    s = f"{abs(v):,.2f}"
    if signed:
        return ("+" if v >= 0 else "−") + "€" + s
    return ("−" if v < 0 else "") + "€" + s


def pct(v, signed=True, dec=1):
    if v is None:
        return "—"
    sign = ("+" if v >= 0 else "−") if signed else ("−" if v < 0 else "")
    return f"{sign}{abs(v) * 100:.{dec}f}%"


def _nice_step(span, target=3):
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def pnl_chart(paper):
    """Step equity curve: flat between events, jumps when a trade settles.

    Returns (svg_html, points_json) -- the JSON carries pixel coords + readout
    text for the crosshair/tooltip layer wired up in SCRIPT.
    """
    W, H, L, R, T, B = 960, 300, 58, 96, 18, 36
    curve = paper["curve"]
    tmin, tmax = curve[0][0], curve[-1][0]
    tspan = max(tmax - tmin, 1.0)
    vals = [v for _, v in curve]
    vmin, vmax = min(vals), max(vals)
    vspan = max(vmax - vmin, PAPER_START_EUR * 0.005)
    step = _nice_step(vspan * 1.3)
    ylo = math.floor((vmin - 0.15 * vspan) / step) * step
    yhi = math.ceil((vmax + 0.15 * vspan) / step) * step

    def X(t):
        return L + (t - tmin) / tspan * (W - L - R)

    def Y(v):
        return T + (yhi - v) / (yhi - ylo) * (H - T - B)

    # horizontal grid + y ticks (clean euro steps)
    grid = ""
    v = ylo
    while v <= yhi + 1e-9:
        gy = Y(v)
        lbl = f"€{v:,.0f}" if step >= 1 else f"€{v:,.2f}"
        grid += (f'<line x1="{L}" y1="{gy:.1f}" x2="{W - R}" y2="{gy:.1f}" class="grid"/>'
                 f'<text x="{L - 8}" y="{gy + 3.5:.1f}" class="gtxt" text-anchor="end">{lbl}</text>')
        v += step

    # x ticks on UTC day boundaries
    day = 86400
    first_mid = math.ceil(tmin / day) * day
    xstep = max(1, math.ceil((tspan / day) / 6)) * day
    t = first_mid
    while t <= tmax:
        gx = X(t)
        grid += (f'<line x1="{gx:.1f}" y1="{T}" x2="{gx:.1f}" y2="{H - B}" class="grid"/>'
                 f'<text x="{gx:.1f}" y="{H - B + 16}" class="xtxt">'
                 f'{datetime.fromtimestamp(t, timezone.utc).strftime("%b %d")}</text>')
        t += xstep

    # step path (hold value until the next settlement)
    px = [(X(t), Y(v)) for t, v in curve]
    pts = [f"{px[0][0]:.1f},{px[0][1]:.1f}"]
    for i in range(1, len(px)):
        pts.append(f"{px[i][0]:.1f},{px[i - 1][1]:.1f}")
        pts.append(f"{px[i][0]:.1f},{px[i][1]:.1f}")
    line = " ".join(pts)
    ybot = H - B
    area = f"{px[0][0]:.1f},{ybot} " + line + f" {px[-1][0]:.1f},{ybot}"

    cls = "up" if paper["pnl"] >= 0 else "down"
    dots = ""
    for i, tr in enumerate(paper["trades"], start=1):  # curve[0]=start, curve[-1]=now
        cx, cy = px[i]
        dots += (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" class="dot" tabindex="0" '
                 f'data-i="{i}" aria-label="{_html.escape(tr["question"], quote=True)}: '
                 f'{_html.escape(eur(tr["pnl"], signed=True))}, bankroll '
                 f'{_html.escape(eur(tr["bank"]))}"/>')
    ex, ey = px[-1]
    endlbl = (f'<text x="{ex + 9:.1f}" y="{ey + 4.5:.1f}" class="endlbl">'
              f'{eur(paper["bank"])}</text>')

    svg = (f'<svg class="pnl {cls}" id="pnl-svg" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Paper bankroll over time">{grid}'
           f'<polygon points="{area}" class="fill"/>'
           f'<polyline points="{line}" class="ln" pathLength="1"/>'
           f'<line id="pnl-xh" class="xh" x1="0" y1="{T}" x2="0" y2="{H - B}" '
           f'visibility="hidden"/>{dots}{endlbl}</svg>')

    # tooltip data: one entry per curve vertex (start, each trade, now)
    points = []
    for i, (ts, bank) in enumerate(curve):
        when = datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d, %H:%M UTC")
        if i == 0:
            q, pnl_s, stake_s = "Test start — initial bankroll", None, None
        elif i == len(curve) - 1:
            q, pnl_s, stake_s = "Now — flat since last settled trade", None, None
        else:
            tr = paper["trades"][i - 1]
            q = tr["question"]
            pnl_s = eur(tr["pnl"], signed=True)
            stake_s = eur(tr["stake"])
        points.append(dict(x=round(px[i][0], 1), y=round(px[i][1], 1), when=when,
                           q=q, pnl=pnl_s, stake=stake_s, bank=eur(bank)))
    pjson = json.dumps(points).replace("<", "\\u003c")
    return svg, pjson


def paper_tab(d):
    p = d["paper"]
    lo_b, hi_b = config.LONGSHOT_BAND
    head = (
        '<div class="section"><h2>If Polymarket were legal — hypothetical paper P&amp;L</h2>'
        '<div class="hypo"><h3>⚠ Simulation only — no orders are or can be placed</h3>'
        '<p style="margin:0">Execution from Poland remains <b>blocked</b> (close-only). This page '
        're-prices the pre-registered strategy on the same public data the test collects: bet '
        f'<b>against</b> every longshot in the frozen {lo_b:.2f}–{hi_b:.2f} band at its '
        'T−24h snapshot, settle at event time, frozen cost model applied. It is an '
        'illustration of scale, <b>not</b> part of the pre-registered test and <b>not</b> evidence '
        'the edge is real — only the holdout verdict at n≥200 decides that.</p></div></div>')

    if d.get("paper_error"):
        return (head + '<div class="alert"><h3>Simulation unavailable this render</h3>'
                '<p style="margin:0">The paper-trade selection no longer matches the real '
                f'analyzer\'s band selection (<code>{_html.escape(d["paper_error"])}</code>), '
                'so the simulation is withheld rather than shown wrong. The pre-registered '
                'test on the other tabs is unaffected.</p></div>')

    if p is None:
        return head + '<div class="panel">No snapshots yet — the simulation starts with data collection.</div>'

    n = len(p["trades"])
    if n == 0:
        cards = stat("Paper bankroll", eur(p["bank"]), f"started {eur(PAPER_START_EUR)}")
        return (head + f'<div class="section"><div class="grid">{cards}</div>'
                '<div class="panel" style="margin-top:14px">No longshot-band trades have settled '
                'yet — the equity curve begins with the first resolved band market.</div></div>')

    pcls = "pos" if p["pnl"] >= 0 else "neg"
    mcls = "pos" if (p["monthly"] or 0) >= 0 else "neg"
    started = datetime.fromtimestamp(d["first_ts"], timezone.utc).strftime("%b %d, %H:%M UTC")
    cards = "".join([
        stat("Paper bankroll", eur(p["bank"]), f"started {eur(PAPER_START_EUR)} · {started}"),
        stat("Total P&amp;L", f'<span class="{pcls}">{eur(p["pnl"], signed=True)}</span>',
             f'{pct(p["ret_total"])} over {p["days"]:.1f} days'),
        stat("Avg monthly return", f'<span class="{mcls}">{pct(p["monthly"])}</span>',
             f'extrapolated from {p["days"]:.1f} days — see caveats'),
        stat("Trades settled", f'{n}', f'{p["wins"]} won · {p["losses"]} lost'),
        stat("Trade rate", f'≈{p["trades_per_month"]:.0f}/mo', "at current band flow"),
    ])

    svg, pjson = pnl_chart(p)
    chart = (
        '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">Paper equity curve '
        f'— {eur(PAPER_START_EUR)} start, all settled band trades</h4>'
        f'<div class="pnlwrap">{svg}<div class="pnltip" id="pnl-tip" hidden></div></div>'
        '<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">Steps mark trade '
        'settlements; the line holds flat between events. Hover or focus a dot for the trade '
        'readout. Dot = one settled trade.</p>'
        f'<script type="application/json" id="pnl-data">{pjson}</script></div>')

    rows_html = ""
    for i, t in enumerate(p["trades"], start=1):
        cls = "pos" if t["pnl"] >= 0 else "neg"
        won = "won" if t["outcome"] < 0.5 else "LOST"
        q = _html.escape(t["question"])
        rows_html += (
            f'<tr><td>{i}</td><td>{fmt_short_date(t["ts"])}</td>'
            f'<td style="text-align:left"><a href="{market_url(t)}" target="_blank" '
            f'rel="noopener">{q}</a></td>'
            f'<td>{t["p"] * 100:.1f}%</td><td>{1 - t["p"]:.3f}</td><td>{eur(t["stake"])}</td>'
            f'<td class="{cls}">{eur(t["pnl"], signed=True)} ({won})</td>'
            f'<td>{eur(t["bank"])}</td></tr>')
    table = (
        '<div class="panel ptable" style="margin-top:16px"><h4 style="margin-top:0">Every paper '
        'trade</h4><table><thead><tr><th>#</th><th>Settled</th><th>Market (we bet against this '
        'outcome)</th><th>Implied p</th><th>Entry price</th><th>Stake</th><th>P&amp;L</th>'
        '<th>Bankroll</th></tr></thead>'
        f'<tbody>{rows_html}</tbody></table></div>')

    assumptions = (
        '<div class="panel" style="margin-top:16px"><h4 style="margin-top:0">Simulation rules</h4>'
        '<ul class="clean" style="color:var(--mut)">'
        f'<li><b>Signal:</b> the exact markets the pre-registered test uses — implied prob in '
        f'[{lo_b:.2f}, {hi_b:.2f}) at the snapshot nearest T−24h (in-sample + holdout combined).</li>'
        '<li><b>Trade:</b> buy the opposite side at 1−p; it pays €1/share if the longshot '
        'fails, €0 if it hits (stake is fully lost).</li>'
        f'<li><b>Sizing:</b> fixed fraction — {PAPER_STAKE_FRAC * 100:.0f}% of the running '
        'bankroll per trade, compounding in settlement order.</li>'
        f'<li><b>Costs (frozen in config.py):</b> {config.EST_SPREAD_SLIPPAGE:.2f}/share '
        f'spread+slippage and ${config.GAS_USD:.2f} gas per trade.</li>'
        '<li><b>Simplifications:</b> EUR = USDC 1:1 (no FX), fills assumed at snapshot price, no '
        'deposit/withdrawal costs, no overlapping-position cash constraint.</li></ul></div>')

    cav = ""
    if p["p_clean"] is not None:
        cav += (f'<li>All {n} trades won so far — but even if prices were <i>perfectly '
                f'calibrated</i> (zero edge), a run this clean happens '
                f'<b>{p["p_clean"] * 100:.0f}% of the time</b>. Nothing is proven yet.</li>')
    cav += (
        f'<li>A losing trade forfeits its whole stake ({PAPER_STAKE_FRAC * 100:.0f}% of bankroll). '
        'At these prices expect roughly 1 loss per 10 trades — none has occurred yet, which '
        'flatters the extrapolation.</li>'
        f'<li>The monthly figure annualizes <b>{p["days"]:.1f} days</b> and {n} trades. It is '
        'statistically meaningless until the pre-registered sample '
        f'(n≥{config.MIN_RESOLVED_MARKETS}) is in — readout per KILL_CRITERIA.md.</li>')
    if p["null_monthly"] is not None:
        cav += (f'<li>If markets are calibrated (no edge), costs alone grind this strategy to '
                f'≈<b>{pct(p["null_monthly"])}/mo</b> — the null you are betting against.</li>')
    cav += ('<li>Council reality check: even with a real edge <i>and</i> venue access, realistic '
            'capacity at this account size is ≈€50–300/yr, not a compounding money '
            'machine.</li>')
    caveats = ('<div class="panel" style="margin-top:16px;border-color:rgba(255,176,32,.45)">'
               '<h4 style="margin-top:0;color:var(--warn)">Why you cannot bank the number above</h4>'
               f'<ul class="clean" style="color:var(--mut)">{cav}</ul></div>')

    return (head + f'<div class="section"><div class="grid">{cards}</div>{chart}{assumptions}'
            f'{caveats}{table}</div>')


# --------------------------------------------------------------------- trajectory panel
def trajectory_panel(d):
    """Overview panel: accrual rates, projections to the contract dates, and what
    edge size the pre-registered test can actually detect. Recomputed every render."""
    t = d["traj"]
    lo_b, hi_b = config.LONGSHOT_BAND
    if not t["rate"]:
        return ('<div class="panel"><h4 style="margin-top:0">Trajectory &amp; statistical power'
                '</h4><p style="color:var(--mut);margin:0">Appears automatically once at least '
                'one complete steady-state collection day exists (a full UTC day starting '
                '&ge;12h after the first snapshot). Check back tomorrow.</p></div>')

    if t["cross_200"] == "reached":
        cross = "reached ✓"
    elif t["cross_200"]:
        cross = "≈ " + fmt_short_date(t["cross_200"])
    else:
        cross = "—"
    ro, hs = t["readout"], t["hard_stop"]
    me_ro = f'{ro["min_edge"] * 100:.1f}pp' if ro["min_edge"] else "n/a"
    me_hs = f'{hs["min_edge"] * 100:.1f}pp' if hs["min_edge"] else "n/a"

    cards = "".join([
        stat("Usable / day", f'{t["rate"]:.0f}',
             f'measured over {t["days_measured"]} steady-state day(s)'),
        stat("n = 200 crossed", cross,
             f'{t["usable_now"]}/{config.MIN_RESOLVED_MARKETS} usable now'),
        stat("Usable at readout", f'≈{ro["usable"]:,.0f}',
             f'gate opens {fmt_short_date(READOUT_GATE_TS)}'),
        stat("Band markets / day", f'{t["band_rate"]:.1f}' if t["band_rate"] else "0",
             f'longshots {lo_b:.2f}–{hi_b:.2f} · {t["band_now"]} so far'),
        stat("Holdout band n", f'≈{ro["band_holdout"]:.0f} / ≈{hs["band_holdout"]:.0f}',
             f'at readout / at hard stop {fmt_short_date(HARD_STOP_TS)}'),
    ])
    return (
        f'<div class="grid">{cards}</div>'
        '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">Statistical power '
        '— what this test <i>can</i> and <i>cannot</i> see</h4>'
        '<p style="color:var(--mut)">The pre-registered PASS needs the 95% confidence interval\'s '
        'lower bound on the holdout longshot band to clear the '
        f'{config.EST_SPREAD_SLIPPAGE:.2f} cost line. Profit per share in the band swings between '
        f'≈+{(lo_b + hi_b) / 2:.2f} (longshot fails) and ≈−{1 - (lo_b + hi_b) / 2:.2f} (longshot '
        f'hits), so the per-share standard deviation is ≈{t["sd"]:.2f} and the CI shrinks only '
        'with √n of the <b>band</b> sample — not the full usable sample.</p>'
        '<ul class="clean" style="color:var(--mut)">'
        f'<li>Smallest gross edge detectable at the <b>readout gate</b> '
        f'({fmt_short_date(READOUT_GATE_TS)}, holdout band n≈{ro["band_holdout"]:.0f}): '
        f'<b>≈{me_ro}</b> per share.</li>'
        f'<li>Smallest gross edge detectable at the <b>hard stop</b> '
        f'({fmt_short_date(HARD_STOP_TS)}, holdout band n≈{hs["band_holdout"]:.0f}): '
        f'<b>≈{me_hs}</b> per share.</li>'
        '<li>The favorite–longshot bias documented in the literature for this price range is '
        'typically <b>2–5pp gross</b> — smaller than either detection floor.</li></ul>'
        '<p style="color:var(--mut);margin-bottom:0"><b>Honest implication:</b> unless the '
        'Polymarket bias is unusually large, the likely readout is a <b>correct '
        '"not detectable at this sample size"</b> — a valid, pre-registered null, not a failure. '
        'This was a property of the frozen design from day one; per KILL_CRITERIA.md nothing '
        'gets changed mid-flight, and these numbers are informational only.</p></div>')


# --------------------------------------------------------------------- health tab
def fmt_bytes(b):
    return f"{b / 1e6:.1f} MB" if b >= 1e5 else f"{b / 1e3:.0f} kB"


JOBS_PANEL = (
    '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">Job logs — GitHub '
    'Actions</h4><p style="color:var(--mut)">The pipeline runs as one hourly workflow '
    '(<code>.github/workflows/update.yml</code>, cron :17): logger → resolver → dashboard → '
    'commit → watchdog. Per-run stdout lives in the repository\'s <b>Actions</b> tab, not in '
    'local log files. Healthy = one “scanned_liquid … snapshots_written” line (logger) and one '
    '“pending_checked … newly_resolved” line (resolver) per run; <code>newly_resolved=0</code> '
    'is normal when no tracked game has finished since the last run. Transient network errors '
    'are retried ×3 before a run gives up, and GitHub emails on any failed run.</p>'
    '<p style="color:var(--mut);font-size:12.5px;margin-bottom:0"><b>Dead-man\'s switch:</b> '
    'the final <code>watchdog.py</code> step fails the run — triggering the failure email — '
    'if the newest snapshot is &gt;6h old or resolved markets stop getting labeled, catching '
    'the silent case where every job exits 0 but no fresh data lands.</p></div>')


def health_tab(d):
    h, f, t = d["health"], d["funnel"], d["traj"]
    if not h:
        return '<div class="panel">No snapshots yet — health reporting starts with data collection.</div>'
    cov = 100.0 * h["hour_buckets"] / h["hours_expected"] if h["hours_expected"] else 0.0
    age_cls = "pos" if (h["snap_age_h"] or 99) <= 2 else "neg"
    pend_note = (f'oldest {h["oldest_pending_h"]:.0f}h past its event'
                 if h["oldest_pending_h"] else "queue empty")
    cards = "".join([
        stat("Newest snapshot", f'<span class="{age_cls}">{h["snap_age_h"]:.1f}h ago</span>',
             "pipeline runs hourly at :17 (GitHub Actions)"),
        stat("Hourly coverage", f'{cov:.0f}%',
             f'{h["hour_buckets"]}/{h["hours_expected"]} hours · {h["hours_lost"]} lost'),
        stat("Snapshots / run", f'{h["per_run"][1]:.0f}',
             f'min {h["per_run"][0]} · max {h["per_run"][2]}'),
        stat("Awaiting resolution", f'{h["pending"]}', pend_note),
        stat("Database", fmt_bytes(h["db_bytes"]), f'journal mode: {h["wal"]}'),
    ])

    if h["gaps"]:
        gap_rows = "".join(
            f'<tr><td>{ts_str(g0)}</td><td>{n}h</td></tr>' for g0, n in h["gaps"])
        gaps = ('<table><thead><tr><th>Last good hour before gap</th><th>Hours missing</th>'
                f'</tr></thead><tbody>{gap_rows}</tbody></table>')
    else:
        gaps = '<p class="pos" style="margin:0">No gaps — every hour since collection began has data.</p>'
    gaps_panel = (
        '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">Snapshot gaps</h4>'
        + gaps +
        '<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">Known causes of gaps: '
        'GitHub Actions cron is best-effort and can skip or delay under load, transient '
        'DNS/network failures (retried ×3 with backoff), plus two historical local-machine-era '
        'causes (sleep at task time; one <code>database is locked</code> collision, fixed '
        '2026-07-03 with WAL + busy_timeout). '
        'Each missing hour slightly thins the usable sample but does not bias it — outages are '
        'independent of match outcomes.</p></div>')

    cats, total = f["cats"], f["total"]
    def frow(label, key, expl):
        n = cats[key]
        p = 100.0 * n / total if total else 0
        return (f'<tr><td style="text-align:left">{label}</td><td>{n}</td><td>{p:.0f}%</td>'
                f'<td style="text-align:left;color:var(--mut)">{expl}</td></tr>')
    funnel_panel = (
        '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">Usable-market '
        f'funnel — why {cats["usable"]} of {total} resolved markets count</h4>'
        '<table><thead><tr><th>Category</th><th>N</th><th>%</th><th>Meaning</th></tr></thead><tbody>'
        + frow("Usable ✓", "usable",
               "has a liquid snapshot within ±12h of T−24h — enters the pre-registered test")
        + frow("Pre-coverage", "pre_coverage",
               "its T−24h window closed before collection began on "
               f"{fmt_short_date(d['first_ts'])} — startup transient, stops growing")
        + frow("Entered market late", "entered_late",
               "first appeared in the top-liquidity universe after its T−24h window — "
               "structural, see below")
        + frow("Other", "other", "sparse/illiquid snapshots around the target window")
        + '</tbody></table>'
        '<p style="color:var(--mut);margin-bottom:0"><b>The structural finding:</b> half of all '
        'resolved sports markets are first seen '
        + (f'<b>{f["lead_med"]:.1f}h before the game</b>' if (f["lead_med"] or 0) >= 0
           else f'<b>only after kick-off</b> (median {-f["lead_med"]:.1f}h into the game)')
        + f' (quartiles: {f["lead_p25"]:+.1f}h to {f["lead_p75"]:+.1f}h relative to game start; '
        'negative = seen only in-game). Sports markets only become liquid on game day, so a '
        'T−24h test can only ever see '
        'the subset already liquid a day ahead — which is also the only subset that would have '
        'been tradeable at T−24h. The low usable yield is a property of the market, not a bug.</p>'
        '</div>')

    update_panel = (
        '<div class="panel" style="margin-top:14px"><h4 style="margin-top:0">How this page keeps '
        'itself up to date</h4><ul class="clean" style="color:var(--mut)">'
        '<li><b>Data:</b> a GitHub Actions workflow (hourly at :17) runs logger → resolver → '
        'dashboard against <code>edge_lab.sqlite</code>, then commits the refreshed database '
        'and this page back to the repo; every number on every tab is recomputed each run.</li>'
        '<li><b>Page:</b> served from <code>docs/</code> by GitHub Pages and auto-reloads every '
        '15 minutes (<code>meta refresh</code>), so the browser tab stays within ~75 minutes '
        'of the newest render.</li>'
        '<li><b>Self-diagnosis:</b> the "Updated" badge in the header turns red if the page it '
        'is showing was generated &gt;2h ago — that means the workflow stopped producing '
        'renders.</li>'
        '<li><b>Dead-man\'s switch:</b> the workflow\'s final <code>watchdog.py</code> step '
        'fails the run (GitHub emails on failure) if snapshots are &gt;6h stale or outcomes '
        'stop getting labeled, even when every job exits cleanly.</li>'
        '</ul></div>')

    return ('<div class="section"><h2>Pipeline health — live</h2>'
            '<p style="color:var(--mut);margin:-4px 0 14px;max-width:820px">Everything below is '
            'recomputed from the database each time this page regenerates. '
            'Green = the experiment is collecting cleanly; anything red deserves a look at the '
            'workflow logs in the repo\'s Actions tab.</p>'
            f'<div class="grid">{cards}</div>{gaps_panel}'
            + JOBS_PANEL
            + funnel_panel + update_panel + '</div>')


# --------------------------------------------------------------------- lab notes tab
NOTES = """
<div class="section"><h2>Lab notes — decisions &amp; changes on the record</h2>
<p style="color:var(--mut);margin:-4px 0 14px;max-width:820px">This tab is the project's paper
trail: what was changed, what was found, and what was decided — each entry dated. The live numbers
these notes refer to are always current on the Overview and Health tabs; full detail lives in
<code>REVIEW-2026-07-03.md</code> in the project folder.</p>

<div class="panel"><h4 style="margin-top:0">2026-07-11 — Observability catch-up after the GitHub
Actions migration (maintenance, frozen parameters untouched)</h4>
<p style="color:var(--mut)">A code review found the monitoring/self-documentation layer still
described the retired Windows Task Scheduler deployment. Fixes: <code>watchdog.py</code> now
actually exists and runs as the workflow's final step (fails the run — and triggers GitHub's
failure email — if snapshots are &gt;6h stale or outcomes stop being labeled); the Health tab,
footer and timeline now describe the real hourly Actions pipeline instead of local task names
and log files; the paper-tab consistency check degrades that tab instead of aborting the whole
page; the workflow's <code>git push</code> retries ×4 with backoff so a transient failure cannot
silently drop an hour of snapshots. None of this touches the frozen test, the market selection,
the snapshot semantics or the analysis.</p></div>

<div class="panel" style="margin-top:16px"><h4 style="margin-top:0">2026-07-03 — Pipeline hardening (maintenance, frozen
parameters untouched)</h4>
<p style="color:var(--mut)">A code review found three data-loss failure modes already visible in
the logs. Fixes, all verified with a selftest + live smoke runs (backup of pre-change code and DB
in <code>backup-2026-07-03/</code>):</p>
<table><thead><tr><th style="text-align:left">Problem observed</th>
<th style="text-align:left">Fix applied</th></tr></thead><tbody>
<tr><td style="text-align:left">“database is locked” abort when jobs collided after the machine
woke from sleep (all catch-up tasks fire at once)</td>
<td style="text-align:left">SQLite WAL journal + 15s busy_timeout; network fetching moved outside
write transactions in logger &amp; resolver</td></tr>
<tr><td style="text-align:left">5 hourly runs lost to single transient DNS failures</td>
<td style="text-align:left">HTTP retry ×3 with 2s/4s backoff on DNS/timeouts/5xx/429</td></tr>
<tr><td style="text-align:left">Resolver re-fetched voided/ambiguous markets forever (unbounded
run growth)</td>
<td style="text-align:left">Resolution queue bounded at 14 days past the event</td></tr>
<tr><td style="text-align:left">28h outcome-labeling stall on Jul 2–3</td>
<td style="text-align:left">Root cause was the resolver being scheduled daily while everything
assumed hourly — schedule fixed, docstring corrected</td></tr>
<tr><td style="text-align:left">Naive timestamps parsed in local time (latent)</td>
<td style="text-align:left">Timezone-less values now assume UTC</td></tr>
</tbody></table>
<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">Known-and-accepted (not fixed, by
choice, to respect the feature freeze): this dashboard reloads the full snapshot table each render
(fine for the 12-week project life); the watchdog detects “resolver not running”, not “resolver
failing”; no git history (a one-time <code>git init</code> is recommended).</p></div>

<div class="panel" style="margin-top:16px"><h4 style="margin-top:0">2026-07-03 — Council check-in
#3: “is there a better strategy?” — verdict: <span class="pos">stay the course</span></h4>
<div class="cols" style="margin-top:10px">
  <div><h4 style="color:var(--good);margin:0 0 6px">Unanimous</h4>
  <ul class="clean" style="color:var(--mut)">
  <li><b>No news-research / event-prediction track:</b> ~100 honest, non-cherry-picked events at
  30–60 min each is a ~40× overrun of the 15 min/week budget; hand-picked events are
  unfalsifiable; even a win is untradeable from Poland.</li>
  <li><b>The frozen test stays frozen.</b></li>
  <li>The <b>structural findings</b> (markets appear ~0.4h before game time; extreme-price
  markets are scarce at every horizon) are the real scientific payload of this project.</li>
  </ul></div>
  <div><h4 style="color:var(--warn);margin:0 0 6px">Decided on the numbers</h4>
  <ul class="clean" style="color:var(--mut)">
  <li>A parallel “Track B” pre-registration (favorites band / shorter horizon) was seriously
  argued — and rejected because a power analysis on the live data showed <b>every</b>
  configuration is also underpowered before the Sep 30 hard stop (best case detects ≈6.4pp vs a
  2–5pp literature effect). Registering it would pre-commit a second predictable null.</li>
  <li>Only sanctioned act: a 5-minute dated <b>holdout boundary</b> note (“data after this
  timestamp is holdout for any future test”), preserving clean data for post-readout ideas.</li>
  <li><b>Chairman's standing rule:</b> that council was the one renegotiation this contract
  gets. Future urges to add tracks → reread that verdict, don't reconvene.</li>
  </ul></div>
</div></div>

<div class="panel" style="margin-top:16px"><h4 style="margin-top:0">The termination contract
(KILL_CRITERIA.md, pre-committed 2026-07-02) — what happens next, no matter what</h4>
<table><thead><tr><th style="text-align:left">Date</th><th style="text-align:left">Event</th>
<th style="text-align:left">Action</th></tr></thead><tbody>
<tr><td style="text-align:left">Until readout</td><td style="text-align:left">Collection
continues unattended</td><td style="text-align:left">Zero new feature work · maintenance ≤15
min/week · analyzer output not acted on</td></tr>
<tr><td style="text-align:left"><b>2026-08-19</b></td><td style="text-align:left">Readout gate
(if usable n ≥ 200 — already assured)</td><td style="text-align:left">Run the pre-registered
holdout test once, write up 1–2 pages, then archive: PASS → writeup, still no trading while
Poland is close-only · FAIL → equally publishable null writeup</td></tr>
<tr><td style="text-align:left"><b>2026-09-30</b></td><td style="text-align:left">Hard stop</td>
<td style="text-align:left">Readout happens with whatever sample exists; all four scheduled
tasks disabled; repo + writeup kept as the portfolio artifact</td></tr>
</tbody></table>
<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">Standing decisions independent of
the verdict: capital plan unchanged (buffer → world ETF in IKE); no on-chain betting venues; no
VPN/geoblock evasion, ever. If reading this creates an urge to renegotiate — that urge is the
thing the contract was built to catch.</p></div>
</div>
"""


CSS = r"""
/* ============ SWISS SIGNAL — white, exposed grid, signal red ============ */
:root{--bg:#ffffff;--panel:#ffffff;--panel2:#f4f4f2;--edge:#101010;--line:#dcdcd8;
--txt:#101010;--mut:#6e6e6a;--acc:#e8380d;--good:#0d7a3e;--warn:#a86a00;--bad:#cf1322;
--chartline:#e6e6e2}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);
font:14.5px/1.55 'Space Grotesk',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc);text-decoration-thickness:1px;text-underline-offset:2px}
.wrap{max-width:1120px;margin:0 auto;padding:0 0 80px;
border-left:1.5px solid var(--edge);border-right:1.5px solid var(--edge)}
.mono,.badge,.mkt-meta,.morebtn,.foot,.ticker,.tabs .tab{font-family:'Space Mono',monospace}

/* --- ticker marquee (full-bleed, above the frame) --- */
.ticker{overflow:hidden;white-space:nowrap;border-bottom:1.5px solid var(--edge);
background:var(--edge);color:#fff;font-size:11px;font-weight:700;letter-spacing:.1em;
text-transform:uppercase;padding:7px 0}
.ticker .in{display:inline-block}
.ticker span{margin:0 13px}
.ticker .sep{color:var(--acc)}

/* --- masthead / hero --- */
header.hero{position:relative;padding:0;border-bottom:1.5px solid var(--edge)}
.mast{display:flex;justify-content:space-between;gap:10px;padding:11px 16px;
border-bottom:1.5px solid var(--edge);font-family:'Space Mono',monospace;font-size:10px;
letter-spacing:.12em;text-transform:uppercase;color:var(--txt)}
.mast .cross{color:var(--acc);font-weight:700}
.hero h1{margin:0;padding:26px 14px 10px;font-weight:700;text-transform:uppercase;
font-size:clamp(64px,17vw,150px);line-height:.9;letter-spacing:-.045em}
.hero h1 .hdot{color:var(--acc)}
.hero .lede{margin:0;padding:6px 16px 20px;color:var(--txt);max-width:720px;font-size:15px}
.hero .lede b{border-bottom:2px solid var(--acc)}
.meta2{display:grid;grid-template-columns:1fr 1fr;border-top:1.5px solid var(--edge)}
.meta2>div{padding:12px 16px;font-family:'Space Mono',monospace;font-size:10px;
letter-spacing:.1em;text-transform:uppercase;color:var(--mut)}
.meta2>div+div{border-left:1.5px solid var(--edge)}
.meta2 b{display:block;color:var(--txt);font-size:12px;margin-top:3px;letter-spacing:.06em}
.badges{display:flex;flex-wrap:wrap;gap:0;border-top:1.5px solid var(--edge)}
.badge{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
padding:10px 14px;border-right:1.5px solid var(--edge);flex:0 1 auto;min-width:0}
.b-live{background:#fff;color:var(--txt)}
.b-blk{background:var(--acc);color:#fff}
.b-mut{background:var(--panel2);color:var(--mut);flex:1 1 auto;border-right:none}
.badge.b-blk a{color:#fff}

/* --- sections --- */
.section{margin-top:0;padding:30px 14px 6px}
.section h2{display:flex;align-items:baseline;gap:10px;font-size:19px;text-transform:uppercase;
letter-spacing:-.01em;color:var(--txt);margin:0 0 14px;font-weight:700;
padding-bottom:10px;border-bottom:1.5px solid var(--edge)}
.section h2::before{content:"";width:10px;height:10px;background:var(--acc);flex:0 0 auto;
align-self:flex-start;margin-top:.42em}

/* --- stat cards as modular grid boxes --- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:1.5px;
background:var(--edge);border:1.5px solid var(--edge)}
/* fill the trailing empty cells of the last row with a light hatch plate */
.grid::after,.mstats::after{content:"";grid-column:auto/-1;
background:repeating-linear-gradient(-45deg,#f0f0ed 0 5px,#fff 5px 10px)}
.card{background:var(--bg);border:none;border-radius:0;padding:14px 14px 13px;min-width:0}
.card .lbl{color:var(--mut);font-size:9.5px;text-transform:uppercase;letter-spacing:.12em;
font-family:'Space Mono',monospace}
.card .val{font-size:30px;font-weight:700;letter-spacing:-.03em;margin-top:8px;line-height:1.05;
overflow-wrap:anywhere}
.card .sub{color:var(--mut);font-size:10.5px;margin-top:5px;font-family:'Space Mono',monospace}
.panel{background:var(--bg);border:1.5px solid var(--edge);border-radius:0;padding:18px 16px}
.panel h4{letter-spacing:-.01em}

/* --- bars: hazard stripes in a framed track --- */
.bar{height:14px;background:var(--bg);border:1.5px solid var(--edge);border-radius:0;
overflow:hidden;margin:8px 0}
.fill{height:100%;border-radius:0;border-right:1.5px solid var(--edge);
background:repeating-linear-gradient(-45deg,var(--acc) 0 6px,transparent 6px 12px)}
.fill.time{background:repeating-linear-gradient(-45deg,var(--edge) 0 6px,transparent 6px 12px)}

.alert{border:1.5px solid var(--acc);background:#fff;border-radius:0;padding:18px 16px;
box-shadow:4px 4px 0 var(--acc)}
.alert h3{margin:0 0 8px;color:var(--acc);text-transform:uppercase;font-size:15px;
letter-spacing:.02em}

/* --- timeline --- */
.tl{list-style:none;margin:0;padding:0}
.tl li{position:relative;padding:0 0 22px 30px;border-left:1.5px solid var(--edge);margin-left:7px}
.tl li:last-child{border-left:1.5px solid transparent}
.tl .dot{position:absolute;left:-8px;top:3px;width:14px;height:14px;border-radius:0;
border:1.5px solid var(--edge);background:var(--bg)}
.dot.done{background:var(--edge)}.dot.now{background:var(--acc);border-color:var(--acc)}
.dot.lock{background:#fff;border-color:var(--acc);box-shadow:inset 0 0 0 3px #fff,inset 0 0 0 14px var(--acc)}
.dot.wait{background:var(--line)}
.tl h4{margin:0 0 3px;font-size:15px}
.tl .st{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;
font-family:'Space Mono',monospace}
.st.done{color:var(--good)}.st.now{color:var(--acc)}.st.lock{color:var(--bad)}.st.wait{color:var(--mut)}
.tl p{margin:4px 0 0;color:var(--mut);font-size:13.5px}

table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:.1em;
font-family:'Space Mono',monospace;border-bottom:1.5px solid var(--edge)}
td{font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--bad)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.q h4{margin:16px 0 4px;color:var(--acc);font-size:14px}
.q p{margin:0 0 6px;color:var(--txt)}
.q .who{color:var(--mut);font-size:12px}
details{background:var(--bg);border:1.5px solid var(--edge);border-radius:0;
padding:12px 14px;margin:10px 0}
summary{cursor:pointer;font-weight:700}
details p,details li{color:var(--mut);font-size:13.5px}
code{background:var(--panel2);border:1px solid var(--line);border-radius:0;padding:1px 5px;
font-size:12.5px;font-family:'Space Mono',monospace}
.foot{margin:30px 0 0;color:var(--mut);font-size:10.5px;letter-spacing:.04em;
border-top:1.5px solid var(--edge);padding:16px 14px 0}
ul.clean{margin:6px 0 0;padding-left:20px}ul.clean li{margin:5px 0}

/* --- tabs: boxed cells, sticky on desktop, bottom bar on phones --- */
.tabs{display:flex;gap:0;margin:0;border-bottom:1.5px solid var(--edge);background:var(--bg)}
.tab{flex:1 1 auto;text-align:center;padding:13px 10px 12px;border:none;
border-right:1.5px solid var(--edge);border-radius:0;color:var(--txt);text-decoration:none;
font-weight:700;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
background:var(--bg);position:static}
.tab:last-child{border-right:none}
.tab:hover{color:var(--acc)}
.tab.active{color:#fff;background:var(--edge);box-shadow:none}
.tabpanel{display:none}
.tabpanel.active{display:block}

/* --- markets tab --- */
.mkt-toolbar{display:flex;align-items:center;gap:14px;margin:18px 0 12px;flex-wrap:wrap}
#mkt-filter{flex:1 1 240px;min-width:0;background:var(--bg);border:1.5px solid var(--edge);
border-radius:0;color:var(--txt);padding:11px 14px;font-size:16px;
font-family:'Space Mono',monospace}
#mkt-filter:focus{outline:none;border-color:var(--acc);box-shadow:3px 3px 0 var(--acc)}
#mkt-filter::placeholder{color:var(--mut)}
.mkt-meta{color:var(--mut);font-size:10.5px;white-space:nowrap;letter-spacing:.06em;
text-transform:uppercase}
.mkt-list{border:1.5px solid var(--edge);border-radius:0;background:var(--bg);overflow:hidden}
.mkt-head,.mkt-row{display:grid;grid-template-columns:1fr 88px 132px 78px 78px 82px;gap:12px;
align-items:center}
.mkt-head{padding:11px 14px;background:var(--edge);color:#fff;
font-size:9.5px;text-transform:uppercase;letter-spacing:.12em;font-weight:700;
font-family:'Space Mono',monospace}
.mkt-head .r{text-align:right}
.mkt{border-bottom:1px solid var(--line)}
.mkt:last-child{border-bottom:none}
.mkt-row{padding:11px 14px}
.m-q{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--txt);
text-decoration:none;font-weight:500}
.m-q:hover{color:var(--acc)}
.m-q .ext{color:var(--mut);font-size:12px}
.m-chance{text-align:right;font-weight:700;font-size:17px;letter-spacing:-.02em}
.m-chance .cbar{display:block;height:3px;margin-top:5px;background:var(--panel2);
border-radius:0;overflow:hidden}
.m-chance .cbar i{display:block;height:100%;background:var(--acc)}
.m-status{font-size:12px}
.mb{display:inline-block;padding:3px 8px;border-radius:0;font-size:9.5px;font-weight:700;
letter-spacing:.08em;text-transform:uppercase;white-space:nowrap;border:1.5px solid var(--edge);
font-family:'Space Mono',monospace}
.mb.act{background:#fff;color:var(--txt)}
.mb.yes{background:#fff;color:var(--good);border-color:var(--good)}
.mb.no{background:#fff;color:var(--acc);border-color:var(--acc)}
.m-res,.m-liq{text-align:right;color:var(--mut);font-size:12px;
font-family:'Space Mono',monospace}
.morebtn{justify-self:end;cursor:pointer;background:var(--bg);color:var(--txt);
border:1.5px solid var(--edge);border-radius:0;padding:6px 11px;font-size:10px;font-weight:700;
letter-spacing:.08em;text-transform:uppercase}
.morebtn:hover{background:var(--edge);color:#fff}
.mkt-detail{padding:0 14px 14px}
.mkt-detail-in{background:var(--panel2);border:1.5px solid var(--edge);border-radius:0;
padding:14px 14px}
.spark{width:100%;height:auto;display:block}
.spark .grid{stroke:var(--chartline);stroke-width:1}
.spark .gtxt{fill:var(--mut);font-size:11px}
.spark .fill{fill:rgba(16,16,16,.06)}
.spark.up .fill{fill:rgba(13,122,62,.08)}
.spark.down .fill{fill:rgba(232,56,13,.08)}
.spark .ln{fill:none;stroke:var(--edge);stroke-width:2}
.spark.up .ln{stroke:var(--good)}
.spark.down .ln{stroke:var(--acc)}
.spark .dot{fill:var(--edge)}
.sparknote{color:var(--mut);font-size:12.5px;padding:6px 2px}
.mstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:1.5px;
background:var(--edge);border:1.5px solid var(--edge);margin-top:12px}
.mstats>div{background:var(--bg);border:none;border-radius:0;padding:8px 11px;
font-weight:700;font-size:14px}
.mstats>div span{display:block;color:var(--mut);font-weight:700;font-size:9px;
text-transform:uppercase;letter-spacing:.1em;margin-bottom:2px;
font-family:'Space Mono',monospace}
.mkt-link{display:inline-block;margin-top:12px;font-size:13px;font-weight:700}
@media(max-width:720px){
  .mkt-head{display:none}
  .mkt-row{display:flex;flex-wrap:wrap;gap:8px 14px}
  .m-q{flex:0 0 100%;white-space:normal;font-size:13.5px;line-height:1.35}
  .m-chance{font-size:17px;color:var(--acc)}
  .m-chance .cbar{display:none}
  .morebtn{margin-left:auto}
}

/* --- paper P&L tab --- */
.hypo{border:1.5px solid var(--edge);background:var(--panel2);
border-radius:0;padding:18px 16px;box-shadow:4px 4px 0 var(--edge)}
.hypo h3{margin:0 0 8px;color:var(--warn);text-transform:uppercase;font-size:14px;
letter-spacing:.02em}
.pnlwrap{position:relative}
.pnl{width:100%;height:auto;display:block}
.pnl .grid{stroke:var(--chartline);stroke-width:1}
.pnl .gtxt,.pnl .xtxt{fill:var(--mut);font-size:11px}
.pnl .xtxt{text-anchor:middle}
.pnl .ln{fill:none;stroke:var(--edge);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.pnl .fill{fill:rgba(16,16,16,.05)}
.pnl .dot{fill:var(--edge);stroke:#fff;stroke-width:2;cursor:pointer;outline:none}
.pnl.up .ln{stroke:var(--good)}
.pnl.up .fill{fill:rgba(13,122,62,.07)}
.pnl.up .dot{fill:var(--good)}
.pnl.down .ln{stroke:var(--acc)}
.pnl.down .fill{fill:rgba(232,56,13,.07)}
.pnl.down .dot{fill:var(--acc)}
.pnl .dot:focus{stroke:var(--edge)}
.pnl .endlbl{fill:var(--txt);font-size:13px;font-weight:700}
.pnl .xh{stroke:var(--mut);stroke-width:1;opacity:.55}
.pnltip{position:absolute;pointer-events:none;background:#fff;border:1.5px solid var(--edge);
border-radius:0;padding:9px 13px;font-size:12.5px;max-width:280px;z-index:5;
box-shadow:4px 4px 0 var(--edge)}
.pnltip .tv{font-size:15px;font-weight:700}
.pnltip .tq{color:var(--mut);margin-top:2px}
.pnltip .tw{color:var(--mut);font-size:11.5px;margin-top:2px}
.ptable td{font-variant-numeric:tabular-nums}

/* =============== mobile (iPhone-class) & motion polish — presentation only =============== */
a,button,.tab{-webkit-tap-highlight-color:transparent}
.mkt{content-visibility:auto;contain-intrinsic-size:auto 48px} /* keeps the 2.7k-row list smooth */

/* desktop: sticky boxed tab row */
.tabs{position:sticky;top:0;z-index:30;overflow-x:auto;overscroll-behavior-x:contain;
scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}

/* interactivity (hover states only where a real pointer exists) */
.morebtn,.tab,.mkt-row,#mkt-filter,.card{transition:background .18s ease,color .18s ease,
box-shadow .18s ease,transform .18s ease}
@media(hover:hover){
.card:hover{background:var(--panel2)}
.mkt-row:hover{background:var(--panel2)}
}
.morebtn:active{transform:translate(1px,1px)}
.tab:active{background:var(--panel2)}

/* motion: every animation (and every animated-from-hidden initial state) lives inside
   this media query, so reduced-motion users get a fully static, fully visible page */
@media (prefers-reduced-motion: no-preference){
html{scroll-behavior:smooth}
@keyframes fadeUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes fadeIn{from{opacity:0}}
@keyframes barGrow{from{width:0}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes tickmove{to{transform:translateX(-50%)}}
@keyframes blink{50%{opacity:.15}}
.ticker .in{animation:tickmove 26s linear infinite}
.b-live .pip,.mast .pip{display:inline-block;color:var(--acc);
animation:blink 1.3s steps(2) infinite}
header.hero h1{animation:fadeUp .55s ease both}
.tabpanel.active{animation:fadeUp .35s ease both}
.fill,.m-chance .cbar i{animation:barGrow 1s cubic-bezier(.22,1,.36,1) both}
.spark .ln,.pnl .ln{stroke-dasharray:1;stroke-dashoffset:1;
animation:draw 1.1s ease .15s forwards}
.spark .fill,.pnl .fill{animation:fadeIn .9s ease .5s both}
.mkt-detail-in{animation:fadeUp .32s ease both}
details[open] summary ~ *{animation:fadeUp .3s ease both}
/* reveal-on-scroll: initial hidden state requires JS (html.js) so no-JS stays visible */
.js .section{opacity:0;transform:translateY(16px);
transition:opacity .6s cubic-bezier(.22,1,.36,1),transform .6s cubic-bezier(.22,1,.36,1)}
.js .section.vis{opacity:1;transform:none}
}

/* wide content scrolls inside its own container instead of squeezing */
@media(max-width:760px){
table{display:block;overflow-x:auto;white-space:nowrap;-webkit-overflow-scrolling:touch}
.pnlwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.pnl{min-width:640px}
#tab-worldcup .panel{overflow-x:auto;-webkit-overflow-scrolling:touch}
#tab-worldcup .panel svg{min-width:600px}
}

/* iPhone-width layout: tabs become a fixed bottom bar */
@media(max-width:700px){
.wrap{border-left:none;border-right:none;padding-bottom:110px}
.tabs{position:fixed;left:0;right:0;bottom:0;top:auto;z-index:60;overflow-x:hidden;
border-top:1.5px solid var(--edge);border-bottom:none;
padding-bottom:env(safe-area-inset-bottom);background:var(--bg)}
.tab{flex:1 1 0;min-width:0;padding:13px 2px 11px;font-size:9px;letter-spacing:.05em;
border-right:1px solid var(--line);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tab:last-child{border-right:none}
.section{padding:26px 12px 6px}
.hero .lede{font-size:14px;padding:6px 12px 18px}
.hero h1{padding:22px 10px 8px}
.grid{grid-template-columns:repeat(2,1fr)}
.card .val{font-size:24px}
.panel{padding:15px 13px}
.section h2{font-size:17px}
th,td{padding:7px 8px;font-size:12.5px}
.morebtn{padding:8px 12px}
.mstats{grid-template-columns:repeat(2,1fr)}
.meta2>div{padding:10px 12px}
.badges{flex-direction:column}
.badges .badge{padding:10px 12px;border-right:none;border-bottom:1.5px solid var(--edge)}
.badges .badge:last-child{border-bottom:none}
.foot{padding:16px 12px 0}
}
"""


def timeline(d):
    collecting = (f"Day {d['days']:.0f} of ~{TARGET_WINDOW_DAYS} · "
                  f"{d['usable']}/{config.MIN_RESOLVED_MARKETS} usable resolved")
    items = [
        ("done", "Step 0 — Legal / tax gate", "RESOLVED",
         "Verified against Polymarket primary sources: Poland is <b>close-only</b> (cannot open "
         "positions). Kalshi is US-only. Execution from Poland is legally blocked; the read-only "
         "research harness proceeds."),
        ("done", "Step 1 — Pre-registration", "FROZEN",
         "Causal mechanism, falsifiable hypothesis, buckets, snapshot horizon, liquidity filter and "
         "cost model locked in <code>config.py</code> before any resolved-outcome data existed."),
        ("done", "Steps 2–4 — Build &amp; schedule", "LIVE",
         "Logger, resolver and analyzer built and tested against the live Gamma API. A GitHub "
         "Actions workflow runs the logger, resolver and dashboard hourly (:17) and publishes "
         "this page via GitHub Pages; pipeline hardened 2026-07-03 (WAL, retries — see Lab "
         "notes)."),
        ("now", "Step 5 — Collect &amp; validate", "IN PROGRESS",
         f"Accumulating snapshots and resolutions for 6–8 weeks. {collecting}. The pre-registered "
         "read-out is the out-of-sample holdout, net of costs."),
        ("lock", "Step 6 — Decision gate", "BLOCKED (execution)",
         "If — and only if — a legal venue opens <b>and</b> the holdout edge survives net of costs, "
         "scope a separate, hard-capped €1,000 execution module. Until then this stays research; "
         "keep the €200/month in a low-cost index fund as the benchmark."),
    ]
    li = ""
    for cls, title, st, body in items:
        li += (f'<li><span class="dot {cls}"></span><h4>{title}</h4>'
               f'<span class="st {cls}">{st}</span><p>{body}</p></li>')
    return f'<ul class="tl">{li}</ul>'


def results_html(d):
    if d["resolved"] == 0:
        return ('<div class="panel"><b>No resolved markets yet.</b><br>'
                '<span style="color:var(--mut)">The calibration read-out appears here automatically '
                'once markets begin resolving (sports markets resolve within days). '
                f'Snapshots are being collected now — {d["snaps"]} so far across {d["markets"]} '
                'markets.</span></div>')
    cal = calibration_rows(d["rows"])
    tbody = ""
    for r in cal:
        gcls = "pos" if r["gap"] > 0 else ("neg" if r["gap"] < 0 else "")
        tbody += (f'<tr><td>{r["band"]}</td><td>{r["n"]}</td>'
                  f'<td>{r["mean_impl"]:.3f}</td><td>{r["realized"]:.3f}</td>'
                  f'<td class="{gcls}">{r["gap"]:+.3f}</td>'
                  f'<td>[{r["lo"]:.3f}, {r["hi"]:.3f}]</td></tr>')
    edge = longshot_edge(d["holdout"])
    if edge:
        vcls = "pos" if edge["survives"] else "neg"
        verdict = (
            f'<div class="panel"><h4 style="margin-top:0">Pre-registered holdout verdict '
            f'(longshot band {config.LONGSHOT_BAND[0]:.2f}–{config.LONGSHOT_BAND[1]:.2f})</h4>'
            f'<p>Gross edge / share: <b>{edge["gross"]:+.4f}</b> '
            f'(95% CI [{edge["glo"]:+.4f}, {edge["ghi"]:+.4f}], n={edge["n"]})<br>'
            f'Cost model: −{config.EST_SPREAD_SLIPPAGE:.4f} · '
            f'<b>NET edge / share: <span class="{vcls}">{edge["net"]:+.4f}</span></b><br>'
            f'Edge survives net of costs (CI lower bound &gt; cost): '
            f'<b class="{vcls}">{"YES" if edge["survives"] else "NO — not yet"}</b></p></div>')
    else:
        verdict = '<div class="panel">No holdout markets in the longshot band yet.</div>'
    note = "" if d["usable"] >= config.MIN_RESOLVED_MARKETS else (
        '<p style="color:var(--warn);font-size:13px">Below the pre-registered minimum sample — '
        'treat as provisional; keep collecting.</p>')
    return (verdict + note +
            '<div class="panel" style="margin-top:16px"><h4 style="margin-top:0">'
            'Full-sample calibration</h4><table><thead><tr><th>Implied bucket</th><th>N</th>'
            '<th>Mean implied</th><th>Realized</th><th>Gap</th><th>Wilson 95%</th></tr></thead>'
            f'<tbody>{tbody}</tbody></table>'
            '<p style="color:var(--mut);font-size:12.5px;margin-bottom:0">A positive <b>gap</b> '
            '(mean implied &gt; realized) in low buckets = longshots overpriced = the edge under '
            'test. The gap equals the expected profit per share of betting against that outcome.'
            '</p></div>')


COUNCIL = """
<div class="cols">
  <div class="panel"><h4 style="margin-top:0;color:var(--good)">Where the council agreed</h4>
  <ul class="clean">
  <li>At €1k–3.4k, absolute profit rounds to zero — the deliverable is a <b>validated edge + reusable infrastructure</b>, not euros.</li>
  <li>Don't let the installed Solana/memecoin toolkit pick the strategy — that's sunk-cost reasoning with the worst fixed-cost ratio.</li>
  <li>Validate the edge <b>before</b> building execution or risking a cent.</li>
  <li>The prediction-market favorite–longshot bias is the one route with a named mechanism and small enough universe to avoid p-hacking.</li>
  </ul></div>
  <div class="panel"><h4 style="margin-top:0;color:var(--warn)">Where it clashed &amp; what it caught</h4>
  <ul class="clean">
  <li><b>Tax:</b> asset or trap? Deferral is not alpha, and crypto losses ring-fence to crypto gains only. Best read: an <i>iteration</i> advantage, not a profit one.</li>
  <li><b>Build vs don't:</b> two advisors argued an index fund beats the bots — cap this at hobby/tuition risk.</li>
  <li><b>Blind spot (all 3 reviewers):</b> legal access. This is the gate that turned out to block execution from Poland.</li>
  <li>Nobody had verified the longshot edge survives fees/competition — which is exactly what this lab now measures.</li>
  </ul></div>
</div>
<div class="panel" style="margin-top:16px">
  <h4 style="margin-top:0;color:var(--acc)">The recommendation &amp; the one thing</h4>
  <p><b>Recommendation:</b> Treat this as an edge-validation project. Build the read-only logger, run the
  pre-registered out-of-sample test over 6–8 weeks, and only ever consider execution if the edge
  survives net of costs <i>and</i> a legal venue exists. Success = "a documented edge survived an honest
  test," not profit.</p>
  <p><b>The one thing:</b> answer the two kill-switch questions before funding anything — can a Polish
  resident legally trade it, and how is it taxed? <span class="pos">Done — and it revealed execution is
  blocked, saving you from building a dead-end.</span></p>
</div>
"""


GUIDE = """
<div class="panel">
<h4 style="margin-top:0">How the machine works</h4>
<p style="color:var(--mut)"><b>logger.py</b> (hourly) → snapshots the implied probability of the
top-liquidity two-outcome sports markets from Polymarket's public Gamma API into SQLite. No account,
no wallet, no trading. <b>resolve.py</b> (hourly) → records the YES/NO outcome of markets that have
resolved. <b>analyze.py</b> → buckets markets by implied probability at T−24h and compares each
bucket's price to how often it actually came true, on an untouched out-of-sample holdout, net of a
cost model. <b>dashboard.py</b> → regenerates this page hourly.</p>

<h4>Run anything by hand</h4>
<p><code>python logger.py</code> · <code>python resolve.py</code> · <code>python analyze.py</code>
· <code>python analyze.py --selftest</code> (verifies the math on synthetic injected-bias data) ·
<code>python dashboard.py</code></p>

<h4>How to read the result</h4>
<p style="color:var(--mut)">The <b>gap</b> = mean implied probability − realized rate in a bucket. In
the low (longshot) buckets, a persistent <b>positive</b> gap on the <i>holdout</i>, larger than the
cost model, means longshots are systematically overpriced and betting against them is +EV. If the
holdout gap is ~zero or the confidence interval crosses the cost line, there is <b>no tradeable
edge</b> — the honest and most common outcome, and a valid, valuable answer.</p>

<h4>Stop / go thresholds (pre-committed)</h4>
<ul class="clean" style="color:var(--mut)">
<li><b>Continue only if:</b> the holdout edge stays positive net of costs across ≥200 resolved markets.</li>
<li><b>Scale only if:</b> it also holds across two distinct market regimes — and a legal venue exists.</li>
<li><b>Stop if:</b> the edge decays out-of-sample, or the CI crosses the cost line. Decay is the
expected fate of most published patterns.</li>
</ul>
</div>
"""


GLOSSARY = """
<details><summary>Favorite–longshot bias</summary><p>Across racetrack, sports and prediction
markets, bettors systematically <b>overpay for unlikely "exciting" outcomes</b> (longshots) and
underprice near-certain favorites. The counterparty losing money is the recreational/partisan
bettor. This lab measures whether it is large enough on Polymarket to beat costs.</p></details>
<details><summary>Implied probability &amp; the "reference outcome"</summary><p>A share price in
[0,1] reads directly as a probability (0.08 = 8%). Each market has two outcomes; we fix the
<b>first-listed one</b> as the reference and track its price and whether it came true — this works
for Yes/No, team-vs-team and Over/Under alike.</p></details>
<details><summary>Out-of-sample / holdout</summary><p>Markets are split by resolution time; the
second half is never used for tuning. An edge only counts if it appears on that untouched holdout —
this is what separates a real edge from a curve-fit.</p></details>
<details><summary>Wilson interval &amp; cost model</summary><p>The Wilson 95% interval is an honest
error bar on a win-rate from a finite sample. The cost model subtracts spread/slippage (2%) + gas
before any edge is declared real, because at €1k <b>fixed costs dominate</b>.</p></details>
<details><summary>Polish tax facts (for when/if a legal venue exists)</summary><p>Flat 19% (PIT-38);
crypto-to-crypto swaps tax-neutral until fiat conversion; crypto ring-fenced as capital income (no
ZUS, no business reclassification); 5-year loss carryforward — but crypto losses offset only future
crypto gains. Prediction-market P&amp;L classification (crypto vs gambling) is unconfirmed and would
need an individual ruling (ORD-IN).</p></details>
<details><summary>The base-rate reality</summary><p>97% of persistent futures day traders lost
money; &lt;1% of day traders are reliably profitable. This is why the project is framed as skill +
infrastructure with capped tuition, not a profit centre.</p></details>
"""


SCRIPT = """<script>
(function(){
  // gate the reveal-on-scroll initial-hidden state on JS actually running
  document.documentElement.classList.add('js');
  var sections = document.querySelectorAll('.section');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (en.isIntersecting) { en.target.classList.add('vis'); io.unobserve(en.target); }
      });
    }, {rootMargin: '0px 0px -6% 0px'});
    sections.forEach(function(s){ io.observe(s); });
  } else {
    sections.forEach(function(s){ s.classList.add('vis'); });
  }

  function showTab(name){
    document.querySelectorAll('.tabpanel').forEach(function(p){
      p.classList.toggle('active', p.id === 'tab-' + name);
    });
    document.querySelectorAll('.tab').forEach(function(t){
      t.classList.toggle('active', t.dataset.tab === name);
    });
  }
  function fromHash(){
    var h = (location.hash || '').replace('#','');
    var valid = ['overview', 'markets', 'paper', 'health', 'notes', 'worldcup'];
    return valid.indexOf(h) !== -1 ? h : 'overview';
  }
  showTab(fromHash());
  window.addEventListener('hashchange', function(){
    showTab(fromHash());
    // a long page scrolled deep would otherwise open the next tab mid-nowhere
    var rm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({top: 0, behavior: rm ? 'auto' : 'smooth'});
  });

  var box = document.getElementById('mkt-filter');
  var count = document.getElementById('mkt-count');
  if (box) box.addEventListener('input', function(){
    var q = this.value.trim().toLowerCase(), n = 0;
    document.querySelectorAll('#tab-markets .mkt').forEach(function(el){
      var show = !q || el.dataset.q.indexOf(q) !== -1;
      el.style.display = show ? '' : 'none';
      if (show) n++;
    });
    if (count) count.textContent = n;
  });

  document.addEventListener('click', function(e){
    var btn = e.target.closest('.morebtn');
    if (!btn) return;
    var detail = btn.closest('.mkt').querySelector('.mkt-detail');
    if (!detail) return;
    if (detail.hasAttribute('hidden')) {
      detail.removeAttribute('hidden'); btn.setAttribute('aria-expanded','true'); btn.textContent='Less \\u25B4';
    } else {
      detail.setAttribute('hidden',''); btn.setAttribute('aria-expanded','false'); btn.textContent='More \\u25BE';
    }
  });

  // --- staleness self-report: if the generator dies, the page flags itself ---
  var gb = document.getElementById('gen-badge');
  if (gb && gb.dataset.gen) {
    var ageH = (Date.now() / 1000 - parseInt(gb.dataset.gen, 10)) / 3600;
    if (ageH > 2) {
      gb.classList.remove('b-mut'); gb.classList.add('b-blk');
      gb.textContent = 'STALE — page generated ' + ageH.toFixed(1) +
        'h ago; check the edge-lab update workflow (Actions tab)';
    }
  }

  // --- paper P&L crosshair + tooltip (data is untrusted -> textContent only) ---
  var svg = document.getElementById('pnl-svg');
  var dataEl = document.getElementById('pnl-data');
  var tip = document.getElementById('pnl-tip');
  var xh = document.getElementById('pnl-xh');
  if (svg && dataEl && tip && xh) {
    var pts = JSON.parse(dataEl.textContent);
    var VBW = 960, VBH = 300;
    function row(cls, text){
      var div = document.createElement('div');
      div.className = cls; div.textContent = text; return div;
    }
    function show(i, wrapRect){
      var p = pts[i];
      tip.replaceChildren(
        row('tv', 'Bankroll ' + p.bank),
        row('tq', p.q + (p.pnl ? ' \\u2014 P&L ' + p.pnl + ' on ' + p.stake + ' stake' : '')),
        row('tw', p.when));
      tip.removeAttribute('hidden');
      var sx = wrapRect.width / VBW, sy = wrapRect.height / VBH;
      var lx = p.x * sx + 14;
      if (lx + tip.offsetWidth > wrapRect.width) lx = p.x * sx - tip.offsetWidth - 14;
      var ly = p.y * sy - tip.offsetHeight - 10;
      if (ly < 0) ly = p.y * sy + 14;
      tip.style.left = Math.max(0, lx) + 'px';
      tip.style.top = ly + 'px';
      xh.setAttribute('x1', p.x); xh.setAttribute('x2', p.x);
      xh.removeAttribute('visibility');
    }
    function hide(){ tip.setAttribute('hidden', ''); xh.setAttribute('visibility', 'hidden'); }
    svg.addEventListener('pointermove', function(e){
      var r = svg.getBoundingClientRect();
      var mx = (e.clientX - r.left) * VBW / r.width;
      var best = 0, bd = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - mx);
        if (d < bd) { bd = d; best = i; }
      }
      show(best, r);
    });
    svg.addEventListener('pointerleave', hide);
    svg.querySelectorAll('.dot').forEach(function(dot){
      dot.addEventListener('focus', function(){
        show(parseInt(dot.dataset.i, 10), svg.getBoundingClientRect());
      });
      dot.addEventListener('blur', hide);
    });
  }
})();
</script>"""


# --------------------------------------------------------------------- world cup tab
WC_TAB_PATH = os.path.join(HERE, "..", "worldcup-2026", "wc_tab.html")


def worldcup_tab():
    """Presentation-only include of the sibling worldcup-2026 project's pre-rendered
    fragment. Any failure degrades to a fallback panel; deliberately NOT called from
    gather() so it can never touch the frozen experiment's data path."""
    try:
        with open(WC_TAB_PATH, encoding="utf-8") as f:
            frag = f.read()
        age_h = (datetime.now(timezone.utc).timestamp()
                 - os.path.getmtime(WC_TAB_PATH)) / 3600
        stale = (' · <span style="color:var(--warn)">STALE — check the edge-lab update '
                 'workflow\'s daily predict slot</span>' if age_h > 26 else "")
        return (f'<p style="color:var(--mut);font-size:12.5px">Updated {age_h:.1f}h ago '
                f'by the daily worldcup-2026 pipeline{stale}</p>{frag}')
    except Exception as exc:  # noqa: BLE001
        return ('<div class="panel">World Cup pipeline output not available '
                f'({_html.escape(str(exc))}) — expected at '
                f'<code>{_html.escape(WC_TAB_PATH)}</code></div>')


def build_html(d):
    gen_ts = datetime.now(timezone.utc).timestamp()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    paper_card = ""
    if d["paper"] and d["paper"]["trades"]:
        pp = d["paper"]
        pcls = "pos" if pp["pnl"] >= 0 else "neg"
        paper_card = stat("Paper P&amp;L (what-if)",
                          f'<span class="{pcls}">{eur(pp["pnl"], signed=True)}</span>',
                          'hypothetical — see <a href="#paper">Paper P&amp;L</a> tab')
    cards = "".join([
        stat("Markets tracked", f'{d["markets"]:,}', "top-liquidity sports"),
        stat("Snapshots logged", f'{d["snaps"]:,}', f'last: {ts_str(d["last_ts"])}'),
        stat("Resolved markets", f'{d["resolved"]:,}',
             f'{d["usable"]} usable · target ≥ {config.MIN_RESOLVED_MARKETS}'),
        stat("Longshot markets", f'{d["band_markets"]:,}',
             f'band {config.LONGSHOT_BAND[0]:.2f}–{config.LONGSHOT_BAND[1]:.2f}'),
        stat("Days collecting", f'{d["days"]:.0f}', f'window ~{TARGET_WINDOW_DAYS}d'),
    ]) + paper_card
    progress = (
        f'<div class="panel"><b>Sample progress</b> — {d["usable"]}/{config.MIN_RESOLVED_MARKETS} '
        f'usable resolved markets ({d["resolved"]} resolved total; usable = has a valid T−24h '
        f'snapshot, what the analyzer counts) {bar(d["pct_sample"])}'
        f'<div style="margin-top:14px"><b>Time in collection window</b> — day {d["days"]:.0f} '
        f'of ~{TARGET_WINDOW_DAYS} {bar(d["pct_time"], "time")}</div></div>')

    paper_tick = ""
    if d["paper"] and d["paper"]["trades"]:
        pp = d["paper"]
        paper_tick = (f'<span>paper P&amp;L {eur(pp["pnl"], signed=True)}</span>'
                      f'<span class="sep">●</span>'
                      f'<span>{pp["wins"]} won / {pp["losses"]} lost</span>'
                      f'<span class="sep">●</span>')
    tick = (f'<span>Polymarket Edge Lab</span><span class="sep">●</span>'
            f'<span>{d["snaps"]:,} snapshots logged</span><span class="sep">●</span>'
            f'<span>{d["markets"]:,} markets tracked</span><span class="sep">●</span>'
            f'{paper_tick}'
            f'<span>day {d["days"]:.0f} of ~{TARGET_WINDOW_DAYS}</span><span class="sep">●</span>'
            f'<span>execution blocked (PL) — research only</span><span class="sep">●</span>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<meta http-equiv="refresh" content="900">
<title>Polymarket Edge Lab</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="ticker" aria-hidden="true"><div class="in">{tick}{tick}</div></div>
<div class="wrap">
<header class="hero">
  <div class="mast"><span>Polymarket Edge Lab</span>
  <span><span class="cross">+</span> read-only harness</span></div>
  <h1>Edge<br>Lab<span class="hdot">.</span></h1>
  <p class="lede">A read-only research harness validating the prediction-market
  <b>favorite–longshot bias</b> before a single euro is risked — the route chosen by your LLM
  Council for a €1,000 + €200/month account under a Polish tax standpoint.</p>
  <div class="meta2">
    <div>Objective<b>Longshot mispricing</b></div>
    <div>Method<b>Pre-registered holdout test</b></div>
  </div>
  <div class="badges">
    <span class="badge b-live"><span class="pip">●</span> Collecting live data</span>
    <span class="badge b-blk">⚠ Execution blocked from Poland (close-only)</span>
    <span class="badge b-mut" id="gen-badge" data-gen="{gen_ts:.0f}">Updated {now} · auto-refresh 15 min</span>
  </div>
</header>

<nav class="tabs">
  <a class="tab active" href="#overview" data-tab="overview">Overview</a>
  <a class="tab" href="#markets" data-tab="markets">Markets</a>
  <a class="tab" href="#paper" data-tab="paper">Paper P&amp;L</a>
  <a class="tab" href="#health" data-tab="health">Health</a>
  <a class="tab" href="#notes" data-tab="notes">Lab notes</a>
  <a class="tab" href="#worldcup" data-tab="worldcup">World Cup</a>
</nav>

<div id="tab-overview" class="tabpanel active">

<div class="section"><h2>Live status</h2><div class="grid">{cards}</div>
<div style="margin-top:14px">{progress}</div></div>

<div class="section"><h2>Trajectory &amp; statistical power</h2>{trajectory_panel(d)}</div>

<div class="section"><h2>⚠ Critical constraint</h2>
<div class="alert"><h3>You cannot legally open positions on Polymarket from Poland</h3>
<p style="margin:0">Polymarket lists Poland as <b>close-only</b> (view &amp; close existing positions,
but new orders are rejected — gambling-law compliance). Kalshi is US-persons-only. So this project is
strictly a <b>research / edge-validation</b> effort: it uses only public market data and never places
an order. Do not use a VPN to evade the geoblock (it breaches Polymarket's terms and Polish law).
This status could change under evolving MiCA enforcement — re-verify before any execution work.</p>
</div></div>

<div class="section"><h2>Project timeline</h2><div class="panel">{timeline(d)}</div></div>

<div class="section"><h2>Current results</h2>{results_html(d)}</div>

<div class="section"><h2>What the council decided (2026-07-02, project charter)</h2>{COUNCIL}
<p style="color:var(--mut);font-size:13px;margin-top:10px">Latest check-in: council #3
(2026-07-03) reviewed the strategy against live data and ruled <b>stay the course — no new
tracks</b>; full verdict on the <a href="#notes">Lab notes</a> tab.</p></div>

<div class="section"><h2>Owner's guide</h2>{GUIDE}</div>

<div class="section"><h2>Learn the concepts</h2>{GLOSSARY}</div>

<div class="section"><h2>Next steps</h2><div class="panel"><ul class="clean">
<li>Let the scheduled logger/resolver run — <b>the clock to the Aug 19 readout gate is the binding constraint.</b></li>
<li>Check this dashboard weekly (Health tab first); watch the longshot <b>gap</b> and whether the holdout CI clears the cost line — informational only until readout.</li>
<li><i>Optional 5-minute act sanctioned by council #3:</i> <code>git init</code> + a dated <code>HOLDOUT_BOUNDARY.md</code> ("data after this timestamp is holdout for any future test").</li>
<li>At the readout gate (2026-08-19), run the pre-registered holdout verdict once and write it up — the structural findings lead, whatever the verdict (see Lab notes).</li>
<li>If pursuing further, get a Polish individual tax ruling (ORD-IN) on prediction-market P&amp;L classification.</li>
<li>Keep the €200/month in a low-cost index fund meanwhile — the council's honest benchmark.</li>
</ul></div></div>

</div><!-- /tab-overview -->

<div id="tab-markets" class="tabpanel">{markets_tab(d["market_rows"])}</div>

<div id="tab-paper" class="tabpanel">{paper_tab(d)}</div>

<div id="tab-health" class="tabpanel">{health_tab(d)}</div>

<div id="tab-notes" class="tabpanel">{NOTES}</div>

<div id="tab-worldcup" class="tabpanel">{worldcup_tab()}</div>

<div class="foot">
Files: <code>polymarket-edge-lab/</code> in the edge-lab repo · Data: <code>edge_lab.sqlite</code> ·
Scheduled: GitHub Actions workflow <code>edge-lab update</code> (hourly :17 — logger → resolver →
dashboard → watchdog), published via GitHub Pages. Source: Polymarket Gamma API (public,
read-only). This is research &amp; general information, not financial, tax or legal advice.
</div>
</div>
{SCRIPT}
</body></html>"""


def main():
    db.init_db(DB_ABS)
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.expanduser("~"), "Desktop", "Polymarket Edge Lab.html")
    d = gather()
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(d))
    print(f"dashboard written: {out}  (markets={d['markets']} snaps={d['snaps']} "
          f"resolved={d['resolved']})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"dashboard error: {exc}", file=sys.stderr)
        sys.exit(1)
