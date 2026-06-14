"""Editable store for roaming-plug assignments.

An assignment records that a metering plug sat on a given device for a period:
``{"id", "plug", "device", "start", "end"|None}``. The analytics only reads
plug/device/start/end (see config.plug_assignments); the ``id`` is just a
stable handle for the dashboard's edit/delete actions.

Persisted as JSON at ``$ENERGY_ASSIGNMENTS_FILE`` so the dashboard (Docker)
and the CLI (calibration host) share one source of truth on a mounted volume.
If the file does not exist yet, the built-in seed from config is returned.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from . import config

PATH = Path(
    os.environ.get("ENERGY_ASSIGNMENTS_FILE")
    or Path(__file__).resolve().parent.parent / "assignments.json"
)


def _normalise(item: dict) -> dict:
    """Ensure required keys + a stable id; drop unknown cruft."""
    return {
        "id": item.get("id") or uuid.uuid4().hex[:8],
        "plug": item["plug"],
        "device": item["device"],
        "start": item["start"],
        "end": item.get("end") or None,
    }


def load() -> list[dict]:
    """All assignments, or the config seed if the store does not exist yet."""
    if not PATH.exists():
        return [_normalise(a) for a in config._DEFAULT_PLUG_ASSIGNMENTS]
    try:
        raw = json.loads(PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [_normalise(a) for a in raw]


def save(items: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps([_normalise(a) for a in items], indent=2))


def ensure_seeded() -> None:
    """Write the config seed once, so the editable store starts populated."""
    if not PATH.exists():
        save([_normalise(a) for a in config._DEFAULT_PLUG_ASSIGNMENTS])


def add(plug: str, device: str, start: str, end: str | None = None) -> dict:
    if not (plug and device and start):
        raise ValueError("plug, device and start are required")
    item = _normalise({"plug": plug, "device": device, "start": start, "end": end})
    items = load()
    items.append(item)
    save(items)
    return item


def update(assignment_id: str, **fields) -> dict | None:
    items = load()
    out = None
    for a in items:
        if a["id"] == assignment_id:
            a.update({k: v for k, v in fields.items()
                      if k in ("plug", "device", "start", "end")})
            out = a
    if out is not None:
        save(items)
    return out


def delete(assignment_id: str) -> bool:
    items = load()
    kept = [a for a in items if a["id"] != assignment_id]
    if len(kept) == len(items):
        return False
    save(kept)
    return True
