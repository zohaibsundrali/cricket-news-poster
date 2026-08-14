"""Telegram bot: approval cards, alerts, and reading button presses.

Telegram doubles as the image store. The generate job uploads the rendered PNG
here and keeps only the returned `file_id`; the publish job downloads it again
when you approve. That means rendered cards never get committed to the repo, so
the public repository does not grow by ~200 KB every three hours forever.

The PNG is sent with `sendDocument` rather than `sendPhoto` on purpose:
`sendPhoto` re-encodes to JPEG and strips quality, while a document round-trips
the exact bytes we later hand to Facebook.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any, NamedTuple

from config import CFG
from services.http import HttpError, get, post

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
CAPTION_LIMIT = 1024  # Telegram hard limit for document/photo captions


class TelegramError(RuntimeError):
    """Raised when the Bot API rejects a call."""


class Callback(NamedTuple):
    """One inline-button press."""

    id: str
    action: str  # "approve" | "reject"
    post_id: str
    chat_id: str
    message_id: int


def _url(method: str) -> str:
    return f"{API_ROOT}/bot{CFG.telegram_bot_token}/{method}"


def _call(method: str, *, data: dict | None = None, files: dict | None = None) -> Any:
    """POST to the Bot API and unwrap `result`, raising TelegramError on failure."""
    try:
        response = post(_url(method), data=data or {}, files=files)
        payload = response.json()
    except HttpError as exc:
        # Surface Telegram's own description when it sent one.
        raise TelegramError(f"{method} failed ({exc.status}): {exc.body}") from exc
    except ValueError as exc:
        raise TelegramError(f"{method} returned non-JSON") from exc

    if not payload.get("ok"):
        raise TelegramError(f"{method}: {payload.get('description', 'unknown error')}")
    return payload.get("result")


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------
def send_message(text: str, *, disable_preview: bool = True) -> int | None:
    """Send a plain HTML message. Never raises — alerting must not break the run."""
    try:
        result = _call(
            "sendMessage",
            data={
                "chat_id": CFG.telegram_chat_id,
                "text": _truncate(text, 4096),
                "parse_mode": "HTML",
                "disable_web_page_preview": str(disable_preview).lower(),
            },
        )
        return result.get("message_id")
    except TelegramError as exc:
        logger.error("Telegram sendMessage failed: %s", exc)
        return None


def send_approval_card(image_path: Path, record: dict) -> dict:
    """Upload the card with Approve/Reject buttons. Returns message_id + file_id."""
    caption = _build_caption(record)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ پوسٹ کریں", "callback_data": f"ap:{record['id']}"},
                {"text": "❌ منسوخ", "callback_data": f"rj:{record['id']}"},
            ]
        ]
    }

    with image_path.open("rb") as handle:
        result = _call(
            "sendDocument",
            data={
                "chat_id": CFG.telegram_chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard),
            },
            files={"document": (image_path.name, handle, "image/png")},
        )

    document = result.get("document") or {}
    return {
        "message_id": result.get("message_id"),
        "file_id": document.get("file_id", ""),
    }


def _build_caption(record: dict) -> str:
    """Everything you need to judge the post at a glance, inside 1024 chars."""
    hashtags = " ".join(record.get("hashtags") or [])
    lines = [
        f"<b>{_esc(record.get('headline_ur', ''))}</b>",
        "",
        _esc(_truncate(record.get("caption_ur", ""), 420)),
        "",
    ]
    if hashtags:
        lines.append(f"🏷 {_esc(hashtags)}")
    lines.append(f"📰 <i>{_esc(record.get('source_name', ''))}</i>")
    lines.append(f"🔗 {_esc(record.get('source_url', ''))}")
    return _truncate("\n".join(lines), CAPTION_LIMIT)


def notify_success(record: dict, fb_post_id: str) -> None:
    """Confirm a live post, with a direct link to it."""
    link = f"https://www.facebook.com/{fb_post_id.replace('_', '/posts/')}"
    send_message(
        "✅ <b>پوسٹ ہو گئی</b>\n\n"
        f"{_esc(record.get('headline_ur', ''))}\n\n"
        f"📰 {_esc(record.get('source_name', ''))}\n"
        f"🔗 <a href=\"{_esc(link)}\">Facebook پر دیکھیں</a>"
    )


def notify_failure(stage: str, error: str, context: str = "") -> None:
    """Alert on a failed run, naming the stage so you know where to look."""
    body = [
        "❌ <b>Cricket bot failed</b>",
        "",
        f"<b>Stage:</b> <code>{_esc(stage)}</code>",
        f"<b>Error:</b> <code>{_esc(_truncate(error, 600))}</code>",
    ]
    if context:
        body += ["", f"<b>Context:</b> {_esc(_truncate(context, 400))}"]
    send_message("\n".join(body))


def notify_info(text: str) -> None:
    """Low-priority notice (nothing to post, token expiring, etc.)."""
    send_message(f"ℹ️ {text}")


def finalize_message(chat_id: str, message_id: int, status_line: str) -> None:
    """Strip the buttons and stamp the outcome so the chat stays unambiguous."""
    try:
        _call(
            "editMessageReplyMarkup",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": []}),
            },
        )
    except TelegramError as exc:
        logger.warning("Could not clear buttons on %s: %s", message_id, exc)

    try:
        _call(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": status_line,
                "parse_mode": "HTML",
                "reply_to_message_id": message_id,
                "disable_web_page_preview": "true",
            },
        )
    except TelegramError as exc:
        logger.warning("Could not post status reply: %s", exc)


# --------------------------------------------------------------------------
# Inbound
# --------------------------------------------------------------------------
def poll_callbacks(offset: int) -> tuple[list[Callback], int]:
    """Read button presses since `offset`. Returns the presses and the next offset.

    Uses a zero-timeout (non-long) poll because this runs as a short cron job,
    not a daemon.
    """
    try:
        updates = _call(
            "getUpdates",
            data={
                "offset": offset,
                "timeout": 0,
                "limit": 50,
                "allowed_updates": json.dumps(["callback_query"]),
            },
        )
    except TelegramError as exc:
        logger.error("getUpdates failed: %s", exc)
        return [], offset

    callbacks: list[Callback] = []
    next_offset = offset

    for update in updates or []:
        next_offset = max(next_offset, int(update.get("update_id", 0)) + 1)
        query = update.get("callback_query")
        if not query:
            continue

        data = query.get("data", "")
        prefix, _, post_id = data.partition(":")
        action = {"ap": "approve", "rj": "reject"}.get(prefix)
        if not action or not post_id:
            logger.debug("Ignoring callback with data %r", data)
            continue

        message = query.get("message") or {}
        callbacks.append(
            Callback(
                id=query.get("id", ""),
                action=action,
                post_id=post_id,
                chat_id=str((message.get("chat") or {}).get("id", CFG.telegram_chat_id)),
                message_id=int(message.get("message_id", 0)),
            )
        )

    logger.info("Polled %d update(s), %d actionable", len(updates or []), len(callbacks))
    return callbacks, next_offset


def answer_callback(callback_id: str, text: str) -> None:
    """Acknowledge the tap so Telegram stops showing a loading spinner."""
    try:
        _call(
            "answerCallbackQuery",
            data={"callback_query_id": callback_id, "text": _truncate(text, 200)},
        )
    except TelegramError as exc:
        logger.warning("answerCallbackQuery failed: %s", exc)


def download_file(file_id: str, dest: Path) -> Path:
    """Fetch a previously uploaded card back out of Telegram."""
    info = _call("getFile", data={"file_id": file_id})
    remote = info.get("file_path")
    if not remote:
        raise TelegramError("getFile returned no file_path")

    response = get(f"{API_ROOT}/file/bot{CFG.telegram_bot_token}/{remote}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    logger.info("Downloaded card from Telegram (%.0f KB)", len(response.content) / 1024)
    return dest


def get_me() -> dict:
    """Identity check used by scripts/check_setup.py."""
    return _call("getMe")
