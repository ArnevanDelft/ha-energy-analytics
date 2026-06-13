# ha-energy-analytics

Per-device energy disaggregation for a Home Assistant + InfluxDB (Synology)
setup. Decomposes whole-house P1 consumption into per-device kWh — measured
loads exactly, the heat-pump compressor from a frequency model, TV / audio /
ventilation / lighting from state-conditioned profiles, and the induction hob
and fridge inferred from the remainder — plus a roaming-plug calibration and a
device-fingerprint library for recognising un-metered devices.

This is the **installable package**. It is consumed by:

- the calibration/CLI workflow (run it on a machine that can reach InfluxDB or
  a recorder DB copy), and
- the [energy-dashboard](../energy-dashboard) web app (Docker on Synology).

## Install

```bash
pip install -e .            # from a checkout (editable)
pip install -e .[plot]      # also pull in matplotlib for --plot
# or straight from git:
pip install "git+https://github.com/ArnevanDelft/ha-energy-analytics.git"
```

## CLI

After install a console command `energy-analysis` is available:

```bash
# last 7 days from a local recorder DB copy
energy-analysis --db ./home-assistant_v2.db --days 7

# a window from the live InfluxDB, with calibration + fingerprints + matching
energy-analysis --source influx --start 2026-06-01 --end 2026-06-08 \
    --calibrate --save-fingerprint --match
```

See `energy_analytics/README_module.md` for the full method, the config model
(`config.py`), the roaming-plug workflow, and the fingerprint format.

## Configuration via environment

| Variable | Default | Purpose |
|---|---|---|
| `INFLUX_HOST` | `192.168.10.237` | InfluxDB host |
| `INFLUX_PORT` | `8086` | InfluxDB port |
| `INFLUX_DATABASE` | `homeassistant` | database name |
| `INFLUX_USERNAME` | _(empty)_ | leave empty for unauthenticated reads |
| `INFLUX_PASSWORD` | — | set if auth is enabled |
| `ENERGY_FINGERPRINT_DIR` | `<package>/fingerprints` | shared fingerprint store (mount a volume on Synology) |

Entity mappings, smart-plug assignments, state profiles and detector
thresholds live in [`energy_analytics/config.py`](energy_analytics/config.py).
