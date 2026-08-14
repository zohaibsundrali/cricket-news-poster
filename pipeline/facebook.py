"""Facebook Graph API publishing.

This is the only module that talks to Facebook. It deliberately does **not**
blindly retry: the Graph API answers a policy rejection with an ordinary 4xx,
and re-sending a rejected post accumulates strikes against the Page. Only the
handful of error codes Facebook documents as transient are ever re-attempted.

Access tokens are redacted from every log line and every raised message.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from config import CFG
from services.http import HttpError, get, post

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"

# Token invalid / expired / missing permission. Retrying cannot help; the human
# has to re-issue the token or grant the scope.
AUTH_ERROR_CODES = frozenset({190, 102, 200, 10})

# Transient: rate limits, temporary API trouble, "please retry" codes.
RETRYABLE_ERROR_CODES = frozenset({1, 2, 4, 17, 32, 613})

# How many times a *retryable* Graph error is re-attempted. Non-retryable
# errors are raised on the first response, always.
MAX_PUBLISH_ATTEMPTS = 3


class FacebookError(RuntimeError):
    """A Graph API failure, with the Facebook error code attached."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        subcode: int | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(_redact(message))
        self.code: int | None = code
        self.subcode: int | None = subcode
        self.status: int | None = status

    @property
    def is_auth_error(self) -> bool:
        """True when the token is invalid or a permission is missing."""
        return self.code in AUTH_ERROR_CODES

    @property
    def is_retryable(self) -> bool:
        """True only for the transient/throttling codes Facebook documents."""
        return self.code in RETRYABLE_ERROR_CODES


def _secrets() -> list[str]:
    return [s for s in (CFG.fb_page_access_token, CFG.fb_app_secret, CFG.fb_app_id) if len(s) > 6]


def _redact(text: str) -> str:
    """Replace any configured secret appearing in `text` with '***'."""
    out = str(text)
    for secret in _secrets():
        out = out.replace(secret, "***")
    return out


def _parse_graph_error(body: str, status: int | None) -> FacebookError:
    """Turn a Graph API error body into a FacebookError. Never raises."""
    code: int | None = None
    subcode: int | None = None
    message = f"Graph API returned HTTP {status}" if status else "Graph API call failed"
    ftype = ""
    try:
        payload = json.loads(body or "{}")
        err = payload.get("error") or {}
        if isinstance(err, dict):
            raw_code = err.get("code")
            raw_sub = err.get("error_subcode")
            code = int(raw_code) if isinstance(raw_code, (int, str)) and str(raw_code).isdigit() else None
            subcode = int(raw_sub) if isinstance(raw_sub, (int, str)) and str(raw_sub).isdigit() else None
            ftype = str(err.get("type") or "")
            if err.get("message"):
                message = str(err["message"])
    except (ValueError, TypeError):  # body was not JSON
        if body:
            message = f"{message}: {body[:200]}"
    detail = f"facebook error code={code} subcode={subcode} type={ftype or '-'}: {message}"
    return FacebookError(detail, code=code, subcode=subcode, status=status)


def _error_from_http(exc: HttpError) -> FacebookError:
    if exc.body:
        return _parse_graph_error(exc.body, exc.status)
    return FacebookError(f"facebook request failed: {exc}", status=exc.status)


def _json_of(response: Any) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise FacebookError(f"facebook returned a non-JSON body: {response.text[:200]}") from exc
    if not isinstance(data, dict):
        raise FacebookError(f"facebook returned an unexpected body: {str(data)[:200]}")
    return data


