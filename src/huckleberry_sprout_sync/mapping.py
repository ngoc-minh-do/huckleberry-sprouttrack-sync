from __future__ import annotations

import logging
from datetime import datetime, time

from .config import Config
from .models import HbRecord, PlannedEvent

_LOGGER = logging.getLogger(__name__)

OZ_TO_ML = 29.5735

# Huckleberry bottleType -> Sprout Track feedType (case-insensitive there too).
BOTTLE_TYPE_TO_FEED_TYPE: dict[str, str] = {
    "Breast Milk": "breast milk",
    "Formula": "formula",
    "Milk": "milk",
    "Cow Milk": "milk",
    "Soy Milk": "other",
    "Goat Milk": "other",
    "Tube Feeding": "other",
    "Other": "other",
}

DIAPER_MODE_TO_TYPE: dict[str, str] = {
    "pee": "WET",
    "poo": "DIRTY",
    "both": "BOTH",
    "dry": "DRY",
}

# Huckleberry poo consistency -> Sprout Track diaper.condition.
CONSISTENCY_TO_CONDITION: dict[str, str] = {
    "loose": "LOOSE",
    "runny": "LOOSE",
    "mucousy": "LOOSE",
    "diarrhea": "LOOSE",
    "solid": "FIRM",
    "hard": "FIRM",
    "pebbles": "FIRM",
}

# Huckleberry poo color -> Sprout Track diaper.color.
COLOR_TO_DIAPER_COLOR: dict[str, str] = {
    "yellow": "YELLOW",
    "brown": "BROWN",
    "black": "BLACK",
    "green": "GREEN",
    "red": "RED",
    "gray": "OTHER",
}

# Huckleberry activity mode -> (Sprout type, playType or None).
ACTIVITY_MODE_MAP: dict[str, tuple[str, str | None]] = {
    "bath": ("bath", None),
    "tummyTime": ("play", "TUMMY_TIME"),
    "indoorPlay": ("play", "INDOOR_PLAY"),
    "outdoorPlay": ("play", "OUTDOOR_PLAY"),
    "walk": ("play", "WALK"),
    "storyTime": ("play", "CUSTOM"),
    "screenTime": ("play", "CUSTOM"),
    "skinToSkin": ("play", "CUSTOM"),
    "brushTeeth": ("play", "CUSTOM"),
}

ACTIVITY_LABEL: dict[str, str] = {
    "storyTime": "Story time",
    "screenTime": "Screen time",
    "skinToSkin": "Skin to skin",
    "brushTeeth": "Brushed teeth",
}

FEED_TYPE_DETAIL: dict[str, str] = {
    "bottle": "BOTTLE",
    "solids": "SOLIDS",
    "breast": "BREAST",
}


def resolve_target_unit(cfg: Config, available_units: list[str] | None) -> str | None:
    """Pick the Sprout Track unitAbbr to send volumes in.

    ``available_units`` is the unitAbbr list from GET /reference?type=units.
    Returns None when no unit is needed or can be resolved.
    """
    if not available_units:
        available_units = ["ML", "OZ"]
    normalized = [unit.upper() for unit in available_units]
    if cfg.sprout_unit:
        if cfg.sprout_unit in normalized:
            return cfg.sprout_unit
        _LOGGER.warning(
            "SPROUT_UNIT=%s is not configured for the Sprout Track family (have %s); falling back",
            cfg.sprout_unit,
            ", ".join(normalized),
        )
    return "ML" if "ML" in normalized else (normalized[0] if normalized else None)


def convert_amount(amount, from_unit: str | None, to_unit: str | None) -> float | int | None:
    if amount is None:
        return None
    if to_unit is None or from_unit is None:
        return amount
    if from_unit.strip().lower() == to_unit.lower():
        value = amount
    elif from_unit.strip().lower() == "ml":
        value = amount / OZ_TO_ML
    elif from_unit.strip().lower() == "oz":
        value = amount * OZ_TO_ML
    else:
        return amount
    if to_unit == "ML":
        return int(round(value))
    return round(value, 1)


