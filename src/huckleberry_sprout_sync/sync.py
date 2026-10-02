from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import Config
from .hb_reader import HuckleberryReader
from .mapping import (
    activity_start,
    existing_key,
    plan_events,
    planned_key,
    resolve_target_unit,
)
from .models import PlannedEvent
from .sprout import SproutClient
from .state import SyncState

_LOGGER = logging.getLogger(__name__)

_WRITE_TYPES = ("feed", "sleep", "diaper", "play", "bath", "measurement", "pump", "medicine", "supplement")


@dataclass
class ApplyResult:
    written: int
    written_by_type: dict[str, int]
    skipped_by_type: dict[str, int]


@dataclass
class SyncResult:
    day: date
    dry_run: bool
    state: str = "synced"
    records: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    planned: int = 0
    written: int = 0
    written_by_type: dict[str, int] = field(default_factory=dict)
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    events: list[PlannedEvent] = field(default_factory=list)


@dataclass
class BackfillResult:
    start: date
    end: date
    dry_run: bool
    days: int = 0
    days_skipped: int = 0
    days_failed: int = 0
    records: int = 0
    planned: int = 0
    written: int = 0
    written_by_type: dict[str, int] = field(default_factory=dict)


async def sync_day(
    cfg: Config,
    target: date | None,
    *,
    force: bool,
    dry_run: bool | None = None,
    child: str | None = None,
) -> SyncResult:
    effective_dry_run = cfg.dry_run if dry_run is None else dry_run
    child = child or cfg.child
    day = target or date.today()
    state = SyncState(cfg.state_path)

    if state.is_synced(day) and not force:
        _LOGGER.info(
            "Day %s already synced (%s events) - skipping. Use --force to re-sync.", day, state.synced_event_count(day)
        )
        return SyncResult(day=day, dry_run=effective_dry_run, state="already-synced")

    reader = HuckleberryReader(cfg)
    sprout = SproutClient(cfg)
    await reader.connect(child=child)
    try:
        baby_id = await sprout.resolve_baby_id()
        unit = _resolve_unit(cfg, sprout, baby_id)
        medicines = await _resolve_medicines(cfg, sprout, baby_id)

        records = await reader.read_day(day)
        events = plan_events(records, cfg, resolved_unit=unit, medicines=medicines)
        counts = _kind_counts(records)
        result = SyncResult(
            day=day,
            dry_run=effective_dry_run,
            state="parsed",
            records=len(records),
            by_kind=counts,
            planned=len(events),
            events=events,
        )
        _LOGGER.info(
            "Read %s: %s -> %d Sprout Track events (dry_run=%s)",
            day,
            _format_counts(counts),
            result.planned,
            effective_dry_run,
        )
        if not events:
            result.state = "no-events"
            return result

        existing = await _collect_existing(
            sprout,
            baby_id,
            {event.sprout_type for event in events},
            cfg,
            earliest=day - timedelta(days=cfg.dedup_since_days),
        )
        applied = await _apply(sprout, baby_id, events, existing, cfg, dry_run=effective_dry_run)
        result.written = applied.written
        result.written_by_type = applied.written_by_type
        result.skipped_by_type = applied.skipped_by_type

        if not effective_dry_run:
            state.mark_synced(day, applied.written)
            _LOGGER.info("Marked %s as synced", day)
        return result
    finally:
        await reader.close()
        await sprout.close()


async def backfill(
    cfg: Config,
    start: date,
    end: date | None,
    *,
    force: bool,
    dry_run: bool | None = None,
    child: str | None = None,
) -> BackfillResult:
    effective_dry_run = cfg.dry_run if dry_run is None else dry_run
    child = child or cfg.child
    day_end = end or date.today()
    state = SyncState(cfg.state_path)
    if start > day_end:
        raise ValueError("backfill start must not be after end")

    reader = HuckleberryReader(cfg)
    sprout = SproutClient(cfg)
    await reader.connect(child=child)
    result = BackfillResult(start=start, end=day_end, dry_run=effective_dry_run)
    try:
        baby_id = await sprout.resolve_baby_id()
        unit = _resolve_unit(cfg, sprout, baby_id)
        medicines = await _resolve_medicines(cfg, sprout, baby_id)

        records = await reader.read_range(start, day_end)
        result.records = len(records)
        by_day: dict[date, list] = defaultdict(list)
        for record in records:
            by_day[record.start.date()].append(record)
        days = sorted(by_day)
        result.days = len(days)

        existing = await _collect_existing(
            sprout,
            baby_id,
            set(_WRITE_TYPES),
            cfg,
            earliest=start - timedelta(days=cfg.dedup_since_days),
        )
        for date_day in days:
            if state.is_synced(date_day) and not force:
                _LOGGER.info("Skip %s (already synced)", date_day)
                result.days_skipped += 1
                continue
            per_day_events = plan_events(by_day[date_day], cfg, resolved_unit=unit, medicines=medicines)
            if not per_day_events:
                continue
            try:
                applied = await _apply(
                    sprout, baby_id, per_day_events, existing, date_day, cfg, dry_run=effective_dry_run
                )
                result.planned += len(per_day_events)
                result.written += applied.written
                for kind, count in applied.written_by_type.items():
                    result.written_by_type[kind] += count
                _LOGGER.info(
                    "--- %s: %d records -> %d selected, %d written",
                    date_day,
                    len(by_day[date_day]),
                    len(per_day_events),
                    applied.written,
                )
                if not effective_dry_run and (applied.written or applied.skipped_by_type):
                    state.mark_synced(date_day, applied.written)
            except Exception:
                result.days_failed += 1
                _LOGGER.exception("Backfill failed for %s", date_day)
        _LOGGER.info(
            "Backfill complete: wrote %d events across %d days (%d skipped, %d failed)",
            result.written,
            result.days - result.days_skipped - result.days_failed,
            result.days_skipped,
            result.days_failed,
        )
        return result
    finally:
        await reader.close()
        await sprout.close()


