"""Heuristic detectors that infer un-metered appliances from the `remainder`
power signal. These are transparent rules, not machine learning -- the point
is that you can see why a watt was assigned where, and tune the thresholds in
config.py against reality.

Two appliances for now:

  induction  large, sustained steps (~1-3.5 kW) lasting >= a few minutes. The
             distinctive thing about an induction hob vs other big loads is the
             magnitude + the fact it sits well above the household baseline.

  fridge     a small, persistent, cycling load. We estimate it as a near-
             constant draw present whenever the remainder's local baseline is
             at least FRIDGE_TYPICAL_W, capped at FRIDGE_MAX_W. This captures
             the compressor duty cycle without claiming the whole baseline.

Everything in the remainder not assigned to these is reported as `other`
(lighting, electronics, standby, kettle, washing machine, ... -- loads we have
no signal to separate yet).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, loader


def _min_run_samples(minutes: float, freq: str) -> int:
    step_s = pd.Timedelta(freq).total_seconds()
    return max(1, int(round(minutes * 60 / step_s)))


def detect_induction(remainder: pd.Series, freq=config.RESAMPLE) -> pd.Series:
    """Power (W) attributed to the induction hob at each timestep."""
    hi = config.INDUCTION_MIN_W
    cap = config.INDUCTION_MAX_W
    above = remainder >= hi

    if config.INDUCTION_HOURS:
        hours = remainder.index.tz_convert("Europe/Amsterdam").hour
        in_window = np.zeros(len(remainder), dtype=bool)
        for lo, up in config.INDUCTION_HOURS:
            in_window |= (hours >= lo) & (hours < up)
        above &= pd.Series(in_window, index=remainder.index)

    # Keep runs whose length is within [MIN, MAX] minutes: long enough to not
    # be a kettle blip, short enough to not be a washing-machine/dryer cycle.
    min_run = _min_run_samples(config.INDUCTION_MIN_MINUTES, freq)
    max_run = _min_run_samples(config.INDUCTION_MAX_MINUTES, freq)
    grp = (above != above.shift()).cumsum()
    run_len = above.groupby(grp).transform("size")
    sustained = above & (run_len >= min_run) & (run_len <= max_run)

    # Attribute the portion above the (pre-cooking) household floor. We use the
    # remainder value itself, capped, as the induction draw -- the floor is
    # already excluded because we only fire above INDUCTION_MIN_W.
    induction = remainder.where(sustained, 0.0).clip(upper=cap)
    induction.name = "induction"
    return induction


def detect_fridge(remainder_after: pd.Series, freq=config.RESAMPLE) -> pd.Series:
    """Estimate the fridge's cyclic draw from what's left after induction.

    We look at the low-amplitude band: whenever the remainder sits in
    (0, FRIDGE_MAX_W], we credit the fridge with its typical running power
    (bounded by what's actually there). This approximates the compressor duty
    cycle. If FRIDGE_W is set, that fixed value is used as the running power.
    """
    run_w = config.FRIDGE_W if config.FRIDGE_W is not None else config.FRIDGE_TYPICAL_W
    in_band = (remainder_after > 0) & (remainder_after <= config.FRIDGE_MAX_W)
    fridge = pd.Series(0.0, index=remainder_after.index, name="fridge")
    fridge[in_band] = np.minimum(run_w, remainder_after[in_band])
    return fridge


def disaggregate(frame: pd.DataFrame, freq=config.RESAMPLE) -> pd.DataFrame:
    """Add induction / fridge / other columns derived from `remainder`.

    Detectors run on the non-negative part of the remainder. `other` is then
    the *exact* residual (remainder - induction - fridge), so the device
    columns always reconcile back to consumption. `other` can be slightly
    negative if the heat-pump compressor estimate overshoots reality.
    """
    out = frame.copy()
    pos = out["remainder"].clip(lower=0.0)
    out["induction"] = detect_induction(pos, freq)
    left = (pos - out["induction"]).clip(lower=0.0)
    fridge = detect_fridge(left, freq)
    # While the calibration plug is on the fridge, the plug measurement
    # (plugged_fridge) is authoritative -- silence the heuristic there.
    out["fridge"] = fridge.mask(loader.device_suppressed(out.index, "fridge"), 0.0)
    out["other"] = out["remainder"] - out["induction"] - out["fridge"]
    return out
