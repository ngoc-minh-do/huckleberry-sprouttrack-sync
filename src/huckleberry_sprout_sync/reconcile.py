"""Compare-driven gap reconciliation against a Sprout Track DB snapshot.

Like :mod:`compare`, `reconcile` diffs Huckleberry history against a Sprout
Track DB snapshot (a copy of the instance's ``baby-tracker.db``) so that only
events truly missing from Sprout are written. Unlike `backfill`, it never
trusts the live ``GET /activities`` dedup window (which cannot enumerate old
history — Sprout Track #290 — so re-running a covered range duplicates rows).
The snapshot is the source of truth for both what is already synced and, via
PUT, the row ids of existing events.

Read-only by default (dry run, honouring ``DRY_RUN``). With ``--no-dry-run``:

* every ``truly_missing`` event is POSTed — the only safe way to fill a gap in
  a partially-synced day,
* ``--update-drift`` PUTs rows whose fields differ from Huckleberry (fixes
  in-place edits; a drifted food-name alias such as the intentional
  porridge/rice-porridge rename is left alone unless you opt in),
* ``--update-moved`` PUTs rows whose time shifted.

``near_dup`` and ``sprout_only`` are reported but never written.

Always run against a fresh snapshot taken right before the run: after writes
the snapshot is stale (it has no record of the new rows), so re-using it would
re-POST them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .compare import SproutRow, compare_events, load_sprout_reference, load_sprout_rows, plan_hb_event_pairs
from .config import Config
from .hb_reader import HuckleberryReader
from .mapping import resolve_target_unit
from .models import HbRecord, PlannedEvent
from .sprout import SproutClient, SproutError

_LOGGER = logging.getLogger(__name__)

# compare Event.fields name -> webhook payload key for the drift PUT, keyed by
# the compare key's second component (the subtype that distinguishes BOTTLE vs
# BREAST feeds). Subtypes handled separately: food, measurement, medicine.
_FIELD_TO_PAYLOAD: dict[tuple[str, str], dict[str, str]] = {
    ("feed", "BOTTLE"): {"bottle_type": "bottleType", "amount": "amount", "unit": "unitAbbr", "notes": "notes"},
    ("feed", "BREAST"): {"side": "side", "duration_min": "duration", "notes": "notes"},
    ("sleep", "sleep"): {"duration_min": "duration", "type": "sleepType", "notes": "notes"},
    ("diaper", "diaper"): {"diaper_type": "diaperType", "condition": "condition", "color": "color", "notes": "notes"},
    ("play", "play"): {"play_type": "playType", "duration_min": "duration", "notes": "notes"},
    ("bath", "bath"): {"bath_type": "bathType", "notes": "notes"},
    ("pump", "pump"): {
        "total": "totalAmount",
        "left": "leftAmount",
        "right": "rightAmount",
        "unit": "unitAbbr",
        "duration_min": "duration",
    },
}


@dataclass
class ReconcilePlan:
    """Classification of one compare, ready to apply."""

    hb_total: int
    sprout_total: int
    matched: int
    near_dup: int
    sprout_only: int
    to_post: list[PlannedEvent] = field(default_factory=list)
    drift: list[tuple[PlannedEvent, SproutRow, list[str]]] = field(default_factory=list)
    moved: list[tuple[PlannedEvent, SproutRow]] = field(default_factory=list)


@dataclass
class ReconcileResult:
    start: date
    end: date
    dry_run: bool
    snapshot_db: str
    hb_total: int
    sprout_total: int
    matched: int
    drift: int
    moved: int
    near_dup: int
    truly_missing: int
    sprout_only: int
    update_drift: bool
    update_moved: bool
    written: int = 0
    updated: int = 0
    written_by_type: dict[str, int] = field(default_factory=dict)
    updated_by_type: dict[str, int] = field(default_factory=dict)
    to_post: list[PlannedEvent] = field(default_factory=list)


def plan_reconcile(
    records: list[HbRecord],
    rows: list[SproutRow],
    cfg: Config,
    *,
    resolved_unit: str | None,
    medicines: dict[str, dict] | None = None,
) -> ReconcilePlan:
    """Classify the gap: which planned events are missing from the snapshot."""
    hb_pairs = plan_hb_event_pairs(records, cfg, resolved_unit=resolved_unit, medicines=medicines)
    planned_by_event = {id(event): planned for event, planned in hb_pairs}
    row_by_event = {id(row.event): row for row in rows}
    comparison = compare_events([event for event, _planned in hb_pairs], [row.event for row in rows])
    return ReconcilePlan(
        hb_total=comparison.hb_total,
        sprout_total=comparison.sprout_total,
        matched=len(comparison.matched),
        near_dup=len(comparison.near_dup),
        sprout_only=len(comparison.sprout_only),
        to_post=[planned_by_event[id(event)] for event in comparison.truly_missing],
        drift=[
            (planned_by_event[id(planned)], row_by_event[id(sprout)], diffs)
            for planned, sprout, diffs in comparison.drift
        ],
        moved=[(planned_by_event[id(planned)], row_by_event[id(sprout)]) for planned, sprout in comparison.moved],
    )


def _field_mapping(planned: PlannedEvent) -> dict[str, str] | None:
    st = planned.sprout_type
    if st == "feed":
        return _FIELD_TO_PAYLOAD[("feed", "BREAST" if planned.kind == "breast" else "BOTTLE")]
    if st == "food":
        return {"foods": "foods", "notes": "notes"}
    if st == "measurement":
        return {"value": "value", "unit": "unit"}
    if st in {"medicine", "supplement"}:
        name_key = "medicineName" if st == "medicine" else "supplementName"
        return {"name": name_key, "amount": "amount", "unit": "unitAbbr", "notes": "notes"}
    return _FIELD_TO_PAYLOAD.get((st, st))


def put_body(planned: PlannedEvent, fields: list[str]) -> dict:
    """PUT body fixing exactly the differing fields, from the planned payload.

    Only fields that map to the webhook payload and are present there are sent;
    an empty result means nothing mapped and the row should be left alone.
    """
    mapping = _field_mapping(planned)
    if mapping is None:
        return {}
    body: dict = {"type": planned.sprout_type}
    for diff in fields:
        # compare diff_fields formats each entry as "name: sprout=... hb=..."
        name = diff.split(": ", 1)[0]
        key = mapping.get(name)
        if key is None or key not in planned.payload:
            continue
        body[key] = planned.payload[key]
    return body


def moved_body(planned: PlannedEvent) -> dict:
    """PUT body moving a row to the planned start time (duration kept for sleep)."""
    body: dict = {"type": planned.sprout_type}
    if planned.sprout_type == "sleep" and planned.payload.get("duration") is not None:
        body["duration"] = planned.payload["duration"]
    time_value = planned.payload.get("time") or planned.payload.get("startTime")
    if time_value is not None:
        body["time"] = time_value
    return body


class SproutWriteClient(Protocol):
    """The slice of :class:`SproutClient` a reconcile writes through."""

    async def resolve_baby_id(self) -> str: ...

    async def warm_up(self, *, timeout: float = 120.0, interval: float = 2.0) -> bool: ...

    async def post_activity(self, baby_id: str, payload: dict) -> dict: ...

    async def update_activity(self, baby_id: str, activity_id: str, payload: dict) -> dict: ...


async def apply_plan(
    sprout: SproutWriteClient,
    cfg: Config,
    db_path: str,
    start: date,
    end: date,
    plan: ReconcilePlan,
    *,
    dry_run: bool,
    update_drift: bool = False,
    update_moved: bool = False,
) -> ReconcileResult:
    result = ReconcileResult(
        start=start,
        end=end,
        dry_run=dry_run,
        snapshot_db=db_path,
        hb_total=plan.hb_total,
        sprout_total=plan.sprout_total,
        matched=plan.matched,
        drift=len(plan.drift),
        moved=len(plan.moved),
        near_dup=plan.near_dup,
        truly_missing=len(plan.to_post),
        sprout_only=plan.sprout_only,
        update_drift=update_drift,
        update_moved=update_moved,
        to_post=list(plan.to_post),
    )
    want_put = (update_drift and plan.drift) or (update_moved and plan.moved)
    if dry_run or not (plan.to_post or want_put):
        return result

    baby_id = await sprout.resolve_baby_id()
    if not await sprout.warm_up():
        raise SproutError("Sprout Track did not come up in time; aborting before any writes")

    for event in plan.to_post:
        when = event.time.astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M")
        _LOGGER.info("POST %s at %s %s", event.sprout_type, when, event.payload)
        await sprout.post_activity(baby_id, event.payload)
        result.written += 1
        result.written_by_type[event.sprout_type] = result.written_by_type.get(event.sprout_type, 0) + 1

    if update_drift:
        for planned, row, diffs in plan.drift:
            body = put_body(planned, diffs)
            if len(body) <= 1:
                when = planned.time.astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M")
                _LOGGER.info("SKIP drift PUT %s at %s: no mapped fields differ (%s)", row.activity_type, when, diffs)
                continue
            when = planned.time.astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M")
            _LOGGER.info("PUT %s %s at %s %s", row.activity_type, row.id, when, body)
            await sprout.update_activity(baby_id, row.id, body)
            result.updated += 1
            result.updated_by_type[planned.sprout_type] = result.updated_by_type.get(planned.sprout_type, 0) + 1

    if update_moved:
        for planned, row in plan.moved:
            body = moved_body(planned)
            when = planned.time.astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M")
            _LOGGER.info("PUT %s %s at %s %s", row.activity_type, row.id, when, body)
            await sprout.update_activity(baby_id, row.id, body)
            result.updated += 1
            result.updated_by_type[planned.sprout_type] = result.updated_by_type.get(planned.sprout_type, 0) + 1

    return result


async def reconcile(
    cfg: Config,
    db_path: str,
    start: date,
    end: date,
    *,
    dry_run: bool | None = None,
    update_drift: bool = False,
    update_moved: bool = False,
    child: str | None = None,
) -> ReconcileResult:
    """Reconcile a Huckleberry range against a Sprout Track DB snapshot."""
    effective_dry_run = cfg.dry_run if dry_run is None else dry_run
    child = child or cfg.child
    reader = HuckleberryReader(cfg)
    sprout = SproutClient(cfg)
    await reader.connect(child=child)
    try:
        units, medicines = load_sprout_reference(db_path)
        unit = resolve_target_unit(cfg, units)
        records = await reader.read_range(start, end)
        rows = load_sprout_rows(db_path)
        plan = plan_reconcile(records, rows, cfg, resolved_unit=unit, medicines=medicines)
        return await apply_plan(
            sprout,
            cfg,
            db_path,
            start,
            end,
            plan,
            dry_run=effective_dry_run,
            update_drift=update_drift,
            update_moved=update_moved,
        )
    finally:
        await reader.close()
        await sprout.close()
