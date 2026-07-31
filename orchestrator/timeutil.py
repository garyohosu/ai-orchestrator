"""UTC timestamp helpers matching the mail package's fixed-width ISO format.

The mail package's ``find_mails(sent_after=...)`` compares timestamps as
plain strings (SPEC 21, mail/SPEC.md 15). Every timestamp orchestrator
stores or compares against mail data must use the exact same
``YYYY-MM-DDTHH:MM:SS.mmmZ`` format, or string comparison silently breaks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_FORMAT = "%Y-%m-%dT%H:%M:%S."
_TIMESTAMP_PATTERN_LENGTH = len("2026-07-30T00:00:00.000Z")


def now_iso() -> str:
    return datetime_to_iso(datetime.now(timezone.utc))


def datetime_to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    milliseconds = value.microsecond // 1000
    return value.strftime(_FORMAT) + f"{milliseconds:03d}Z"


def iso_to_datetime(value: str) -> datetime:
    if len(value) != _TIMESTAMP_PATTERN_LENGTH or not value.endswith("Z"):
        raise ValueError(f"not a valid timestamp: {value!r}")
    body, millis = value[:-1].split(".")
    dt = datetime.strptime(body, "%Y-%m-%dT%H:%M:%S")
    return dt.replace(microsecond=int(millis) * 1000, tzinfo=timezone.utc)


def shift_ms(value: str, delta_ms: int) -> str:
    """Return ``value`` shifted by ``delta_ms`` milliseconds, same format."""
    dt = iso_to_datetime(value)
    return datetime_to_iso(dt + timedelta(milliseconds=delta_ms))
