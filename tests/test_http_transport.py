"""Tests for bounded public-source HTTP retries and diagnostics."""

import socket
import unittest
from email.message import Message
from urllib.error import HTTPError, URLError

from se_admiten_humanos.http_transport import request_bytes


class TransportError(RuntimeError):
    pass


class Response:
    def __init__(self, payload: bytes, content_type: str = "text/html") -> None:
        self.payload = payload
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def fetch(opener, sleeps):
    return request_bytes(
        "https://example.test/sitemap.xml",
        allowed_hostname="example.test",
        user_agent="test-agent",
        source_label="Example",
        stage="sitemap discovery",
        error_type=TransportError,
        timeout_seconds=2,
        max_bytes=100,
        attempts=3,
        sleep=sleeps.append,
        jitter=lambda: 0.5,
        opener=opener,
    )


class HttpTransportTests(unittest.TestCase):
    def test_timeout_is_retried_and_context_is_preserved(self) -> None:
        outcomes = [
            URLError(socket.timeout()),
            URLError(socket.timeout()),
            Response(b"ok"),
        ]
        calls = 0
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            nonlocal calls
            outcome = outcomes[calls]
            calls += 1
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.assertEqual(fetch(opener, sleeps), b"ok")
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_exhausted_timeout_names_source_stage_and_attempts(self) -> None:
        def opener(*_args, **_kwargs):
            raise URLError(socket.timeout())

        with self.assertRaisesRegex(
            TransportError,
            "Example sitemap discovery request failed after 3 attempts: TimeoutError",
        ):
            fetch(opener, [])

    def test_rate_limit_honors_bounded_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "45"
        outcomes = [
            HTTPError("https://example.test", 429, "busy", headers, None),
            Response(b"ok"),
        ]
        sleeps: list[float] = []

        def opener(*_args, **_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.assertEqual(fetch(opener, sleeps), b"ok")
        self.assertEqual(sleeps, [30.0])

    def test_non_retryable_http_error_fails_immediately(self) -> None:
        calls = 0

        def opener(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise HTTPError("https://example.test", 403, "forbidden", None, None)

        with self.assertRaisesRegex(TransportError, "returned HTTP 403"):
            fetch(opener, [])
        self.assertEqual(calls, 1)

    def test_404_is_an_expected_missing_resource(self) -> None:
        def opener(*_args, **_kwargs):
            raise HTTPError("https://example.test", 404, "missing", None, None)

        self.assertIsNone(fetch(opener, []))


if __name__ == "__main__":
    unittest.main()
