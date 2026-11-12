"""A learner's own schedule: their time zone and when the lesson and the translation task arrive.

Every learner keeps their own copy in the `learners` table; the settings only provide the
defaults a new learner starts with. Pure helpers turn "now" (UTC) into the learner's local
time and decide whether a moment falls into a daily window.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

MAX_QUIZ_DELAY_MINUTES = 12 * 60
_CLOCK = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


def valid_timezone(name: str) -> str:
    """`name` if it is a known IANA zone (Europe/Moscow, Asia/Vladivostok), else ValueError."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"unknown timezone {name!r}") from error
    return name


def parse_clock(text: str) -> time | None:
    """ "9:30", "09:30" or "21.00" as a time of day; None if it is not one."""
    match = _CLOCK.match(text.strip())
    return None if match is None else time(int(match.group(1)), int(match.group(2)))


class LearnerSchedule(BaseModel):
    timezone: str = "Europe/Moscow"
    lesson_send_time: time = time(9, 0)
    translation_send_time: time = time(18, 0)
    quiz_delay_minutes: int = Field(default=60, ge=0, le=MAX_QUIZ_DELAY_MINUTES)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        return valid_timezone(value)


def local_now(timezone: str, now: datetime) -> datetime:
    """`now` (timezone-aware) as wall-clock time in `timezone`."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ZoneInfo(timezone))


def local_today(timezone: str, now: datetime) -> date:
    return local_now(timezone, now).date()


def in_daily_window(local: datetime, start: time, hours: int) -> bool:
    """Whether the local wall-clock time is in [start, start + hours), never past midnight."""
    minute = local.hour * 60 + local.minute
    first = start.hour * 60 + start.minute
    return first <= minute < min(first + hours * 60, 24 * 60)
