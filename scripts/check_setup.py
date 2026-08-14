"""One-off preflight: run this after adding your secrets.

    python scripts/check_setup.py

Every check prints a line. Required checks failing means the bot cannot run and
the script exits non-zero; warnings (feeds with no items, a token expiring soon)
are printed but do not fail the run.

This file is a human-facing CLI, so it uses print() rather than logging.
Secret *values* are never printed — only whether they are set.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import feedparser  # noqa: E402

from config import CFG, FEEDS, FONT_DIR, FONT_FILES  # noqa: E402
from pipeline.facebook import check_token, verify_setup  # noqa: E402
from services.http import HttpError, get, post  # noqa: E402

OK = "✅"
BAD = "❌"
WARN = "⚠️"

REQUIRED_ENV = [
    "FB_PAGE_ID",
    "FB_PAGE_ACCESS_TOKEN",
    "FB_APP_ID",
    "FB_APP_SECRET",
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

# env var -> Config attribute
ENV_TO_ATTR = {
    "FB_PAGE_ID": "fb_page_id",
    "FB_PAGE_ACCESS_TOKEN": "fb_page_access_token",
    "FB_APP_ID": "fb_app_id",
    "FB_APP_SECRET": "fb_app_secret",
    "GEMINI_API_KEY": "gemini_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}

TOKEN_WARN_DAYS = 14

_failures: list[str] = []
_warnings: list[str] = []


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


def ok(text: str) -> None:
    print(f"  {OK} {text}")


def fail(text: str) -> None:
    print(f"  {BAD} {text}")
    _failures.append(text)


def warn(text: str) -> None:
    print(f"  {WARN} {text}")
    _warnings.append(text)


# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------
def check_env() -> bool:
    heading("1. Environment variables")
    missing = [name for name, attr in ENV_TO_ATTR.items() if not getattr(CFG, attr, "")]
    for name in REQUIRED_ENV:
        if name in missing:
            fail(f"{name} is missing")
        else:
            ok(f"{name} is set")
    if missing:
        print()
        print("     Add these as GitHub Actions Secrets (Settings -> Secrets and")
        print("     variables -> Actions), or put them in a local .env file.")
        return False
    return True


# ---------------------------------------------------------------------------
# 2. Facebook page + token
# ---------------------------------------------------------------------------
def check_facebook() -> None:
    heading("2. Facebook Page")
    setup = verify_setup()
    if setup.get("ok"):
        ok(f"Token can see Page: \"{setup.get('page_name')}\" (id {CFG.fb_page_id})")
        print("     ^ confirm this is the Page you want to post to.")
    else:
        fail(f"Cannot read the Page: {setup.get('reason')}")

    heading("3. Facebook token")
    info = check_token()
    if not info.get("valid"):
        fail(f"Page access token is not valid: {info.get('reason') or 'unknown reason'}")
        return

    ok(f"Token is valid (type: {info.get('type') or 'unknown'})")

    scopes = info.get("scopes") or []
    if scopes:
        ok(f"Scopes: {', '.join(scopes)}")
        for needed in ("pages_manage_posts", "pages_read_engagement"):
            if needed not in scopes:
                warn(f"Scope '{needed}' is not listed — publishing may fail")
    else:
        warn("Token reported no scopes")

    expires_at = info.get("expires_at")
    days = info.get("days_left")
    if expires_at == 0:
        ok("Token NEVER EXPIRES (System User token) — nothing to renew.")
    elif days is None:
        warn("Token expiry is unknown — check it manually in Graph API Explorer")
    elif days < TOKEN_WARN_DAYS:
        warn(
            f"Token expires in {days} day(s)! Generate a long-lived / System User "
            "token now, or the bot will stop posting."
        )
    else:
        ok(f"Token expires in {days} day(s)")


# ---------------------------------------------------------------------------
# 4. Telegram
# ---------------------------------------------------------------------------
def check_telegram() -> None:
    heading("4. Telegram")
    base = f"https://api.telegram.org/bot{CFG.telegram_bot_token}"
    try:
        me = get(f"{base}/getMe").json()
    except (HttpError, ValueError) as exc:
        fail(f"getMe failed — is TELEGRAM_BOT_TOKEN correct? ({_clean(exc)})")
        return

    if not me.get("ok"):
        fail(f"getMe rejected: {me.get('description', me)}")
        return
    username = (me.get("result") or {}).get("username", "?")
    ok(f"Bot is @{username}")

    try:
        sent = post(
            f"{base}/sendMessage",
            data={
                "chat_id": CFG.telegram_chat_id,
                "text": "✅ cricket-news-poster: setup check — this chat is wired up correctly.",
            },
        ).json()
    except (HttpError, ValueError) as exc:
        fail(
            f"Could not send a test message to chat {CFG.telegram_chat_id} "
            f"({_clean(exc)}). Send /start to the bot first, and check TELEGRAM_CHAT_ID."
        )
        return

    if sent.get("ok"):
        ok(f"Test message delivered to chat {CFG.telegram_chat_id} — check Telegram now.")
    else:
        fail(f"sendMessage rejected: {sent.get('description', sent)}")


# ---------------------------------------------------------------------------
# 5. Gemini
# ---------------------------------------------------------------------------
def check_gemini() -> None:
    heading("5. Gemini API")
    try:
        response = get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": CFG.gemini_api_key},
        )
        payload = response.json()
    except (HttpError, ValueError) as exc:
        fail(f"Gemini key rejected or unreachable ({_clean(exc)})")
        return

    models = payload.get("models") or []
    names = [str(m.get("name", "")).removeprefix("models/") for m in models if isinstance(m, dict)]
    if not names:
        fail("Gemini returned no models — is GEMINI_API_KEY enabled for the API?")
        return
    ok(f"API key works ({len(names)} models visible)")

    wanted = CFG.gemini_model
    if wanted in names:
        ok(f"Configured model '{wanted}' is available")
    else:
        fail(f"Configured model '{wanted}' is NOT available. Set GEMINI_MODEL to one of:")
        for name in sorted(n for n in names if n.startswith("gemini-")):
            print(f"       - {name}")


# ---------------------------------------------------------------------------
# 6. Feeds
# ---------------------------------------------------------------------------
def check_feeds() -> None:
    heading("6. News feeds")
    for feed in FEEDS:
        name = feed["name"]
        try:
            response = get(feed["url"])
            parsed = feedparser.parse(response.content)
            count = len(parsed.entries)
        except (HttpError, ValueError) as exc:
            warn(f"{name}: unreachable ({_clean(exc)})")
            continue
        if count == 0:
            warn(f"{name}: 0 items — the feed may have moved")
        else:
            ok(f"{name}: {count} items")


# ---------------------------------------------------------------------------
# 7. Fonts
# ---------------------------------------------------------------------------
def check_fonts() -> None:
    heading("7. Fonts")
    for key, (filename, _url) in FONT_FILES.items():
        path = FONT_DIR / filename
        if path.exists() and path.stat().st_size > 50 * 1024:
            ok(f"{key}: {filename} ({path.stat().st_size / 1024:.0f} KB)")
        else:
            fail(f"{key}: {filename} missing — run `python scripts/fetch_fonts.py`")


def _clean(exc: object) -> str:
    """Shorten an exception for display and strip any secret that leaked into it."""
    text = str(exc)
    for secret in (
        CFG.telegram_bot_token,
        CFG.gemini_api_key,
        CFG.fb_page_access_token,
        CFG.fb_app_secret,
    ):
        if len(secret) > 6:
            text = text.replace(secret, "***")
    return text[:200]


def main() -> int:
    print("=" * 62)
    print(" cricket-news-poster — setup check")
    print("=" * 62)

    if check_env():
        check_facebook()
        check_telegram()
        check_gemini()
    else:
        print()
        print("Skipping API checks until every required secret is set.")
    check_feeds()
    check_fonts()

    heading("Summary")
    if _failures:
        print(f"  {BAD} {len(_failures)} required check(s) failed:")
        for item in _failures:
            print(f"       - {item}")
    else:
        print(f"  {OK} All required checks passed.")
    if _warnings:
        print(f"  {WARN} {len(_warnings)} warning(s):")
        for item in _warnings:
            print(f"       - {item}")
    print()
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
