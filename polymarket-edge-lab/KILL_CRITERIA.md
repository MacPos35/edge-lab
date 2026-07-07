# Kill criteria (pre-committed 2026-07-02, with 0 resolved markets in the DB)

This file is the termination contract for polymarket-edge-lab, written per the LLM Council
verdict of 2026-07-02 **before any outcome data existed**. Changing it after outcomes
accumulate voids the pre-registration.

## Readout schedule
- **Readout date:** the first daily-resolve run on or after **2026-08-19** (7 weeks of
  collection) **if** usable resolved markets ≥ `config.MIN_RESOLVED_MARKETS` (200).
- **Hard stop:** **2026-09-30**. The readout happens on that date with whatever sample exists.
- Between now and readout: **zero new feature work**, maintenance capped at 15 min/week
  (watchdog alerts only). The dashboard may be looked at; `analyze.py` output is not acted on.

## Outcomes (run `python analyze.py`, holdout section only)
| Verdict | Condition (as coded in `longshot_verdict`) | Action |
|---|---|---|
| **PASS** | Holdout longshot band: 95% CI lower bound of gross edge − cost model > 0, with usable n ≥ 200 | Write up the result (1-2 pages, methods + table). Re-verify Polymarket's Poland geoblock status. If still close-only: **archive** — disable the Logger/Resolve/Dashboard/Watchdog tasks, keep the repo + writeup as a portfolio artifact. **No execution or funding code gets built while Poland is close-only, even on a PASS.** |
| **FAIL** | Holdout CI lower bound − costs ≤ 0 with usable n ≥ 200 | Write up the null result (equally publishable). **Archive**: disable all four scheduled tasks, keep repo + writeup. |
| **INCONCLUSIVE** | Usable resolved markets < 200 on 2026-09-30 | **Archive, do not extend.** Data collection is cheap to restart if a legal venue ever opens; a stalled sample is not worth defending. |

## Standing decisions independent of the readout
- The €1,000 + €200/month follows the council's capital plan (emergency buffer first, then
  accumulating world ETF in an IKE via a standing order). No edge-lab outcome changes this.
- No extension of the harness to on-chain betting venues (Azuro/Overtime/SX LP-ing):
  unverified legality under the Polish Gambling Act and unvalidated economics.
- VPN/geoblock evasion remains out of scope permanently.

If, at readout time, there is an urge to renegotiate this file — that urge is the
Contrarian's diagnosis: the project doing psychological work instead of financial work.
