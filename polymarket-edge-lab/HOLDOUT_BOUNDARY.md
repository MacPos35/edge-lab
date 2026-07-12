# Holdout boundary (the one act sanctioned by council check-in #3, 2026-07-03)

**Declared 2026-07-12T07:20Z.**

All markets whose anchor time (`COALESCE(game_start_ts, resolves_ts)`) falls **after
2026-07-12T07:20Z** are reserved as untouched holdout for any *future* (post-readout)
hypothesis. No exploratory analysis, screen, or parameter choice may look at outcomes
in that region before such a test is pre-registered.

This does not affect the pre-registered favorite–longshot test in any way: that test's
own in-sample/holdout split, parameters, and readout schedule remain exactly as frozen
in `config.py` and `KILL_CRITERIA.md`.

Context for the date: everything before this declaration has been exposed at least in
aggregate (the dashboard's calibration table and live holdout verdict are rendered
hourly per the KILL_CRITERIA "may be looked at" clause), so the clean region for any
new idea starts now, not at collection start.
