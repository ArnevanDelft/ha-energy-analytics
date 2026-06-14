"""Device fingerprints: persist what a plug week teaches us about a device.

A fingerprint captures two layers:

  * scalar calibration -- running/standby watts, duty cycle, kWh/day, and (for
    state-profiled devices) the measured watts per HA state. These feed back
    into config.py.
  * the cycle SHAPE -- how the device switches on and off over time: typical
    on-duration, off-gap, cycles/day, and a normalised power curve of a
    representative cycle. This is the part that lets us later recognise the
    device in the P1 remainder *without* the plug (see matcher.py).

Fingerprints are stored as JSON under fingerprints/<device>.json so the
library accumulates as the plug tours the house. Re-running for a device that
already has a fingerprint keeps the previous versions under "history".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, loader, profiles

# Power below this (W) counts as "off/standby" when finding cycles.
ON_THRESHOLD_W = 5.0
# Ignore on-runs shorter than this -- they are sensor noise, not real cycles.
MIN_ON_SECONDS = 60.0
# A representative cycle is resampled to this many points so shapes of
# different durations can be compared directly.
SHAPE_POINTS = 32
# Directory holding the JSON library. Override with $ENERGY_FINGERPRINT_DIR so
# the CLI (calibration host) and the dashboard (Docker) can point at the same
# shared store -- e.g. a mounted volume on the Synology.
FINGERPRINT_DIR = Path(
    os.environ.get("ENERGY_FINGERPRINT_DIR")
    or Path(__file__).resolve().parent.parent / "fingerprints"
)


def _find_cycles(power: pd.Series, step_h: float):
    """Yield (on-duration_h, peak_W, mean_W, [resampled shape]) per on-cycle.

    A cycle is a maximal run of consecutive samples above ON_THRESHOLD_W that
    lasts at least MIN_ON_SECONDS (shorter runs are sensor noise).
    """
    on = (power.to_numpy() > ON_THRESHOLD_W)
    vals = power.to_numpy()
    min_samples = max(1, int(round(MIN_ON_SECONDS / (step_h * 3600.0))))
    i, n = 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n and on[j]:
            j += 1
        seg = vals[i:j]
        if len(seg) < min_samples:
            i = j
            continue
        dur_h = len(seg) * step_h
        shape = np.interp(
            np.linspace(0, len(seg) - 1, SHAPE_POINTS),
            np.arange(len(seg)),
            seg,
        ) if len(seg) > 1 else np.full(SHAPE_POINTS, seg[0])
        yield dur_h, float(seg.max()), float(seg.mean()), shape
        i = j


def extract(power: pd.Series, device: str, entity: str, start, end,
            freq=config.RESAMPLE, states: pd.Series | None = None) -> dict:
    """Build a fingerprint dict from a plug-measured power series."""
    step_h = pd.Timedelta(freq).total_seconds() / 3600.0
    days = max((end - start).total_seconds() / 86400.0, 1e-9)
    kwh = float(loader.integrate_kwh(power, freq))
    running = power[power > ON_THRESHOLD_W]

    cycles = list(_find_cycles(power, step_h))
    durations = np.array([c[0] for c in cycles]) if cycles else np.array([])
    shapes = np.array([c[3] for c in cycles]) if cycles else np.empty((0, SHAPE_POINTS))

    fp = {
        "device": device,
        "entity": entity,
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": round(days, 2)},
        "freq": freq,
        "samples": int(len(power)),
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scalar": {
            "mean_w": round(float(power.mean()), 2),
            "peak_w": round(float(power.max()), 2),
            "running_w": round(float(running.mean()), 2) if not running.empty else 0.0,
            "standby_w": round(float(power[power <= ON_THRESHOLD_W].mean()), 2)
            if (power <= ON_THRESHOLD_W).any() else 0.0,
            "duty_cycle": round(len(running) / len(power), 4) if len(power) else 0.0,
            "kwh_total": round(kwh, 3),
            "kwh_per_day": round(kwh / days, 3),
        },
        "cycles": {
            "count": int(len(cycles)),
            "per_day": round(len(cycles) / days, 2),
            "on_duration_h": {
                "median": round(float(np.median(durations)), 3) if len(durations) else None,
                "p25": round(float(np.percentile(durations, 25)), 3) if len(durations) else None,
                "p75": round(float(np.percentile(durations, 75)), 3) if len(durations) else None,
            },
            # Median power curve of a typical on-cycle, in W (the matchable shape).
            "shape_w": [round(float(v), 1) for v in np.median(shapes, axis=0)]
            if len(shapes) else [],
        },
    }

    if states is not None:
        per_state = {}
        for st in states.dropna().unique():
            sel = power[states == st]
            if len(sel) >= 10:
                per_state[str(st)] = round(float(sel.mean()), 1)
        fp["per_state_w"] = per_state

    return fp


def save(fp: dict, directory: Path = FINGERPRINT_DIR) -> Path:
    """Write fingerprint to <dir>/<device>.json, archiving any prior version."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fp['device']}.json"
    if path.exists():
        prev = json.loads(path.read_text())
        history = prev.pop("history", [])
        history.append({k: prev[k] for k in ("period", "scalar", "cycles",
                                             "extracted_at") if k in prev})
        fp = {**fp, "history": history}
    path.write_text(json.dumps(fp, indent=2))
    return path


def load_all(directory: Path = FINGERPRINT_DIR) -> dict[str, dict]:
    """Load every stored fingerprint, keyed by device name."""
    if not directory.exists():
        return {}
    out = {}
    for path in sorted(directory.glob("*.json")):
        try:
            fp = json.loads(path.read_text())
            out[fp.get("device", path.stem)] = fp
        except (json.JSONDecodeError, OSError) as err:
            print(f"  ! skipping {path.name}: {err}")
    return out


def save_from_assignments(loader_obj, start, end, freq=config.RESAMPLE,
                          directory: Path = FINGERPRINT_DIR) -> list[Path]:
    """Extract + save a fingerprint for every plug assignment in the window."""
    start, end = loader._utc(start), loader._utc(end)
    written = []
    for a in config.plug_assignments():
        win_start = max(loader._utc(pd.Timestamp(a["start"], tz="Europe/Amsterdam")), start)
        win_end = min(
            loader._utc(pd.Timestamp(a["end"], tz="Europe/Amsterdam")) if a.get("end") else end,
            end,
        )
        if win_start >= win_end:
            continue
        power = loader_obj.load(a["plug"], win_start, win_end, freq)
        if power.max() == 0:
            print(f"  no data for '{a['device']}' in window -- skipped")
            continue
        states = None
        if a["device"] in config.STATE_PROFILES:
            prof = config.STATE_PROFILES[a["device"]]
            states = loader_obj.load_states(
                prof["entity"], win_start, win_end, freq,
                stale_hours=profiles._stale_hours_for(prof["entity"]),
            )
        fp = extract(power, a["device"], a["plug"], win_start, win_end, freq, states)
        path = save(fp, directory)
        written.append(path)
        sc = fp["scalar"]
        print(f"  saved {path.name}: {sc['running_w']} W running, "
              f"duty {sc['duty_cycle']:.0%}, {fp['cycles']['per_day']} cycles/day, "
              f"{sc['kwh_per_day']} kWh/day")
    return written
