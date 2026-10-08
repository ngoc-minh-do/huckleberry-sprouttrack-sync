from __future__ import annotations

from datetime import datetime, timedelta
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
        "sprout_base_url": "https://sprout-track.example.com",
        "sprout_api_keys": ("st_live_test",),
        "sprout_baby_id": None,
        "sprout_unit": None,
        "timezone_name": "Asia/Tokyo",
        "dry_run": True,
        "child": None,
        "sync_feed": True,
        "sync_sleep": True,
        "sync_diaper": True,
        "sync_activity": True,
        "sync_temperature": True,
        "sync_pump": True,
        "sync_growth": True,
        "sync_medication": True,
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
    assert convert_amount(160, "ml", "OZ") == round(160 / 29.5735, 2)
    assert convert_amount(5.4, "oz", "ML") == 160
    assert convert_amount(3, "oz", "OZ") == 3
    assert convert_amount(1.25, "ml", "ML") == 1.25
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
    record = HbRecord(kind="temperature", start=at(7, 0), payload={"amount": 36.5, "units": "C", "notes": "36.5"})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert events[0].sprout_type == "measurement"
    assert events[0].payload["measurementType"] == "TEMPERATURE"
    assert events[0].payload["value"] == 36.5
    assert "notes" not in events[0].payload  # webhook rejects notes on measurement


def test_sync_toggles():
    cfg = make_config(
        sync_feed=False,
        sync_diaper=False,
        sync_activity=False,
        sync_temperature=False,
        sync_pump=False,
        sync_growth=False,
        sync_medication=False,
        sync_sleep=True,
    )
    records = [
        HbRecord(kind="bottle", start=at(12), payload={"bottle_type": "Formula", "amount": 160, "units": "ml"}),
        HbRecord(kind="diaper", start=at(8), payload={"mode": "pee"}),
        HbRecord(kind="sleep", start=at(13), end=at(14), payload={"duration_seconds": 3600}),
    ]
    events = plan_events(records, cfg, resolved_unit="ML")
    assert [event.sprout_type for event in events] == ["sleep"]


def test_plan_pump_leftright():
    cfg = make_config()
    record = HbRecord(
        kind="pump",
        start=at(6, 30),
        end=at(6, 50),
        payload={"entry_mode": "leftright", "left_amount": 60, "right_amount": 70, "units": "ml", "notes": "90"},
    )
    events = plan_events([record], cfg, resolved_unit="ML")
    assert len(events) == 1
    payload = events[0].payload
    assert payload["type"] == "pump"
    assert payload["leftAmount"] == 60
    assert payload["rightAmount"] == 70
    assert payload["unitAbbr"] == "ML"
    assert payload["duration"] == 20
    assert "notes" not in payload  # webhook rejects notes on pump
    assert planned_key(events[0]) == ("pump", "pump")


def test_plan_pump_total():
    cfg = make_config()
    record = HbRecord(
        kind="pump",
        start=at(6, 30),
        payload={"entry_mode": "total", "left_amount": 75, "right_amount": 75, "units": "ml"},
    )
    events = plan_events([record], cfg, resolved_unit="OZ")
    payload = events[0].payload
    assert "leftAmount" not in payload and "rightAmount" not in payload
    assert payload["totalAmount"] == round(150 / 29.5735, 2)
    assert payload["unitAbbr"] == "OZ"


def test_plan_growth():
    cfg = make_config()
    record = HbRecord(
        kind="growth",
        start=at(10, 0),
        payload={"weight": 8.2, "weight_units": "kg", "height": 68, "height_units": "cm"},
    )
    events = plan_events([record], cfg, resolved_unit="ML")
    by_type = {event.kind: event for event in events}
    assert set(by_type) == {"WEIGHT", "HEIGHT"}
    assert by_type["WEIGHT"].payload["value"] == 8.2
    assert by_type["WEIGHT"].payload["unit"] == "KG"
    assert by_type["HEIGHT"].payload["unit"] == "CM"
    assert planned_key(by_type["WEIGHT"]) == ("measurement", "WEIGHT")


