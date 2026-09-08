"""Weekly admission windows, evaluated in local wall-clock time."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class TimeWindow:
    days: tuple[int, ...]
    start: int
    end: int

    def matches(self, local: datetime) -> bool:
        minute = local.hour * 60 + local.minute
        day = local.weekday()
        if self.start < self.end:
            return day in self.days and self.start <= minute < self.end
        return (day in self.days and minute >= self.start) or (
            (day - 1) % 7 in self.days and minute < self.end
        )


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: ZoneInfo | None = None
    allow: tuple[TimeWindow, ...] = ()
    deny: tuple[TimeWindow, ...] = ()

    def allows(self, now: datetime | None = None) -> bool:
        instant = now if now is not None else datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("schedule requires a timezone-aware datetime")
        local = instant.astimezone(self.timezone)
        return (not self.allow or any(rule.matches(local) for rule in self.allow)) and not any(
            rule.matches(local) for rule in self.deny
        )


def _minute(value: object, path: str, *, end: bool = False) -> int:
    if end and value == "24:00":
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError(f"{path} must be HH:MM" + (" (or 24:00)" if end else ""))
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def load_schedule(raw: object) -> ScheduleConfig:
    if not isinstance(raw, dict) or raw.keys() - {"timezone", "allow", "deny"}:
        raise ValueError("schedule must be a table with timezone, allow and/or deny")
    zone = None
    if "timezone" in raw:
        name = raw["timezone"]
        if not isinstance(name, str) or not name:
            raise ValueError("schedule.timezone must be an IANA timezone name")
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"schedule.timezone is invalid or unavailable: {name!r}") from exc

    def windows(kind: str) -> tuple[TimeWindow, ...]:
        entries = raw.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f"schedule.{kind} must be an array of tables")  # noqa: TRY004
        result = []
        for index, entry in enumerate(entries):
            path = f"schedule.{kind}[{index}]"
            if not isinstance(entry, dict) or entry.keys() - {"days", "start", "end"}:
                raise ValueError(f"{path} must be a table with days, start and end")
            days = entry.get("days", list(_DAYS))
            if not isinstance(days, list) or not days or any(
                not isinstance(day, str) or day not in _DAYS for day in days
            ):
                raise ValueError(f"{path}.days must be a nonempty array of mon..sun")
            start = _minute(entry.get("start"), f"{path}.start")
            end = _minute(entry.get("end"), f"{path}.end", end=True)
            if start == end:
                raise ValueError(f"{path}: start and end must differ; use 00:00–24:00 for all day")
            result.append(TimeWindow(tuple(_DAYS.index(day) for day in days), start, end))
        return tuple(result)

    return ScheduleConfig(zone, windows("allow"), windows("deny"))
