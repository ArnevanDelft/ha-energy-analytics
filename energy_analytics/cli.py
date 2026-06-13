#!/usr/bin/env python3
"""Disaggregate whole-house energy into per-device usage.

Reads power signals from either a local Home Assistant recorder DB copy
(default, works off-LAN) or the live InfluxDB v1 instance, decomposes true
house consumption, and attributes it to devices -- measured ones exactly,
the induction hob and fridge by inference from the P1 remainder.

Examples:
  # Last 7 days from the local DB copy
  energy-analysis --db ./home-assistant_v2.db --days 7 --plot out.png

  # A specific window from InfluxDB (on the LAN)
  energy-analysis --source influx --start 2026-06-01 --end 2026-06-08
"""

import argparse
import sys

import pandas as pd

from energy_analytics import config, decompose, disaggregate, report
from energy_analytics.loader import InfluxLoader, SqliteLoader


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["sqlite", "influx"], default="sqlite")
    p.add_argument("--db", default="./home-assistant_v2.db",
                   help="path to recorder DB copy (sqlite source)")
    p.add_argument("--host", default=config.INFLUX_HOST)
    p.add_argument("--port", type=int, default=config.INFLUX_PORT)
    p.add_argument("--influx-db", default=config.INFLUX_DATABASE)
    p.add_argument("--username", default=config.INFLUX_USERNAME)
    p.add_argument("--password", default=config.INFLUX_PASSWORD)
    p.add_argument("--start", help="ISO date/time, e.g. 2026-06-01")
    p.add_argument("--end", help="ISO date/time, e.g. 2026-06-08")
    p.add_argument("--days", type=float, default=7,
                   help="if --start/--end omitted, analyse the last N days of data")
    p.add_argument("--freq", default=config.RESAMPLE, help="resample grid, e.g. 30s")
    p.add_argument("--plot", help="write a PNG breakdown plot to this path")
    p.add_argument("--daily", action="store_true", help="also print per-day table")
    p.add_argument("--learn", action="store_true",
                   help="fit state wattages to the data and print suggestions")
    p.add_argument("--calibrate", action="store_true",
                   help="report fitted config values from roaming-plug weeks")
    p.add_argument("--save-fingerprint", action="store_true",
                   help="extract+store a device fingerprint for each plug "
                        "assignment overlapping the window")
    p.add_argument("--match", action="store_true",
                   help="match stored fingerprints against the 'other' "
                        "remainder (recognise devices without the plug)")
    return p.parse_args(argv)


def resolve_window(loader_obj, args):
    if args.start and args.end:
        return pd.Timestamp(args.start, tz="UTC"), pd.Timestamp(args.end, tz="UTC")
    if hasattr(loader_obj, "span"):
        _, latest = loader_obj.span()
        if latest is None:
            sys.exit("No data found in the recorder DB.")
        end = pd.Timestamp(latest)
    else:
        end = pd.Timestamp.now(tz="UTC")
    return end - pd.Timedelta(days=args.days), end


def main(argv=None):
    args = parse_args(argv)
    if args.source == "sqlite":
        loader_obj = SqliteLoader(args.db)
    else:
        loader_obj = InfluxLoader(args.host, args.port, args.influx_db,
                                  args.username, args.password)

    start, end = resolve_window(loader_obj, args)
    print(f"Window: {start} .. {end}  (grid {args.freq}, source {args.source})")

    frame = decompose.build_power_frame(loader_obj, start, end, args.freq)
    frame = disaggregate.disaggregate(frame, args.freq)

    print("\n=== Energy breakdown (kWh) ===")
    with pd.option_context("display.max_rows", None, "display.width", 120):
        print(report.breakdown_kwh(frame, args.freq).to_string())

    if args.daily:
        print("\n=== Per-day kWh ===")
        with pd.option_context("display.max_rows", None, "display.width", 160):
            print(report.daily_kwh(frame, args.freq).to_string())

    if args.learn:
        from energy_analytics import learn
        print("\n=== Learned state wattages (suggestions for config.py) ===")
        table = learn.learn_profiles(loader_obj, frame, start, end, args.freq)
        with pd.option_context("display.max_rows", None, "display.width", 120):
            print(table.to_string())
        print("\nCopy plausible learned_W values into STATE_PROFILES; ignore "
              "rows with tiny time_share_% or implausible magnitudes "
              "(collinearity).")

    if args.calibrate:
        from energy_analytics import calibrate
        print("\n=== Roaming-plug calibration ===")
        calibrate.calibrate(loader_obj, start, end, args.freq)

    if args.save_fingerprint:
        from energy_analytics import fingerprint
        print("\n=== Saving device fingerprints ===")
        written = fingerprint.save_from_assignments(loader_obj, start, end, args.freq)
        if not written:
            print("  no plug assignments overlap this window.")

    if args.match:
        from energy_analytics import matcher
        print("\n=== Fingerprint matches in 'other' remainder ===")
        table = matcher.match(frame["other"], args.freq)
        print(table.to_string() if not table.empty
              else "  no stored fingerprints matched (or library empty).")

    if args.plot:
        report.plot(frame, args.plot, args.freq)

    return 0


if __name__ == "__main__":
    sys.exit(main())
