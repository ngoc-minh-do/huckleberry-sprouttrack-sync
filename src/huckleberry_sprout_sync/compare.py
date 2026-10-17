"""Read-only comparison of Huckleberry history against a Sprout Track DB snapshot.

Reads Huckleberry through the sync's own reader/planner (so the comparison uses
the exact mapping the sync writes), reads a Sprout SQLite snapshot, and
classifies each event per type:

  matched       same key + same instant, no field differences
  drift         same key + instant, but one or more fields differ (in-place edit)
  moved         same key, time shifted (start/end edited) -> reports old/new
  near_dup      planned in Huckleberry, no exact match, but a same-key row sits
                within 5 minutes in Sprout (likely a Huckleberry source duplicate
                the sync collapsed)
  truly_missing planned in Huckleberry and absent from Sprout (a real gap)
  sprout_only   present in Sprout but no Huckleberry counterpart (extra)

This module never writes to Sprout Track; it only reads a DB file (a copy of
the instance's ``baby-tracker.db``).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config
from .hb_reader import HuckleberryReader
from .mapping import plan_events, resolve_target_unit
from .models import HbRecord, PlannedEvent

MOVE_WINDOW = timedelta(minutes=15)
DUP_WINDOW = timedelta(minutes=5)
EXACT_WINDOW = timedelta(seconds=2)

FEEDTYPE_TO_BOTTLE = {"formula": "formula", "breast milk": "breast milk", "milk": "milk", "other": "other"}


@dataclass
class Event:
    key: tuple[str, str]
    time: datetime
    fields: dict
    origin: str = ""

    def ident(self) -> str:
        return f"{self.key[0]}/{self.key[1]}"


@dataclass
class SproutRow:
    """A Sprout Track snapshot row plus what the write API needs to target it.

    ``id`` is the original database row id (the webhook GET /activities path
    cannot reach old rows, so edits must reference the snapshot id) and
    ``activity_type`` is the webhook activity type the row belongs to.
    """

    event: Event
    id: str
    activity_type: str


@dataclass
class Comparison:
    matched: list[tuple[Event, Event]] = field(default_factory=list)
    drift: list[tuple[Event, Event, list[str]]] = field(default_factory=list)
    moved: list[tuple[Event, Event]] = field(default_factory=list)
    near_dup: list[Event] = field(default_factory=list)
    truly_missing: list[Event] = field(default_factory=list)
    sprout_only: list[Event] = field(default_factory=list)
    hb_total: int = 0
    sprout_total: int = 0


def _norm(value):
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _parse(raw) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def load_sprout_reference(db_path: str) -> tuple[list[str], dict[str, dict]]:
    """Units + medicines/supplements straight from the snapshot (no API needed)."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    units = [row["unitAbbr"] for row in con.execute("select unitAbbr from Unit")]
    medicines: dict[str, dict] = {}
    for row in con.execute("select * from Medicine where deletedAt is null"):
        name = (row["name"] or "").strip()
        if name:
            medicines[name.lower()] = {
                "name": name,
                "isSupplement": bool(row["isSupplement"]),
                "typicalDoseSize": row["typicalDoseSize"],
                "unitAbbr": row["unitAbbr"],
            }
    con.close()
    return units, medicines