def test_plan_medication_matched_and_skipped():
    cfg = make_config()
    medicines = {
        "infant tylenol": {"name": "Infant Tylenol", "isSupplement": False},
        "vitamin d drops": {"name": "Vitamin D Drops", "isSupplement": True},
    }
    matched = HbRecord(
        kind="medication", start=at(9, 0), payload={"name": "Infant Tylenol", "amount": 1.25, "units": "ml"}
    )
    supplement = HbRecord(
        kind="medication", start=at(9, 30), payload={"name": "Vitamin D Drops", "amount": 1, "units": "ml"}
    )
    unknown = HbRecord(kind="medication", start=at(10, 0), payload={"name": "Cough Syrup", "amount": 5, "units": "ml"})
    events = plan_events([matched, supplement, unknown], cfg, resolved_unit="ML", medicines=medicines)
    by_type = {event.sprout_type: event for event in events}
    assert by_type["medicine"].payload["medicineName"] == "Infant Tylenol"
    assert by_type["medicine"].payload["amount"] == 1.25
    assert by_type["medicine"].payload["unitAbbr"] == "ML"
    assert by_type["supplement"].payload["supplementName"] == "Vitamin D Drops"
    assert len(events) == 2


def test_plan_medication_zero_dose_uses_typical():
    cfg = make_config()
    medicines = {"vitamin k": {"name": "Vitamin K", "isSupplement": True, "typicalDoseSize": 1.0, "unitAbbr": "ML"}}
    zero = HbRecord(kind="medication", start=at(18, 12), payload={"name": "Vitamin K", "amount": 0.0, "units": "drops"})
    events = plan_events([zero], cfg, resolved_unit="ML", medicines=medicines)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["supplementName"] == "Vitamin K"
    assert payload["amount"] == 1.0
    assert payload["unitAbbr"] == "ML"


def test_plan_medication_drops_uses_medicine_unit():
    cfg = make_config()
    medicines = {"vitamin d": {"name": "Vitamin D", "isSupplement": True, "typicalDoseSize": 2.0, "unitAbbr": "DROP"}}
    dose = HbRecord(kind="medication", start=at(9, 0), payload={"name": "Vitamin D", "amount": 5.0, "units": "drops"})
    events = plan_events([dose], cfg, resolved_unit="ML", medicines=medicines)
    payload = events[0].payload
    assert payload["amount"] == 5.0
    assert payload["unitAbbr"] == "DROP"


def test_temperature_kind_is_measurement_subtype():
    cfg = make_config()
    record = HbRecord(kind="temperature", start=at(7, 0), payload={"amount": 36.5, "units": "C"})
    events = plan_events([record], cfg, resolved_unit="ML")
    assert planned_key(events[0]) == ("measurement", "TEMPERATURE")


def test_reader_entry_normalization():
    reader = HuckleberryReader(make_config())
    bottle = reader._to_record(
        "feed",
        {"mode": "bottle", "start": 1699177500, "bottleType": "Formula", "amount": 160, "units": "ml"},
        1699177500,
    )
    assert bottle is not None
    assert bottle.kind == "bottle"
    assert bottle.payload["amount"] == 160

    sleep = reader._to_record("sleep", {"start": 1699177500, "duration": 3600}, 1699177500)
    assert sleep is not None
    assert sleep.end is not None
    assert sleep.kind == "sleep"
    assert (sleep.end - sleep.start).total_seconds() == 3600

    diaper = reader._to_record("diaper", {"mode": "both", "start": 1699177500, "consistency": "loose"}, 1699177500)
    assert diaper is not None
    assert diaper.payload["mode"] == "both"

    temp = reader._to_record(
        "health", {"mode": "temperature", "start": 1699177500, "amount": 36.5, "units": "C"}, 1699177500
    )
    assert temp is not None
    assert temp.kind == "temperature"

    growth = reader._to_record("health", {"mode": "growth", "start": 1699177500, "weight": 3}, 1699177500)
    assert growth is not None
    assert growth.kind == "growth"
    assert growth.payload["weight"] == 3

    pump = reader._to_record(
        "pump",
        {
            "start": 1699177500,
            "duration": 600,
            "entryMode": "leftright",
            "leftAmount": 60,
            "rightAmount": 70,
            "units": "ml",
        },
        1699177500,
    )
    assert pump is not None
    assert pump.end is not None
    assert pump.kind == "pump"
    assert (pump.end - pump.start).total_seconds() == 600

    medication = reader._to_record(
        "health",
        {"mode": "medication", "start": 1699177500, "medication_name": "Infant Tylenol", "amount": 1.25, "units": "ml"},
        1699177500,
    )
    assert medication is not None
    assert medication.kind == "medication"


def test_local_today_is_in_configured_timezone() -> None:
    cfg = make_config(timezone_name="Asia/Tokyo")
    assert cfg.local_today == datetime.now(TZ).date()
