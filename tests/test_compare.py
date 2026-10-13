from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from huckleberry_sprout_sync.compare import Event, compare_events, diff_fields, format_report

TZ = ZoneInfo("Asia/Tokyo")
T0 = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)


def ev(key, minutes, fields):
    return Event(key=key, time=T0 + timedelta(minutes=minutes), fields=fields)


SLEEP = ("sleep", "sleep")
FIELDS = {"duration_min": 60, "type": "NAP", "notes": None}


def test_diff_fields():
    assert diff_fields({"a": 1, "b": None}, {"a": 2, "b": None}) == ["a: sprout=1 hb=2"]
    assert diff_fields(FIELDS, dict(FIELDS)) == []


def test_compare_matched_drift_missing_extra():
    hb = [
        ev(SLEEP, 0, dict(FIELDS)),
        ev(SLEEP, 120, {**FIELDS, "notes": "edited in hb"}),
        ev(SLEEP, 240, dict(FIELDS)),
    ]
    sprout = [
        ev(SLEEP, 0, dict(FIELDS)),
        ev(SLEEP, 120, {**FIELDS, "notes": "old"}),
        ev(SLEEP, 600, dict(FIELDS)),
    ]
    result = compare_events(hb, sprout)
    assert len(result.matched) == 1
    assert len(result.drift) == 1
    assert result.drift[0][2] == ["notes: sprout='old' hb='edited in hb'"]
    assert len(result.truly_missing) == 1
    assert len(result.sprout_only) == 1
    assert result.sprout_only[0].time == T0 + timedelta(minutes=600)


def test_compare_moved_within_window():
    hb = [ev(SLEEP, 0, dict(FIELDS))]
    sprout = [ev(SLEEP, 7, dict(FIELDS))]
    result = compare_events(hb, sprout)
    assert len(result.moved) == 1
    assert not result.truly_missing and not result.sprout_only


def test_compare_near_dup_when_row_already_consumed():
    hb = [ev(SLEEP, 0, dict(FIELDS)), ev(SLEEP, 3, dict(FIELDS))]
    sprout = [ev(SLEEP, 0, dict(FIELDS))]
    result = compare_events(hb, sprout)
    assert len(result.matched) == 1
    assert len(result.near_dup) == 1
    assert not result.truly_missing


def test_format_report_smoke():
    result = compare_events([ev(SLEEP, 0, dict(FIELDS))], [])
    text = format_report(result, TZ, T0.date(), T0.date())
    assert "TRULY-MISSING" in text and "matched=0" in text