def load_sprout_rows(db_path: str) -> list[SproutRow]:
    """Load every non-deleted activity row from a Sprout Track snapshot.

    Each row keeps the original database ``id`` and webhook activity type so a
    reconcile can PUT edits back through /activities/:activityId. Times are
    parsed from the snapshot exactly as :func:`load_sprout_events` does, so the
    comparison input is identical.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    foods = {row["id"]: row["name"] for row in cur.execute("select id,name from Food")}
    meds: dict[str, str] = {}
    supplements: set[str] = set()
    for row in cur.execute("select id,name,isSupplement from Medicine"):
        meds[row["id"]] = row["name"]
        if row["isSupplement"]:
            supplements.add(row["id"])
    rows: list[SproutRow] = []

    def add(
        key: tuple[str, str],
        row_id: str,
        when: datetime | None,
        fields: dict,
        activity_type: str | None = None,
    ) -> None:
        if when is not None:
            rows.append(
                SproutRow(
                    event=Event(key=key, time=when, fields=fields, origin="sprout"),
                    id=row_id,
                    activity_type=activity_type or key[0],
                )
            )

    for row in cur.execute("select * from FeedLog where deletedAt is null"):
        if row["type"] == "BREAST":
            add(
                ("feed", "BREAST"),
                row["id"],
                _parse(row["startTime"]) or _parse(row["time"]),
                {
                    "side": row["side"],
                    "duration_min": round((row["feedDuration"] or 0) / 60) or None,
                    "notes": _norm(row["notes"]),
                },
            )
        else:
            add(
                ("feed", "BOTTLE"),
                row["id"],
                _parse(row["time"]),
                {
                    "bottle_type": _norm((row["bottleType"] or "").lower()) or None,
                    "amount": _norm(row["amount"]),
                    "unit": _norm(row["unitAbbr"]),
                    "notes": _norm(row["notes"]),
                },
            )
    for row in cur.execute("select * from FoodLog where deletedAt is null"):
        items = json.loads(row["foods"]) if row["foods"] else []
        names = [foods.get(item.get("foodId")) for item in items]
        names = [name for name in names if name]
        if not names and row["foodId"]:
            names = [foods.get(row["foodId"])]
        add(("food", "food"), row["id"], _parse(row["time"]), {"foods": frozenset(names), "notes": _norm(row["notes"])})
    for row in cur.execute("select * from SleepLog where deletedAt is null"):
        add(
            ("sleep", "sleep"),
            row["id"],
            _parse(row["startTime"]),
            {"duration_min": row["duration"], "type": row["type"], "notes": _norm(row["notes"])},
        )
    for row in cur.execute("select * from DiaperLog where deletedAt is null"):
        add(
            ("diaper", "diaper"),
            row["id"],
            _parse(row["time"]),
            {
                "diaper_type": row["type"],
                "condition": row["condition"],
                "color": row["color"],
                "notes": _norm(row["notes"]),
            },
        )
    for row in cur.execute("select * from PlayLog where deletedAt is null"):
        add(
            ("play", "play"),
            row["id"],
            _parse(row["startTime"]),
            {"play_type": row["type"], "duration_min": row["duration"], "notes": _norm(row["notes"])},
        )
    for row in cur.execute("select * from BathLog where deletedAt is null"):
        add(
            ("bath", "bath"),
            row["id"],
            _parse(row["time"]),
            {"bath_type": row["bathType"], "notes": _norm(row["notes"])},
        )
    for row in cur.execute("select * from Measurement where deletedAt is null"):
        add(
            ("measurement", row["type"]),
            row["id"],
            _parse(row["date"]),
            {"value": _norm(row["value"]), "unit": _norm(row["unit"]), "notes": _norm(row["notes"])},
        )
    for row in cur.execute("select * from PumpLog where deletedAt is null"):
        add(
            ("pump", "pump"),
            row["id"],
            _parse(row["startTime"]),
            {
                "total": _norm(row["totalAmount"]),
                "left": _norm(row["leftAmount"]),
                "right": _norm(row["rightAmount"]),
                "unit": _norm(row["unitAbbr"]),
                "duration_min": row["duration"],
                "notes": _norm(row["notes"]),
            },
        )
    for row in cur.execute("select * from MedicineLog where deletedAt is null"):
        add(
            ("medicine", "medicine"),
            row["id"],
            _parse(row["time"]),
            {
                "name": _norm(meds.get(row["medicineId"])),
                "amount": _norm(row["doseAmount"]),
                "unit": _norm(row["unitAbbr"]),
                "notes": _norm(row["notes"]),
            },
            activity_type="supplement" if row["medicineId"] in supplements else "medicine",
        )
    con.close()
    return rows


def load_sprout_events(db_path: str) -> list[Event]:
    """Planned-comparable events from a Sprout Track DB snapshot (see :func:`load_sprout_rows`)."""
    return [row.event for row in load_sprout_rows(db_path)]


def plan_hb_event_pairs(
    records: list[HbRecord],
    cfg: Config,
    *,
    resolved_unit: str | None,
    medicines: dict[str, dict] | None = None,
) -> list[tuple[Event, PlannedEvent]]:
    """Planned Huckleberry events paired with their PlannedEvent write payload.

    Keeps the webhook payload (needed to POST a missing event or PUT an edit)
    attached to the comparison ``Event`` used by :func:`compare_events`.
    """
    out: list[tuple[Event, PlannedEvent]] = []
    for planned in plan_events(records, cfg, resolved_unit=resolved_unit, medicines=medicines):
        kind = planned.kind
        payload = planned.payload
        key: tuple[str, str] | None = None
        fields: dict = {}
        if planned.sprout_type == "feed" and kind == "bottle":
            key = ("feed", "BOTTLE")
            fields = {
                "bottle_type": _norm(FEEDTYPE_TO_BOTTLE.get(payload.get("feedType"), payload.get("feedType"))),
                "amount": _norm(payload.get("amount")),
                "unit": _norm(payload.get("unitAbbr")),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "feed" and kind == "breast":
            key = ("feed", "BREAST")
            fields = {
                "side": payload.get("side"),
                "duration_min": payload.get("duration"),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "food":
            names = payload.get("foods") or ([payload.get("foodName")] if payload.get("foodName") else [])
            key = ("food", "food")
            fields = {"foods": frozenset(names), "notes": _norm(payload.get("notes"))}
        elif planned.sprout_type == "sleep":
            key = ("sleep", "sleep")
            fields = {
                "duration_min": payload.get("duration"),
                "type": payload.get("sleepType"),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "diaper":
            key = ("diaper", "diaper")
            fields = {
                "diaper_type": payload.get("diaperType"),
                "condition": payload.get("condition"),
                "color": payload.get("color"),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "play":
            key = ("play", "play")
            fields = {
                "play_type": payload.get("playType"),
                "duration_min": payload.get("duration"),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "bath":
            key = ("bath", "bath")
            fields = {"bath_type": payload.get("bathType"), "notes": _norm(payload.get("notes"))}
        elif planned.sprout_type == "measurement":
            key = ("measurement", planned.kind)
            fields = {
                "value": _norm(payload.get("value")),
                "unit": _norm(payload.get("unit")),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type == "pump":
            key = ("pump", "pump")
            fields = {
                "total": _norm(payload.get("totalAmount")),
                "left": _norm(payload.get("leftAmount")),
                "right": _norm(payload.get("rightAmount")),
                "unit": _norm(payload.get("unitAbbr")),
                "duration_min": payload.get("duration"),
                "notes": _norm(payload.get("notes")),
            }
        elif planned.sprout_type in {"medicine", "supplement"}:
            key = ("medicine", "medicine")
            fields = {
                "name": _norm(payload.get("medicineName") or payload.get("supplementName")),
                "amount": _norm(payload.get("amount")),
                "unit": _norm(payload.get("unitAbbr")),
                "notes": _norm(payload.get("notes")),
            }
        if key is not None:
            out.append((Event(key=key, time=planned.time, fields=fields, origin="hb"), planned))
    return out


def plan_hb_events(
    records: list[HbRecord], cfg: Config, *, resolved_unit: str | None, medicines: dict[str, dict] | None = None
) -> list[Event]:
    return [
        event for event, _planned in plan_hb_event_pairs(records, cfg, resolved_unit=resolved_unit, medicines=medicines)
    ]


def diff_fields(sprout_fields: dict, hb_fields: dict) -> list[str]:
    out: list[str] = []
    for name in sorted(set(sprout_fields) | set(hb_fields)):
        left, right = sprout_fields.get(name), hb_fields.get(name)
        if left != right:
            out.append(f"{name}: sprout={left!r} hb={right!r}")
    return out


def compare_events(hb: list[Event], sprout: list[Event]) -> Comparison:
    result = Comparison(hb_total=len(hb), sprout_total=len(sprout))
    by_key: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in sprout:
        by_key[event.key].append(event)
    for pool in by_key.values():
        pool.sort(key=lambda item: item.time)

    used: set[int] = set()
    for planned in sorted(hb, key=lambda item: item.time):
        candidates = [c for c in by_key.get(planned.key, []) if id(c) not in used]
        exact = [c for c in candidates if abs((c.time - planned.time).total_seconds()) <= EXACT_WINDOW.total_seconds()]
        if exact:
            # Several same-instant candidates: pair the closest by content, not order.
            best = min(
                exact,
                key=lambda c: (
                    len(diff_fields(c.fields, planned.fields)),
                    abs((c.time - planned.time).total_seconds()),
                ),
            )
            used.add(id(best))
            diffs = diff_fields(best.fields, planned.fields)
            if diffs:
                result.drift.append((planned, best, diffs))
            else:
                result.matched.append((planned, best))
            continue
        near = next((c for c in candidates if abs(c.time - planned.time) <= MOVE_WINDOW), None)
        if near is not None:
            used.add(id(near))
            result.moved.append((planned, near))
            continue
        result.truly_missing.append(planned)

    result.sprout_only = [event for event in sprout if id(event) not in used]
    result.near_dup = [
        planned
        for planned in result.truly_missing
        if any(abs(event.time - planned.time) <= DUP_WINDOW for event in by_key.get(planned.key, []))
    ]
    result.truly_missing = [planned for planned in result.truly_missing if planned not in result.near_dup]
    return result


def format_report(result: Comparison, tz: ZoneInfo, start: date, end: date) -> str:
    lines = [f"=== comparison {start}..{end} ({tz.key}) ==="]
    lines.append(
        f"HB planned={result.hb_total} sprout={result.sprout_total} matched={len(result.matched)} "
        f"drift={len(result.drift)} moved={len(result.moved)} "
        f"near_dup={len(result.near_dup)} truly_missing={len(result.truly_missing)} sprout_only={len(result.sprout_only)}"
    )
    drift_by_type: dict[str, int] = defaultdict(int)
    for planned, _sprout, _diffs in result.drift:
        drift_by_type[planned.ident()] += 1
    lines.append("drift by type: " + ", ".join(f"{key}={value}" for key, value in sorted(drift_by_type.items())))

    def by_day(events: list[Event]) -> str:
        counts: dict[str, int] = defaultdict(int)
        for event in events:
            counts[f"{event.time.astimezone(tz):%Y-%m-%d}"] += 1
        return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))

    lines.append("truly_missing by day: " + by_day(result.truly_missing))
    lines.append("sprout_only by day: " + by_day(result.sprout_only))
    lines.append("")

    lines.append(f"===== MOVED (time edited in HB?): {len(result.moved)} =====")
    for planned, sprout in sorted(result.moved, key=lambda pair: pair[0].time):
        lines.append(
            f"  {planned.ident():18} sprout={sprout.time.astimezone(tz):%Y-%m-%d %H:%M:%S}  "
            f"hb={planned.time.astimezone(tz):%Y-%m-%d %H:%M:%S}"
        )
    lines.append("")

    lines.append(f"===== DRIFT (field edited in HB?): {len(result.drift)} =====")
    for planned, _sprout, diffs in sorted(result.drift, key=lambda row: row[0].time):
        lines.append(f"  {planned.time.astimezone(tz):%Y-%m-%d %H:%M:%S} {planned.ident():16} | " + "; ".join(diffs))
    lines.append("")

    for title, events in (
        ("NEAR-DUP (likely HB source duplicate)", result.near_dup),
        ("TRULY-MISSING (absent in Sprout)", result.truly_missing),
        ("SPROUT-ONLY (extra in Sprout)", result.sprout_only),
    ):
        lines.append(f"===== {title}: {len(events)} =====")
        for event in sorted(events, key=lambda item: item.time):
            lines.append(f"  {event.time.astimezone(tz):%Y-%m-%d %H:%M:%S} {event.ident():16} {event.fields}")
        lines.append("")
    return "\n".join(lines)


async def collect(reader: HuckleberryReader, cfg: Config, db_path: str, start: date, end: date) -> Comparison:
    units, medicines = load_sprout_reference(db_path)
    unit = resolve_target_unit(cfg, units)
    records = await reader.read_range(start, end)
    hb = plan_hb_events(records, cfg, resolved_unit=unit, medicines=medicines)
    sprout = load_sprout_events(db_path)
    return compare_events(hb, sprout)
