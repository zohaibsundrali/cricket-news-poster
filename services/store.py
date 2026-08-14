"""The bot's whole database: one JSON file committed back to the repo.

There is no server and no real database, so `data/state.json` is both the state
and the audit log. Two consequences drive the design here: the file must stay
human-readable in a git diff (hence indent=2 / ensure_ascii=False so Urdu shows up
as Urdu), and it must never be able to brick the bot — a corrupt or missing file
degrades to an empty state with a warning instead of an exception.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from config import (
    DATA_DIR,
    POSTS_RETENTION,
    RECENT_TITLES_WINDOW,
    SEEN_RETENTION,
    STATE_PATH,
)
from services.timeutil import from_iso, iso, now_utc

logger = logging.getLogger(__name__)

STATE_VERSION = 1

STATUSES: frozenset[str] = frozenset(
    {
        "pending_approval",
        "posted",
        "rejected",
        "failed",
        "expired",
        "skipped",
    }
)

# Declared field order of a post record; also the key order written to disk.
POST_FIELDS: tuple[str, ...] = (
    "id",
    "url_hash",
    "source_url",
    "source_name",
    "title_en",
    "headline_ur",
    "summary_ur",
    "caption_ur",
    "fb_caption",
    "hashtags",
    "category_ur",
    "telegram_message_id",
    "telegram_file_id",
    "fb_post_id",
    "status",
    "error_stage",
    "error_msg",
    "created_at",
    "updated_at",
    "posted_at",
)

_LIST_FIELDS = frozenset({"summary_ur", "hashtags"})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def new_post_id() -> str:
    """Sortable, collision-resistant post id, e.g. "20260814T1030Z-a1b2c3"."""
    return f"{now_utc().strftime('%Y%m%dT%H%MZ')}-{secrets.token_hex(3)}"


def empty_state() -> dict[str, Any]:
    """A brand-new state document; also the fallback when the file is unusable."""
    return {"version": STATE_VERSION, "telegram_offset": 0, "seen": {}, "posts": []}


def _sort_key(value: str | None) -> datetime:
    """Order records/hashes by ISO timestamp, treating junk as oldest-possible."""
    return from_iso(value) or _EPOCH


class Store:
    """Read/modify/write wrapper around `data/state.json`."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data if data is not None else empty_state()
        self._normalise()

    # ------------------------------------------------------------------ io

    @classmethod
    def load(cls) -> Store:
        """Load state from disk, falling back to an empty state on any problem."""
        try:
            raw = STATE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("No state file at %s; starting fresh", STATE_PATH)
            return cls()
        except OSError as exc:
            logger.warning("Cannot read state file %s (%s); starting fresh", STATE_PATH, exc)
            return cls()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Corrupt state file %s (%s); starting fresh", STATE_PATH, exc)
            return cls()

        if not isinstance(data, dict):
            logger.warning("State file %s is not a JSON object; starting fresh", STATE_PATH)
            return cls()

        store = cls(data)
        logger.debug(
            "Loaded state: %d seen, %d posts", len(store.data["seen"]), len(store.data["posts"])
        )
        return store

    def save(self) -> None:
        """Write atomically (tmp + os.replace) so an interrupted run cannot truncate state."""
        self._normalise()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, STATE_PATH)
        logger.debug(
            "Saved state to %s (%d seen, %d posts)",
            STATE_PATH,
            len(self.data["seen"]),
            len(self.data["posts"]),
        )

    # ---------------------------------------------------------------- seen

    def is_seen(self, url_hash: str) -> bool:
        """True when this article URL has already been through the pipeline."""
        return bool(url_hash) and url_hash in self.data["seen"]

    def mark_seen(self, url_hash: str) -> None:
        """Record a URL hash and prune the oldest so state.json stays small."""
        if not url_hash:
            return
        self.data["seen"][url_hash] = iso(now_utc())
        self._prune_seen()

    def _prune_seen(self) -> None:
        seen: dict[str, str] = self.data["seen"]
        if len(seen) <= SEEN_RETENTION:
            return
        kept = sorted(seen.items(), key=lambda kv: _sort_key(kv[1]), reverse=True)[:SEEN_RETENTION]
        # Re-sort oldest-first so appends stay at the bottom of the git diff.
        self.data["seen"] = dict(sorted(kept, key=lambda kv: _sort_key(kv[1])))

    # --------------------------------------------------------------- posts

    def add_post(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append a post record (filling defaults), mark its URL seen, and prune."""
        entry = self._complete(record)
        self.data["posts"].append(entry)
        self._prune_posts()
        self.mark_seen(entry.get("url_hash") or "")
        logger.debug("Added post %s (status=%s)", entry.get("id"), entry.get("status"))
        return entry

    def _prune_posts(self) -> None:
        posts: list[dict[str, Any]] = self.data["posts"]
        if len(posts) <= POSTS_RETENTION:
            return
        kept = sorted(posts, key=lambda p: _sort_key(p.get("created_at")), reverse=True)
        self.data["posts"] = list(reversed(kept[:POSTS_RETENTION]))

    def get_post(self, post_id: str) -> dict[str, Any] | None:
        """Find a post record by id, or None."""
        if not post_id:
            return None
        for record in self.data["posts"]:
            if record.get("id") == post_id:
                return record
        return None

    def update_post(self, post_id: str, **fields: Any) -> dict[str, Any] | None:
        """Patch a record in place and refresh updated_at; returns it, or None if unknown."""
        record = self.get_post(post_id)
        if record is None:
            logger.warning("update_post: unknown post id %r", post_id)
            return None
        status = fields.get("status")
        if status is not None and status not in STATUSES:
            logger.warning("update_post: unknown status %r on %s", status, post_id)
        record.update(fields)
        record["updated_at"] = iso(now_utc())
        return record

    def pending(self) -> list[dict[str, Any]]:
        """Posts still awaiting a Telegram decision, oldest first (expire these first)."""
        waiting = [p for p in self.data["posts"] if p.get("status") == "pending_approval"]
        return sorted(waiting, key=lambda p: _sort_key(p.get("created_at")))

    def recent_titles(self, limit: int = RECENT_TITLES_WINDOW) -> list[str]:
        """Recent English headlines, newest first — the near-duplicate check reads this."""
        if limit <= 0:
            return []
        newest = sorted(self.data["posts"], key=lambda p: _sort_key(p.get("created_at")), reverse=True)
        titles: list[str] = []
        for record in newest:
            title = (record.get("title_en") or "").strip()
            if title:
                titles.append(title)
            if len(titles) >= limit:
                break
        return titles

    def counts(self) -> dict[str, int]:
        """Status histogram for the Telegram daily summary; every status is always present."""
        tally: dict[str, int] = {status: 0 for status in sorted(STATUSES)}
        for record in self.data["posts"]:
            status = str(record.get("status") or "unknown")
            tally[status] = tally.get(status, 0) + 1
        return tally

    # ------------------------------------------------------------ internals

    def _complete(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return the record with every documented key present, in declared order."""
        stamp = iso(now_utc())
        entry: dict[str, Any] = {}
        for key in POST_FIELDS:
            if key in record:
                entry[key] = record[key]
            elif key in _LIST_FIELDS:
                entry[key] = []
            else:
                entry[key] = None
        entry["id"] = entry["id"] or new_post_id()
        entry["status"] = entry["status"] or "pending_approval"
        entry["created_at"] = entry["created_at"] or stamp
        entry["updated_at"] = entry["updated_at"] or stamp
        # Preserve any extra keys a caller added rather than silently dropping them.
        for key, value in record.items():
            if key not in entry:
                entry[key] = value
        if entry["status"] not in STATUSES:
            logger.warning("add_post: unknown status %r on %s", entry["status"], entry["id"])
        return entry

    def _normalise(self) -> None:
        """Repair a partially-valid state document so callers can assume the shape."""
        data = self.data
        if not isinstance(data.get("version"), int):
            data["version"] = STATE_VERSION
        if not isinstance(data.get("telegram_offset"), int):
            data["telegram_offset"] = 0
        seen = data.get("seen")
        if not isinstance(seen, dict):
            if seen is not None:
                logger.warning("State 'seen' was %s; resetting", type(seen).__name__)
            data["seen"] = {}
        else:
            data["seen"] = {str(k): str(v) for k, v in seen.items()}
        posts = data.get("posts")
        if not isinstance(posts, list):
            if posts is not None:
                logger.warning("State 'posts' was %s; resetting", type(posts).__name__)
            data["posts"] = []
        else:
            data["posts"] = [p for p in posts if isinstance(p, dict)]
