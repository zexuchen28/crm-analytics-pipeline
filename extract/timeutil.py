"""Timestamp helpers. The CRM contract is ISO-8601 UTC at second precision
(e.g. 2026-10-09T12:30:00Z), which also sorts correctly as a plain string."""
from datetime import datetime, timedelta, timezone

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc)


def shift_ts(value: str, seconds: int) -> str:
    return fmt_ts(parse_ts(value) + timedelta(seconds=seconds))
