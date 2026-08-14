"""Stage 1: read every configured RSS feed and turn its items into Candidates."""

from __future__ import annotations

import base64
import binascii
import html as html_mod
import logging
import re
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlparse

import feedparser

from config import FEEDS
from models import Candidate
from services.http import HttpError, get
from services.timeutil import parse_feed_date

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SUMMARY_CAP = 600
_MIN_TITLE_CHARS = 20

# Publisher URLs sit as plain text inside the decoded Google News payload.
_URL_IN_BLOB_RE = re.compile(r"https?://[^\s\x00-\x1f\"'<>]+")
# The payload is protobuf, so trailing framing bytes can sit flush against the
# URL; keep only the leading run of legal URL characters.
_URL_CHARS_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]*")
_GOOGLE_HOSTS = ("google.com", "gstatic.com", "googleusercontent.com")


def _clean_text(value: Any) -> str:
    """Unescape entities, drop HTML tags and collapse whitespace."""
    if not value:
        return ""
    try:
        text = _TAG_RE.sub(" ", str(value))
        text = html_mod.unescape(text)
        # A second pass catches tags that were entity-encoded in the feed.
        text = _TAG_RE.sub(" ", text)
        return _WS_RE.sub(" ", text).strip()
    except Exception:  # pragma: no cover - defensive
        return ""


def _is_google_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    return any(host == g or host.endswith("." + g) for g in _GOOGLE_HOSTS)


def resolve_google_news_url(url: str) -> str | None:
    """Recover the publisher URL hidden inside a news.google.com/rss/articles link."""
    if not url:
        return None
    try:
        if "/articles/" not in url:
            # Already a direct link (some Google News items are not redirects).
            return url if not _is_google_host(url) else None

        segment = url.split("/articles/", 1)[1]
        segment = segment.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
        if not segment:
            return None

        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii", "ignore"))
        blob = raw.decode("latin-1", "replace")

        for match in _URL_IN_BLOB_RE.finditer(blob):
            trimmed = _URL_CHARS_RE.match(match.group(0))
            if not trimmed:
                continue
            found = trimmed.group(0).rstrip(".,;)")
            if len(found) > 20 and not _is_google_host(found):
                return found
        return None
    except (binascii.Error, ValueError, UnicodeError):
        return None
    except Exception:  # pragma: no cover - never let a bad link abort the run
        logger.debug("google news decode failed for %s", url, exc_info=True)
        return None


def _entry_published(entry: Any) -> datetime | None:
    """Best-effort published date from any of feedparser's date fields."""
    for key in ("published_parsed", "updated_parsed", "published", "updated", "created"):
        value = entry.get(key) if hasattr(entry, "get") else None
        if not value:
            continue
        try:
            parsed = parse_feed_date(value)
        except Exception:
            continue
        if parsed is not None:
            return parsed
    return None


def _entry_source_name(entry: Any, default: str) -> str:
    """Google News wraps the real publisher name in an item-level <source> element."""
    source = entry.get("source") if hasattr(entry, "get") else None
    if not source:
        return default
    name = ""
    if isinstance(source, str):
        name = source
    elif isinstance(source, dict):
        name = source.get("title") or source.get("value") or ""
    else:
        name = getattr(source, "title", "") or ""
    name = _clean_text(name)
    return name or default


def _entry_summary(entry: Any) -> str:
    raw = ""
    if hasattr(entry, "get"):
        raw = entry.get("summary") or entry.get("description") or ""
        if not raw:
            content = entry.get("content") or []
            if isinstance(content, list) and content:
                first = content[0]
                raw = first.get("value", "") if isinstance(first, dict) else str(first)
    text = _clean_text(raw)
    return text[:_SUMMARY_CAP]


def _candidates_from_feed(feed: dict) -> list[Candidate]:
    """Fetch and parse one feed. Raises on network failure so the caller can count it."""
    name = str(feed.get("name") or feed.get("url") or "unknown")
    url = str(feed.get("url") or "")
    weight = float(feed.get("weight", 1.0) or 1.0)
    region = str(feed.get("region") or "global")
    is_google = bool(feed.get("google_news"))

    response = get(url)
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", 0) and not getattr(parsed, "entries", None):
        raise ValueError(f"unparseable feed {name}: {getattr(parsed, 'bozo_exception', '')}")

    out: list[Candidate] = []
    for entry in getattr(parsed, "entries", []) or []:
        try:
            link = (entry.get("link") or "").strip() if hasattr(entry, "get") else ""
            title = _clean_text(entry.get("title") if hasattr(entry, "get") else "")
            if not link or len(title) < _MIN_TITLE_CHARS:
                continue

            if is_google:
                resolved = resolve_google_news_url(link)
                if not resolved:
                    # Without a publisher URL the extraction stage can never run.
                    logger.debug("dropping unresolvable google news link: %s", link)
                    continue
                link = resolved

            out.append(
                Candidate(
                    url=link,
                    title=title,
                    source=_entry_source_name(entry, name) if is_google else name,
                    source_weight=weight,
                    region=region,
                    published=_entry_published(entry),
                    summary=_entry_summary(entry),
                )
            )
        except Exception:  # pragma: no cover - a single bad item must not kill the feed
            logger.debug("skipping malformed entry in %s", name, exc_info=True)
            continue
    return out


def _dedupe_by_url(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Keep one candidate per URL, preferring the most trusted source."""
    best: dict[str, Candidate] = {}
    for c in candidates:
        key = c.url.strip().rstrip("/")
        current = best.get(key)
        if current is None or c.source_weight > current.source_weight:
            best[key] = c
    return list(best.values())


def discover() -> list[Candidate]:
    """Collect candidates from every configured feed; a dead feed is skipped, never fatal."""
    collected: list[Candidate] = []
    ok = 0
    failed = 0

    for feed in FEEDS:
        name = str(feed.get("name") or feed.get("url") or "unknown")
        try:
            items = _candidates_from_feed(feed)
        except HttpError as exc:
            failed += 1
            logger.warning("feed failed (http) %s: %s", name, exc)
            continue
        except Exception as exc:  # pragma: no cover - parser blowups, odd encodings
            failed += 1
            logger.warning("feed failed (parse) %s: %s", name, exc)
            continue
        ok += 1
        logger.debug("feed %s produced %d candidates", name, len(items))
        collected.extend(items)

    candidates = _dedupe_by_url(collected)
    logger.info(
        "discover: %d/%d feeds ok, %d failed, %d candidates (%d before url-dedupe)",
        ok,
        len(FEEDS),
        failed,
        len(candidates),
        len(collected),
    )
    return candidates
