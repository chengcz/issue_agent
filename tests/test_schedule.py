from datetime import datetime

import pytest

from issue_agent.config import load_config
from issue_agent.schedule import load_schedule


def rule(start="18:00", end="09:00", **extra):
    return dict(start=start, end=end, **extra)


@pytest.mark.parametrize("config,instant,expected", [
    ({}, "2026-09-07T12:00:00+08:00", True),
    ({"allow": [rule()]}, "2026-09-07T18:00:00+08:00", True),
    ({"allow": [rule()]}, "2026-09-07T08:59:59+08:00", True),
    ({"allow": [rule()]}, "2026-09-07T09:00:00+08:00", False),
    ({"allow": [rule()]}, "2026-09-07T17:59:59+08:00", False),
    ({"deny": [rule("09:00", "18:00", days=["mon"])]},
     "2026-09-07T12:00:00+08:00", False),
    ({"deny": [rule("09:00", "18:00", days=["mon"])]},
     "2026-09-06T12:00:00+08:00", True),
    ({"allow": [rule()], "deny": [rule("20:00", "21:00")]},
     "2026-09-07T20:00:00+08:00", False),
    ({"allow": [rule("09:00", "10:00"), rule("12:00", "13:00")]},
     "2026-09-07T12:30:00+08:00", True),
    ({"allow": [rule(days=["sun"])]}, "2026-09-07T08:00:00+08:00", True),
    ({"allow": [rule(days=["sun"])]}, "2026-09-07T18:00:00+08:00", False),
    ({"allow": [rule("00:00", "24:00", days=["sun"])]},
     "2026-09-07T00:00:00+08:00", False),
    ({"allow": [rule("00:00", "24:00")]}, "2026-09-07T23:59:59+08:00", True),
    ({"allow": [rule()]}, "2026-09-07T10:00:00+00:00", True),
])
def test_windows(config, instant, expected):
    schedule = load_schedule({"timezone": "Asia/Shanghai", **config})
    assert schedule.allows(datetime.fromisoformat(instant)) is expected


@pytest.mark.parametrize("instant,expected", [
    ("2026-11-01T05:30:00+00:00", True),  # First 01:30
    ("2026-11-01T06:30:00+00:00", True),  # Repeated 01:30
    ("2026-11-01T07:00:00+00:00", False),
    ("2026-03-08T06:59:00+00:00", True),
    ("2026-03-08T07:00:00+00:00", False),  # Skipped 02:00
])
def test_dst(instant, expected):
    schedule = load_schedule({"timezone": "America/New_York", "allow": [rule("01:00", "02:00")]})
    assert schedule.allows(datetime.fromisoformat(instant)) is expected


def test_system_timezone():
    instant = datetime.fromisoformat("2026-09-07T12:00:00+00:00")
    local = instant.astimezone()
    schedule = load_schedule({"allow": [rule("00:00", "24:00", days=[
        ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[local.weekday()]
    ])]})
    assert schedule.allows(instant)
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.allows(datetime(2026, 9, 7))  # noqa: DTZ001 - deliberately invalid input


@pytest.mark.parametrize("raw", [
    [], {"unknown": True}, {"timezone": "Missing/Zone"}, {"timezone": ""},
    {"timezone": 8}, {"allow": {}}, {"deny": [1]},
    {"allow": [rule(start="24:00")]}, {"allow": [rule(start="9:00")]},
    {"allow": [rule(end="24:01")]}, {"allow": [rule(end="18:00")]},
    {"allow": [rule(days=[])]}, {"allow": [rule(days="mon")]},
    {"allow": [rule(days=["monday"])]}, {"allow": [rule(days=[1])]},
    {"allow": [{"start": "18:00"}]}, {"allow": [rule(extra=True)]},
])
def test_invalid_schedule(raw):
    with pytest.raises(ValueError, match="schedule"):
        load_schedule(raw)


def test_config_load(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[schedule]\ntimezone = "Asia/Shanghai"\n'
                      '[[schedule.deny]]\nstart = "09:00"\nend = "18:00"\n')
    assert not load_config(config).schedule.allows(datetime.fromisoformat("2026-09-07T12:00+08:00"))
    config.write_text('schedule = "invalid"')
    with pytest.raises(ValueError, match="schedule"):
        load_config(config)


def test_once_outside_window_skips_preflight(tmp_path, monkeypatch, caplog):
    import asyncio
    from argparse import Namespace
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from issue_agent import cli

    path = tmp_path / "config.toml"
    path.write_text("")
    config = replace(load_config(path), schedule=load_schedule({
        "deny": [rule("00:00", "24:00")]
    }))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    preflight = AsyncMock()
    monkeypatch.setattr(cli, "preflight_labels", preflight)
    with caplog.at_level("INFO"):
        assert asyncio.run(cli.async_main(Namespace(command="once", config=str(path)))) == 0
    preflight.assert_not_awaited()
    assert "execution window closed" in caplog.text
