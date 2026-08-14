"""Uniform logging setup for both entrypoints."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging() -> None:
    """Configure root logging once, at a level controlled by LOG_LEVEL."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # These libraries are extremely chatty at DEBUG and drown the run log.
    for noisy in ("urllib3", "trafilatura", "charset_normalizer", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
