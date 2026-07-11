# polymarket-edge-lab

A **read-only edge-validation harness** for the prediction-market *favorite–longshot bias*.
This is a research pipeline, **not** a trading bot. It collects public Polymarket data, labels
resolved markets, and runs a pre-registered out-of-sample calibration test to decide whether a
tradeable edge exists **before** any capital is ever risked.

Chosen by the LLM Council (5 advisors + 3 peer reviews) as the single most defensible automatable
route for a €1,000 + €200/month account under a Polish tax standpoint. See
`../../.claude/plans/use-the-llm-council-breezy-coral.md` for the full rationale.

---

## ⚠️ Step 0 — Legal / tax gate result (RESOLVED: execution is BLOCKED from Poland)

The council's decisive catch was: *can a Polish resident even legally trade this?* Verified against
primary sources (2026):

- **Polymarket access from Poland = "close-only."** Polish users may view market data and **close**
  existing positions, but **cannot open new positions — orders submitted from Poland are rejected**
  (gambling-law compliance; Poland has a state betting monopoly). Sources: Polymarket Help Center
  *Geographic Restrictions*, and Polymarket docs *api-reference/geoblock* (Poland listed under
  "Close-Only" alongside Singapore, Thailand, Taiwan).
- **Kalshi = US-persons only** → not a legal venue for a Polish resident either.
- **Polish tax treatment** (only relevant *if* a legal venue existed): because Polymarket settles
  on-chain in USDC, disposals would most likely fall under **PIT-38 crypto/capital income at 19%**
  rather than the gambling regime — but this is unconfirmed for prediction-market P&L specifically
  and would need an *interpretacja indywidualna* (ORD-IN). Moot while execution is blocked.

