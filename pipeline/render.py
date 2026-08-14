"""Render the Urdu news card to a PNG using Chromium.

Why a headless browser instead of Pillow: Urdu is a cursive right-to-left script
that needs real text shaping (contextual letterforms, ligatures, mark
positioning). Pillow only shapes correctly when it was compiled against libraqm,
which is not guaranteed on a CI runner. Chromium ships HarfBuzz and always gets
Nastaliq right, and it gives us CSS for the layout as a bonus.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import (
    BRAND_ACCENT,
    BRAND_DEEP,
    BRAND_HANDLE,
    BRAND_NAME,
    BUILD_DIR,
    CARD_HEIGHT,
    CARD_WIDTH,
    FONT_DIR,
    FONT_FILES,
    LOGO_PATH,
    TEMPLATE_DIR,
)
from models import Article, Post
from services.timeutil import now_utc, urdu_date

logger = logging.getLogger(__name__)


class RenderError(RuntimeError):
    """Raised when the card cannot be produced."""


def _font_uri(key: str) -> str:
    """Absolute file:// URI for a font, so the file:// page can load it."""
    path = FONT_DIR / FONT_FILES[key][0]
    if not path.exists():
        raise RenderError(
            f"Font missing: {path}. Run `python scripts/fetch_fonts.py` first."
        )
    return path.resolve().as_uri()


def _logo_uri() -> str:
    """Inline the logo as a data URI; a file:// <img> is blocked in some setups."""
    if not LOGO_PATH.exists():
        return ""
    data = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def build_html(post: Post, article: Article) -> str:
    """Fill the card template with this post's content."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("card.html")
    return template.render(
        width=CARD_WIDTH,
        height=CARD_HEIGHT,
        accent=BRAND_ACCENT,
        deep=BRAND_DEEP,
        brand_name=BRAND_NAME,
        brand_handle=BRAND_HANDLE,
        logo_uri=_logo_uri(),
        font_nastaliq=_font_uri("nastaliq"),
        font_naskh=_font_uri("naskh"),
        font_latin=_font_uri("latin"),
        category_ur=post.category_ur or "کرکٹ خبر",
        headline_ur=post.headline_ur,
        summary_ur=post.summary_ur,
        source_name=article.source,
        date_ur=urdu_date(article.published or now_utc()),
    )


def render_card(post: Post, article: Article, out_path: Path) -> Path:
    """Render the card to `out_path` and return it."""
    from playwright.sync_api import sync_playwright  # imported lazily: heavy

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_path = BUILD_DIR / "card.html"
    html_path.write_text(build_html(post, article), encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[
                "--no-sandbox",  # CI runners execute as root
                "--disable-dev-shm-usage",
                "--font-render-hinting=none",  # consistent glyph metrics
            ]
        )
        try:
            page = browser.new_page(
                viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT},
                device_scale_factor=1,
            )
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            try:
                # The template signals when the fit-to-card pass has finished.
                page.wait_for_function(
                    "document.documentElement.dataset.fit === 'done'", timeout=15000
                )
            except Exception:
                # A fit timeout is cosmetic, not fatal — shoot it anyway.
                logger.warning("Card fit script did not signal; rendering as-is")
            page.screenshot(path=str(out_path), type="png")
        finally:
            browser.close()

    size_kb = out_path.stat().st_size / 1024
    logger.info("Rendered card %s (%.0f KB)", out_path.name, size_kb)
    return out_path
