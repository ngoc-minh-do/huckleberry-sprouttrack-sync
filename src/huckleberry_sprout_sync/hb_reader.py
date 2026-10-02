from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import aiohttp

from .config import Config, ConfigError
from .models import HbRecord

_LOGGER = logging.getLogger(__name__)

# Huckleberry Firestore collections -> (interval collection or document path, entry kind resolver)
_COLLECTIONS: list[tuple[str, str]] = [
    ("feed", "intervals"),
    ("sleep", "intervals"),
    ("activities", "intervals"),
    ("diaper", "intervals"),
    ("health", "data"),
    ("pump", "intervals"),
]


class HuckleberryReader:
    """Reads Huckleberry history straight from Firestore (same transport the app uses).

    Direct access mirrors `huckleberry-api`'s own internals; the library exposes
    write helpers but no general history reader, so we stream the interval
    subcollections and normalize rows into :class:`HbRecord` values.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._api = None
        self._child_uid: str | None = None

    async def connect(self, child: str | None = None) -> None:
        from huckleberry_api import HuckleberryAPI

        self._session = aiohttp.ClientSession()
        self._api = HuckleberryAPI(
            email=self.config.huckleberry_email,
            password=self.config.huckleberry_password,
            timezone=self.config.timezone_name,
            websession=self._session,
        )
        user = await self._api.get_user()
        if not user.childList:
            raise ConfigError("Huckleberry account has no children registered")
        self._child_uid = self._resolve_child_uid(user.childList, child)
        _LOGGER.info("Connected to Huckleberry as %s (child=%s)", user.email, self._child_uid)

    @staticmethod
    def _resolve_child_uid(child_list: list, child: str | None) -> str:
        if not child:
            return child_list[0].cid
        needle = child.strip().lower()
        for ref in child_list:
            if ref.cid == needle:
                return ref.cid
            nickname = (ref.nickname or "").strip().lower()
            if nickname and needle in nickname:
                return ref.cid
        raise ConfigError(f"No Huckleberry child matches {child!r}")

    async def read_day(self, day: date) -> list[HbRecord]:
        return await self.read_range(day, day)

    async def read_range(self, start: date, end: date) -> list[HbRecord]:
        if not self._api or not self._child_uid:
            raise RuntimeError("HuckleberryReader.connect() must be called before read_range()")
        client = await self._api._get_firestore_client()
        child = self._child_uid

        earliest = datetime.combine(start, time(0, 0), tzinfo=self.config.timezone).timestamp() - 24 * 3600
        latest = datetime.combine(end, time(0, 0), tzinfo=self.config.timezone) + timedelta(days=1)
        latest = latest.timestamp() + 24 * 3600

        records: list[HbRecord] = []
        for top, sub in _COLLECTIONS:
            try:
                subref = client.collection(top).document(child).collection(sub)
                async for doc in subref.stream():
                    for entry in self._entries(doc.to_dict() or {}):
                        start_sec = self._as_number(entry.get("start"))
                        if start_sec is None or start_sec < earliest or start_sec > latest:
                            continue
                        record = self._to_record(top, entry, start_sec)
                        if record is not None:
                            records.append(record)
            except Exception as exc:
                _LOGGER.warning("Could not read %s/%s history: %s", top, sub, exc)
        records.sort(key=lambda record: record.start)
        _LOGGER.info("Read %d Huckleberry history rows in %s..%s", len(records), start, end)
        return records

    @staticmethod
    def _entries(raw: dict) -> list[dict]:
        if raw.get("multi") is True:
            data = raw.get("data")
            if isinstance(data, dict):
                return [value for value in data.values() if isinstance(value, dict)]
            return []
        return [raw]

    @staticmethod
    def _as_number(value) -> float | None:
        try:
            number = float(value)
        except TypeError, ValueError:
            return None
        return number if number == number else None  # NaN guard

    def _started(self, start_sec: float, *, duration_sec: float | None) -> tuple[datetime, datetime | None]:
        start = datetime.fromtimestamp(start_sec, tz=self.config.timezone)
        end = start + timedelta(seconds=duration_sec) if duration_sec is not None and duration_sec > 0 else None
        return start, end

    def _to_record(self, top: str, entry: dict, start_sec: float) -> HbRecord | None:
        mode = entry.get("mode")
        if top == "feed":
            if mode == "bottle":
                start, _ = self._started(start_sec, duration_sec=None)
                return HbRecord(
                    kind="bottle",
                    start=start,
                    payload={
                        "bottle_type": entry.get("bottleType"),
                        "amount": self._as_number(entry.get("amount")),
                        "units": entry.get("units", "ml"),
                        "notes": entry.get("notes"),
                    },
                )
            if mode == "solids":
                start, _ = self._started(start_sec, duration_sec=None)
                foods = self._solids_foods(entry.get("foods"))
                return HbRecord(
                    kind="solids",
                    start=start,
                    payload={
                        "foods": foods,
                        "notes": entry.get("notes"),
                    },
                )
            if mode == "breast":
                left = self._as_number(entry.get("leftDuration")) or 0.0
                right = self._as_number(entry.get("rightDuration")) or 0.0
                start, end = self._started(start_sec, duration_sec=None)
                duration_sec = left + right
                if duration_sec > 0:
                    end = start + timedelta(seconds=duration_sec)
                return HbRecord(
                    kind="breast",
                    start=start,
                    end=end,
                    payload={
                        "side": entry.get("lastSide"),
                        "duration_seconds": duration_sec,
                        "notes": entry.get("notes"),
                    },
                )
            return None
        if top == "sleep":
            duration_sec = self._as_number(entry.get("duration"))
            if duration_sec is None or duration_sec <= 0:
                return None
            start, end = self._started(start_sec, duration_sec=duration_sec)
            return HbRecord(kind="sleep", start=start, end=end, payload={"duration_seconds": duration_sec})
        if top == "diaper":
            start, _ = self._started(start_sec, duration_sec=None)
            return HbRecord(
                kind="diaper",
                start=start,
                payload={
                    "mode": mode,
                    "consistency": entry.get("consistency"),
                    "color": entry.get("color"),
                    "rash": bool(entry.get("diaperRash")),
                    "notes": entry.get("notes"),
                },
            )
        if top == "activities":
            if not mode:
                return None
            duration_sec = self._as_number(entry.get("duration"))
            start, end = self._started(start_sec, duration_sec=duration_sec)
            kind = "bath" if mode == "bath" else "activity"
            return HbRecord(
                kind=kind,
                start=start,
                end=end,
                payload={"mode": mode, "duration_seconds": duration_sec, "notes": (entry.get("notes") or "").strip()},
            )
        if top == "health" and mode == "temperature":
            start, _ = self._started(start_sec, duration_sec=None)
            return HbRecord(
                kind="temperature",
                start=start,
                payload={
                    "amount": self._as_number(entry.get("amount")),
                    "units": entry.get("units", "C"),
                    "notes": entry.get("notes"),
                },
            )
        if top == "health" and mode == "growth":
            start, _ = self._started(start_sec, duration_sec=None)
            payload = {
                "weight": self._as_number(entry.get("weight")),
                "weight_units": entry.get("weightUnits"),
                "height": self._as_number(entry.get("height")),
                "height_units": entry.get("heightUnits"),
                "head": self._as_number(entry.get("head")),
                "head_units": entry.get("headUnits"),
            }
            return HbRecord(kind="growth", start=start, payload=payload)
        if top == "health" and mode == "medication":
            start, _ = self._started(start_sec, duration_sec=None)
            return HbRecord(
                kind="medication",
                start=start,
                payload={
                    "name": entry.get("medication_name"),
                    "amount": self._as_number(entry.get("amount")),
                    "units": entry.get("units"),
                    "notes": entry.get("notes"),
                },
            )
        if top == "pump":
            duration_sec = self._as_number(entry.get("duration"))
            start, end = self._started(start_sec, duration_sec=duration_sec)
            return HbRecord(
                kind="pump",
                start=start,
                end=end,
                payload={
                    "entry_mode": entry.get("entryMode", "leftright"),
                    "left_amount": self._as_number(entry.get("leftAmount")),
                    "right_amount": self._as_number(entry.get("rightAmount")),
                    "units": entry.get("units", "ml"),
                    "notes": entry.get("notes"),
                },
            )
        return None

    @staticmethod
    def _solids_foods(foods) -> list[str]:
        if not isinstance(foods, dict):
            return []
        names: list[str] = []
        for value in foods.values():
            if not isinstance(value, dict):
                continue
            name = value.get("created_name") or value.get("name")
            if name:
                names.append(str(name))
        return names

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
