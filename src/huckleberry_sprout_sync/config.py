from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


_DEFAULT_TIMEZONE = "Asia/Tokyo"


@dataclass(frozen=True)
class Config:
    huckleberry_email: str
    huckleberry_password: str
    sprout_base_url: str
    sprout_api_keys: tuple[str, ...]
    sprout_baby_id: str | None
    sprout_unit: str | None
    timezone_name: str
    dry_run: bool
    child: str | None
    sync_feed: bool
    sync_sleep: bool
    sync_diaper: bool
    sync_activity: bool
    sync_temperature: bool
    sync_pump: bool
    sync_growth: bool
    sync_medication: bool
    night_start_hour: int
    dedup_window_minutes: int
    dedup_since_days: int
    write_delay_seconds: float
    apprise_url: str | None

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    @property
    def local_today(self) -> date:
        """Today's calendar date in the configured timezone (host-clock independent)."""
        return datetime.now(self.timezone).date()


def _resolve_timezone_name() -> str:
    raw = os.environ.get("TZ")
    if raw is None:
        return _DEFAULT_TIMEZONE
    name = raw.strip()
    name = name.removeprefix(":")
    if not name:
        return _DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TZ={raw!r} is not a valid IANA timezone name") from exc
    return name


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def load_config(env_path: Path | None = None, *, dry_run: bool | None = None) -> Config:
    load_dotenv(env_path)

    def require(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"Missing required environment variable {name}")
        return value

    huckleberry_email = require("HUCKLEBERRY_EMAIL")
    huckleberry_password = require("HUCKLEBERRY_PASSWORD")

    # One or more API keys (comma/space separated). Each key has its own
    # independent 30 writes/min bucket, so round-robin use multiplies the
    # write throughput by the number of keys.
    sprout_api_keys: list[str] = []
    for raw in os.environ.get("SPROUT_API_KEYS", "").replace(",", " ").split():
        key = raw.strip()
        if key and key not in sprout_api_keys:
            sprout_api_keys.append(key)
    if not sprout_api_keys:
        raise ConfigError("Missing required environment variable SPROUT_API_KEYS")

    if sprout_api_keys[0] == "st_live_your_key_here":
        raise ConfigError("SPROUT_API_KEYS is still the placeholder; set real keys or DRY_RUN stays off")

    if dry_run is None:
        dry_run = _parse_bool(os.environ.get("DRY_RUN"), True)

    sprout_unit = (os.environ.get("SPROUT_UNIT") or "").strip().upper() or None
    return Config(
        huckleberry_email=huckleberry_email,
        huckleberry_password=huckleberry_password,
        sprout_base_url=(os.environ.get("SPROUT_BASE_URL", "https://sprout-track.example.com").strip().rstrip("/")),
        sprout_api_keys=tuple(sprout_api_keys),
        sprout_baby_id=(os.environ.get("SPROUT_BABY_ID") or "").strip() or None,
        sprout_unit=sprout_unit,
        timezone_name=_resolve_timezone_name(),
        dry_run=dry_run,
        child=(os.environ.get("CHILD") or "").strip() or None,
        sync_feed=_parse_bool(os.environ.get("SYNC_FEED"), True),
        sync_sleep=_parse_bool(os.environ.get("SYNC_SLEEP"), True),
        sync_diaper=_parse_bool(os.environ.get("SYNC_DIAPER"), True),
        sync_activity=_parse_bool(os.environ.get("SYNC_ACTIVITY"), True),
        sync_temperature=_parse_bool(os.environ.get("SYNC_TEMPERATURE"), True),
        sync_pump=_parse_bool(os.environ.get("SYNC_PUMP"), True),
        sync_growth=_parse_bool(os.environ.get("SYNC_GROWTH"), True),
        sync_medication=_parse_bool(os.environ.get("SYNC_MEDICATION"), True),
        night_start_hour=_parse_int(os.environ.get("NIGHT_START_HOUR"), 20),
        dedup_window_minutes=_parse_int(os.environ.get("DEDUP_WINDOW_MINUTES"), 15),
        dedup_since_days=_parse_int(os.environ.get("DEDUP_SINCE_DAYS"), 7),
        write_delay_seconds=_parse_float(os.environ.get("WRITE_DELAY_SECONDS"), 2.2),
        apprise_url=(os.environ.get("APPRISE_URL") or "").strip() or None,
    )
