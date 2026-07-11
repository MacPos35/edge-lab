"""Dead-man's switch: fail loudly when the pipeline goes stale despite green runs.

The failure mode this catches is the silent one: every job exits 0 but no fresh
data lands -- e.g. Gamma changes its response shape and the logger keeps writing
zero snapshots, or resolved markets stop getting labeled. Runs as the last step
of the hourly GitHub Actions workflow; a non-zero exit fails the run, which
triggers GitHub's failure email. (Replaces the Windows-era watchdog task from
the original Task Scheduler deployment, which alerted on stale job logs --
on Actions the logs don't persist, so this checks the data itself.)

    python watchdog.py
"""

import sys
from datetime import datetime, timezone

import db

SNAPSHOT_STALE_HOURS = 6        # logger writes hourly; 6h of nothing = dead
LABELING_WINDOW_DAYS = (2, 13)  # anchors old enough to have resolved, young
                                # enough to still be in the resolver queue
LABELING_MIN_MARKETS = 20       # only judge the labeling rate on a real sample
LABELING_MIN_FRACTION = 0.10    # sports resolve within days; ~0 labeled = resolver broken


def run():
    now = datetime.now(timezone.utc).timestamp()
    problems = []
    with db.connect() as conn:
        last_snap = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
        if last_snap is None:
            problems.append("no snapshots in the database at all")
        else:
            age_h = (now - last_snap) / 3600
            print(f"newest snapshot: {age_h:.1f}h old (limit {SNAPSHOT_STALE_HOURS}h)")
            if age_h > SNAPSHOT_STALE_HOURS:
                problems.append(f"newest snapshot is {age_h:.1f}h old -- logger "
                                "runs are not landing data")

        lo_d, hi_d = LABELING_WINDOW_DAYS
        total, labeled = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(outcome IS NOT NULL), 0) FROM markets "
            "WHERE COALESCE(game_start_ts, resolves_ts) BETWEEN ? AND ?",
            (now - hi_d * 86400, now - lo_d * 86400)).fetchone()
        if total >= LABELING_MIN_MARKETS:
            frac = labeled / total
            print(f"labeling rate, anchors {lo_d}-{hi_d}d old: {labeled}/{total} "
                  f"({frac:.0%}, floor {LABELING_MIN_FRACTION:.0%})")
            if frac < LABELING_MIN_FRACTION:
                problems.append(f"only {labeled}/{total} markets anchored {lo_d}-{hi_d} "
                                "days ago have outcomes -- resolver is not labeling")
        else:
            print(f"labeling check skipped: only {total} markets anchored "
                  f"{lo_d}-{hi_d}d old (need {LABELING_MIN_MARKETS})")

    if problems:
        for p in problems:
            print(f"WATCHDOG: {p}", file=sys.stderr)
        return 1
    print("watchdog OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
