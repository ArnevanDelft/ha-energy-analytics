"""Configuration for the energy disaggregation: which Home Assistant entities
map to which signal, and the tunable thresholds for the heuristic device
detectors.

All power entities are in watts (W) and treated as *step* signals (a value
holds until the next reported change), which is how Home Assistant records
them. Energy is obtained by time-weighted integration (see loader.integrate).
"""

import os

# --- InfluxDB connection (shared by the CLI and the dashboard) ------------
# Defaults target the Synology; override via environment for Docker.
INFLUX_HOST = os.environ.get("INFLUX_HOST", "192.168.10.237")
INFLUX_PORT = int(os.environ.get("INFLUX_PORT", "8086"))
INFLUX_DATABASE = os.environ.get("INFLUX_DATABASE", "homeassistant")
# Reads on this v1 instance are unauthenticated; leave username empty to skip
# the auth header. Set INFLUX_USERNAME / INFLUX_PASSWORD if auth is enabled.
INFLUX_USERNAME = os.environ.get("INFLUX_USERNAME", "")
INFLUX_PASSWORD = os.environ.get("INFLUX_PASSWORD") or os.environ.get("INFLUXDB_PASSWORD")

# --- Grid (P1 smart meter) ------------------------------------------------
# Net active power at the meter: positive = importing from grid,
# negative = exporting (feeding solar back). Updates ~every 5 s.
GRID_POWER = "sensor.p1_meter_active_power"
GRID_POWER_PHASES = [
    "sensor.p1_meter_active_power_l1",
    "sensor.p1_meter_active_power_l2",
    "sensor.p1_meter_active_power_l3",
]

# --- Solar production -----------------------------------------------------
# AC output power of the PV inverter(s), in W. These are SUMMED, so list only
# non-overlapping sources. `dne3a230em_uitgangsvermogen` is the inverter AC
# output; `thuis_total_uitgangsvermogen` is the SAME inverter reported again
# (do NOT add it). `power_production_now` is a forecast, not actuals.
SOLAR_POWER = [
    "sensor.dne3a230em_uitgangsvermogen",
    "sensor.growatt_3000s_pv_vermogen",  # second (string) inverter; sparse so far
]

# --- Directly measured loads ---------------------------------------------
# Each of these is attributed exactly (it has its own power sensor).
MEASURED_LOADS = {
    "heatpump_addheat": "sensor.warmtepomp_power_internal_add_heat",  # kW! scaled below
    "plug_schuur": "sensor.innr_schakelaar_schuur_vermogen",
    "plug_woonkamer": "sensor.zigbee_schakelaar_woonkamer_vermogen",
}
# Note: the Innr SP 240 (sensor.energiemonitoring_vermogen) is the *roaming*
# calibration plug -- it moves between devices, so it lives in
# PLUG_ASSIGNMENTS below, not here.

# Entities whose unit is kW (not W); the loader multiplies these by 1000.
KW_ENTITIES = {"sensor.warmtepomp_power_internal_add_heat"}

# --- Heat-pump compressor estimate ---------------------------------------
# The NIBE F1255 does not report compressor electrical power (the BE1/2/3
# current sensors read 0). We estimate it from compressor frequency with a
# linear curve: 0 W below MIN_HZ, rising to RATED_W at RATED_HZ. Calibrate
# RATED_W against P1 once you have winter data (see report --calibrate hint).
HEATPUMP_FREQ = "sensor.warmtepomp_current_compressor_frequency"
HEATPUMP_MIN_HZ = 15.0     # below this the compressor is effectively off
HEATPUMP_RATED_HZ = 90.0   # nameplate / max frequency
HEATPUMP_RATED_W = 2600.0  # est. electrical input at rated Hz (TUNE ME)

# --- Roaming calibration plug ----------------------------------------------
# Move a metering smart plug from device to device and register each period
# here. Within a period the plug's measurement is attributed to that device
# (and the overlapping heuristic/profile is suppressed, so nothing is counted
# twice). Run with --calibrate to turn a week of plug data into fitted config
# values.
#
#   plug:    the plug's power entity (W)
#   device:  free-form name; use "fridge" or a STATE_PROFILES key ("tv",
#            "nad_amp", ...) to calibrate the corresponding model, anything
#            else ("dishwasher") to just measure a new device
#   start /  ISO dates (local). end may be None = "still there".
#   end
PLUG_ASSIGNMENTS = [
    # Innr SP 240 roaming plug. Set "start" to the day you place it on the
    # device and leave "end": None while it's there; add an end date (and a
    # new entry) when you move it on.
    {"plug": "sensor.energiemonitoring_vermogen", "device": "fridge",
     "start": "2026-06-13", "end": None},
]

