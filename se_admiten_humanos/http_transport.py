"""Small, bounded HTTP transport for conservative public-source polling."""

from __future__ import annotations

import random
import time
import urllib.request
from collections.abc import Callable, Collection
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit


_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _failure_name(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError) and isinstance(error.reason, BaseException):
        return type(error.reason).__name__
    return type(error).__name__


def _retry_after_seconds(headers: Message | None) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, 30.0))


def request_bytes(
    url: str,
    *,
    allowed_hostname: str,
    user_agent: str,
    source_label: str,
    stage: str,
    error_type: type[RuntimeError],
    timeout_seconds: float,
    max_bytes: int,
    accepted_content_types: Collection[str] = (),
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    opener: Callable[..., object] | None = None,
) -> bytes | None:
    """Fetch one first-party resource with conservative transient retries.

    A 404 is an expected missing-resource result. Timeouts, URL transport errors,
    rate limits, and selected server failures are retried. Contract failures such
    as an unexpected content type or oversized body fail immediately.
    """

    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_hostname:
        raise ValueError(f"Refusing an unexpected {source_label} URL")
    if attempts <= 0:
        raise ValueError("HTTP retry attempts must be greater than zero")
    if timeout_seconds <= 0 or max_bytes <= 0:
        raise ValueError("HTTP timeout and response limit must be greater than zero")

    open_request = urllib.request.urlopen if opener is None else opener
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            with open_request(  # type: ignore[attr-defined]
                request, timeout=timeout_seconds
            ) as response:
                if accepted_content_types:
                    content_type = response.headers.get_content_type()
                    if content_type not in accepted_content_types:
                        raise error_type(
                            f"{source_label} {stage} returned unexpected content type "
                            f"{content_type!r}"
                        )
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise error_type(
                        f"{source_label} {stage} response exceeded "
                        f"{max_bytes // 1_000_000} MB safety limit"
                    )
                return payload
        except HTTPError as error:
            if error.code == 404:
                return None
            if error.code not in _RETRYABLE_HTTP_STATUSES:
                raise error_type(
                    f"{source_label} {stage} returned HTTP {error.code}"
                ) from error
            last_error = error
            retry_after = _retry_after_seconds(error.headers)
        except (URLError, TimeoutError) as error:
            last_error = error
            retry_after = None

        if attempt < attempts:
            base_delay = min(4.0, 0.5 * (2 ** (attempt - 1)))
            delay = (
                retry_after
                if retry_after is not None
                else base_delay * (0.75 + jitter() * 0.5)
            )
            sleep(delay)

    assert last_error is not None
    raise error_type(
        f"{source_label} {stage} request failed after {attempts} attempts: "
        f"{_failure_name(last_error)}"
    ) from last_error
