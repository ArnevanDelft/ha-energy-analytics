"""Data loading for the energy analysis.

Two interchangeable back-ends return the same thing: a uniform-grid pandas
Series of watts for a given entity over a time window.

  * SqliteLoader  -- reads the high-resolution `states` table from a (copy of
                     the) Home Assistant recorder DB. Use this off-LAN / for
                     development. Limited to recorder retention (~30 days).
  * InfluxLoader  -- reads from the InfluxDB v1 instance that HA writes to
                     live. Use this on-LAN for the full history. Matches the
                     schema produced by the HA influxdb integration and your
                     migrate_ha_to_influxdb.py: measurement=unit, tag
                     entity_id=<object id>, field `value`.

Both apply the same post-processing: parse to float, treat the signal as a
step function (forward-fill onto a uniform grid), and scale kW->W where needed.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import requests

from . import config


def _object_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def _utc(ts) -> pd.Timestamp:
    """Coerce to a tz-aware UTC Timestamp whether the input is naive or aware."""
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _postprocess(s: pd.Series, entity_id: str, start, end, freq: str) -> pd.Series:
    """Step-fill a raw (timestamp, value) series onto a uniform grid in watts."""
    s = pd.to_numeric(s, errors="coerce").dropna()
    if entity_id in config.KW_ENTITIES:
        s = s * 1000.0
    grid = pd.date_range(start=start, end=end, freq=freq, tz="UTC")
    if s.empty:
        return pd.Series(0.0, index=grid, name=entity_id)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    # Step signal: each reading holds until the next -> reindex with ffill.
    out = s.reindex(s.index.union(grid)).ffill().reindex(grid)
    out = out.fillna(0.0)
    out.name = entity_id
    return out


def _step_fill_states(raw: pd.Series, start, end, freq, stale_hours=None) -> pd.Series:
    """Step-fill a string-valued state series onto the uniform grid.

    Points before the first observation become None. If `stale_hours` is set,
    points more than that many hours past the LAST observation also become
    None -- guards against holding a state forever after recording stopped
    (e.g. media_player was excluded from the recorder on 2026-06-09).
    """
    grid = pd.date_range(start=start, end=end, freq=freq, tz="UTC")
    if raw.empty:
        return pd.Series([None] * len(grid), index=grid, dtype="object")
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    out = raw.reindex(raw.index.union(grid)).ffill().reindex(grid)
    out = out.astype("object").where(out.notna(), None)
    if stale_hours is not None:
        cutoff = raw.index.max() + pd.Timedelta(hours=stale_hours)
        out[out.index > cutoff] = None
    return out


class SqliteLoader:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self._has_meta = bool(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='states_meta'"
            ).fetchone()
        )

    def span(self):
        """Earliest/latest state timestamps available (UTC)."""
        row = self.conn.execute(
            "SELECT MIN(last_updated_ts), MAX(last_updated_ts) FROM states"
        ).fetchone()
        to_dt = lambda t: datetime.fromtimestamp(t, tz=timezone.utc) if t else None
        return to_dt(row[0]), to_dt(row[1])

    def load(self, entity_id, start, end, freq=config.RESAMPLE) -> pd.Series:
        start, end = _utc(start), _utc(end)
        if self._has_meta:
            sql = """
                SELECT s.last_updated_ts AS ts, s.state AS v
                FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
                WHERE m.entity_id = ? AND s.last_updated_ts BETWEEN ? AND ?
                ORDER BY s.last_updated_ts
            """
        else:
            sql = """
                SELECT last_updated_ts AS ts, state AS v FROM states
                WHERE entity_id = ? AND last_updated_ts BETWEEN ? AND ?
                ORDER BY last_updated_ts
            """
        # Pull a little history before `start` so the step-fill has a seed value.
        rows = self.conn.execute(
            sql, (entity_id, start.timestamp() - 3600, end.timestamp())
        ).fetchall()
        if rows:
            idx = pd.to_datetime([r["ts"] for r in rows], unit="s", utc=True)
            raw = pd.Series([r["v"] for r in rows], index=idx)
        else:
            raw = pd.Series(dtype="float64")
        return _postprocess(raw, entity_id, start, end, freq)

    def _state_rows(self, entity_id, start, end, with_attrs=False):
        attrs_col = ", COALESCE(sa.shared_attrs, s.attributes) AS attrs" if with_attrs else ""
        attrs_join = "LEFT JOIN state_attributes sa ON s.attributes_id = sa.attributes_id" \
            if with_attrs else ""
        if self._has_meta:
            sql = f"""
                SELECT s.last_updated_ts AS ts, s.state AS v{attrs_col}
                FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id
                {attrs_join}
                WHERE m.entity_id = ? AND s.last_updated_ts BETWEEN ? AND ?
                ORDER BY s.last_updated_ts
            """
        else:
            sql = f"""
                SELECT s.last_updated_ts AS ts, s.state AS v{attrs_col}
                FROM states s {attrs_join}
                WHERE s.entity_id = ? AND s.last_updated_ts BETWEEN ? AND ?
                ORDER BY s.last_updated_ts
            """
        # Seed window of 7 days: state devices change rarely, so we need a
        # longer look-back than power sensors to find the value at `start`.
        return self.conn.execute(
            sql, (entity_id, start.timestamp() - 7 * 86400, end.timestamp())
        ).fetchall()

    def load_states(self, entity_id, start, end, freq=config.RESAMPLE,
                    stale_hours=None) -> pd.Series:
        """String-valued state series on the uniform grid (None = no data)."""
        start, end = _utc(start), _utc(end)
        rows = [r for r in self._state_rows(entity_id, start, end)
                if r["v"] not in config.NON_STATES]
        if rows:
            idx = pd.to_datetime([r["ts"] for r in rows], unit="s", utc=True)
            raw = pd.Series([r["v"] for r in rows], index=idx)
        else:
            raw = pd.Series(dtype="object")
        out = _step_fill_states(raw, start, end, freq, stale_hours)
        out.name = entity_id
        return out

    def load_light(self, entity_id, start, end, freq=config.RESAMPLE) -> pd.Series:
        """Fraction of full brightness (0..1) the light runs at, on the grid."""
        import json

        start, end = _utc(start), _utc(end)
        ts_list, frac_list = [], []
        for r in self._state_rows(entity_id, start, end, with_attrs=True):
            state = r["v"]
            if state in config.NON_STATES:
                continue
            if state != "on":
                frac = 0.0
            else:
                frac = 1.0
                if r["attrs"]:
                    try:
                        b = json.loads(r["attrs"]).get("brightness")
                        if b is not None:
                            frac = float(b) / 255.0
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
            ts_list.append(r["ts"])
            frac_list.append(frac)
        if ts_list:
            idx = pd.to_datetime(ts_list, unit="s", utc=True)
            raw = pd.Series(frac_list, index=idx)
        else:
            raw = pd.Series(dtype="float64")
        grid = pd.date_range(start=start, end=end, freq=freq, tz="UTC")
        if raw.empty:
            return pd.Series(0.0, index=grid, name=entity_id)
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()
        out = raw.reindex(raw.index.union(grid)).ffill().reindex(grid).fillna(0.0)
        out.name = entity_id
        return out

    def list_entities(self, domain: str) -> list[str]:
        if self._has_meta:
            rows = self.conn.execute(
                "SELECT entity_id FROM states_meta WHERE entity_id LIKE ?",
                (f"{domain}.%",),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT entity_id FROM states WHERE entity_id LIKE ?",
                (f"{domain}.%",),
            ).fetchall()
        return sorted(r["entity_id"] for r in rows)


class InfluxLoader:
    """Reads HA's live InfluxDB v1 over the HTTP query API (InfluxQL)."""

    def __init__(self, host="192.168.10.237", port=8086, database="homeassistant",
                 username="hauser", password=None):
        self.url = f"http://{host}:{port}/query"
        self.params = {"db": database, "epoch": "ns"}
        self.session = requests.Session()
        self.timeout = int(os.environ.get("INFLUX_TIMEOUT", "90"))
        if username:
            self.session.auth = (username, password or "")

    @classmethod
    def from_env(cls) -> "InfluxLoader":
        """Build a loader from the INFLUX_* settings in config (env-driven)."""
        return cls(config.INFLUX_HOST, config.INFLUX_PORT, config.INFLUX_DATABASE,
                   config.INFLUX_USERNAME, config.INFLUX_PASSWORD)

    def load(self, entity_id, start, end, freq=config.RESAMPLE) -> pd.Series:
        start, end = _utc(start), _utc(end)
        # The HA integration tags rows with the object id (no domain prefix).
        oid = _object_id(entity_id)
        # measurement is the unit; we don't know it up front, so match on the
        # entity_id tag across measurements via a regex-free OR is awkward in
        # InfluxQL -- instead query the value field grouped by the tag.
        q = (
            f'SELECT "value" FROM /.*/ '
            f"WHERE \"entity_id\" = '{oid}' "
            f"AND time >= {int(start.timestamp()*1e9)-3600_000_000_000} "
            f"AND time <= {int(end.timestamp()*1e9)}"
        )
        raw = self._query_series(q, "value")
        return _postprocess(raw, entity_id, start, end, freq)

    def _query_series(self, q, field) -> pd.Series:
        resp = self.session.get(self.url, params={**self.params, "q": q},
                                timeout=self.timeout)
        resp.raise_for_status()
        results = resp.json().get("results", [{}])[0].get("series", [])
        frames = []
        for serie in results:
            cols = serie["columns"]
            if field not in cols:
                continue
            ti, vi = cols.index("time"), cols.index(field)
            vals = serie["values"]
            idx = pd.to_datetime([r[ti] for r in vals], unit="ns", utc=True)
            frames.append(pd.Series([r[vi] for r in vals], index=idx))
        out = pd.concat(frames) if frames else pd.Series(dtype="object")
        return out.dropna()

    def _query_frame(self, q) -> pd.DataFrame:
        """Run a query once and return all fields as a time-indexed frame."""
        resp = self.session.get(self.url, params={**self.params, "q": q},
                                timeout=self.timeout)
        resp.raise_for_status()
        results = resp.json().get("results", [{}])[0].get("series", [])
        frames = []
        for serie in results:
            df = pd.DataFrame(serie["values"], columns=serie["columns"])
            df.index = pd.to_datetime(df["time"], unit="ns", utc=True)
            frames.append(df.drop(columns=["time"]))
        return pd.concat(frames) if frames else pd.DataFrame()

    def _time_clause(self, start, end, lookback_s=0):
        return (f"time >= {int(start.timestamp()*1e9) - int(lookback_s*1e9)} "
                f"AND time <= {int(end.timestamp()*1e9)}")

    def load_states(self, entity_id, start, end, freq=config.RESAMPLE,
                    stale_hours=None) -> pd.Series:
        """Non-numeric states live in the `state` field. Unit-less entities are
        stored under a measurement named after the full entity_id, so we query
        that measurement directly instead of scanning all of them with /.*/."""
        start, end = _utc(start), _utc(end)
        q = (f'SELECT "state" FROM "{entity_id}" '
             f"WHERE {self._time_clause(start, end, 7 * 86400)}")
        raw = self._query_series(q, "state")
        raw = raw[~raw.isin(config.NON_STATES)]
        out = _step_fill_states(raw, start, end, freq, stale_hours)
        out.name = entity_id
        return out

    def load_light(self, entity_id, start, end, freq=config.RESAMPLE) -> pd.Series:
        """Brightness fraction (0..1). Lights are stored under a measurement
        named after the entity_id, with `state` (on/off) and `brightness`
        fields -- fetched in a single direct query."""
        start, end = _utc(start), _utc(end)
        tc = self._time_clause(start, end, 7 * 86400)
        q = f'SELECT "state","brightness" FROM "{entity_id}" WHERE {tc}'
        df = self._query_frame(q)
        grid = pd.date_range(start=start, end=end, freq=freq, tz="UTC")
        if df.empty or "state" not in df:
            return pd.Series(0.0, index=grid, name=entity_id)
        states = df["state"].dropna()
        bright = (df["brightness"].dropna() if "brightness" in df
                  else pd.Series(dtype="float64"))
        states = states[~states.isin(config.NON_STATES)]
        on = _step_fill_states(states, start, end, freq).eq("on")
        frac = pd.Series(1.0, index=grid)
        if not bright.empty:
            bright = pd.to_numeric(bright, errors="coerce").dropna() / 255.0
            bright = bright[~bright.index.duplicated(keep="last")].sort_index()
            frac = bright.reindex(bright.index.union(grid)).ffill().reindex(grid).fillna(1.0)
        out = frac.where(on, 0.0)
        out.name = entity_id
        return out

    def list_entities(self, domain: str) -> list[str]:
        q = f'SHOW TAG VALUES WITH KEY = "entity_id" WHERE "domain" = \'{domain}\''
        resp = self.session.get(self.url, params={**self.params, "q": q}, timeout=60)
        resp.raise_for_status()
        results = resp.json().get("results", [{}])[0].get("series", [])
        oids = {row[1] for serie in results for row in serie.get("values", [])}
        return sorted(f"{domain}.{o}" for o in oids)


