# Energy disaggregation

Breaks whole-house electricity use down to individual devices, using the P1
smart-meter signal plus whatever else Home Assistant records.

## What it does

1. **Reconstructs true consumption.** The P1 meter only reports *net* power
   (import minus solar export). Real house consumption is recovered as
   `grid_power + solar_power`.
2. **Subtracts directly-measured loads** — solar inverters, smart plugs, the
   heat-pump backup heater. These are attributed exactly.
3. **Estimates the heat-pump compressor** from its frequency (the NIBE F1255
   doesn't report compressor watts; its `current_be*` sensors read 0).
4. **Profiles status devices** (`STATE_PROFILES` in config) — TV, Bluesound
   zones, NAD amp, WTW ventilation get a fixed wattage per reported state, and
   lighting is estimated per bulb as max-watts × brightness (Hue group
   entities are excluded to avoid double counting).
5. **Infers the induction hob and fridge** from the leftover *remainder* signal
   using transparent heuristics.
6. Whatever's still unexplained is reported as **`other`** (oven, dishwasher,
   washing machine, dryer, standby, …).

The device columns always **reconcile to total consumption**.

## Usage

```bash
cd scripts
# Last 7 days from a local recorder DB copy (works off-LAN)
venv/bin/python run_energy_analysis.py --db ./home-assistant_v2.db --days 7 \
    --daily --plot energy_breakdown.png

# A specific window from the live InfluxDB (on the LAN)
INFLUXDB_PASSWORD=... venv/bin/python run_energy_analysis.py --source influx \
    --start 2026-06-01 --end 2026-06-08 --plot week.png
```

Both back-ends produce the same kWh breakdown table and PNG. The SQLite source
is limited to recorder retention (~30 days of 5 s data); InfluxDB has the full
history back to 2022 (though P1/solar only — the heat pump was added 24 May
2026).

## Accuracy & how to calibrate

These figures are only as good as the model. Tune in `config.py`:

- **Heat-pump compressor** — `HEATPUMP_RATED_W` (default 2600 W) is the biggest
  unknown. Calibrate it on a winter night when the compressor runs steadily and
  nothing else is on: read P1, subtract the known baseline, and set `RATED_W`
  so the estimate matches. If `other` goes negative, the estimate is too high.
- **Induction hob** — this is *inferred*, not measured, and is the least
  reliable number. It's gated to meal windows (`INDUCTION_HOURS`) and a power
  band / run-length to avoid grabbing the oven, dishwasher, washing machine and
  dryer, which also draw >800 W. It will still over- or under-count; treat it as
  an order-of-magnitude figure, not a meter reading. To make it exact you'd need
  a clamp meter on the hob circuit.
- **Fridge** — modelled as a fixed running power (`FRIDGE_TYPICAL_W`, 90 W)
  during its compressor duty cycle. Set `FRIDGE_W` if you know the plate rating,
  or put the fridge on a smart plug for ground truth.