def plan_events(
    records: list[HbRecord],
    cfg: Config,
    *,
    resolved_unit: str | None,
) -> list[PlannedEvent]:
    events: list[PlannedEvent] = []
    for record in records:
        event = _plan_one(record, cfg, resolved_unit)
        if event is not None:
            events.append(event)
    events.sort(key=lambda event: (event.time, event.sprout_type))
    return events


def _plan_one(record: HbRecord, cfg: Config, resolved_unit: str | None) -> PlannedEvent | None:
    kind = record.kind
    if kind == "bottle":
        return _plan_bottle(record, cfg, resolved_unit)
    if kind == "solids":
        return _plan_solids(record, cfg)
    if kind == "breast":
        return _plan_breast(record, cfg)
    if kind == "sleep":
        return _plan_sleep(record, cfg)
    if kind == "diaper":
        return _plan_diaper(record, cfg)
    if kind == "bath":
        return _plan_bath(record, cfg)
    if kind == "activity":
        return _plan_play(record, cfg)
    if kind == "temperature":
        return _plan_temperature(record, cfg)
    _LOGGER.info("Ignoring unknown Huckleberry record kind %s at %s", kind, record.start)
    return None


def _summary(text: str) -> str:
    return text.replace("\n", " ").strip() if text else ""


def _plan_bottle(record: HbRecord, cfg: Config, resolved_unit: str | None) -> PlannedEvent | None:
    if not cfg.sync_feed:
        return None
    feed_type = BOTTLE_TYPE_TO_FEED_TYPE.get(record.payload.get("bottle_type") or "", "other")
    payload: dict = {
        "type": "feed",
        "feedType": feed_type,
        "time": record.start.isoformat(),
    }
    amount = convert_amount(record.payload.get("amount"), record.payload.get("units"), resolved_unit)
    if amount is not None:
        payload["amount"] = amount
        payload["unitAbbr"] = resolved_unit
    notes = _summary(record.payload.get("notes"))
    if notes:
        payload["notes"] = notes
    amount_label = "" if amount is None else f" {amount}{resolved_unit or ''}"
    return PlannedEvent(
        sprout_type="feed",
        kind="bottle",
        time=record.start,
        payload=payload,
        summary=f"{feed_type} bottle{amount_label}",
    )


def _plan_solids(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_feed:
        return None
    foods = record.payload.get("foods") or []
    payload: dict = {
        "type": "feed",
        "feedType": "SOLIDS",
        "time": record.start.isoformat(),
    }
    if foods:
        payload["food"] = ", ".join(foods)
    notes = _summary(record.payload.get("notes"))
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="feed",
        kind="solids",
        time=record.start,
        payload=payload,
        summary=f"solids: {', '.join(foods[:3])}{' …' if len(foods) > 3 else ''}",
    )


def _plan_breast(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_feed:
        return None
    duration_sec = float(record.payload.get("duration_seconds") or 0.0)
    duration_min = max(1, int(round(duration_sec / 60)))
    payload: dict = {
        "type": "feed",
        "feedType": "BREAST",
        "time": record.start.isoformat(),
        "duration": duration_min,
    }
    side = record.payload.get("side")
    if side in {"left", "right"}:
        payload["side"] = side.upper()
    notes = _summary(record.payload.get("notes"))
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="feed",
        kind="breast",
        time=record.start,
        payload=payload,
        summary=f"breast {duration_min}min",
    )


def _plan_sleep(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_sleep or record.end is None:
        return None
    duration_min = max(1, int(round((record.end - record.start).total_seconds() / 60)))
    sleep_type = "NIGHT_SLEEP" if record.start.time() >= time(cfg.night_start_hour) else "NAP"
    notes = _summary(record.payload.get("notes"))
    payload: dict = {
        "type": "sleep",
        "sleepType": sleep_type,
        "action": "log",
        "time": record.start.isoformat(),
        "duration": duration_min,
    }
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="sleep",
        kind="sleep",
        time=record.start,
        payload=payload,
        summary=f"{sleep_type} ~{record.end.strftime('%H:%M')} ({duration_min}min)",
    )


