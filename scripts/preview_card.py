"""Render a sample card with dummy Urdu content — no credentials needed.

Use this to iterate on the card design, check the fonts shape correctly, and see
what a long headline looks like before it ever reaches Facebook.

    python scripts/preview_card.py
    python scripts/preview_card.py --long
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import BUILD_DIR  # noqa: E402
from models import Article, Post  # noqa: E402
from services.log import setup_logging  # noqa: E402
from services.timeutil import now_utc  # noqa: E402

SAMPLES: dict[str, tuple[Post, Article]] = {
    "short": (
        Post(
            headline_ur="بابر اعظم کی شاندار سنچری، پاکستان کامیاب",
            summary_ur=[
                "بابر اعظم نے 114 رنز کی ناقابلِ شکست اننگز کھیلی۔",
                "پاکستان نے ہدف 47ویں اوور میں پورا کر لیا۔",
                "شاہین آفریدی نے تین وکٹیں حاصل کیں۔",
            ],
            caption_ur="قومی ٹیم نے شاندار کارکردگی دکھائی۔",
            hashtags=["#Cricket", "#کرکٹ", "#Pakistan"],
            category_ur="میچ رپورٹ",
        ),
        Article(
            url="https://example.com",
            title="Babar Azam century",
            text="",
            source="ESPNcricinfo",
            published=now_utc(),
        ),
    ),
    "long": (
        Post(
            headline_ur="پاکستان سپر لیگ کے آئندہ سیزن کے شیڈول کا باضابطہ اعلان کر دیا گیا",
            summary_ur=[
                "ایونٹ کا آغاز اگلے سال فروری کی پہلی تاریخ سے ہوگا۔",
                "کل 34 میچز چار مختلف شہروں میں کھیلے جائیں گے۔",
            ],
            caption_ur="تفصیلات جلد جاری کی جائیں گی۔",
            hashtags=["#PSL", "#کرکٹ"],
            category_ur="اعلان",
        ),
        Article(
            url="https://example.com",
            title="PSL schedule",
            text="",
            source="Dawn Sport",
            published=now_utc(),
        ),
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a sample news card")
    parser.add_argument("--long", action="store_true", help="use the long-headline sample")
    args = parser.parse_args()

    setup_logging()
    from pipeline.render import render_card

    post, article = SAMPLES["long" if args.long else "short"]
    out = BUILD_DIR / ("preview_long.png" if args.long else "preview.png")
    render_card(post, article, out)
    print(f"\n✅ Preview written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
