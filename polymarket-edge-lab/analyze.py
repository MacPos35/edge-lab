"""Out-of-sample calibration / favorite-longshot test.

For every resolved market we take the snapshot nearest to (resolution - horizon), bucket
markets by implied probability, and compare each bucket's mean implied probability to its
realized YES-rate. Markets are time-split: first half = in-sample sanity, second half =
untouched HOLDOUT. The pre-registered readout is the holdout longshot band, net of costs.

    python analyze.py            # read out the real collected data
    python analyze.py --selftest # verify the math on synthetic injected-bias data

Interpretation: for a binary market a NO share bought at (1 - p) pays 1 if NO occurs, so the
expected profit per share equals (implied_prob - outcome) averaged over the band -- i.e. the
calibration gap IS the gross edge per share of "bet against longshots."
"""

import math
import random
import sqlite3
import sys

import config
import db


# --------------------------------------------------------------------------- stats
def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return (center - half, center + half)


def mean_ci(samples, z=1.96):
    """Normal-approx CI for the mean of a list of floats."""
    n = len(samples)
    if n == 0:
        return (0.0, 0.0, 0.0)
    m = sum(samples) / n
    if n == 1:
        return (m, m, m)
    var = sum((x - m) ** 2 for x in samples) / (n - 1)
    se = math.sqrt(var / n)
    return (m, m - z * se, m + z * se)


def bucket_of(p):
    edges = config.PROB_BUCKETS
    for i in range(len(edges) - 1):
        if edges[i] <= p < edges[i + 1]:
            return i
    return len(edges) - 2  # p == 1.0 lands in the last bucket


# --------------------------------------------------------------------------- data
def load_rows(conn):
    """Return [(anchor_ts, implied_prob, outcome)] for usable resolved markets.

    anchor_ts = gameStartTime when known, else endDate. Gamma's endDate is unreliable
    for sports markets (observed weeks before the actual game), so the pre-registered
    T-24h horizon is measured back from the event time, which is always pre-outcome.
    """
    target = config.SNAPSHOT_HORIZON_HOURS * 3600
    tol = config.SNAPSHOT_TOLERANCE_HOURS * 3600
    rows = []
    markets = conn.execute(
        "SELECT id, COALESCE(game_start_ts, resolves_ts) AS anchor_ts, outcome "
        "FROM markets WHERE outcome IS NOT NULL "
        "AND COALESCE(game_start_ts, resolves_ts) IS NOT NULL"
    ).fetchall()
    for mk in markets:
        want_ts = mk["anchor_ts"] - target
        snap = conn.execute(
            """
            SELECT implied_prob, liquidity, ABS(ts - ?) AS dist
            FROM snapshots
            WHERE market_id = ? AND liquidity >= ?
            ORDER BY dist ASC LIMIT 1
            """,
            (want_ts, mk["id"], config.MIN_LIQUIDITY_USD),
        ).fetchone()
        if snap and snap["dist"] <= tol:
            rows.append((mk["anchor_ts"], float(snap["implied_prob"]), float(mk["outcome"])))
    return rows


# --------------------------------------------------------------------------- report
def calibration_table(rows, title):
    edges = config.PROB_BUCKETS
    print(f"\n{title}  (n={len(rows)})")
    print(f"{'bucket':>13} {'n':>5} {'mean_impl':>10} {'realized':>9} {'gap':>7} {'wilson95':>17}")
    for i in range(len(edges) - 1):
        b = [r for r in rows if bucket_of(r[1]) == i]
        n = len(b)
        if n == 0:
            continue
        mean_impl = sum(r[1] for r in b) / n
        yes = sum(1 for r in b if r[2] >= 0.5)
        realized = yes / n
        lo, hi = wilson(yes, n)
        gap = mean_impl - realized
        print(f"[{edges[i]:.2f},{edges[i+1]:.2f}) {n:>5} {mean_impl:>10.3f} "
              f"{realized:>9.3f} {gap:>+7.3f}   [{lo:.3f},{hi:.3f}]")


