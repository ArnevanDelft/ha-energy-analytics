"""Recognise fingerprinted devices in the residual signal (no plug needed).

Once a device has a stored fingerprint (fingerprint.py), we can look for its
signature in the `other`/remainder series of any later window: find on-cycles
in the remainder and score each against every fingerprint by (a) running-power
similarity and (b) cycle-shape correlation. This is a *report* -- it estimates
how much of `other` each known device probably explains -- and deliberately
does not rewrite the attribution, because a confident plug measurement should
always win over a shape guess.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, fingerprint


# A shape curve must vary by at least this fraction of its mean before its
# correlation is treated as informative (flat plateaus carry no shape info).
SHAPE_REL_VARIATION = 0.1


def _score(seg_shape: np.ndarray, seg_mean: float, seg_dur_h: float,
           fp: dict) -> float:
    """0..1 similarity between an observed cycle and a fingerprint.

    Primary features are running power and cycle duration (these distinguish a
    fridge from a dishwasher). The intra-cycle shape is only mixed in as a
    bonus when BOTH curves actually vary -- for flat-plateau devices its
    correlation is pure noise and would otherwise penalise a true match.
    """
    ref_run = fp["scalar"].get("running_w", 0.0)
    ref_dur = (fp["cycles"].get("on_duration_h") or {}).get("median")
    if ref_run <= 0:
        return 0.0
    power_sim = max(0.0, 1.0 - abs(seg_mean - ref_run) / ref_run)
    if ref_dur:
        dur_sim = max(0.0, 1.0 - abs(seg_dur_h - ref_dur) / ref_dur)
    else:
        dur_sim = power_sim  # no duration reference: lean on power
    base = 0.5 * power_sim + 0.5 * dur_sim

    ref_shape = np.array(fp["cycles"].get("shape_w") or [])
    informative = (
        len(ref_shape) == len(seg_shape)
        and ref_shape.mean() > 0 and seg_shape.mean() > 0
        and ref_shape.std() > SHAPE_REL_VARIATION * ref_shape.mean()
        and seg_shape.std() > SHAPE_REL_VARIATION * seg_shape.mean()
    )
    if informative:
        shape_sim = max(0.0, float(np.corrcoef(seg_shape, ref_shape)[0, 1]))
        base = 0.8 * base + 0.2 * shape_sim
    return base


def match(remainder: pd.Series, freq=config.RESAMPLE,
          fingerprints: dict | None = None, min_score: float = 0.6) -> pd.DataFrame:
    """Attribute `other`-cycles to the best-matching stored fingerprint.

    Returns a per-device table: matched cycles, kWh, and mean confidence.
    """
    fps = fingerprints if fingerprints is not None else fingerprint.load_all()
    if not fps:
        return pd.DataFrame(columns=["matched_cycles", "kwh", "mean_score"])

    step_h = pd.Timedelta(freq).total_seconds() / 3600.0
    cycles = list(fingerprint._find_cycles(remainder.clip(lower=0.0), step_h))
    rows = {d: {"matched_cycles": 0, "kwh": 0.0, "score_sum": 0.0} for d in fps}
    for dur_h, _peak, mean_w, shape in cycles:
        best_dev, best_score = None, min_score
        for dev, fp in fps.items():
            s = _score(shape, mean_w, dur_h, fp)
            if s >= best_score:
                best_dev, best_score = dev, s
        if best_dev is not None:
            r = rows[best_dev]
            r["matched_cycles"] += 1
            r["kwh"] += mean_w * dur_h / 1000.0
            r["score_sum"] += best_score

    out = []
    for dev, r in rows.items():
        if r["matched_cycles"]:
            out.append({
                "device": dev,
                "matched_cycles": r["matched_cycles"],
                "kwh": round(r["kwh"], 3),
                "mean_score": round(r["score_sum"] / r["matched_cycles"], 2),
            })
    return pd.DataFrame(out).set_index("device") if out else \
        pd.DataFrame(columns=["matched_cycles", "kwh", "mean_score"])
