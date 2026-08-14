"""Central configuration. Every module reads settings from here, never from os.environ directly.

Secrets come from environment variables (GitHub Actions Secrets in production,
a local .env file in development). Tunables are plain module constants so you can
retune the bot by editing this one file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: only used for local development
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is not installed in CI
    pass


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BUILD_DIR = ROOT / "build"
ASSETS_DIR = ROOT / "assets"
FONT_DIR = ASSETS_DIR / "fonts"
TEMPLATE_DIR = ROOT / "templates"
STATE_PATH = DATA_DIR / "state.json"


class ConfigError(RuntimeError):
    """Raised when a required secret is missing or malformed."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name, str(default)).lower()
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# News sources
# --------------------------------------------------------------------------
# Only feeds verified to return items are listed. `weight` is the source-trust
# multiplier used by the ranker; `region` drives the Pakistan relevance boost.
FEEDS: list[dict] = [
    {
        "name": "ESPNcricinfo",
        "url": "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
        "weight": 1.30,
        "region": "global",
    },
    {
        "name": "BBC Sport Cricket",
        "url": "https://feeds.bbci.co.uk/sport/cricket/rss.xml",
        "weight": 1.25,
        "region": "global",
    },
    {
        "name": "Dawn Sport",
        "url": "https://www.dawn.com/feeds/sport",
        "weight": 1.20,
        "region": "pk",
    },
    {
        "name": "The News Sport",
        "url": "https://www.thenews.com.pk/rss/1/8",
        "weight": 1.10,
        "region": "pk",
    },
    {
        "name": "Times of India Cricket",
        "url": "https://timesofindia.indiatimes.com/rssfeeds/54829575.cms",
        "weight": 0.95,
        "region": "global",
    },
    {
        "name": "Google News (Pakistan Cricket)",
        "url": "https://news.google.com/rss/search?q=cricket+Pakistan+when:2d&hl=en-PK&gl=PK&ceid=PK:en",
        "weight": 1.00,
        "region": "pk",
        "google_news": True,
    },
    {
        "name": "Google News (Cricket)",
        "url": "https://news.google.com/rss/search?q=cricket+when:1d&hl=en-PK&gl=PK&ceid=PK:en",
        "weight": 0.90,
        "region": "global",
        "google_news": True,
    },
]

# --------------------------------------------------------------------------
# Relevance scoring — tuned for a Pakistani audience with global reach
# --------------------------------------------------------------------------
PK_KEYWORDS = [
    "pakistan", "psl", "pakistan super league", "babar azam", "mohammad rizwan",
    "shaheen afridi", "shaheen shah", "naseem shah", "haris rauf", "shadab khan",
    "fakhar zaman", "saim ayub", "abrar ahmed", "pcb", "green shirts",
    "lahore qalandars", "karachi kings", "peshawar zalmi", "multan sultans",
    "islamabad united", "quetta gladiators", "national stadium", "gaddafi stadium",
    "rawalpindi", "shan masood", "mohammad amir", "imad wasim", "azhar ali",
]

HEAT_KEYWORDS = [
    "world cup", "t20 world cup", "champions trophy", "asia cup", "ipl", "ashes",
    "final", "semi-final", "semi final", "record", "century", "double century",
    "hat-trick", "hat trick", "five-wicket", "retire", "retirement", "banned",
    "ban", "controversy", "sacked", "resign", "injury", "ruled out", "comeback",
    "debut", "captain", "captaincy", "fastest", "highest", "wins", "beat",
    "victory", "defeat", "thriller", "super over", "man of the match",
]

LOW_INTEREST_KEYWORDS = [
    "county championship", "second xi", "club cricket", "under-15", "u15",
    "minor counties", "trial match", "practice match", "warm-up",
]