async def _resolve_medicines(cfg: Config, sprout: SproutClient, baby_id: str) -> dict[str, dict]:
    """Name lookup (lowercased) -> {"name", "isSupplement"} for the family's
    medicines and supplements, from GET /reference?type=medicines."""
    try:
        reference = await sprout.get_reference(baby_id, "medicines")
    except Exception as exc:
        _LOGGER.warning("Could not fetch Sprout Track medicine reference: %s", exc)
        return {}
    medicines: dict[str, dict] = {}
    for entry in reference.get("medicines") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        medicines[name.lower()] = {"name": name, "isSupplement": bool(entry.get("isSupplement"))}
    for entry in reference.get("supplements") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        medicines.setdefault(name.lower(), {"name": name, "isSupplement": True})
    _LOGGER.info("Sprout Track medicine/supplement reference: %d entries", len(medicines))
    return medicines


async def _resolve_unit(cfg: Config, sprout: SproutClient, baby_id: str) -> str | None:
    try:
        reference = await sprout.get_reference(baby_id, "units")
        units = [Unit["unitAbbr"] for Unit in reference.get("units") or []]
    except Exception as exc:
        _LOGGER.warning("Could not fetch Sprout Track reference units: %s", exc)
        units = []
    unit = resolve_target_unit(cfg, units)
    _LOGGER.info("Sprout Track volume unit: %s (configured: %s)", unit or "(none)", ", ".join(units) or "(unknown)")
    return unit


async def _collect_existing(
    sprout: SproutClient,
    baby_id: str,
    types: set[str],
    cfg: Config,
    *,
    earliest: date,
) -> dict[tuple[str, str], list[float]]:
    since = datetime.combine(earliest, datetime.min.time(), tzinfo=cfg.timezone)
    existing: dict[tuple[str, str], list[float]] = defaultdict(list)
    for activity_type in sorted(types):
        for activity in await sprout.list_activities(baby_id, activity_type, since):
            key = existing_key(activity)
            start = activity_start(activity)
            if start is None:
                continue
            existing[key].append(start.timestamp())
    return existing


async def _apply(
    sprout: SproutClient,
    baby_id: str,
    events: list[PlannedEvent],
    existing: dict[tuple[str, str], list[float]],
    cfg: Config,
    *,
    dry_run: bool,
) -> ApplyResult:
    window = cfg.dedup_window_minutes * 60
    written = 0
    written_by_type: dict[str, int] = defaultdict(int)
    skipped_by_type: dict[str, int] = defaultdict(int)
    for event in events:
        key = planned_key(event)
        start_ts = event.time.timestamp()
        duplicates = [when for when in existing.get(key, []) if abs(when - start_ts) <= window]
        if duplicates:
            dup_time = datetime.fromtimestamp(min(duplicates), tz=cfg.timezone).strftime("%Y-%m-%d %H:%M")
            _LOGGER.info(
                "SKIP %s at %s (already in Sprout Track at %s)",
                event.sprout_type,
                event.time.strftime("%H:%M"),
                dup_time,
            )
            skipped_by_type[event.sprout_type] += 1
            continue
        _LOGGER.info("PLAN %s at %s %s", event.sprout_type, event.time.strftime("%H:%M"), event.payload)
        if not dry_run:
            await sprout.post_activity(baby_id, event.payload)
            written += 1
            written_by_type[event.sprout_type] += 1
    return ApplyResult(written=written, written_by_type=dict(written_by_type), skipped_by_type=dict(skipped_by_type))


def _kind_counts(records) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record.kind] += 1
    return dict(counts)


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "0 records"
    return " ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
