# worldcup-2026

Daily prediction pipeline for the remaining FIFA World Cup 2026 matches (through the
July 19 final). Output appears as the **World Cup tab** of the Polymarket Edge Lab
dashboard (`../polymarket-edge-lab/dashboard.py` includes `wc_tab.html` — presentation
layer only, the frozen experiment is untouched).

Design was council-reviewed (4 specialists: model / data / dashboard / integration)
on 2026-07-05.

## How it works (one script, one daily run)

`predict.py`:

1. **Fetch** — one call for the whole tournament:
   `site.api.espn.com/.../fifa.world/scoreboard?dates=20260611-20260719`
   (~100 events, results + fixtures). Retry ×3 with backoff; a failed or shrunken
   response never overwrites the last-good `data/scoreboard.json`.
2. **Fit** — Elo-anchored Poisson. Baseline expected goals from a **frozen
   pre-tournament Elo snapshot** (eloratings.net end-2025; hardcoded in predict.py —
   current Elo would double-count tournament matches). Per-team attack/defense
   multipliers = observed/Elo-expected goal rates, shrunk toward 1 with
   `w = n/(n+6)` (~0.45 at 5 games — tournament form never gets more than half the
   say). Goals only; shots/possession deliberately unused (n≈5 ⇒ overfitting).
   Dixon-Coles low-score adjustment hardcoded at ρ=−0.10. Hosts get +50 Elo in
   their own country.
3. **Predict** — per remaining match: W/D/L in 90' (score grid 0–8×0–8), top-3
   scorelines, P(advance) = P(win90) + P(draw90)·[P(winET) + P(drawET)·½]
   (extra time at λ/3, penalties a 50/50 coin flip).
4. **Simulate** — 10,000 Monte Carlo runs of the remaining bracket → champion odds.
   Bracket built from ESPN placeholder slots ("Round of 16 5 Winner" = 5th R16 match
   chronologically); semifinal/final synthesized from FIFA numbering until their
   events appear in the feed.
5. **Grade** — predictions are **frozen at issue** and never revised. Once a match
   finishes, its prediction is graded with a binary Brier score on P(advance)
   (0 = perfect, 0.25 = coin flip; lower is better). Pure-Elo (w=0) and
   pure-tournament (w=1) variants are graded alongside as a blend sanity check.
6. **Render** — `wc_tab.html` fragment (atomic write) + `data/predictions.json`
   state (issued predictions, grades, champion-odds history for the ▲▼ deltas).

```
python predict.py             # daily run
python predict.py --selftest  # synthetic checks, no network
```

## Schedule

One Task Scheduler job, **WorldCup2026-Predict**, daily 04:30 local (last matches end
~04:00 local), `StartWhenAvailable` for the laptop-asleep case, launched hidden via
`wscript.exe //B run_hidden.vbs` (house rule: no visible console windows). The
dashboard task (hourly :18) picks the fresh fragment up automatically; the tab shows
a STALE warning if the fragment is older than 26 h.

## Honesty notes

- Toy model: ~5 games/team of tournament data plus a static prior. Champion odds are
  ±10 pt fuzzy at best. **Not a betting signal** — and execution from Poland is
  blocked anyway (see `../polymarket-edge-lab/README.md`, Step 0).
- Independent-Poisson tails understate draws and blowouts; favorites' champion odds
  run modestly hot.
- The shrinkage constant K=6 is a prior belief, not a fitted value; the w=0/w=1
  variant Briers in the track record are the check on it.
- ESPN's API is unofficial and can change without notice; the last-good-file guard
  degrades to yesterday's predictions rather than breaking.
- Everything dies naturally after July 19 — delete the scheduled task then
  (`schtasks /delete /tn WorldCup2026-Predict /f`).
