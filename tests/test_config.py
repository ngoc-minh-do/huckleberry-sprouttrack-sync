from __future__ import annotations

import pytest

from huckleberry_sprout_sync.config import ConfigError, _resolve_timezone_name


def test_tz_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert _resolve_timezone_name() == "Asia/Tokyo"


def test_tz_defaults_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "")
    assert _resolve_timezone_name() == "Asia/Tokyo"


def test_tz_strips_leading_colon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", ":America/New_York")
    assert _resolve_timezone_name() == "America/New_York"


def test_tz_respects_valid_iana_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "America/New_York")
    assert _resolve_timezone_name() == "America/New_York"


def test_tz_rejects_non_iana_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "JST-9")
    with pytest.raises(ConfigError):
        _resolve_timezone_name()
