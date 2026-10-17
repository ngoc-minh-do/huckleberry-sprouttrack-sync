from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from huckleberry_sprout_sync.compare import load_sprout_rows
from huckleberry_sprout_sync.config import Config
from huckleberry_sprout_sync.models import HbRecord, PlannedEvent
from huckleberry_sprout_sync.reconcile import apply_plan, moved_body, plan_reconcile, put_body

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


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, tzinfo=TZ)


def utc_iso(hour: int, minute: int = 0) -> str:
    return at(hour, minute).astimezone(UTC).isoformat()


def make_snapshot(path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Unit (unitAbbr TEXT);
        CREATE TABLE Food (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE Medicine (
            id TEXT PRIMARY KEY, name TEXT, isSupplement INTEGER,
            typicalDoseSize REAL, unitAbbr TEXT, deletedAt TEXT
        );
        CREATE TABLE FeedLog (
            id TEXT PRIMARY KEY, type TEXT, startTime TEXT, time TEXT,
            feedDuration INTEGER, side TEXT, bottleType TEXT, amount REAL,
            unitAbbr TEXT, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE FoodLog (
            id TEXT PRIMARY KEY, time TEXT, foods TEXT, foodId TEXT, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE SleepLog (
            id TEXT PRIMARY KEY, startTime TEXT, duration INTEGER, type TEXT, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE DiaperLog (
            id TEXT PRIMARY KEY, time TEXT, type TEXT, condition TEXT, color TEXT, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE PlayLog (
            id TEXT PRIMARY KEY, startTime TEXT, type TEXT, duration INTEGER, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE BathLog (id TEXT PRIMARY KEY, time TEXT, bathType TEXT, notes TEXT, deletedAt TEXT);
        CREATE TABLE Measurement (
            id TEXT PRIMARY KEY, date TEXT, type TEXT, value REAL, unit TEXT, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE PumpLog (
            id TEXT PRIMARY KEY, startTime TEXT, totalAmount REAL, leftAmount REAL,
            rightAmount REAL, unitAbbr TEXT, duration INTEGER, notes TEXT, deletedAt TEXT
        );
        CREATE TABLE MedicineLog (
            id TEXT PRIMARY KEY, time TEXT, medicineId TEXT, doseAmount REAL,
            unitAbbr TEXT, notes TEXT, deletedAt TEXT
        );
        """
    )
    con.execute("INSERT INTO Unit (unitAbbr) VALUES ('ML')")
    con.commit()
    return con


def add_feed(con, row_id: str, hour: int, minute: int = 0, *, amount: float = 160.0):
    con.execute(
        "INSERT INTO FeedLog (id, type, startTime, time, feedDuration, side, bottleType, amount, unitAbbr, notes, deletedAt) "
        "VALUES (?, 'BOTTLE', NULL, ?, NULL, NULL, 'Formula', ?, 'ML', NULL, NULL)",
        (row_id, utc_iso(hour, minute), amount),
    )


def bottle_record(hour: int, minute: int = 0, *, amount: float = 160.0) -> HbRecord:
    return HbRecord(
        kind="bottle",
        start=at(hour, minute),
        payload={"bottle_type": "Formula", "amount": amount, "units": "ml"},
    )


class FakeSprout:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.resolved: str | None = None
        self.warmed = False

    async def resolve_baby_id(self) -> str:
        self.resolved = "b1"
        return "b1"

    async def warm_up(self, **kwargs) -> bool:
        self.warmed = True
        return True

    async def post_activity(self, baby_id: str, payload: dict) -> dict:
        self.posted.append((baby_id, payload))
        return {}

    async def update_activity(self, baby_id: str, activity_id: str, payload: dict) -> dict:
        self.updated.append((baby_id, activity_id, payload))
        return {}

    async def close(self) -> None:
        pass


def test_plan_reconcile_posts_only_missing(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    add_feed(con, "f1", 12, 0)
    con.commit()
    con.close()

    cfg = make_config()
    records = [bottle_record(12, 0), bottle_record(13, 0, amount=150.0)]
    plan = plan_reconcile(records, load_sprout_rows(db), cfg, resolved_unit="ML")

    assert plan.hb_total == 2
    assert plan.sprout_total == 1
    assert plan.matched == 1
    assert plan.near_dup == 0
    assert len(plan.to_post) == 1
    assert plan.to_post[0].payload["time"] == "2026-10-05T13:00:00+09:00"
    assert plan.to_post[0].payload["amount"] == 150
    assert plan.to_post[0].payload["unitAbbr"] == "ML"


def test_near_dup_never_posted(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    add_feed(con, "f1", 12, 0)
    con.commit()
    con.close()

    cfg = make_config()
    records = [bottle_record(12, 0), bottle_record(12, 3)]
    plan = plan_reconcile(records, load_sprout_rows(db), cfg, resolved_unit="ML")

    assert plan.near_dup == 1
    assert plan.to_post == []


@pytest.mark.asyncio
async def test_reconcile_dry_run_writes_nothing(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    add_feed(con, "f1", 12, 0)
    con.commit()
    con.close()

    cfg = make_config()
    plan = plan_reconcile([bottle_record(13, 0)], load_sprout_rows(db), cfg, resolved_unit="ML")
    sprout = FakeSprout()
    result = await apply_plan(sprout, cfg, str(db), cfg.local_today, cfg.local_today, plan, dry_run=True)

    assert result.dry_run is True
    assert result.written == 0
    assert result.updated == 0
    assert sprout.posted == []
    assert sprout.updated == []
    assert sprout.resolved is None  # never touches the API in dry run
    assert not sprout.warmed


@pytest.mark.asyncio
async def test_reconcile_applies_posts_and_drift_updates(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    add_feed(con, "f1", 12, 0)  # exact match
    add_feed(con, "f2", 13, 0, amount=100.0)  # drifts (100 vs 160)
    con.commit()
    con.close()

    cfg = make_config()
    records = [bottle_record(12, 0), bottle_record(13, 0)]
    plan = plan_reconcile(records, load_sprout_rows(db), cfg, resolved_unit="ML")
    assert plan.matched == 1
    assert len(plan.drift) == 1
    assert plan.to_post == []

    sprout = FakeSprout()
    result = await apply_plan(sprout, cfg, str(db), cfg.local_today, cfg.local_today, plan, dry_run=False)
    assert result.written == 0  # nothing missing
    assert result.updated == 0  # drift updates need the opt-in flag
    assert sprout.posted == []
    assert sprout.updated == []

    sprout = FakeSprout()
    result = await apply_plan(
        sprout, cfg, str(db), cfg.local_today, cfg.local_today, plan, dry_run=False, update_drift=True
    )
    assert result.updated == 1
    assert sprout.updated == [("b1", "f2", {"type": "feed", "amount": 160})]
    assert sprout.warmed


@pytest.mark.asyncio
async def test_reconcile_applies_moved_updates(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    add_feed(con, "f3", 12, 7, amount=150.0)  # 7 minutes later than Huckleberry
    con.commit()
    con.close()

    cfg = make_config()
    plan = plan_reconcile([bottle_record(12, 0, amount=150.0)], load_sprout_rows(db), cfg, resolved_unit="ML")
    assert len(plan.moved) == 1
    assert plan.to_post == []

    sprout = FakeSprout()
    result = await apply_plan(
        sprout, cfg, str(db), cfg.local_today, cfg.local_today, plan, dry_run=False, update_moved=True
    )
    assert result.updated == 1
    assert sprout.updated == [("b1", "f3", {"type": "feed", "time": "2026-10-05T12:00:00+09:00"})]


def test_put_body_maps_only_differing_fields():
    planned = PlannedEvent(
        sprout_type="feed",
        kind="bottle",
        time=at(12),
        payload={"type": "feed", "time": utc_iso(12), "amount": 160, "unitAbbr": "ML", "notes": "hi"},
    )
    assert put_body(planned, ["amount", "notes"]) == {"type": "feed", "amount": 160, "notes": "hi"}
    assert put_body(planned, ["side"]) == {"type": "feed"}  # unmapped field discarded


def test_moved_body_keeps_sleep_duration():
    planned = PlannedEvent(
        sprout_type="sleep",
        kind="sleep",
        time=at(12),
        payload={"type": "sleep", "time": utc_iso(12), "duration": 60},
    )
    assert moved_body(planned) == {"type": "sleep", "duration": 60, "time": utc_iso(12)}


def test_load_sprout_rows_tags_supplements(tmp_path):
    db = tmp_path / "snapshot.db"
    con = make_snapshot(db)
    con.execute("INSERT INTO Medicine VALUES ('m1', 'Vitamin D', 1, NULL, NULL, NULL)")
    con.execute(
        "INSERT INTO MedicineLog (id, time, medicineId, doseAmount, unitAbbr, notes, deletedAt) "
        "VALUES ('ml1', ?, 'm1', 5, 'DROP', NULL, NULL)",
        (utc_iso(9, 0),),
    )
    con.commit()
    con.close()

    rows = load_sprout_rows(db)
    assert len(rows) == 1
    assert rows[0].id == "ml1"
    assert rows[0].activity_type == "supplement"
