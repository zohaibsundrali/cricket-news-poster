"""Stage 2: canonicalise URLs and drop anything we have already posted or seen."""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
import string
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from config import DUPLICATE_TITLE_RATIO
from models import Candidate

logger = logging.getLogger(__name__)

# Params that only identify the referrer/campaign, never the article itself.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "ref", "ref_src", "icid", "cmp", "at_medium",
    "at_campaign", "amp",
}
_TRACKING_PREFIXES = ("utm_",)

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "vs", "v", "is", "are", "was", "were", "as", "by", "from", "his", "her",
    "their", "after", "before",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_TABLE = str.maketrans({ch: " " for ch in string.punctuation + "‘’“”–—…"})


def canonical_url(url: str) -> str:
    """Normalise a URL so trackers and cosmetic variants collapse to one key."""
    if not url:
        return ""
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "http").lower()
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if parts.port and parts.port not in (80, 443):
            host = f"{host}:{parts.port}"

        path = parts.path or ""
        if path.endswith("/amp"):
            path = path[: -len("/amp")]
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")

        kept = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
            and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
        ]
        query = urlencode(sorted(kept))

        if not host:
            return raw.lower()
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:  # pragma: no cover - malformed URLs still need a stable key
        logger.debug("canonical_url failed for %r", url, exc_info=True)
        return raw.lower()


def url_hash(url: str) -> str:
    """Short, stable identity for a URL — what state.json remembers."""
    return hashlib.sha256(canonical_url(url).encode("utf-8", "replace")).hexdigest()[:16]


def normalize_title(title: str) -> str:
    """Token-sorted, stopword-free form of a headline so word order stops mattering."""
    if not title:
        return ""
    text = title.lower().translate(_PUNCT_TABLE)
    tokens = [t for t in _WS_RE.sub(" ", text).split(" ") if t and t not in _STOPWORDS]
    return " ".join(sorted(tokens))


def title_similarity(a: str, b: str) -> float:
    """0..1 similarity of two headlines after normalisation (stdlib difflib only)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_duplicate_title(title: str, recent_titles: Iterable[str]) -> bool:
    """True when the headline is a near-match of anything in recent_titles."""
    try:
        for other in recent_titles or []:
            if title_similarity(title, str(other)) >= DUPLICATE_TITLE_RATIO:
                return True
    except Exception:  # pragma: no cover
        logger.debug("title dedupe comparison failed", exc_info=True)
    return False


def _recent_titles(store: Any) -> list[str]:
    try:
        return [str(t) for t in (store.recent_titles() or [])]
    except Exception:
        logger.warning("store.recent_titles() failed; treating history as empty", exc_info=True)
        return []


def _is_seen(store: Any, h: str) -> bool:
    try:
        return bool(store.is_seen(h))
    except Exception:
        logger.debug("store.is_seen() failed for %s", h, exc_info=True)
        return False


def filter_new(candidates: Sequence[Candidate], store: Any) -> list[Candidate]:
    """Stamp url_hash on each candidate and keep only genuinely new stories."""
    history = _recent_titles(store)
    seen_hashes: set[str] = set()
    batch_titles: list[str] = []
    fresh: list[Candidate] = []
    dropped_seen = dropped_title = dropped_batch = dropped_bad = 0

    for c in candidates or []:
        try:
            c.url_hash = url_hash(c.url)
        except Exception:  # pragma: no cover
            dropped_bad += 1
            continue

        if _is_seen(store, c.url_hash):
            dropped_seen += 1
            continue
        if c.url_hash in seen_hashes:
            dropped_batch += 1
            continue
        if is_duplicate_title(c.title, history):
            dropped_title += 1
            continue
        if is_duplicate_title(c.title, batch_titles):
            dropped_batch += 1
            continue

        seen_hashes.add(c.url_hash)
        batch_titles.append(c.title)
        fresh.append(c)

    logger.info(
        "dedupe: %d in -> %d new (dropped %d already-seen, %d title-dupes, "
        "%d in-batch dupes, %d unusable)",
        len(candidates or []),
        len(fresh),
        dropped_seen,
        dropped_title,
        dropped_batch,
        dropped_bad,
    )
    return fresh
