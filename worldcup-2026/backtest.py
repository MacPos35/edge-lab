"""Leakage-free backtest of predict.py parameters on the collected tournament data.

Written for the 2026-07-12 council review (COUNCIL-2026-07-12.md). Read-only:
uses data/scoreboard.json + data/predictions.json, writes nothing. Two evaluations,
both strictly one-step-ahead (fit only on matches finished before prediction time):

1. REPRODUCE — re-derive every frozen knockout P(advance) from the scoreboard using
   only matches finished before its issued_at, and check it matches predictions.json.
   Validates that this harness scores exactly the model that made the predictions.

2. SWEEP — sequential evaluation under alternative parameters:
   a) 90' W/D/L multiclass Brier + log loss over all played matches (n≈100);
   b) knockout P(advance) binary Brier over the graded frozen predictions (n≈10).

Interpretation caution (Contrarian seat): a sweep on collected data is in-sample
hyperparameter selection even when each prediction is out-of-sample. Directional
evidence only; the out-of-sample test is the tracked council variant (p_adv_cv).

    python backtest.py            # reproduce + baseline + parameter sweeps
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

import predict as P

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = dict(SHRINK_K=P.SHRINK_K, ELO_SCALE=P.ELO_SCALE, RHO=P.RHO,
                MU_TOTAL=P.MU_TOTAL, HOST_ELO_BONUS=P.HOST_ELO_BONUS)


def load():
    with open(os.path.join(HERE, "data", "scoreboard.json"), encoding="utf-8") as f:
        matches, _ = P.parse_events(json.load(f))
    with open(os.path.join(HERE, "data", "predictions.json"), encoding="utf-8") as f:
        state = json.load(f)
    played = [m for m in matches if m["state"] == "post" and m["home"]["score"] is not None
              and m["home"]["name"] in P.ELO and m["away"]["name"] in P.ELO]
    return matches, played, state


def ts(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def set_params(**kw):
    for k, v in {**DEFAULTS, **kw}.items():
        setattr(P, k, v)


def fit_before(played, cutoff):
    """Factors fit only on matches finished (kickoff + 3h) before `cutoff`."""
    prior = [m for m in played if ts(m["date"]) + timedelta(hours=3) <= cutoff]
    return P.fit_factors(prior), len(prior)


def outcome_90(m):
    """90-minute W/D/L; a match that reached ET or pens was a draw at 90'."""
    if "AET" in m["detail"] or "Pens" in m["detail"]:
        return "d"
    hs, as_ = m["home"]["score"], m["away"]["score"]
    return "h" if hs > as_ else ("a" if as_ > hs else "d")


def reproduce(matches, played, state):
    print("== reproduce frozen predictions (harness validation) ==")
    set_params()
    worst = 0.0
    for eid, pred in state["issued"].items():
        factors, nprior = fit_before(played, ts(pred["issued_at"]))
        m = next(m for m in matches if m["id"] == eid)
        p = P.predict_match(pred["home"], pred["away"], m["venue_country"], factors)
        d = abs(p["p_adv"] - pred["p_adv"])
        worst = max(worst, d)
        print(f"  {pred['home'][:14]:14s} v {pred['away'][:14]:14s} "
              f"recorded {pred['p_adv']:.4f} rebuilt {p['p_adv']:.4f} (fit on {nprior})")
    print(f"  worst diff: {worst:.4f} {'OK' if worst < 5e-4 else '** MISMATCH **'}")
    return worst < 5e-4


def wdl_eval(played, min_prior=8):
    """Sequential 90' W/D/L eval. Returns (n, mean multiclass Brier, mean log loss)."""
    br = ll = 0.0
    n = 0
    for m in played:
        factors, nprior = fit_before(played, ts(m["date"]))
        if nprior < min_prior:
            continue
        p = P.predict_match(m["home"]["name"], m["away"]["name"], m["venue_country"], factors)
        probs = dict(h=p["p_home"], d=p["p_draw"], a=p["p_away"])
        out = outcome_90(m)
        br += sum((probs[k] - (1.0 if k == out else 0.0)) ** 2 for k in probs)
        ll += -math.log(max(probs[out], 1e-12))
        n += 1
    return n, br / n, ll / n


def ko_eval(matches, played, state):
    """Rebuild graded knockout P(advance) under current params; mean binary Brier."""
    tot = 0.0
    n = 0
    for eid, pred in state["issued"].items():
        if not pred.get("graded"):
            continue
        factors, _ = fit_before(played, ts(pred["issued_at"]))
        m = next(m for m in matches if m["id"] == eid)
        p = P.predict_match(pred["home"], pred["away"], m["venue_country"], factors)
        tot += (p["p_adv"] - (1.0 if pred["home_advanced"] else 0.0)) ** 2
        n += 1
    return n, tot / n


def sweep(matches, played, state, name, key, values):
    print(f"\n== sweep {name} (other params at deployed defaults) ==")
    print(f"{name:>10} | WDL n  brier  logloss | KO n  brier")
    for v in values:
        set_params(**{key: v})
        n1, b1, l1 = wdl_eval(played)
        n2, b2 = ko_eval(matches, played, state)
        mark = "  <- deployed" if v == DEFAULTS[key] else ""
        print(f"{v!s:>10} | {n1:3d}  {b1:.4f}  {l1:.4f} | {n2:2d}  {b2:.4f}{mark}")
    set_params()


def main():
    matches, played, state = load()
    ok = reproduce(matches, played, state)
    if not ok:
        print("harness does not reproduce the frozen predictions — fix before trusting sweeps")

    set_params()
    n, b, l = wdl_eval(played)
    print(f"\nbaseline 90' W/D/L : n={n}  brier={b:.4f}  logloss={l:.4f}"
          f"  (uniform 1.0986, hindsight base rates ~1.066)")
    n, b = ko_eval(matches, played, state)
    print(f"baseline KO advance: n={n}  brier={b:.4f}  (coin flip 0.25)")
    draws = sum(1 for m in played if outcome_90(m) == "d")
    print(f"draws at 90': {draws}/{len(played)} observed")

    sweep(matches, played, state, "SHRINK_K", "SHRINK_K", [1, 2, 4, 6, 9, 15, 30, 1e9])
    sweep(matches, played, state, "ELO_SCALE", "ELO_SCALE", [500, 600, 700, 850.0, 1000, 1200])
    sweep(matches, played, state, "RHO", "RHO", [-0.20, -0.15, -0.10, -0.05, 0.0])
    sweep(matches, played, state, "MU_TOTAL", "MU_TOTAL", [2.3, 2.5, 2.7, 2.9, 3.1])
    sweep(matches, played, state, "HOST_BONUS", "HOST_ELO_BONUS", [0, 25, 50.0, 100, 150])


if __name__ == "__main__":
    main()
