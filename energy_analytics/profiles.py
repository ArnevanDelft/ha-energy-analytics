"""State-conditioned power profiles.

Devices with no power meter but a status in HA (TV, Bluesound zones, NAD amp,
WTW ventilation) get a fixed wattage per state (config.STATE_PROFILES).
Lighting is estimated per bulb as max-watts x brightness, summed into one
`lighting` column. Both are *attributed estimates*: cheap, transparent, and
they shrink the `other` bucket so the induction/fridge inference gets a
cleaner remainder to work on.
"""

from __future__ import annotations

import pandas as pd

from . import config
from .loader import _utc


def _stale_hours_for(entity_id: str):
    domain = entity_id.split(".", 1)[0]
    return config.STATE_STALE_HOURS.get(domain)


def profile_power(loader_obj, start, end, freq=config.RESAMPLE) -> pd.DataFrame:
    """One watts column per STATE_PROFILES device."""
    cols = {}
    for name, prof in config.STATE_PROFILES.items():
        try:
            states = loader_obj.load_states(
                prof["entity"], start, end, freq,
                stale_hours=_stale_hours_for(prof["entity"]),
            )
        except Exception as err:
            print(f"  ! could not load states for {prof['entity']}: {err}")
            continue
        watts = states.map(lambda s: prof["watts"].get(s, prof["default"])
                           if s is not None else prof["default"])
        cols[name] = watts.astype("float64")
    return pd.DataFrame(cols)


def lighting_power(loader_obj, start, end, freq=config.RESAMPLE) -> pd.Series:
    """Aggregate estimated lighting watts (individual bulbs only, no groups)."""
    grid = pd.date_range(start=_utc(start), end=_utc(end), freq=freq)
    total = pd.Series(0.0, index=grid, name="lighting")
    try:
        lights = loader_obj.list_entities("light")
    except Exception as err:
        print(f"  ! could not list lights: {err}")
        return total
    for eid in lights:
        if eid in config.LIGHT_GROUPS:
            continue
        max_w = config.LIGHT_MAX_W.get(eid, config.LIGHT_DEFAULT_MAX_W)
        try:
            frac = loader_obj.load_light(eid, start, end, freq)
        except Exception as err:
            print(f"  ! could not load {eid}: {err}")
            continue
        total = total.add(frac * max_w, fill_value=0.0)
    total.name = "lighting"
    return total