**Consequence for this project:**
- ❌ **Do NOT build or run any funding / order-signing / execution code.** Trading Polymarket from
  Poland is not permitted. (Using a VPN to evade the geoblock would breach Polymarket's ToS and
  Poland's gambling law — out of scope and not recommended.)
- ✅ **The read-only research harness below remains fully legal and valuable.** It uses only the
  public Gamma API (no account, no wallet, no position-taking). It answers whether the edge is real
  and builds venue-agnostic, reusable calibration infrastructure. Option value: Poland's status may
  change under evolving MiCA enforcement, and the analysis transfers to any market-data source.

**Success is defined as:** *"a documented favorite–longshot edge survives an honest out-of-sample
test net of costs"* — a validated edge + reusable infrastructure, **not** euros of profit.

---

## Step 1 — Pre-registration (freeze BEFORE looking at collected data)

**Causal mechanism / losing counterparty.** Recreational and partisan bettors systematically
*overpay* for exciting low-probability outcomes ("longshots") and *underprice* near-certain
favorites; prices also update slowly after news. This is the classic **favorite–longshot bias**,
documented across racetrack and sports betting for decades and observed in Polymarket/Kalshi
calibration studies.

**Falsifiable hypothesis.** Each market is two-outcome; we fix the **reference outcome = the
first-listed outcome** (works for Yes/No, team-vs-team, and Over/Under alike) and track its price
as the implied probability. At a fixed horizon before resolution, group markets by that implied
probability. If the bias is present, **low-probability buckets have the reference outcome come true
*less* often than its price implies**, and/or **high-probability buckets come true *more* often than
implied**. Null hypothesis: markets are calibrated (realized rate ≈ mean implied probability in every
bucket), so no exploitable edge exists after costs.

**Frozen parameters** (locked in `config.py` — changing them after seeing data invalidates the test):
- **Category:** exactly ONE (default `sports`) to avoid multiple-hypothesis mining.
- **Snapshot horizon:** the snapshot nearest **T − 24h** before each market's resolution.
- **Probability buckets:** edges in `config.PROB_BUCKETS`.
- **Liquidity filter:** markets below `config.MIN_LIQUIDITY_USD` are excluded.
- **Longshot band under test:** `config.LONGSHOT_BAND` (default 0.05–0.15).
- **Minimum sample:** `config.MIN_RESOLVED_MARKETS` resolved markets before the test is read out.

**Out-of-sample discipline.** Markets are sorted by resolution time; the **first half is in-sample
sanity, the second half is an untouched holdout**. The edge must appear on the holdout to count.

> **Maintenance amendment 2026-07-03 (61 usable markets, no frozen parameter touched):** a code
> review found three data-loss failure modes already visible in the logs (a `database is locked`
> abort, 5 DNS-failure aborts, and a resolver schedule drift that cost a 28h labeling gap). Fixes
> applied: SQLite WAL + 15s busy_timeout; HTTP retry ×3 with backoff on transient errors; network
> fetching moved outside write transactions in logger/resolve; the resolver queue bounded at 14
> days past anchor (voided/ambiguous markets stop being re-fetched forever); naive timestamps in
> `_parse_iso` now assume UTC instead of local time (latent — all observed Gamma fields carry
> timezones). None of these change the frozen experiment parameters in `config.py`, the market
> selection, the snapshot semantics, or the analysis; they only protect data collection. Backup of
> pre-change code + DB in `backup-2026-07-03/`. Same day, presentation-layer only: the dashboard
> gained Health and Lab-notes tabs plus a trajectory/statistical-power panel — all recomputed live
> from the DB at each hourly render; nothing feeds back into the test.

> **Amendment 2026-07-02 (0 resolved markets in DB, pre-registration intact):** live data showed
> Gamma's `endDate` is unreliable for sports markets — sometimes **weeks before** the actual game
> (`gameStartTime`). The T−24h horizon and time-split are therefore anchored on
> `COALESCE(gameStartTime, endDate)` instead of raw `endDate`, and the resolver queue uses the
> same anchor. The anchor is always pre-outcome, so no lookahead is introduced. See
> `KILL_CRITERIA.md` for the pre-committed readout schedule and termination contract.

**Cost model** (subtracted before declaring an edge real): `config.EST_SPREAD_SLIPPAGE` round-trip
plus `config.GAS_USD` per trade. Only ONE category and ONE bucketing are tested (no p-hacking).

---

## Files
| File | Role |
|------|------|
| `config.py`  | Frozen test parameters (single source of truth) |
| `db.py`      | SQLite schema + idempotent upsert helpers |
| `logger.py`  | Read-only Gamma-API poller → snapshots (run hourly) |
| `resolve.py` | Backfills YES/NO outcomes for resolved markets (run hourly) |
| `analyze.py` | Out-of-sample calibration / bucket test (`--selftest` for a synthetic check) |
| `dashboard.py` | Renders the published HTML dashboard from the live DB (run hourly) |
| `watchdog.py` | Dead-man's switch: fails the CI run when data goes stale despite green jobs |
| `requirements.txt` | `requests` (stdlib `sqlite3`, `math`, `statistics` otherwise) |

## Usage
```bash
pip install -r requirements.txt
python logger.py            # one poll now (smoke test); schedule hourly
python resolve.py           # backfill outcomes; schedule hourly
python analyze.py --selftest  # verify the math on synthetic injected-bias data
python analyze.py           # after 6–8 weeks: read out the pre-registered test
```

Scheduling is handled by `../.github/workflows/update.yml`: one GitHub Actions job, hourly at
:17, runs `logger.py → resolve.py → dashboard.py → watchdog.py` (resolve before dashboard, so
each render has fresh outcomes) and commits the refreshed sqlite + published page back to the
repo. `watchdog.py` is the dead-man's switch — it fails the run (GitHub emails on failure) if
the newest snapshot is >6h old or outcomes stop being labeled, catching silent-green failures.
(The original deployment ran the same scripts from Windows Task Scheduler; that era's notes
live in the dashboard's Lab-notes tab.)
**The 6–8-week data-collection clock is the binding constraint — keep the workflow running.**

> Note: Gamma caps deep pagination (HTTP 422 past offset ~2–3k), so each run captures the
> **top ~2,100 markets by liquidity** and the category subset within them (~340 sports markets,
> liquidity ≥ 35k on first run). That is the liquid, tradeable universe — thin markets are excluded
> by design. The logger stops gracefully at the ceiling.

## Step 6 — Decision gate (currently N/A)
Execution is blocked from Poland (see Step 0). If, and only if, a legal venue becomes available
**and** the holdout test shows the edge surviving net of costs, scope a *separate*, hard-capped
€1,000 execution module then. Until then this stays a research project; keep the €200/month in a
low-cost index fund as the council's benchmark.