def longshot_verdict(rows, label):
    lo_b, hi_b = config.LONGSHOT_BAND
    band = [r for r in rows if lo_b <= r[1] < hi_b]
    n = len(band)
    print(f"\n=== Longshot band {lo_b:.2f}-{hi_b:.2f} on {label} (n={n}) ===")
    if n == 0:
        print("  no markets in band")
        return
    per_share = [r[1] - r[2] for r in band]          # implied - outcome = profit/share
    gross, glo, ghi = mean_ci(per_share)
    cost = config.EST_SPREAD_SLIPPAGE
    net = gross - cost
    print(f"  gross edge/share : {gross:+.4f}  (95% CI [{glo:+.4f}, {ghi:+.4f}])")
    print(f"  cost model       : -{cost:.4f}  (spread/slippage) + gas ${config.GAS_USD}")
    print(f"  NET edge/share   : {net:+.4f}")
    survives = (glo - cost) > 0 and n >= 1
    print(f"  edge survives net of costs (CI lower bound): {'YES' if survives else 'NO'}")
    return survives


def analyze(conn):
    rows = load_rows(conn)
    rows.sort(key=lambda r: r[0])  # by event/anchor time
    total = len(rows)
    print(f"usable resolved markets: {total}  (min required: {config.MIN_RESOLVED_MARKETS})")
    if total < config.MIN_RESOLVED_MARKETS:
        print("NOTE: below pre-registered minimum -- keep collecting; result is provisional.")

    half = total // 2
    in_sample, holdout = rows[:half], rows[half:]
    calibration_table(in_sample, "IN-SAMPLE calibration")
    calibration_table(holdout, "HOLDOUT calibration")
    longshot_verdict(in_sample, "IN-SAMPLE (sanity only)")
    print("\n>>> PRE-REGISTERED READOUT (holdout is what counts):")
    longshot_verdict(holdout, "HOLDOUT")


# --------------------------------------------------------------------------- selftest
def selftest():
    """Seed an in-memory DB with an injected favorite-longshot bias and confirm recovery."""
    injected = 0.05  # longshots priced 0.05 above their true probability
    n = 6000
    rng = random.Random(42)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    base_ts = 1_700_000_000.0
    for i in range(n):
        true_p = rng.random()
        implied = true_p + injected if true_p < 0.20 else true_p
        implied = min(max(implied, 0.001), 0.999)
        outcome = 1.0 if rng.random() < true_p else 0.0
        resolves = base_ts + i * 3600
        conn.execute(
            "INSERT INTO markets (id, question, category, created_ts, resolves_ts, "
            "outcome, closed, active, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,1,0,?,?)",
            (str(i), "synthetic", "sports", resolves - 5 * 86400, resolves,
             outcome, base_ts, base_ts),
        )
        conn.execute(
            "INSERT INTO snapshots (market_id, ts, yes_price, implied_prob, liquidity) "
            "VALUES (?,?,?,?,?)",
            (str(i), resolves - config.SNAPSHOT_HORIZON_HOURS * 3600, implied, implied, 5000.0),
        )
    conn.commit()

    rows = load_rows(conn)
    lo_b, hi_b = config.LONGSHOT_BAND
    band = [r for r in rows if lo_b <= r[1] < hi_b]
    gross, glo, ghi = mean_ci([r[1] - r[2] for r in band])
    print(f"selftest: injected longshot bias = +{injected:.3f}")
    print(f"selftest: recovered gross edge/share in band = {gross:+.4f} "
          f"(95% CI [{glo:+.4f}, {ghi:+.4f}], n={len(band)})")
    ok = abs(gross - injected) < 0.02 and glo > 0
    print(f"selftest: {'PASS' if ok else 'FAIL'} "
          f"(recovered edge within tolerance of injected and CI excludes 0)")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    db.init_db()
    with db.connect() as c:
        analyze(c)
