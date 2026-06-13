"""Turn roaming-plug weeks into fitted config values.

For every PLUG_ASSIGNMENTS entry overlapping the analysis window this prints:

  * general stats -- mean/peak watts, daily kWh, share of house consumption;
  * device == "fridge"      -> running power, duty cycle, and the values to
                               put in FRIDGE_W / FRIDGE_TYPICAL_W;
  * device in STATE_PROFILES -> measured mean watts per reported state: the
                               exact numbers for that profile's `watts` map
                               (ground truth; supersedes --learn regression).
"""

from __future__ import annotations

import pandas as pd

from . import config, loader, profiles

# A plugged appliance drawing less than this is considered "off/standby" when
# computing duty cycles.
RUNNING_THRESHOLD_W = 5.0


def _overlap(a, start, end):
    a_start = pd.Timestamp(a["start"], tz="Europe/Amsterdam")
    a_end = pd.Timestamp(a["end"], tz="Europe/Amsterdam") if a.get("end") \
        else pd.Timestamp.max.tz_localize("UTC")
    return max(a_start, start), min(a_end, end)


def calibrate(loader_obj, start, end, freq=config.RESAMPLE):
    start, end = loader._utc(start), loader._utc(end)
    any_output = False
    for a in config.PLUG_ASSIGNMENTS:
        win_start, win_end = _overlap(a, start, end)
        if win_start >= win_end:
            continue
        any_output = True
        device = a["device"]
        print(f"\n--- plug on '{device}' "
              f"({win_start:%Y-%m-%d} .. {win_end:%Y-%m-%d}) ---")

        power = loader_obj.load(a["plug"], win_start, win_end, freq)
        days = (win_end - win_start).total_seconds() / 86400.0
        kwh = loader.integrate_kwh(power, freq)
        if power.max() == 0:
            print("  no plug data in this window (plug offline or not "
                  "reporting yet)")
            continue
        print(f"  mean {power.mean():.1f} W | peak {power.max():.0f} W | "
              f"{kwh:.2f} kWh total | {kwh / days:.2f} kWh/day")

        running = power[power > RUNNING_THRESHOLD_W]
        if device == "fridge" and not running.empty:
            duty = len(running) / len(power)
            print(f"  fridge fit: running power {running.mean():.0f} W, "
                  f"duty cycle {duty:.0%}")
            print(f"  -> config: FRIDGE_W = {running.mean():.0f}")
        elif device in config.STATE_PROFILES:
            prof = config.STATE_PROFILES[device]
            states = loader_obj.load_states(
                prof["entity"], win_start, win_end, freq,
                stale_hours=profiles._stale_hours_for(prof["entity"]),
            )
            rows = []
            for state in states.dropna().unique():
                sel = power[states == state]
                if len(sel) < 10:
                    continue
                rows.append({
                    "state": state,
                    "measured_W": round(sel.mean(), 1),
                    "configured_W": prof["watts"].get(state, prof["default"]),
                    "time_share_%": round(100 * len(sel) / len(power), 1),
                })
            if rows:
                table = pd.DataFrame(rows).set_index("state")
                print(table.to_string())
                fitted = {r["state"]: float(r["measured_W"]) for r in rows}
                print(f"  -> config: STATE_PROFILES['{device}']['watts'] = {fitted}")
        elif not running.empty:
            duty = len(running) / len(power)
            print(f"  active {duty:.0%} of the time at {running.mean():.0f} W mean "
                  f"-- new device; consider adding a profile or keeping the "
                  f"plug on it")
    if not any_output:
        print("\nNo plug assignments overlap this window. Add an entry to "
              "PLUG_ASSIGNMENTS in config.py once the plug is placed.")
