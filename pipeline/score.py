"""Stage 3: editorial ranking — Pakistan relevance first, global heat second."""

from __future__ import annotations

import logging
import re
from typing import Iterable, Sequence

from config import (
    HEAT_KEYWORDS,
    LOW_INTEREST_KEYWORDS,
    MAX_ARTICLE_AGE_HOURS,
    PK_KEYWORDS,
    PK_MENTION_FACTOR,
    RECENCY_HALFLIFE_HOURS,
    SCORE_WEIGHTS,
)
from models import Candidate
from services.timeutil import hours_since

logger = logging.getLogger(__name__)

# Single short words need word boundaries so "ban" does not match "banner";
# longer words and phrases are matched as plain substrings so "pakistan"
# still fires on "pakistani" and "record" on "records".
_SHORT_WORD_MAX = 5


def _compile(keywords: Iterable[str]) -> list[tuple[str, re.Pattern[str] | None]]:
    compiled: list[tuple[str, re.Pattern[str] | None]] = []
    for kw in keywords:
        low = str(kw).strip().lower()
        if not low:
            continue
        if " " not in low and len(low) <= _SHORT_WORD_MAX:
            compiled.append((low, re.compile(rf"\b{re.escape(low)}s?\b")))
        else:
            compiled.append((low, None))
    return compiled


_PK = _compile(PK_KEYWORDS)
_HEAT = _compile(HEAT_KEYWORDS)
_LOW = _compile(LOW_INTEREST_KEYWORDS)


def _first_match(haystack: str, compiled: Sequence[tuple[str, re.Pattern[str] | None]]) -> str | None:
    """Return the first matching keyword, or None."""
    for word, pattern in compiled:
        if pattern is not None:
            if pattern.search(haystack):
                return word
        elif word in haystack:
            return word
    return None


def _haystack(c: Candidate) -> str:
    return f"{c.title or ''} {c.summary or ''}".lower()


def _age_hours(c: Candidate) -> float | None:
    """Hours since publication, or None when the feed gave us no date."""
    if c.published is None:
        return None
    try:
        return float(hours_since(c.published))
    except Exception:  # pragma: no cover
        logger.debug("hours_since failed for %s", c.url, exc_info=True)
        return None


def score_candidate(c: Candidate) -> Candidate:
    """Compute c.score and record why, so the Actions log explains every decision."""
    score = 0.0
    reasons: list[str] = []
    try:
        text = _haystack(c)

        # Recency: linear decay to zero; unknown dates get half credit rather
        # than being punished as if they were old.
        w_recency = float(SCORE_WEIGHTS.get("recency", 0.0))
        age = _age_hours(c)
        if age is None:
            points = w_recency * 0.5
            reasons.append(f"recency:unknown +{points:.1f}")
        else:
            fresh = max(0.0, 1.0 - (age / RECENCY_HALFLIFE_HOURS)) if RECENCY_HALFLIFE_HOURS else 0.0
            points = w_recency * fresh
            reasons.append(f"recency:{age:.1f}h +{points:.1f}")
        score += points

        # A story *about* Pakistan earns the full boost; one that merely mentions
        # Pakistan in passing ("...ahead of the Pakistan Test series") earns a
        # fraction. Without this split, any article whose summary name-drops a
        # Pakistani team outranks genuine Pakistan news.
        title_text = (c.title or "").lower()
        pk_title_hit = _first_match(title_text, _PK)
        pk_body_hit = _first_match(text, _PK)
        if pk_title_hit:
            points = float(SCORE_WEIGHTS.get("pakistan", 0.0))
            score += points
            reasons.append(f"PK-title:{pk_title_hit} +{points:.1f}")
        elif pk_body_hit:
            points = float(SCORE_WEIGHTS.get("pakistan", 0.0)) * PK_MENTION_FACTOR
            score += points
            reasons.append(f"PK-mention:{pk_body_hit} +{points:.1f}")
        if (c.region or "").lower() == "pk":
            points = float(SCORE_WEIGHTS.get("pk_feed_bonus", 0.0))
            score += points
            reasons.append(f"pk_feed +{points:.1f}")

        heat_hit = _first_match(text, _HEAT)
        if heat_hit:  # counted once no matter how many heat words appear
            points = float(SCORE_WEIGHTS.get("heat", 0.0))
            score += points
            reasons.append(f"heat:{heat_hit} +{points:.1f}")

        points = float(SCORE_WEIGHTS.get("source", 0.0)) * float(c.source_weight or 0.0)
        score += points
        reasons.append(f"source:{c.source} +{points:.1f}")

        low_hit = _first_match(text, _LOW)
        if low_hit:
            points = float(SCORE_WEIGHTS.get("low_interest", 0.0))
            score += points
            reasons.append(f"low:{low_hit} {points:+.1f}")
    except Exception:  # pragma: no cover - a scoring bug must not abort the run
        logger.warning("scoring failed for %s", c.url, exc_info=True)

    c.score = round(score, 2)
    c.score_reasons.extend(reasons)
    return c


def rank(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Drop stale items, score the rest, return best-first."""
    scored: list[Candidate] = []
    too_old = 0

    for c in candidates or []:
        try:
            age = _age_hours(c)
            if age is not None and age > MAX_ARTICLE_AGE_HOURS:
                too_old += 1
                continue
            scored.append(score_candidate(c))
        except Exception:  # pragma: no cover
            logger.warning("skipping candidate during ranking: %s", getattr(c, "url", "?"), exc_info=True)

    scored.sort(key=lambda x: x.score, reverse=True)
    logger.info(
        "rank: %d candidates scored, %d dropped as older than %.0fh",
        len(scored),
        too_old,
        MAX_ARTICLE_AGE_HOURS,
    )
    for i, c in enumerate(scored[:3], start=1):
        logger.info("  #%d %.1f  %s  [%s]", i, c.score, c.title[:90], ", ".join(c.score_reasons))
    return scored
