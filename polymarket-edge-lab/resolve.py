"""Resolution backfill.

For markets whose end date has passed but whose outcome is still unknown, fetch the
market by id from Gamma and, if it has resolved, record the YES/NO outcome. This turns
the snapshot time series into a labeled dataset for the calibration test. Run hourly
(scheduled at :12; the watchdog alerts if this log goes stale for more than 3h).

    python resolve.py
"""

import sys
import time
import urllib.error
from datetime import datetime, timezone

import config
import db
from logger import _get_json, _as_list


def _resolved_outcome(market):
    """Return 1.0 (YES), 0.0 (NO), or None if not conclusively resolved.

    A resolved binary market reports outcomePrices collapsed to ~[1,0] or ~[0,1].
    Require the market to be closed and the prices to be decisive.
    """
    if not market.get("closed"):
        return None
    prices = _as_list(market.get("outcomePrices"))
    if len(prices) != 2:
        return None
    try:
        yes = float(prices[0])
        no = float(prices[1])
    except (TypeError, ValueError):
        return None
    if yes >= 0.98 and no <= 0.02:
        return 1.0
    if no >= 0.98 and yes <= 0.02:
        return 0.0
    return None  # closed but ambiguous / voided -- leave unresolved


def fetch_market(market_id):
    """Fetch one market by id via the path endpoint.

    The listing endpoint (/markets?id=X) silently EXCLUDES closed markets, which are
    exactly the ones we need to label -- only /markets/{id} returns them reliably.
    """
    try:
        market = _get_json(f"/markets/{market_id}", {})
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):   # deleted/withdrawn market: leave unresolved
            return None
        raise
    if isinstance(market, dict):
        return market
    if isinstance(market, list) and market:
        return market[0]
    return None


def run():
    db.init_db()
    now = datetime.now(timezone.utc).timestamp()
    checked = 0

    with db.connect() as conn:
        pending = db.markets_needing_resolution(conn, now)

    # Do all the (slow, rate-limited) network fetching WITHOUT a DB connection open:
    # holding a write transaction across minutes of fetches blocks the hourly logger.
    outcomes = []
    for row in pending:
        checked += 1
        if checked > 1:
            time.sleep(config.RATE_SLEEP)   # stay under Gamma's unauth rate limit
        market = fetch_market(row["id"])
        if not market:
            continue
        outcome = _resolved_outcome(market)
        if outcome is not None:
            outcomes.append((row["id"], outcome))

    with db.connect() as conn:
        for market_id, outcome in outcomes:
            db.set_outcome(conn, market_id, outcome)
    resolved = len(outcomes)

    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
          f"pending_checked={checked} newly_resolved={resolved}")
    return resolved


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print(f"resolve error: {exc}", file=sys.stderr)
        sys.exit(1)
