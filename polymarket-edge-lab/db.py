"""SQLite schema and idempotent helpers for the edge lab.

Two tables:
  markets   -- one row per binary market (metadata + resolved outcome)
  snapshots -- append-only time series of implied probability per market

All writes are idempotent (INSERT OR REPLACE / INSERT OR IGNORE) so the hourly
logger and daily resolver can run repeatedly without creating duplicates.
"""

import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id           TEXT PRIMARY KEY,
    question     TEXT,
    category     TEXT,
    slug         TEXT,          -- polymarket event slug (for the public URL); may be NULL
    created_ts   REAL,          -- unix seconds (may be NULL)
    resolves_ts  REAL,          -- unix seconds of endDate
    game_start_ts REAL,         -- unix seconds of gameStartTime; anchors the test for sports
                                -- (Gamma endDate is sometimes weeks before the actual game)
    outcome      REAL,          -- 1.0 = YES resolved, 0.0 = NO, NULL = unresolved
    closed       INTEGER,       -- 0/1
    active       INTEGER,       -- 0/1
    first_seen   REAL,
    last_seen    REAL
);

CREATE TABLE IF NOT EXISTS snapshots (
    market_id    TEXT,
    ts           REAL,          -- unix seconds of this observation
    yes_price    REAL,          -- price of the YES/first outcome in [0,1]
    implied_prob REAL,          -- == yes_price for a binary market
    liquidity    REAL,
    PRIMARY KEY (market_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots(market_id);
CREATE INDEX IF NOT EXISTS idx_markets_resolved ON markets(outcome, resolves_ts);
"""


@contextmanager
def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # The hourly jobs (logger/resolve/dashboard) can overlap, especially in the
    # catch-up storm after the machine wakes from sleep; WAL lets readers coexist
    # with the single writer and busy_timeout makes a second writer wait instead
    # of aborting the whole run with "database is locked".
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Idempotent column additions for databases created before a schema change."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(markets)")}
    if "slug" not in cols:
        conn.execute("ALTER TABLE markets ADD COLUMN slug TEXT")
    if "game_start_ts" not in cols:
        conn.execute("ALTER TABLE markets ADD COLUMN game_start_ts REAL")


def init_db(path=None):
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_market(conn, *, id, question, category, slug, created_ts, resolves_ts,
                  game_start_ts, closed, active, now):
    """Insert or update market metadata, preserving first_seen, slug and any known outcome."""
    conn.execute(
        """
        INSERT INTO markets (id, question, category, slug, created_ts, resolves_ts,
                             game_start_ts, outcome, closed, active, first_seen, last_seen)
        VALUES (:id, :question, :category, :slug, :created_ts, :resolves_ts,
                :game_start_ts, NULL, :closed, :active, :now, :now)
        ON CONFLICT(id) DO UPDATE SET
            question    = excluded.question,
            category    = excluded.category,
            slug        = COALESCE(excluded.slug, markets.slug),
            created_ts  = COALESCE(markets.created_ts, excluded.created_ts),
            resolves_ts = excluded.resolves_ts,
            game_start_ts = COALESCE(excluded.game_start_ts, markets.game_start_ts),
            closed      = excluded.closed,
            active      = excluded.active,
            last_seen   = excluded.last_seen
        """,
        dict(id=id, question=question, category=category, slug=slug, created_ts=created_ts,
             resolves_ts=resolves_ts, game_start_ts=game_start_ts, closed=closed,
             active=active, now=now),
    )


def insert_snapshot(conn, *, market_id, ts, yes_price, implied_prob, liquidity):
    conn.execute(
        """
        INSERT OR IGNORE INTO snapshots (market_id, ts, yes_price, implied_prob, liquidity)
        VALUES (?, ?, ?, ?, ?)
        """,
        (market_id, ts, yes_price, implied_prob, liquidity),
    )


def set_outcome(conn, market_id, outcome):
    conn.execute("UPDATE markets SET outcome = ? WHERE id = ?", (outcome, market_id))


# Markets still unresolved this long after their event are voided/ambiguous on
# Gamma (never collapse to a decisive price); stop re-fetching them every run so
# the resolver's runtime stays bounded. Sports disputes settle within days.
RESOLVE_MAX_AGE_DAYS = 14


def markets_needing_resolution(conn, now_ts):
    """Markets whose event time has passed but outcome is still unknown.

    Anchor on gameStartTime when known (Gamma's endDate is unreliable for sports:
    sometimes weeks before the actual game), falling back to endDate. Markets whose
    anchor passed more than RESOLVE_MAX_AGE_DAYS ago are dropped from the queue.
    """
    return conn.execute(
        "SELECT id FROM markets WHERE outcome IS NULL "
        "AND COALESCE(game_start_ts, resolves_ts) IS NOT NULL "
        "AND COALESCE(game_start_ts, resolves_ts) < ? "
        "AND COALESCE(game_start_ts, resolves_ts) >= ?",
        (now_ts, now_ts - RESOLVE_MAX_AGE_DAYS * 86400),
    ).fetchall()
