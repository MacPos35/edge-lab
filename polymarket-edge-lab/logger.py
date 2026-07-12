"""Read-only Polymarket Gamma-API poller.

Fetches currently-active two-outcome markets in the frozen category and appends one
implied-probability snapshot per market. The *reference outcome* is always the first-listed
outcome (index 0): we record its price as the implied probability and, at resolution, whether
it won. No account, no wallet, no order placement -- this only reads public market data.

Markets are pulled ordered by liquidity descending and pagination stops once markets fall
below the liquidity floor (config.MIN_LIQUIDITY_USD), which both keeps the universe to the
markets we actually test and avoids the Gamma deep-offset (HTTP 422) ceiling. Run hourly.

    python logger.py
"""

import json
import re
import sys
import time
from datetime import datetime, timezone

import urllib.request
import urllib.parse
import urllib.error

import config
import db


def _get_json(path, params, attempts=3):
    """GET + parse JSON, retrying transient failures (DNS/timeout/5xx/429).

    Non-transient HTTP errors (e.g. 422 deep-offset ceiling, 404/410 gone) are
    raised on the first attempt so callers keep their existing semantics.
    """
    url = f"{config.GAMMA_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "edge-lab/1.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500) or attempt == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == attempts - 1:
                raise
        time.sleep(2.0 * (2 ** attempt))   # 2s, 4s between retries


def _parse_iso(s):
    """Return unix seconds from an ISO-8601 string, or None.

    Gamma timestamps are UTC; a rare naive value (e.g. date-only endDateIso)
    must not be interpreted in the machine's local timezone.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _as_list(v):
    """Gamma returns outcomes/outcomePrices as JSON-encoded strings or lists."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _matches_category(market):
    parts = [str(market.get("slug", "")), str(market.get("question", ""))]
    for ev in market.get("events") or []:
        parts += [str(ev.get("slug", "")), str(ev.get("ticker", "")), str(ev.get("title", ""))]
    hay = " ".join(parts).lower()
    tokens = set(_TOKEN_RE.split(hay))
    return any(
        (kw in tokens) if " " not in kw else (kw in hay)
        for kw in config.CATEGORY_KEYWORDS
    )


def _event_slug(m):
    """Best public-URL slug: the event slug (clean, e.g. 'nba-finals-winner') if present,
    else the market's own slug. Returns None if neither exists."""
    for ev in m.get("events") or []:
        s = ev.get("slug")
        if s:
            return str(s)
    s = m.get("slug")
    return str(s) if s else None


def _liquidity(m):
    for k in ("liquidityNum", "liquidity"):
        v = m.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def fetch_liquid_markets():
    """Yield active markets ordered by liquidity desc, stopping below the liquidity floor."""
    for page in range(config.MAX_PAGES):
        params = {
            "closed": "false",
            "active": "true",
            "limit": config.PAGE_LIMIT,
            "offset": page * config.PAGE_LIMIT,
            "order": "liquidityNum",
            "ascending": "false",
        }
        try:
            batch = _get_json("/markets", params)
        except urllib.error.HTTPError as e:
            if e.code == 422:   # deep-offset ceiling: treat as end of data
                return
            raise
        if not batch:
            return

        crossed_floor = False
        for m in batch:
            if _liquidity(m) < config.MIN_LIQUIDITY_USD:
                crossed_floor = True
                continue
            yield m
        # ordered desc, so once a page contains a sub-floor market the rest are thinner
        if crossed_floor or len(batch) < config.PAGE_LIMIT:
            return
        time.sleep(config.RATE_SLEEP)


def run():
    db.init_db()
    now = datetime.now(timezone.utc).timestamp()
    kept = snapped = 0

    # Fetch the whole universe BEFORE opening the write transaction: the paginated
    # scan takes ~20s+ and holding the write lock across it starves the other jobs.
    liquid = list(fetch_liquid_markets())
    seen = len(liquid)

    with db.connect() as conn:
        for m in liquid:
            if not _matches_category(m):
                continue
            outcomes = _as_list(m.get("outcomes"))
            prices = _as_list(m.get("outcomePrices"))
            if len(outcomes) != 2 or len(prices) != 2:
                continue
            try:
                ref_price = float(prices[0])   # reference outcome = index 0
            except (TypeError, ValueError):
                continue
            if not (0.0 <= ref_price <= 1.0):
                continue

            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid:
                continue
            kept += 1

            db.upsert_market(
                conn,
                id=mid,
                question=m.get("question", ""),
                category=config.CATEGORY_LABEL,
                slug=_event_slug(m),
                created_ts=_parse_iso(m.get("createdAt") or m.get("startDate")),
                resolves_ts=_parse_iso(m.get("endDate") or m.get("endDateIso")),
                game_start_ts=_parse_iso(m.get("gameStartTime")),
                closed=int(bool(m.get("closed"))),
                active=int(bool(m.get("active"))),
                now=now,
            )
            db.insert_snapshot(
                conn,
                market_id=mid,
                ts=now,
                yes_price=ref_price,
                implied_prob=ref_price,
                liquidity=_liquidity(m),
            )
            snapped += 1

    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
          f"scanned_liquid={seen} category_2outcome={kept} snapshots_written={snapped}")
    return snapped


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the scheduler log
        print(f"logger error: {exc}", file=sys.stderr)
        sys.exit(1)
