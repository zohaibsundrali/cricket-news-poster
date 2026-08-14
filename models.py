"""Shared data structures passed between pipeline stages.

Every stage takes one of these and returns another. Keeping them here means no
pipeline module has to import another pipeline module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Candidate:
    """A headline seen in a feed, before we have fetched the article body."""

    url: str
    title: str
    source: str
    source_weight: float = 1.0
    region: str = "global"
    published: datetime | None = None  # timezone-aware UTC
    summary: str = ""
    url_hash: str = ""
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)


@dataclass
class Article:
    """A candidate whose full text has been extracted."""

    url: str
    title: str
    text: str
    source: str
    published: datetime | None = None
    top_image: str = ""


@dataclass
class Post:
    """The Urdu content produced by the AI stage, ready to render and publish."""

    headline_ur: str
    summary_ur: list[str]
    caption_ur: str
    hashtags: list[str]
    category_ur: str = "کرکٹ خبر"
    key_numbers: list[str] = field(default_factory=list)
