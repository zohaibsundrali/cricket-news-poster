"""Shared HTTP layer.

Every outbound call in the bot goes through here so that retries, timeouts and
the browser-ish User-Agent are configured in exactly one place. Callers only ever
see :class:`HttpError`, never a `requests` exception, which keeps the pipeline
modules free of any dependency on the HTTP client we happen to use.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

_BODY_LIMIT = 500
_RETRY_STATUSES = [429, 500, 502, 503, 504]
_RETRY_METHODS = frozenset({"GET", "POST"})

_session: requests.Session | None = None


class HttpError(RuntimeError):
    """Any HTTP failure, with the server's status and (truncated) body attached."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status: int | None = status
        self.body: str = (body or "")[:_BODY_LIMIT]


def _build_session() -> requests.Session:
    """Create the configured session; kept separate so tests can build a fresh one."""
    session = requests.Session()
    retry = Retry(
        total=HTTP_RETRIES,
        connect=HTTP_RETRIES,
        read=HTTP_RETRIES,
        status=HTTP_RETRIES,
        backoff_factor=1.5,
        status_forcelist=_RETRY_STATUSES,
        allowed_methods=_RETRY_METHODS,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def get_session() -> requests.Session:
    """Return the process-wide session so connections (and retries) are pooled."""
    global _session
    if _session is None:
        _session = _build_session()
        logger.debug("http session created (retries=%s, timeout=%ss)", HTTP_RETRIES, HTTP_TIMEOUT)
    return _session


def _body_of(response: requests.Response | None) -> str:
    """Best-effort read of a response body for error reporting; never raises."""
    if response is None:
        return ""
    try:
        return response.text or ""
    except Exception:  # pragma: no cover - decoding of a broken body
        return ""


def request(method: str, url: str, **kw: Any) -> requests.Response:
    """Perform a request with the shared session, raising HttpError on any failure."""
    kw.setdefault("timeout", HTTP_TIMEOUT)
    session = get_session()
    try:
        response = session.request(method.upper(), url, **kw)
    except requests.RequestException as exc:
        logger.debug("%s %s -> transport error: %s", method.upper(), url, exc)
        raise HttpError(f"{method.upper()} {url} failed: {exc}") from exc

    logger.debug("%s %s -> %s", method.upper(), url, response.status_code)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise HttpError(
            f"{method.upper()} {url} returned HTTP {response.status_code}",
            status=response.status_code,
            body=_body_of(response),
        ) from exc
    return response


def get(url: str, **kw: Any) -> requests.Response:
    """Shorthand for `request("GET", url, ...)`."""
    return request("GET", url, **kw)


def post(url: str, **kw: Any) -> requests.Response:
    """Shorthand for `request("POST", url, ...)`."""
    return request("POST", url, **kw)
