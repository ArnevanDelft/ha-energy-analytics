"""Learn state wattages from the data instead of guessing them.

Least-squares fit: the pre-profile remainder (consumption minus measured loads
minus the compressor estimate) is regressed on indicator columns -- one per
(device, state) pair -- plus the lighting fraction and an intercept (the
always-on baseline: fridge, router, standby, ...).

The fitted coefficients are the data's estimate of each state's wattage.
They're suggestions to copy into config.STATE_PROFILES, not automatic
overrides: a coefficient goes wrong when two devices always switch together
(collinearity), so eyeball them before adopting. Negative fits are reported
as 0 (a device cannot generate power).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, profiles


def learn_profiles(loader_obj, frame: pd.DataFrame, start, end,
                   freq=config.RESAMPLE) -> pd.DataFrame:
    """Return a table: device/state, configured W, learned W, share of time."""
    # Target: what the profiles are trying to explain.
    target = (
        frame["consumption"]
        - frame[list(config.MEASURED_LOADS)].sum(axis=1)
        - frame["heatpump_comp"]
    )

    # Dummy coding: per device the most-common state is the *reference* and is
    # left out (it is collinear with the intercept). Each fitted coefficient
    # is then "extra watts compared to the reference state" -- e.g. tv:on
    # relative to tv:off, which is the actionable number anyway.
    regressors: dict[tuple[str, str], pd.Series] = {}
    references: dict[str, str] = {}
    for name, prof in config.STATE_PROFILES.items():
        try:
            states = loader_obj.load_states(
                prof["entity"], start, end, freq,
                stale_hours=profiles._stale_hours_for(prof["entity"]),
            )
        except Exception as err:
            print(f"  ! could not load states for {prof['entity']}: {err}")
            continue
        shares = {st: states.eq(st).mean() for st in prof["watts"]}
        observed = {st: sh for st, sh in shares.items() if sh > 0.005}
        if not observed:
            print(f"  (skip {name} -- no state observed often enough to fit)")
            continue
        ref = max(observed, key=observed.get)
        references[name] = ref
        for state, share in observed.items():
            if state != ref:
                regressors[(name, state)] = states.eq(state).astype(float)

    lighting_frac = profiles.lighting_power(loader_obj, start, end, freq)
    light_scale = lighting_frac.max()
    if light_scale > 0:
        regressors[("lighting", "x configured")] = lighting_frac / light_scale

    if not regressors:
        return pd.DataFrame(columns=["configured_W", "learned_W", "time_share_%"])

    X = np.column_stack([s.to_numpy() for s in regressors.values()]
                        + [np.ones(len(target))])
    y = target.to_numpy()
    ok = np.isfinite(y)
    coef, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)

    rows = []
    for i, ((dev, state), series) in enumerate(regressors.items()):
        if dev == "lighting":
            configured = 1.0
            learned = coef[i] / light_scale if light_scale else 0.0
            label = "lighting (scale factor)"
        else:
            ref = references[dev]
            watts = config.STATE_PROFILES[dev]["watts"]
            configured = watts[state] - watts[ref]
            learned = coef[i]
            label = f"{dev}: {state} (vs {ref})"
        rows.append({
            "device_state": label,
            "configured_dW": round(configured, 1),
            "learned_dW": round(max(learned, 0.0), 1),
            "time_share_%": round(100 * series.mean(), 1),
        })
    rows.append({
        "device_state": "(baseline incl. reference states)",
        "configured_dW": float("nan"),
        "learned_dW": round(coef[-1], 1),
        "time_share_%": 100.0,
    })
    return pd.DataFrame(rows).set_index("device_state")
