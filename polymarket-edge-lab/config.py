"""Frozen test parameters for the favorite-longshot validation harness.

Single source of truth, imported by logger.py and analyze.py so the pre-registration
in README.md is enforced in code. DO NOT change the analysis parameters after data
collection has started -- doing so invalidates the out-of-sample test.
"""

import dnsfix  # noqa: F401  side effect only: DoH fallback for the ISP DNS block

# --- Data source (read-only, public, no auth) ---
GAMMA_BASE = "https://gamma-api.polymarket.com"
HTTP_TIMEOUT = 20          # seconds per request
PAGE_LIMIT = 100           # markets per page
MAX_PAGES = 60             # safety cap on pagination per run
RATE_SLEEP = 1.1           # seconds between requests (Gamma ~60 req/min unauth)

# --- Storage ---
DB_PATH = "edge_lab.sqlite"

# --- FROZEN experiment parameters (pre-registered) ---
# Exactly ONE category. Polymarket markets carry no usable `category`/`tags` field, so the
# category is derived by matching these league/sport tokens against a market's slug, question,
# and event slug/ticker/title (see logger._matches_category). Tokens are matched whole
# (hyphen/space delimited) to limit false positives.
CATEGORY_LABEL = "sports"
CATEGORY_KEYWORDS = [
    # league / tour codes seen in Polymarket slugs & event tickers
    "nfl", "nba", "wnba", "mlb", "nhl", "mls", "nwsl", "wta", "atp", "npb", "kbo",
    "epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel", "ncaa",
    "ufc", "pga", "f1", "motogp",
    # plain-language sport words
    "soccer", "football", "tennis", "baseball", "basketball", "hockey",
    "golf", "boxing", "cricket", "rugby",
]

# Snapshot used by the test = the one closest to (resolution_time - SNAPSHOT_HORIZON_HOURS),
# accepted only if within SNAPSHOT_TOLERANCE_HOURS of that target.
SNAPSHOT_HORIZON_HOURS = 24
SNAPSHOT_TOLERANCE_HOURS = 12

# Probability bucket edges (implied prob of the YES/observed outcome).
PROB_BUCKETS = [0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50, 0.70, 0.80, 0.85, 0.90, 0.95, 1.0]

# Exclude thin markets (USD liquidity as reported by Gamma).
MIN_LIQUIDITY_USD = 500.0

# The specific band whose miscalibration we pre-commit to testing.
LONGSHOT_BAND = (0.05, 0.15)

# Minimum resolved+usable markets before the pre-registered result is read out.
MIN_RESOLVED_MARKETS = 200

# --- Cost model (subtracted before declaring an edge real) ---
EST_SPREAD_SLIPPAGE = 0.02   # round-trip fraction of notional
GAS_USD = 0.05               # Polygon gas per trade (cents)
