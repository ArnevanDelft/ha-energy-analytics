"""Energy decomposition.

Builds a single power frame on a uniform grid and derives:

  grid_power      net at the meter (+import / -export)
  solar_power     total PV AC output
  consumption     true house consumption = grid_import - grid_export + solar
                  = grid_power + solar_power   (signs work out: export is -grid)
  <measured>      each directly-metered load
  heatpump_comp   compressor estimate from frequency
  remainder       consumption - sum(measured) - heatpump_comp
                  i.e. everything not yet attributed (incl. induction, fridge,
                  lighting, standby, electronics, ...)

`remainder` is the search space for the inferred detectors.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, loader, profiles


def heatpump_compressor_power(freq_hz: pd.Series) -> pd.Series:
    """Linear freq->electrical-power model, clamped to [0, RATED_W]."""
    lo, hi, rated = config.HEATPUMP_MIN_HZ, config.HEATPUMP_RATED_HZ, config.HEATPUMP_RATED_W
    frac = (freq_hz - lo) / (hi - lo)
    power = (frac.clip(lower=0.0) * rated).clip(lower=0.0, upper=rated)
    power = power.where(freq_hz >= lo, 0.0)
    power.name = "heatpump_comp"
    return power


def build_power_frame(loader_obj, start, end, freq=config.RESAMPLE) -> pd.DataFrame:
    f = pd.DataFrame()

    grid = loader.load_many(loader_obj, [config.GRID_POWER], start, end, freq)
    f["grid_power"] = grid[config.GRID_POWER]

    solar = loader.load_many(loader_obj, config.SOLAR_POWER, start, end, freq)
    f["solar_power"] = solar.sum(axis=1) if not solar.empty else 0.0

    # True consumption. Net grid already nets out export, so adding gross solar
    # back reconstructs what the house actually drew.
    f["consumption"] = f["grid_power"] + f["solar_power"]

    measured = loader.load_many(loader_obj, list(config.MEASURED_LOADS.values()),
                                start, end, freq)
    for name, eid in config.MEASURED_LOADS.items():
        f[name] = measured[eid] if eid in measured else 0.0

    hp = loader.load_many(loader_obj, [config.HEATPUMP_FREQ], start, end, freq)
    freq_series = hp[config.HEATPUMP_FREQ] if config.HEATPUMP_FREQ in hp \
        else pd.Series(0.0, index=f.index)
    f["heatpump_comp"] = heatpump_compressor_power(freq_series)

    # Roaming calibration plug: within each assignment window, move the plug's
    # watts from the generic plug column into a per-device column. Totals are
    # unchanged; only the attribution label moves.
    eid_to_col = {eid: name for name, eid in config.MEASURED_LOADS.items()}
    plugged_cols = []
    for a in config.PLUG_ASSIGNMENTS:
        col = eid_to_col.get(a["plug"])
        if col is None:
            extra = loader.load_many(loader_obj, [a["plug"]], start, end, freq)
            if a["plug"] not in extra:
                continue
            col = f"plug_{loader._object_id(a['plug'])}"
            f[col] = extra[a["plug"]]
            eid_to_col[a["plug"]] = col
        mask = loader.assignment_mask(f.index, a)
        dev_col = f"plugged_{a['device']}"
        if dev_col not in f:
            f[dev_col] = 0.0
            plugged_cols.append(dev_col)
        f.loc[mask, dev_col] += f.loc[mask, col]
        f.loc[mask, col] = 0.0

    prof = profiles.profile_power(loader_obj, start, end, freq)
    for col in prof:
        # The plug measuring a profiled device supersedes its profile.
        f[col] = prof[col].mask(loader.device_suppressed(f.index, col), 0.0)
    f["lighting"] = profiles.lighting_power(loader_obj, start, end, freq)

    f["attributed"] = (
        f[list(config.MEASURED_LOADS)].sum(axis=1)
        + f["heatpump_comp"]
        + f[list(prof.columns)].sum(axis=1)
        + f["lighting"]
        + (f[plugged_cols].sum(axis=1) if plugged_cols else 0.0)
    )
    # Unclipped remainder, so the energy books reconcile exactly:
    #   consumption == attributed + remainder  (at every step).
    # It can go slightly negative when the compressor estimate overshoots
    # actual draw -- that is a useful calibration signal, not noise to hide.
    f["remainder"] = f["consumption"] - f["attributed"]

    return f