# --- State-profiled devices ------------------------------------------------
# Devices with no power meter but a status in HA: each state gets a fixed
# wattage. `default` covers unknown/unavailable/unseen states. Watts are
# catalogue estimates -- run with --learn to get data-fitted suggestions.
STATE_PROFILES = {
    "tv": {
        "entity": "media_player.48oled935_12_2",  # clean on/off sub-entity
        "watts": {"on": 120.0, "off": 0.5},
        "default": 0.5,
    },
    "bluesound_voorkamer": {
        "entity": "media_player.voorkamer",
        "watts": {"playing": 15.0, "paused": 7.0, "idle": 6.0, "off": 3.0},
        "default": 6.0,
    },
    "bluesound_eetkamer": {
        "entity": "media_player.eetkamer",
        "watts": {"playing": 15.0, "paused": 7.0, "idle": 6.0, "off": 3.0},
        "default": 6.0,
    },
    "nad_amp": {
        "entity": "media_player.nad_c338",
        "watts": {"playing": 40.0, "on": 20.0, "idle": 20.0, "off": 1.0},
        "default": 1.0,
    },
    # WTW ventilation runs 24/7; fan power depends on the flow mode
    # (flowmode speeds 50/150/190/250 m3/h on this unit).
    "ventilation": {
        "entity": "sensor.ebusd_excellent_fanmode",
        "watts": {"Reduced": 15.0, "Normal": 40.0, "High": 80.0},
        "default": 40.0,
    },
}

# States meaning "no signal" rather than a real device state.
NON_STATES = {"unavailable", "unknown", ""}

# Recording for these domains stopped on 2026-06-09 (recorder exclude), so a
# forward-filled last state would be held forever. After this many hours
# without an observation the profile falls back to `default` watts.
STATE_STALE_HOURS = {"media_player": 24}

# --- Lighting ---------------------------------------------------------------
# Aggregate estimate over individual bulbs: max watts x brightness/255 while
# on. Hue group/zone entities duplicate their member bulbs and MUST be
# excluded (list below taken from the Hue attrs in the recorder DB).
LIGHT_DEFAULT_MAX_W = 7.0
LIGHT_MAX_W = {  # per-entity overrides where the default is clearly wrong
    "light.hue_enrave_ceiling_1": 33.0,
    "light.hue_enrave_ceiling_2": 33.0,
    "light.u7_in_wall_led": 1.0,
    "light.u7_lite_led": 1.0,
    "light.usw_flex_mini_tv_kast_led": 1.0,
    "light.usw_flex_mini_zolder_led": 1.0,
}
LIGHT_GROUPS = {
    "light.achtertuin", "light.badkamer_1evd", "light.badkamer_2e",
    "light.bed", "light.berging", "light.eethoek", "light.eettafel",
    "light.kamer_marijn", "light.keuken", "light.overloop_1e_verdieping",
    "light.overloop_2e_vd", "light.slaapkamer_jacomijn",
    "light.slaapkamer_pieter", "light.toilet_2", "light.trap_1e_verdieping",
    "light.trap_en_overloop_1e_verd", "light.vestibule_gang",
    "light.woonkamer", "light.woonkamer_2", "light.woonkamer_midden",
    "light.woonkamer_zitgedeelte", "light.zolder",
}

# --- Induction hob detector (inferred from the unexplained remainder) -----
# Induction draws large, fast steps (~1-3.5 kW) for minutes at meal times.
INDUCTION_MIN_W = 800.0      # remainder above this may be cooking
INDUCTION_MAX_W = 7400.0     # cap (3-phase hob fuse ceiling)
INDUCTION_MIN_MINUTES = 2.0   # ignore shorter blips (kettle, microwave bursts)
INDUCTION_MAX_MINUTES = 90.0  # runs longer than this are likely an appliance cycle, not a hob
# Only count cooking inside meal windows (local time). This is the cheapest way
# to stop the detector eating the dishwasher / washing machine / dryer, which
# also draw >800 W. Set to None to consider the whole day.
INDUCTION_HOURS = [(7, 9), (11, 14), (17, 21)]

# --- Fridge detector ------------------------------------------------------
# Fridge/freezer compressors cycle: a small, near-constant draw that switches
# on/off. We estimate the persistent low-amplitude cyclic band in the
# remainder. Set FRIDGE_W if you know the plate rating to override detection.
FRIDGE_TYPICAL_W = 90.0   # assumed running power of one fridge compressor
FRIDGE_MAX_W = 250.0      # ceiling for what counts as fridge (vs other loads)
FRIDGE_W = None           # set to a number to force a fixed running power

# --- Resampling -----------------------------------------------------------
# All series are aligned onto a uniform grid before maths. 30 s keeps induction
# steps sharp while staying light (~30 days = 86k rows).
RESAMPLE = "30s"