# Weights applied by pipeline/score.py
SCORE_WEIGHTS = {
    "recency": 40.0,        # decays linearly to 0 over RECENCY_HALFLIFE_HOURS
    "pakistan": 30.0,       # per-article boost when PK keywords match
    "heat": 18.0,           # global trending signal
    "source": 12.0,         # multiplied by feed weight
    "low_interest": -25.0,  # penalty
    "pk_feed_bonus": 6.0,   # article came from a Pakistan-region feed
}
RECENCY_HALFLIFE_HOURS = 14.0
# A Pakistan keyword found only in the summary (not the headline) is usually an
# incidental mention, so it earns this fraction of the full Pakistan boost.
PK_MENTION_FACTOR = 0.35
MAX_ARTICLE_AGE_HOURS = 36.0  # anything older is dropped outright

# --------------------------------------------------------------------------
# Pipeline behaviour
# --------------------------------------------------------------------------
MAX_CANDIDATES = 6          # how many articles to attempt before giving up
MIN_ARTICLE_CHARS = 320     # extraction below this length is treated as failed
DUPLICATE_TITLE_RATIO = 0.72
RECENT_TITLES_WINDOW = 200
SEEN_RETENTION = 1200       # url hashes kept in state.json
POSTS_RETENTION = 400       # post records kept in state.json

HTTP_TIMEOUT = 25
HTTP_RETRIES = 3
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 CricketNewsBot/1.0"
)

# --------------------------------------------------------------------------
# Brand / card design
# --------------------------------------------------------------------------
CARD_WIDTH = 1080
CARD_HEIGHT = 1080
BRAND_NAME = _env("BRAND_NAME", "کرکٹ اپڈیٹس")
BRAND_HANDLE = _env("BRAND_HANDLE", "")
BRAND_ACCENT = _env("BRAND_ACCENT", "#F5C542")
BRAND_DEEP = _env("BRAND_DEEP", "#04140E")
LOGO_PATH = ASSETS_DIR / "logo.png"

FONT_FILES = {
    "nastaliq": (
        "NotoNastaliqUrdu.ttf",
        "https://github.com/google/fonts/raw/main/ofl/notonastaliqurdu/NotoNastaliqUrdu%5Bwght%5D.ttf",
    ),
    "naskh": (
        "NotoNaskhArabic.ttf",
        "https://github.com/google/fonts/raw/main/ofl/notonaskharabic/NotoNaskhArabic%5Bwght%5D.ttf",
    ),
    "latin": (
        "Inter.ttf",
        "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf",
    ),
}


@dataclass
class Config:
    # Facebook
    fb_page_id: str = field(default_factory=lambda: _env("FB_PAGE_ID"))
    fb_page_access_token: str = field(default_factory=lambda: _env("FB_PAGE_ACCESS_TOKEN"))
    fb_app_id: str = field(default_factory=lambda: _env("FB_APP_ID"))
    fb_app_secret: str = field(default_factory=lambda: _env("FB_APP_SECRET"))
    fb_api_version: str = field(default_factory=lambda: _env("FB_API_VERSION", "v21.0"))

    # AI provider — "gemini" (default, free tier) or "openai_compatible"
    ai_provider: str = field(default_factory=lambda: _env("AI_PROVIDER", "gemini"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))
    # Used only when ai_provider == "openai_compatible" (Groq, OpenRouter, ...)
    ai_base_url: str = field(default_factory=lambda: _env("AI_BASE_URL"))
    ai_api_key: str = field(default_factory=lambda: _env("AI_API_KEY"))
    ai_model: str = field(default_factory=lambda: _env("AI_MODEL"))

    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))

    # Behaviour switches
    auto_approve: bool = field(default_factory=lambda: _flag("AUTO_APPROVE", False))
    dry_run: bool = field(default_factory=lambda: _flag("DRY_RUN", False))
    approval_timeout_hours: int = field(default_factory=lambda: _int("APPROVAL_TIMEOUT_HOURS", 12))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Asia/Karachi"))

    def require(self, *names: str) -> None:
        """Raise ConfigError listing every missing secret at once."""
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            env_names = [n.upper() for n in missing]
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(env_names)
                + ". Set these as GitHub Actions Secrets (or in a local .env file)."
            )

    @property
    def ai_key(self) -> str:
        return self.gemini_api_key if self.ai_provider == "gemini" else self.ai_api_key


CFG = Config()
