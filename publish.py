"""Entrypoint B — read Telegram approvals and publish to Facebook.

Runs every 15 minutes. Deliberately cheap: no browser, no fonts, no AI calls.
It only reads button presses, uploads already-rendered cards to Facebook, and
retires anything that has gone stale.

Kept separate from generate.py because a GitHub Actions runner is ephemeral and
cannot sit waiting for you to tap a button.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from config import BUILD_DIR, CFG, ConfigError
from services import telegram
from services.log import setup_logging
from services.store import Store
from services.timeutil import from_iso, hours_since, iso, now_utc

logger = logging.getLogger(__name__)

CARDS_DIR = BUILD_DIR / "cards"
TOKEN_CHECK_INTERVAL_HOURS = 24
TOKEN_WARN_DAYS = 14


def _card_path(record: dict) -> Path:
    """Local copy of the rendered card, downloading it back from Telegram if needed."""
    local = CARDS_DIR / f"{record['id']}.png"
    if local.exists() and local.stat().st_size > 1024:
        return local

    file_id = record.get("telegram_file_id")
    if not file_id:
        raise RuntimeError(
            f"No local card and no telegram_file_id for {record['id']}; cannot publish."
        )
    return telegram.download_file(file_id, local)


def publish_record(store: Store, record: dict) -> bool:
    """Push one approved record to Facebook. Returns True on success.

    The record is already persisted as `pending_approval` before this runs, so a
    crash mid-publish can never lose the fact that we attempted it.
    """
    from pipeline.facebook import FacebookError, publish_photo  # lazy: keeps import light

    post_id = record["id"]

    if CFG.dry_run:
        logger.info("DRY_RUN: would publish %s to Facebook", post_id)
        store.update_post(post_id, status="skipped", error_stage="dry_run")
        return False

    try:
        CFG.require("fb_page_id", "fb_page_access_token")
    except ConfigError as exc:
        logger.error("%s", exc)
        store.update_post(post_id, status="failed", error_stage="config", error_msg=str(exc))
        telegram.notify_failure("config", str(exc), f"post {post_id}")
        return False

    try:
        image = _card_path(record)
        caption = record.get("fb_caption") or record.get("caption_ur") or ""
        fb_post_id = publish_photo(image, caption)
    except FacebookError as exc:
        store.update_post(
            post_id, status="failed", error_stage="facebook", error_msg=str(exc)
        )
        if exc.is_auth_error:
            telegram.notify_failure(
                "facebook-auth",
                str(exc),
                "The Page access token looks invalid or is missing a permission. "
                "Regenerate it in Meta Business Settings and update the "
                "FB_PAGE_ACCESS_TOKEN secret.",
            )
        else:
            telegram.notify_failure("facebook", str(exc), f"post {post_id}")
        return False
    except Exception as exc:  # noqa: BLE001 - unattended job: log, alert, move on
        logger.exception("Unexpected failure publishing %s", post_id)
        store.update_post(
            post_id, status="failed", error_stage="publish", error_msg=str(exc)
        )
        telegram.notify_failure("publish", str(exc), f"post {post_id}")
        return False

    store.update_post(
        post_id, status="posted", fb_post_id=fb_post_id, posted_at=iso(now_utc())
    )
    store.save()  # persist immediately: never risk re-posting the same story
    telegram.notify_success(record, fb_post_id)
    logger.info("Published %s as %s", post_id, fb_post_id)
    return True


def _handle_callbacks(store: Store) -> int:
    """Apply every pending button press. Returns how many posts were published."""
    offset = int(store.data.get("telegram_offset", 0) or 0)
    callbacks, next_offset = telegram.poll_callbacks(offset)
    store.data["telegram_offset"] = next_offset

    published = 0
    for cb in callbacks:
        record = store.get_post(cb.post_id)

        if record is None:
            telegram.answer_callback(cb.id, "یہ پوسٹ ریکارڈ میں نہیں ملی")
            logger.warning("Callback for unknown post %s", cb.post_id)
            continue

        if record.get("status") != "pending_approval":
            telegram.answer_callback(
                cb.id, f"پہلے ہی: {record.get('status')}"
            )
            telegram.finalize_message(
                cb.chat_id, cb.message_id, f"ℹ️ پہلے ہی <b>{record.get('status')}</b>"
            )
            continue

        if cb.action == "reject":
            store.update_post(cb.post_id, status="rejected")
            telegram.answer_callback(cb.id, "منسوخ کر دی گئی")
            telegram.finalize_message(cb.chat_id, cb.message_id, "❌ <b>منسوخ</b>")
            logger.info("Rejected %s", cb.post_id)
            continue

        telegram.answer_callback(cb.id, "فیس بک پر پوسٹ کی جا رہی ہے…")
        if publish_record(store, record):
            published += 1
            telegram.finalize_message(cb.chat_id, cb.message_id, "✅ <b>پوسٹ ہو گئی</b>")
        else:
            telegram.finalize_message(cb.chat_id, cb.message_id, "⚠️ <b>ناکام</b>")

    return published


def _expire_stale(store: Store) -> None:
    """Retire approval cards you never answered, so they stop piling up."""
    limit = CFG.approval_timeout_hours
    for record in store.pending():
        age = hours_since(from_iso(record.get("created_at")))
        if age >= limit:
            store.update_post(record["id"], status="expired")
            logger.info("Expired %s after %.1fh", record["id"], age)
            telegram.notify_info(
                f"⏳ ایک پوسٹ {limit} گھنٹے میں منظور نہ ہونے پر خارج کر دی گئی:\n"
                f"{record.get('headline_ur', '')}"
            )


def _maybe_check_token(store: Store) -> None:
    """Warn before the Facebook token dies, at most once a day."""
    last = from_iso(store.data.get("last_token_check"))
    if hours_since(last) < TOKEN_CHECK_INTERVAL_HOURS:
        return
    if not (CFG.fb_app_id and CFG.fb_app_secret and CFG.fb_page_access_token):
        return

    from pipeline.facebook import check_token

    store.data["last_token_check"] = iso(now_utc())
    info = check_token()

    if not info.get("valid"):
        telegram.notify_failure(
            "facebook-token",
            info.get("reason", "token reported invalid"),
            "Publishing will fail until FB_PAGE_ACCESS_TOKEN is refreshed.",
        )
        return

    days = info.get("days_left")
    if days is None:
        logger.info("Facebook token never expires (System User token).")
    elif days <= TOKEN_WARN_DAYS:
        telegram.notify_info(
            f"🔑 Facebook token expires in <b>{days}</b> day(s). "
            "Regenerate it in Meta Business Settings and update the secret."
        )


def main() -> int:
    setup_logging()
    try:
        CFG.require("telegram_bot_token", "telegram_chat_id")
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    store = Store.load()
    published = 0
    try:
        published = _handle_callbacks(store)
        _expire_stale(store)
        _maybe_check_token(store)
    finally:
        store.save()

    logger.info(
        "Publish run complete: %d published, %d still pending",
        published,
        len(store.pending()),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
