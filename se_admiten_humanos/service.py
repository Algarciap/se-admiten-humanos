"""Run the public onboarding site and rental worker in one Railway service."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .web import WebApplication, WebSettings
from .worker import WorkerSettings, run_worker


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        if self.path != "/health":
            super().log_message(format, *args)


def web_settings_from_environment(
    environment: Mapping[str, str] | None = None,
) -> tuple[WebSettings, int]:
    values = os.environ if environment is None else environment
    bot_username = values.get("TELEGRAM_BOT_USERNAME", "").strip()
    if not bot_username:
        raise ValueError("TELEGRAM_BOT_USERNAME is required for web onboarding")
    try:
        port = int(values.get("PORT", "8080"))
    except ValueError as error:
        raise ValueError("PORT must be a whole number") from error
    if not 1 <= port <= 65_535:
        raise ValueError("PORT must be between 1 and 65535")
    secure_value = values.get("WEB_COOKIE_SECURE", "true").strip().casefold()
    if secure_value not in {"true", "false"}:
        raise ValueError("WEB_COOKIE_SECURE must be true or false")
    status_page_token = values.get("STATUS_PAGE_TOKEN", "").strip()
    if status_page_token and len(status_page_token) < 24:
        raise ValueError("STATUS_PAGE_TOKEN must contain at least 24 characters")
    return (
        WebSettings(
            database_path=values.get("DATABASE_PATH", "rentals.sqlite3").strip(),
            bot_username=bot_username,
            secure_cookies=secure_value == "true",
            status_page_token=status_page_token,
        ),
        port,
    )


def run_web_server(settings: WebSettings, port: int) -> int:
    """Serve only the website, primarily for local preview."""

    with make_server(
        "0.0.0.0",
        port,
        WebApplication(settings),
        server_class=ThreadingWSGIServer,
        handler_class=QuietRequestHandler,
    ) as server:
        print(f"Web onboarding listening on http://0.0.0.0:{port}", flush=True)
        server.serve_forever()
    return 0


def run_combined_service(
    worker_settings: WorkerSettings,
    web_settings: WebSettings,
    port: int,
    environment: Mapping[str, str] | None = None,
) -> int:
    """Serve HTTP while the single-replica background watcher polls listings."""

    values = dict(os.environ if environment is None else environment)
    with make_server(
        "0.0.0.0",
        port,
        WebApplication(web_settings),
        server_class=ThreadingWSGIServer,
        handler_class=QuietRequestHandler,
    ) as server:
        worker_exit_code: list[int] = []

        def worker_target() -> None:
            worker_exit_code.append(run_worker(worker_settings, values))
            server.shutdown()

        worker_thread = threading.Thread(
            target=worker_target,
            name="rental-worker",
            daemon=True,
        )
        worker_thread.start()
        print(f"Combined service listening on http://0.0.0.0:{port}", flush=True)
        server.serve_forever()
        worker_thread.join(timeout=5)
        return worker_exit_code[0] if worker_exit_code else 1
