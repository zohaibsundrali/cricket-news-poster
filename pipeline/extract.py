"""Stage 4: pull the readable article body out of a publisher page."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import trafilatura

from config import MIN_ARTICLE_CHARS
from models import Article, Candidate
from services.http import HttpError, get
from services.timeutil import parse_feed_date

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 6000  # the AI stage never needs more; keeps token use tiny
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")
_SENTENCE_END_RE = re.compile(r"[.!?۔؟](?:[\"'”’)\]]*)\s")


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _tidy(text: str) -> str:
    """Normalise whitespace and cap the body on a sentence boundary."""
    cleaned = _TRAILING_WS_RE.sub("\n", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = _BLANK_LINES_RE.sub("\n\n", cleaned).strip()
    if len(cleaned) <= _MAX_TEXT_CHARS:
        return cleaned

    window = cleaned[: _MAX_TEXT_CHARS + 1]
    cut = 0
    for m in _SENTENCE_END_RE.finditer(window):
        cut = m.end()
    if cut < _MAX_TEXT_CHARS // 2:  # no sensible sentence break: fall back to a word break
        cut = window.rfind(" ")
    if cut <= 0:
        cut = _MAX_TEXT_CHARS
    return cleaned[:cut].strip()


def _meta_get(meta: Any, key: str) -> str:
    """trafilatura returns a Document object (newer) or a dict (older)."""
    if meta is None:
        return ""
    value = meta.get(key) if isinstance(meta, dict) else getattr(meta, key, None)
    return str(value).strip() if value else ""


def _meta_date(meta: Any) -> datetime | None:
    raw = _meta_get(meta, "date")
    if not raw:
        return None
    try:
        return parse_feed_date(raw)
    except Exception:
        return None


def extract(url: str) -> Article | None:
    """Fetch a page and return its Article, or None with the reason logged."""
    if not url:
        return None
    try:
        html = get(url).text
    except HttpError as exc:
        logger.warning("extract: fetch failed for %s: %s", url, exc)
        return None
    except Exception as exc:  # pragma: no cover - unexpected transport errors
        logger.warning("extract: unexpected fetch error for %s: %s", url, exc)
        return None

    if not html:
        logger.warning("extract: empty response body for %s", url)
        return None

    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("extract: trafilatura raised for %s: %s", url, exc)
        return None

    if not text:
        logger.warning("extract: no readable body found at %s", url)
        return None

    text = _tidy(text)
    if len(text) < MIN_ARTICLE_CHARS:
        logger.warning("extract: body too short (%d < %d) at %s", len(text), MIN_ARTICLE_CHARS, url)
        return None

    meta: Any = None
    try:
        meta = trafilatura.extract_metadata(html)
    except Exception:  # pragma: no cover - metadata is optional
        logger.debug("extract: metadata failed for %s", url, exc_info=True)

    return Article(
        url=url,
        title=_meta_get(meta, "title"),
        text=text,
        source=_meta_get(meta, "sitename") or _host(url),
        published=_meta_date(meta),
        top_image=_meta_get(meta, "image"),
    )


def extract_with_fallback(candidate: Candidate) -> Article | None:
    """Full-text extraction, falling back to the feed summary when the page is unreadable."""
    try:
        article = extract(candidate.url)
    except Exception:  # pragma: no cover - extract() is already defensive
        logger.warning("extract_with_fallback: extract() raised for %s", candidate.url, exc_info=True)
        article = None

    if article is not None:
        if not article.title:
            article.title = candidate.title
        if article.published is None:
            article.published = candidate.published
        if not article.source:
            article.source = candidate.source
        return article

    summary = (candidate.summary or "").strip()
    if len(summary) >= MIN_ARTICLE_CHARS:
        logger.info("extract: using feed-summary fallback for %s", candidate.url)
        return Article(
            url=candidate.url,
            title=candidate.title,
            text=_tidy(summary),
            source=candidate.source or _host(candidate.url),
            published=candidate.published,
            top_image="",
        )

    logger.warning(
        "extract: giving up on %s (no body, summary only %d chars)", candidate.url, len(summary)
    )
    return None
