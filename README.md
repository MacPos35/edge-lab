# edge-lab

Keeps two research projects up to date 24/7 for free, with no laptop and no manual input,
via **GitHub Actions + GitHub Pages**.

- **`polymarket-edge-lab/`** — read-only Polymarket favorite-longshot calibration harness.
  Hourly `logger.py` (snapshot) + `resolve.py` (backfill outcomes) into `edge_lab.sqlite`;
  `dashboard.py` renders the published page. No account, no keys.
- **`worldcup-2026/`** — daily World Cup 2026 predictor (`predict.py`, ESPN public API,
  stdlib only). Emits `wc_tab.html`, which the dashboard embeds as its "World Cup" tab.
  Time-boxed to the 2026-07-19 final.

## How it runs

`.github/workflows/update.yml` fires hourly on a managed runner:
`logger → resolve → (predict, daily slot only) → dashboard → checkpoint sqlite → commit → watchdog`.
The commit both **persists state** (sqlite / predictions.json / wc_tab.html) and
**publishes** `docs/index.html`. Nothing else is needed — the page self-refreshes every
15 min, so a phone bookmark stays current on its own. GitHub emails on any run failure,
and the final `watchdog.py` step deliberately fails the run when data goes stale even
though every job exited 0 (dead-man's switch).

**Published dashboard:** enable Pages (Settings → Pages → Source = `main`, `/docs`); the URL
is `https://<user>.github.io/edge-lab/`.

## Run locally

```bash
# World Cup
cd worldcup-2026 && python predict.py --selftest && python predict.py

# Polymarket + dashboard (must run from polymarket-edge-lab/ so the sqlite path resolves)
cd polymarket-edge-lab && python logger.py && python resolve.py && python dashboard.py ../docs/index.html
```

Pure Python 3.11 stdlib — no `pip install` needed.
