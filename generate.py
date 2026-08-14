"""Entrypoint A — find a story, write it in Urdu, render the card, ask for approval.

Runs six times a day at Pakistani waking hours. One run produces at most one
post. If the top-ranked article cannot be extracted or written up, it falls
through to the next candidate rather than abandoning the slot.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from config import BUILD_DIR, CFG, MAX_CANDIDATES, ConfigError
from models import Article, Candidate, Post
from services import telegram
from services.log import setup_logging
from services.store import Store, new_post_id
from services.timeutil import iso, now_utc

logger = logging.getLogger(__name__)

CARDS_DIR = BUILD_DIR / "cards"


def _select_and_build(candidates: list[Candidate], store: Store) -> tuple[Candidate, Article, Post, Path] | None:
    """Walk the ranked candidates until one survives extraction, writing and rendering."""
    from pipeline.compose import ComposeError, compose
    from pipeline.extract import extract_with_fallback
    from pipeline.render import RenderError, render_card

    for index, candidate in enumerate(candidates[:MAX_CANDIDATES], start=1):
        logger.info(
            "Candidate %d/%d (score %.1f): %s",
            index, min(len(candidates), MAX_CANDIDATES), candidate.score, candidate.title,
        )

        article = extract_with_fallback(candidate)
        if article is None:
            # Mark it seen so a permanently unreadable URL is not retried forever.
            store.mark_seen(candidate.url_hash)
            continue

        try:
            post = compose(article)
        except ComposeError as exc:
            logger.warning("Compose failed for %s: %s", candidate.url, exc)
            store.mark_seen(candidate.url_hash)
            continue

        post_id = new_post_id()
        try:
            image = render_card(post, article, CARDS_DIR / f"{post_id}.png")
        except RenderError:
            # Environmental (missing font, no browser), not article-specific —
            # the next candidate would fail identically, so fail loudly instead.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Render failed for %s: %s", candidate.url, exc)
            store.mark_seen(candidate.url_hash)
            continue

        return candidate, article, post, image

    return None


def _build_record(candidate: Candidate, article: Article, post: Post, image: Path) -> dict:
    from pipeline.compose import full_caption

    return {
        "id": image.stem,
        "url_hash": candidate.url_hash,
        "source_url": article.url,
        "source_name": article.source,
        "title_en": article.title,
        "headline_ur": post.headline_ur,
        "summary_ur": post.summary_ur,
        "caption_ur": post.caption_ur,
        "fb_caption": full_caption(post, article),
        "hashtags": post.hashtags,
        "category_ur": post.category_ur,
        "status": "pending_approval",
        "created_at": iso(now_utc()),
    }


def run() -> int:
    from pipeline.dedupe import filter_new
    from pipeline.discover import discover
    from pipeline.score import rank

    store = Store.load()

    candidates = discover()
    if not candidates:
        logger.warning("No candidates from any feed")
        telegram.notify_info("کسی بھی فیڈ سے خبر نہیں ملی — اگلی باری پر دوبارہ کوشش ہوگی۔")
        return 0

    fresh = filter_new(candidates, store)
    ranked = rank(fresh)
    if not ranked:
        logger.info("Nothing new to post this run")
        store.save()  # persist any hashes marked during filtering
        return 0

    built = _select_and_build(ranked, store)
    if built is None:
        store.save()
        logger.error("No candidate survived the pipeline")
        telegram.notify_failure(
            "pipeline",
            "No candidate survived extraction/compose/render",
            f"Tried {min(len(ranked), MAX_CANDIDATES)} article(s)",
        )
        return 1

    candidate, article, post, image = built
    record = _build_record(candidate, article, post, image)

    if CFG.dry_run:
        logger.info("DRY_RUN: card at %s", image)
        logger.info("DRY_RUN headline: %s", post.headline_ur)
        logger.info("DRY_RUN caption:\n%s", record["fb_caption"])
        return 0

    # Persist BEFORE anything outbound. If the process dies after this point we
    # still know the story was consumed, so it can never be posted twice.
    store.add_post(record)
    store.save()

    if CFG.auto_approve:
        from publish import publish_record

        logger.info("AUTO_APPROVE is on — publishing without review")
        ok = publish_record(store, record)
        store.save()
        return 0 if ok else 1

    try:
        sent = telegram.send_approval_card(image, record)
    except telegram.TelegramError as exc:
        store.update_post(record["id"], status="failed",
                          error_stage="telegram", error_msg=str(exc))
        store.save()
        logger.error("Could not send approval card: %s", exc)
        return 1

    store.update_post(
        record["id"],
        telegram_message_id=sent.get("message_id"),
        telegram_file_id=sent.get("file_id"),
    )
    store.save()
    logger.info("Approval card sent for %s — waiting for your tap", record["id"])
    return 0


def main() -> int:
    setup_logging()
    try:
        CFG.require("telegram_bot_token", "telegram_chat_id")
        if not CFG.ai_key:
            raise ConfigError(
                "Missing required configuration: GEMINI_API_KEY (or AI_API_KEY when "
                "AI_PROVIDER is openai_compatible)."
            )
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - unattended: always alert before dying
        logger.exception("Generate run crashed")
        telegram.notify_failure("generate", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