def assignment_mask(index: pd.DatetimeIndex, assignment: dict) -> pd.Series:
    """Boolean mask of grid points covered by a PLUG_ASSIGNMENTS entry.
    start/end are interpreted as local (Europe/Amsterdam) dates."""
    start = pd.Timestamp(assignment["start"], tz="Europe/Amsterdam")
    mask = index >= start
    if assignment.get("end"):
        mask &= index < pd.Timestamp(assignment["end"], tz="Europe/Amsterdam")
    return pd.Series(mask, index=index)


def device_suppressed(index: pd.DatetimeIndex, device: str) -> pd.Series:
    """Mask of times where `device` is plug-measured (model must yield)."""
    out = pd.Series(False, index=index)
    for a in config.plug_assignments():
        if a["device"] == device:
            out |= assignment_mask(index, a)
    return out


def load_many(loader, entity_ids, start, end, freq=config.RESAMPLE) -> pd.DataFrame:
    """Load several entities aligned on one grid. Missing entities become 0."""
    data = {}
    for eid in entity_ids:
        try:
            data[eid] = loader.load(eid, start, end, freq)
        except Exception as err:  # one bad/absent series shouldn't kill the run
            print(f"  ! could not load {eid}: {err}")
    return pd.DataFrame(data)


def integrate_kwh(power_w: pd.Series | pd.DataFrame, freq=config.RESAMPLE):
    """Integrate a uniform-grid watt series/frame to kWh over the whole window."""
    step_h = pd.Timedelta(freq).total_seconds() / 3600.0
    return (power_w * step_h / 1000.0).sum()
