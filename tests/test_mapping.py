from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from huckleberry_sprout_sync.config import Config
from huckleberry_sprout_sync.hb_reader import HuckleberryReader
from huckleberry_sprout_sync.mapping import (
    convert_amount,
    plan_events,
    planned_key,
    resolve_target_unit,
)
from huckleberry_sprout_sync.models import HbRecord

TZ = ZoneInfo("Asia/Tokyo")


def make_config(**overrides) -> Config:
    defaults: dict = {
        "huckleberry_email": "a@b.c",
        "huckleberry_password": "pw",
        "sprout_base_url": "https://sprout-track.ngoclab.com",
        "sprout_api_key": "st_live_test",
        "sprout_baby_id": None,
        "sprout_unit": None,
        "timezone_name": "Asia/Tokyo",
        "dry_run": True,
        "data_dir": Path("data"),
        "child": None,
        "sync_feed": True,
        "sync_sleep": True,
        "sync_diaper": True,
        "sync_activity": True,
        "sync_temperature": True,
        "night_start_hour": 20,
        "dedup_window_minutes": 15,
        "dedup_since_days": 7,
        "write_delay_seconds": 0.0,
        "apprise_url": None,
    }
    defaults.update(overrides)
    return Config(**defaults)


def at(hour: int, minute: int = 0, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def test_resolve_target_unit_prefers_ml():
    cfg = make_config()
    assert resolve_target_unit(cfg, ["OZ", "ML"]) == "ML"
    assert resolve_target_unit(cfg, ["OZ"]) == "OZ"
    assert resolve_target_unit(cfg, None) == "ML"


def test_resolve_target_unit_respects_config():
    cfg = make_config(sprout_unit="OZ")
    assert resolve_target_unit(cfg, ["ML", "OZ"]) == "OZ"


def test_resolve_target_unit_falls_back_on_unconfigured_unit():
    cfg = make_config(sprout_unit="L")
    assert resolve_target_unit(cfg, ["ML", "OZ"]) == "ML"


def test_convert_amount():
    assert convert_amount(160, "ml", "ML") == 160
    assert convert_amount(160, "ml", "OZ") == 5.4
    assert convert_amount(5.4, "oz", "ML") == 160
    assert convert_amount(3, "oz", "OZ") == 3
    assert convert_amount(None, "ml", "ML") is None


def test_plan_bottle():
    cfg = make_config()
    record = HbRecord(kind="bottle", start=at(12, 25), payload={"bottle_type": "Formula", "amount": 160, "units": "ml"})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert len(events) == 1
    event = events[0]
    assert event.payload["time"] == "2026-10-05T12:25:00+09:00"
    assert event.payload["feedType"] == "formula"
    assert event.payload["amount"] == 160
    assert event.payload["unitAbbr"] == "ML"
    assert planned_key(event) == ("feed", "BOTTLE")


def test_plan_solids():
    cfg = make_config()
    record = HbRecord(kind="solids", start=at(11, 0), payload={"foods": ["Rice", "Miso Soup"]})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert events[0].payload["feedType"] == "SOLIDS"
    assert events[0].payload["food"] == "Rice, Miso Soup"
    assert planned_key(events[0]) == ("feed", "SOLIDS")


def test_plan_breast():
    cfg = make_config()
    start = at(9, 0)
    record = HbRecord(
        kind="breast", start=start, end=start + timedelta(minutes=15), payload={"side": "left", "duration_seconds": 900}
    )
    events = plan_events([record], cfg, resolved_unit="ML")
    assert events[0].payload["feedType"] == "BREAST"
    assert events[0].payload["duration"] == 15
    assert events[0].payload["side"] == "LEFT"


def test_plan_sleep_nap_vs_night():
    cfg = make_config()
    nap = HbRecord(kind="sleep", start=at(13, 0), end=at(14, 30), payload={"duration_seconds": 5400})
    night = HbRecord(kind="sleep", start=at(21, 0), end=at(22, 0), payload={"duration_seconds": 3600})
    events = plan_events([nap, night], cfg, resolved_unit="ML")
    by_hour = {event.time.hour: event for event in events}
    assert by_hour[13].payload["sleepType"] == "NAP"
    assert by_hour[13].payload["duration"] == 90
    assert by_hour[21].payload["sleepType"] == "NIGHT_SLEEP"


def test_plan_diaper_mapping():
    cfg = make_config()
    record = HbRecord(
        kind="diaper", start=at(8, 0), payload={"mode": "both", "consistency": "loose", "color": "yellow"}
    )
    events = plan_events([record], cfg, resolved_unit="ML")
    payload = events[0].payload
    assert payload["diaperType"] == "BOTH"
    assert payload["condition"] == "LOOSE"
    assert payload["color"] == "YELLOW"


def test_plan_activity_modes():
    cfg = make_config()
    outdoor = HbRecord(kind="activity", start=at(10, 0), payload={"mode": "outdoorPlay", "duration_seconds": 1800})
    tummy = HbRecord(kind="activity", start=at(15, 0), payload={"mode": "tummyTime"})
    events = plan_events([outdoor, tummy], cfg, resolved_unit="ML")
    by_mode = {event.payload.get("playType"): event for event in events}
    assert by_mode["OUTDOOR_PLAY"].sprout_type == "play"
    assert by_mode["OUTDOOR_PLAY"].payload["duration"] == 30
    assert by_mode["TUMMY_TIME"].sprout_type == "play"


def test_plan_bath():
    cfg = make_config()
    record = HbRecord(kind="bath", start=at(18, 0), payload={"mode": "bath"})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert events[0].sprout_type == "bath"
    assert events[0].payload["bathType"] == "Full Bath"


def test_plan_temperature():
    cfg = make_config()
    record = HbRecord(kind="temperature", start=at(7, 0), payload={"amount": 36.5, "units": "C"})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert events[0].sprout_type == "measurement"
    assert events[0].payload["measurementType"] == "TEMPERATURE"
    assert events[0].payload["value"] == 36.5


def test_sync_toggles():
    cfg = make_config(sync_feed=False, sync_diaper=False, sync_activity=False, sync_temperature=False, sync_sleep=True)
    records = [
        HbRecord(kind="bottle", start=at(12), payload={"bottle_type": "Formula", "amount": 160, "units": "ml"}),
        HbRecord(kind="diaper", start=at(8), payload={"mode": "pee"}),
        HbRecord(kind="sleep", start=at(13), end=at(14), payload={"duration_seconds": 3600}),
    ]
    events = plan_events(records, cfg, resolved_unit="ML")
    assert [event.sprout_type for event in events] == ["sleep"]


def test_reader_entry_normalization():
    reader = HuckleberryReader(make_config())
    bottle = reader._to_record(
        "feed",
        {"mode": "bottle", "start": 1699177500, "bottleType": "Formula", "amount": 160, "units": "ml"},
        1699177500,
    )
    assert bottle.kind == "bottle"
    assert bottle.payload["amount"] == 160

    sleep = reader._to_record("sleep", {"start": 1699177500, "duration": 3600}, 1699177500)
    assert sleep.kind == "sleep"
    assert (sleep.end - sleep.start).total_seconds() == 3600

    diaper = reader._to_record("diaper", {"mode": "both", "start": 1699177500, "consistency": "loose"}, 1699177500)
    assert diaper.payload["mode"] == "both"

    temp = reader._to_record(
        "health", {"mode": "temperature", "start": 1699177500, "amount": 36.5, "units": "C"}, 1699177500
    )
    assert temp.kind == "temperature"

    assert reader._to_record("health", {"mode": "growth", "start": 1699177500, "weight": 3}, 1699177500) is None
