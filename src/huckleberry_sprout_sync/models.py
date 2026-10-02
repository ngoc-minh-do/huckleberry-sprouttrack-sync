from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

HuckleberryKind = Literal["bottle", "solids", "breast", "sleep", "diaper", "activity", "bath", "temperature"]

SproutType = Literal["feed", "diaper", "sleep", "play", "bath", "note", "measurement"]


@dataclass(frozen=True)
class HbRecord:
    """One normalized Huckleberry history entry read from Firestore."""

    kind: HuckleberryKind
    start: datetime
    end: datetime | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedEvent:
    """A Sprout Track webhook write plus the metadata needed for dedup."""

    sprout_type: SproutType
    kind: str
    time: datetime
    payload: dict
    summary: str = ""