- **State profiles** — run with `--learn` to regression-fit the wattages
  against the P1 remainder. Coefficients are dummy-coded ("extra watts vs the
  device's most-common state"); the intercept is the house's always-on
  baseline. Trust a learned value only when its `time_share_%` is meaningful
  and the magnitude is physically plausible — a device that's only ever used
  while cooking will absorb induction watts (e.g. Bluesound eetkamer fitting
  at +500 W is the hob, not the speaker).

## Roaming calibration plug

Move a metering smart plug from device to device, a week at a time, and
register each stay as an *assignment*: `{plug, device, start, end|None}`
(`end: None` = still attached).

Assignments live in an **editable JSON store** (`assignments.py`), managed at
runtime — easiest via the [energy-dashboard](../../energy-dashboard) UI, or
programmatically:

```python
from energy_analytics import assignments
assignments.add("sensor.energiemonitoring_vermogen", "fridge", "2026-06-13")
```

The store path is `$ENERGY_ASSIGNMENTS_FILE` (default next to the package). If
the file doesn't exist yet, the seed in `config._DEFAULT_PLUG_ASSIGNMENTS` is
used. All code reads assignments fresh via `config.plug_assignments()`.

During an assignment the plug's measurement is attributed to that device and
the overlapping model is silenced (the fridge heuristic, or the device's state
profile), so nothing is double-counted. Then:

```bash
venv/bin/python run_energy_analysis.py --db ./home-assistant_v2.db --days 7 --calibrate
```

prints fitted values per stay: for the fridge its running power and duty cycle
(`FRIDGE_W`), for profiled devices the measured mean watts per state — ground
truth that supersedes the `--learn` regression. For a new device (dishwasher,
dryer) it reports mean watts, duty cycle and kWh/day so you can decide whether
to give it a profile.

Suggested tour (one week each): fridge → TV/media corner → NAD amp →
dishwasher → washing machine. After the tour the `other` bucket should mostly
be oven, kettle and standby.

## Device fingerprints

A fingerprint is a stored profile of a device, built from a plug week and kept
in `fingerprints/<device>.json` so the library grows as the plug tours the
house. It captures two layers:

- **scalar calibration** — running/standby watts, duty cycle, kWh/day, and
  (for state-profiled devices) measured watts per HA state;
- **cycle shape** — how the device switches on/off: cycles per day, typical
  on-duration (median/IQR), and a normalised power curve of a representative
  cycle.

Build one for every active plug assignment:

```bash
venv/bin/python run_energy_analysis.py --db ./home-assistant_v2.db --days 7 --save-fingerprint
```

Re-running for a device that already has a fingerprint archives the old version
under `history`, so you can see how a device changes over time.

### Recognising devices without the plug

Once a device is fingerprinted, `--match` looks for its signature in the
`other`/remainder series of any later window — even after the plug has moved
on:

```bash
venv/bin/python run_energy_analysis.py --db ./home-assistant_v2.db --days 7 --match
```

Cycles in the remainder are scored against every stored fingerprint by
running-power and cycle-duration similarity (intra-cycle shape is a bonus only
when it actually varies — a flat fridge plateau carries no shape information).
The result is a **report** of how much of `other` each known device probably
explains; it deliberately does not overwrite the attribution, because a live
plug measurement should always beat a shape guess. Tune the acceptance bar
with the `min_score` argument in `matcher.match` (default 0.6).

## Known data gaps

- `media_player` was excluded from the recorder on 2026-06-09
  (configuration.yaml), so the local DB copy has no TV/Bluesound states after
  that date. The influxdb integration is unfiltered and listens to the event
  bus directly, so InfluxDB on the Synology should still receive them — use
  `--source influx` for post-June-9 windows, or narrow the recorder exclusion
  to specific entities instead of the whole domain. A stale-data guard
  (`STATE_STALE_HOURS`) drops media-player states to `default` watts 24 h
  after the last observation so a forward-filled "on" can't run forever.

- Smart plugs (`innr_*`, `zigbee_schakelaar_woonkamer`) have hourly history but
  no recent `states` — they may be offline/renamed. The code loads them anyway
  and treats a missing series as 0.
- `growatt_3000s_pv_vermogen` (second inverter) reports only sporadically so
  far; once it streams reliably it's already summed into `solar_power`.

## Layout

| file | role |
|------|------|
| `config.py` | entity → signal mapping and all tunable thresholds |
| `loader.py` | SQLite + InfluxDB back-ends, step-fill, kWh integration |
| `decompose.py` | consumption, measured loads, compressor model, remainder |
| `profiles.py` | state→watts profiles (TV, audio, ventilation) + lighting |
| `disaggregate.py` | induction + fridge detectors |
| `learn.py` | `--learn`: regression-fit state wattages from the data |
| `calibrate.py` | `--calibrate`: fitted config values from roaming-plug weeks |
| `fingerprint.py` | `--save-fingerprint`: build/store device fingerprints |
| `matcher.py` | `--match`: recognise fingerprinted devices in the remainder |
| `report.py` | kWh tables and the breakdown plot |
| `../run_energy_analysis.py` | CLI entry point |
