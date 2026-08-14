"""Time helpers.

Feed dates arrive in three incompatible shapes (feedparser struct_time, ISO 8601,
RFC 2822) and every one of them can be malformed. Everything here normalises to
timezone-aware UTC and returns None instead of raising, because one bad feed entry
must never abort a run.
"""

from __future__ import annotations

import calendar
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import CFG

logger = logging.getLogger(__name__)

URDU_MONTHS: tuple[str, ...] = (
    "جنوری",
    "فروری",
    "مارچ",
    "اپریل",
    "مئی",
    "جون",
    "جولائی",
    "اگست",
    "ستمبر",
    "اکتوبر",
    "نومبر",
    "دسمبر",
)

URDU_DIGITS: str = "۰۱۲۳۴۵۶۷۸۹"
_DIGIT_MAP = str.maketrans("0123456789", URDU_DIGITS)

MISSING_DATE_HOURS: float = 999.0


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _local_zone() -> ZoneInfo:
    """Resolve CFG.timezone, falling back to UTC if the tzdata name is unknown."""
    try:
        return ZoneInfo(CFG.timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("Unknown timezone %r; falling back to UTC", CFG.timezone)
        return ZoneInfo("UTC")


def to_local(dt: datetime) -> datetime:
    """Convert to the configured display timezone; naive input is assumed UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_local_zone())


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes, otherwise convert; the single normalisation point."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_feed_date(value: Any) -> datetime | None:
    """Parse any feed date shape into timezone-aware UTC, or None when unusable."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return _as_utc(value)

    # feedparser hands back a struct_time (or a plain 9-tuple) already in UTC.
    if isinstance(value, time.struct_time):
        try:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            logger.debug("Unparseable struct_time %r: %s", value, exc)
            return None

    if isinstance(value, tuple) and len(value) >= 9:
        try:
            return datetime.fromtimestamp(calendar.timegm(tuple(value)[:9]), tz=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError) as exc:
            logger.debug("Unparseable time tuple %r: %s", value, exc)
            return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None

    if not isinstance(value, str):
        logger.debug("Unsupported feed date type %s", type(value).__name__)
        return None

    raw = value.strip()
    if not raw:
        return None

    # ISO 8601 first: cheapest, and what our own state file writes.
    candidate = raw.replace("Z", "+00:00") if raw.endswith(("Z", "z")) else raw
    try:
        return _as_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    # RFC 2822 — the RSS/Atom default.
    try:
        return _as_utc(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError) as exc:
        logger.debug("Unparseable date string %r: %s", raw, exc)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d %b %Y"):
        try:
            return _as_utc(datetime.strptime(raw, fmt))
        except ValueError:
            continue

    logger.debug("Giving up on feed date %r", raw)
    return None


def hours_since(dt: datetime | None) -> float:
    """Age in hours; a missing date scores as ancient so the ranker drops it."""
    if dt is None:
        return MISSING_DATE_HOURS
    delta: timedelta = now_utc() - _as_utc(dt)
    return delta.total_seconds() / 3600.0


def to_urdu_digits(text: str) -> str:
    """Replace ASCII digits with Urdu-Arabic ones for on-card text."""
    return text.translate(_DIGIT_MAP)


def urdu_date(dt: datetime | None = None) -> str:
    """Render a datetime in local time as Urdu, e.g. "14 اگست 2026".

    ASCII digits, not Urdu-Indic ones: the body copy carries scores and overs in
    ASCII (that is how Pakistani sports media writes them), so Urdu-Indic
    numerals here would clash on the same card. They also read ambiguously at
    small sizes — ۴ is easily mistaken for ۲.
    """
    local = to_local(dt if dt is not None else now_utc())
    month = URDU_MONTHS[local.month - 1]
    return f"{local.day} {month} {local.year}"


def iso(dt: datetime | None = None) -> str:
    """Serialise to a UTC ISO 8601 string — the only date format in state.json."""
    return _as_utc(dt if dt is not None else now_utc()).isoformat()


def from_iso(s: str | None) -> datetime | None:
    """Inverse of :func:`iso`, tolerant of anything :func:`parse_feed_date` handles."""
    if not s:
        return None
    return parse_feed_date(s)