def _plan_diaper(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_diaper:
        return None
    diaper_type = DIAPER_MODE_TO_TYPE.get(record.payload.get("mode") or "")
    if diaper_type is None:
        return None
    payload: dict = {
        "type": "diaper",
        "diaperType": diaper_type,
        "time": record.start.isoformat(),
    }
    consistency = record.payload.get("consistency")
    condition = CONSISTENCY_TO_CONDITION.get(consistency)
    if condition:
        payload["condition"] = condition
    color = COLOR_TO_DIAPER_COLOR.get(record.payload.get("color"))
    if color:
        payload["color"] = color
    notes = _summary(record.payload.get("notes"))
    if record.payload.get("rash"):
        notes = (notes + "; rash") if notes else "rash"
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="diaper",
        kind="diaper",
        time=record.start,
        payload=payload,
        summary=f"{diaper_type}" + (f" {condition}" if condition else ""),
    )


def _plan_bath(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_activity:
        return None
    payload: dict = {
        "type": "bath",
        "bathType": "Full Bath",
        "time": record.start.isoformat(),
    }
    notes = _summary(record.payload.get("notes"))
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="bath",
        kind="bath",
        time=record.start,
        payload=payload,
        summary="bath",
    )


def _plan_play(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_activity:
        return None
    mode = record.payload.get("mode") or ""
    sprout_type, play_type = ACTIVITY_MODE_MAP.get(mode, ("play", "CUSTOM"))
    notes = _summary(record.payload.get("notes"))
    label = ACTIVITY_LABEL.get(mode, mode)
    payload: dict = {
        "type": sprout_type,
        "time": record.start.isoformat(),
    }
    if play_type:
        payload["playType"] = play_type
    raw_notes = notes
    if mode in ACTIVITY_LABEL:
        raw_notes = f"{ACTIVITY_LABEL[mode]}" if not notes else f"{ACTIVITY_LABEL[mode]}: {notes}"
    if raw_notes:
        payload["notes"] = raw_notes
    duration_min = _duration_minutes(record)
    if duration_min:
        payload["duration"] = duration_min
    return PlannedEvent(
        sprout_type=sprout_type,
        kind="play",
        time=record.start,
        payload=payload,
        summary=f"{label}{f' ({duration_min}min)' if duration_min else ''}",
    )


def _duration_minutes(record: HbRecord) -> int | None:
    if record.end is not None:
        seconds = (record.end - record.start).total_seconds()
    else:
        seconds = record.payload.get("duration_seconds")
    if not seconds:
        return None
    return max(1, int(round(float(seconds) / 60)))


def _plan_temperature(record: HbRecord, cfg: Config) -> PlannedEvent | None:
    if not cfg.sync_temperature:
        return None
    amount = record.payload.get("amount")
    if amount is None:
        return None
    units = record.payload.get("units") or "C"
    payload: dict = {
        "type": "measurement",
        "measurementType": "TEMPERATURE",
        "time": record.start.isoformat(),
        "value": amount,
        "unit": units,
    }
    notes = _summary(record.payload.get("notes"))
    if notes:
        payload["notes"] = notes
    return PlannedEvent(
        sprout_type="measurement",
        kind="measurement",
        time=record.start,
        payload=payload,
        summary=f"temp {amount}{units}",
    )


def planned_key(event: PlannedEvent) -> tuple[str, str]:
    """Dedupe identity: (Sprout activity type[, subtype]) so bottles and solids
    at the same time still both sync."""
    if event.sprout_type == "feed":
        return ("feed", FEED_TYPE_DETAIL.get(event.kind, "feed"))
    return (event.sprout_type, event.sprout_type)


def existing_key(activity: dict) -> tuple[str, str]:
    """Dedupe identity for an activity already in Sprout Track."""
    activity_type = activity.get("activityType")
    details = activity.get("details") or {}
    if activity_type == "feed":
        return ("feed", details.get("type") or "feed")
    return (activity_type or "", activity_type or "")


def activity_start(activity: dict) -> datetime | None:
    """Best-guess start time for a stored Sprout Track activity."""
    details = activity.get("details") or {}
    raw = details.get("startTime") or activity.get("time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