def publish_photo(image_path: Path, caption: str) -> str:
    """Upload `image_path` to the Page with `caption`; return the new post id.

    Raises FacebookError on any Graph failure. Non-retryable failures (anything
    outside RETRYABLE_ERROR_CODES) are raised after a single attempt so that a
    rejected post is never sent twice.
    """
    image_path = Path(image_path)
    if not image_path.exists():
        raise FacebookError(f"image not found: {image_path}")

    size = image_path.stat().st_size
    url = f"{GRAPH_BASE}/{CFG.fb_api_version}/{CFG.fb_page_id}/photos"
    logger.info(
        "publishing photo to page %s (image=%s, %.0f KB, caption=%d chars)",
        CFG.fb_page_id,
        image_path.name,
        size / 1024,
        len(caption),
    )

    last: FacebookError | None = None
    for attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
        try:
            with image_path.open("rb") as fh:
                response = post(
                    url,
                    files={"source": (image_path.name, fh, "image/png")},
                    data={
                        "caption": caption,
                        "access_token": CFG.fb_page_access_token,
                        "published": "true",
                    },
                )
        except HttpError as exc:
            err = _error_from_http(exc)
            if not err.is_retryable or attempt == MAX_PUBLISH_ATTEMPTS:
                logger.error("facebook publish failed permanently: %s", _redact(str(err)))
                raise err from exc
            last = err
            logger.warning(
                "facebook publish attempt %d/%d hit a transient error: %s",
                attempt,
                MAX_PUBLISH_ATTEMPTS,
                _redact(str(err)),
            )
            continue

        data = _json_of(response)
        if data.get("error"):
            err = _parse_graph_error(json.dumps(data), 200)
            if not err.is_retryable or attempt == MAX_PUBLISH_ATTEMPTS:
                logger.error("facebook publish rejected: %s", _redact(str(err)))
                raise err
            last = err
            continue

        post_id = str(data.get("post_id") or data.get("id") or "")
        if not post_id:
            raise FacebookError(f"facebook accepted the upload but returned no id: {str(data)[:200]}")
        logger.info("published facebook post %s", post_id)
        return post_id

    raise last or FacebookError("facebook publish failed for an unknown reason")


def check_token() -> dict:
    """Inspect the Page access token. Never raises.

    Returns {"valid", "expires_at", "days_left", "scopes", "type", "reason"}.
    `expires_at == 0` means a never-expiring System User token.
    """
    result: dict = {
        "valid": False,
        "expires_at": None,
        "days_left": None,
        "scopes": [],
        "type": "",
        "reason": "",
    }
    if not CFG.fb_page_access_token:
        result["reason"] = "FB_PAGE_ACCESS_TOKEN is not set"
        return result
    if not (CFG.fb_app_id and CFG.fb_app_secret):
        result["reason"] = "FB_APP_ID / FB_APP_SECRET are required to inspect the token"
        return result

    url = f"{GRAPH_BASE}/{CFG.fb_api_version}/debug_token"
    params = {
        "input_token": CFG.fb_page_access_token,
        "access_token": f"{CFG.fb_app_id}|{CFG.fb_app_secret}",
    }
    try:
        response = get(url, params=params)
        payload = response.json()
    except HttpError as exc:
        err = _error_from_http(exc)
        result["reason"] = _redact(str(err))
        return result
    except Exception as exc:  # pragma: no cover - defensive: must never raise
        result["reason"] = _redact(f"token inspection failed: {exc}")
        return result

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        result["reason"] = _redact(f"unexpected debug_token response: {str(payload)[:200]}")
        return result

    result["valid"] = bool(data.get("is_valid"))
    result["type"] = str(data.get("type") or "")
    scopes = data.get("scopes")
    result["scopes"] = [str(s) for s in scopes] if isinstance(scopes, list) else []

    expires_at = data.get("expires_at")
    if isinstance(expires_at, int):
        result["expires_at"] = expires_at
        if expires_at == 0:
            result["days_left"] = None
            result["reason"] = "token never expires (System User token)"
        else:
            result["days_left"] = max(0, int((expires_at - time.time()) // 86400))

    if not result["valid"] and not result["reason"]:
        err_block = data.get("error")
        if isinstance(err_block, dict) and err_block.get("message"):
            result["reason"] = _redact(str(err_block["message"]))
        else:
            result["reason"] = "token is not valid"
    return result


def verify_setup() -> dict:
    """Confirm the token can actually see the configured Page. Never raises."""
    result = {"ok": False, "page_name": "", "reason": ""}
    if not (CFG.fb_page_id and CFG.fb_page_access_token):
        result["reason"] = "FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN are not set"
        return result

    url = f"{GRAPH_BASE}/{CFG.fb_api_version}/{CFG.fb_page_id}"
    try:
        response = get(
            url,
            params={"fields": "name,id", "access_token": CFG.fb_page_access_token},
        )
        data = _json_of(response)
    except HttpError as exc:
        result["reason"] = _redact(str(_error_from_http(exc)))
        return result
    except FacebookError as exc:
        result["reason"] = _redact(str(exc))
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result["reason"] = _redact(f"page lookup failed: {exc}")
        return result

    if data.get("error"):
        result["reason"] = _redact(str(_parse_graph_error(json.dumps(data), 200)))
        return result
    if str(data.get("id") or "") != CFG.fb_page_id:
        result["reason"] = (
            f"token resolved page id {data.get('id')!r}, expected {CFG.fb_page_id!r}"
        )
        result["page_name"] = str(data.get("name") or "")
        return result

    result["ok"] = True
    result["page_name"] = str(data.get("name") or "")
    return result
