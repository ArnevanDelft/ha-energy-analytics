"""Turn a disaggregated power frame into a kWh breakdown + optional plot."""

from __future__ import annotations

import pandas as pd

from . import config, loader

# Columns that represent end-use device power (must not overlap each other).
DEVICE_COLS = (
    ["heatpump_comp"]
    + list(config.MEASURED_LOADS)
    + sorted({f"plugged_{a['device']}" for a in config.PLUG_ASSIGNMENTS})
    + list(config.STATE_PROFILES)
    + ["lighting", "induction", "fridge", "other"]
)

PRETTY = {
    "heatpump_comp": "Heat pump (compressor, est.)",
    "heatpump_addheat": "Heat pump (backup heater)",
    "plug_sp240": "Smart plug SP240",
    "plug_schuur": "Smart plug schuur",
    "plug_woonkamer": "Smart plug woonkamer",
    "tv": "TV OLED935 (profiled)",
    "bluesound_voorkamer": "Bluesound voorkamer (profiled)",
    "bluesound_eetkamer": "Bluesound eetkamer (profiled)",
    "nad_amp": "NAD C338 amp (profiled)",
    "ventilation": "WTW ventilation (profiled)",
    "lighting": "Lighting (estimated)",
    "induction": "Induction hob (inferred)",
    "fridge": "Fridge (inferred)",
    "other": "Other / unattributed",
}
PRETTY.update({
    f"plugged_{a['device']}": f"{a['device'].capitalize()} (plug-measured)"
    for a in config.PLUG_ASSIGNMENTS
})


def breakdown_kwh(frame: pd.DataFrame, freq=config.RESAMPLE) -> pd.DataFrame:
    cols = [c for c in DEVICE_COLS if c in frame]
    kwh = loader.integrate_kwh(frame[cols], freq)
    total_cons = loader.integrate_kwh(frame["consumption"], freq)
    solar = loader.integrate_kwh(frame["solar_power"], freq)
    out = kwh.to_frame("kWh")
    out.index = [PRETTY.get(c, c) for c in out.index]
    out["%_of_consumption"] = (out["kWh"] / total_cons * 100).round(1)
    out = out.sort_values("kWh", ascending=False)
    out.loc["— TOTAL consumption —", "kWh"] = total_cons
    out.loc["(solar produced)", "kWh"] = solar
    return out.round(2)


def daily_kwh(frame: pd.DataFrame, freq=config.RESAMPLE) -> pd.DataFrame:
    """Per-day kWh per device (local time)."""
    cols = [c for c in DEVICE_COLS if c in frame]
    step_h = pd.Timedelta(freq).total_seconds() / 3600.0
    energy = frame[cols] * step_h / 1000.0
    local = energy.copy()
    local.index = local.index.tz_convert("Europe/Amsterdam")
    daily = local.groupby(local.index.date).sum()
    daily.columns = [PRETTY.get(c, c) for c in daily.columns]
    return daily.round(2)


def plot(frame: pd.DataFrame, path: str, freq=config.RESAMPLE):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cols = [c for c in DEVICE_COLS if c in frame]
    local = frame.copy()
    local.index = local.index.tz_convert("Europe/Amsterdam")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    # Stacked area needs non-negative values; `other` can dip slightly negative
    # when the compressor estimate overshoots. Clip for the picture only.
    local[cols].clip(lower=0).plot.area(ax=ax1, linewidth=0)
    local["consumption"].plot(ax=ax1, color="black", linewidth=0.6,
                              label="measured consumption")
    ax1.set_ylabel("Power (W)")
    ax1.set_title("Disaggregated house consumption")
    ax1.legend(loc="upper left", fontsize=8, ncol=2)

    local[["grid_power", "solar_power", "consumption"]].plot(ax=ax2, linewidth=0.7)
    ax2.axhline(0, color="grey", linewidth=0.5)
    ax2.set_ylabel("Power (W)")
    ax2.set_title("Grid (net) vs solar vs consumption")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    print(f"  plot -> {path}")
