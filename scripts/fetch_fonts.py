"""Download the Urdu/Latin fonts the card renderer needs.

Fonts are *not* committed to the repository (they are large binaries and the
repo is public). CI fetches them into `assets/fonts/` and caches the directory,
so this script normally does nothing after the first run.

Usage: python scripts/fetch_fonts.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import FONT_DIR, FONT_FILES  # noqa: E402
from services.http import HttpError, get  # noqa: E402

logger = logging.getLogger(__name__)

# A truncated/HTML error page is far smaller than a real font file, so anything
# below this is treated as "not downloaded".
MIN_FONT_BYTES = 50 * 1024


def _is_present(path: Path) -> bool:
    return path.exists() and path.stat().st_size > MIN_FONT_BYTES


def fetch_font(key: str, filename: str, url: str) -> bool:
    """Download one font unless a healthy copy already exists. Returns success."""
    target = FONT_DIR / filename
    if _is_present(target):
        logger.info("%-9s ok       %s (%.0f KB, cached)", key, filename, target.stat().st_size / 1024)
        return True

    logger.info("%-9s fetching %s", key, filename)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        response = get(url)
        tmp.write_bytes(response.content)
    except HttpError as exc:
        logger.error("%-9s FAILED   %s: %s", key, filename, exc)
        tmp.unlink(missing_ok=True)
        return False
    except OSError as exc:
        logger.error("%-9s FAILED   could not write %s: %s", key, filename, exc)
        return False

    size = tmp.stat().st_size
    if size <= MIN_FONT_BYTES:
        logger.error("%-9s FAILED   %s is only %d bytes — not a font", key, filename, size)
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(target)
    logger.info("%-9s saved    %s (%.0f KB)", key, filename, size / 1024)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    for key, (filename, url) in FONT_FILES.items():
        fetch_font(key, filename, url)

    missing = [f for f, _ in FONT_FILES.values() if not _is_present(FONT_DIR / f)]
    if missing:
        logger.error("missing fonts after download: %s", ", ".join(missing))
        return 1
    logger.info("all %d fonts present in %s", len(FONT_FILES), FONT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
