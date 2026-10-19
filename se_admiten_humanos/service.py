"""Run the public onboarding site and rental worker in one Railway service."""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from urllib.parse import urlsplit

from .web import WebApplication, WebSettings
from .accounts import MailSettings
from .worker import WorkerSettings, run_telegram_link_worker, run_worker


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-") -> None:
        # Email capabilities are carried in URLs; never put query strings in logs.
        if urlsplit(self.path).path != "/health":
            self.log_message('"%s %s" %s %s', self.command, urlsplit(self.path).path, code, size)

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
    accounts_value = values.get("ACCOUNTS_ENABLED", "false").strip().casefold()
    if accounts_value not in {"true", "false"}:
        raise ValueError("ACCOUNTS_ENABLED must be true or false")
    idealista_email_webhook_token = values.get(
        "IDEALISTA_EMAIL_WEBHOOK_TOKEN", ""
    ).strip()
    telegram_bot_token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if idealista_email_webhook_token:
        if len(idealista_email_webhook_token) < 32:
            raise ValueError("IDEALISTA_EMAIL_WEBHOOK_TOKEN must contain at least 32 characters")
        if not telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for Idealista email alerts")
    mail = None
    if accounts_value == "true":
        tls_value = values.get("SMTP_IMPLICIT_TLS", "false").strip().casefold()
        if tls_value not in {"true", "false"}:
            raise ValueError("SMTP_IMPLICIT_TLS must be true or false")
        try:
            smtp_port = int(values.get("SMTP_PORT", "587"))
        except ValueError:
            raise ValueError("SMTP_PORT must be a whole number") from None
        mail = MailSettings(
            public_url=values.get("PUBLIC_BASE_URL", "").strip(),
            host=values.get("SMTP_HOST", "").strip(),
            sender=values.get("SMTP_FROM", "").strip(),
            port=smtp_port,
            username=values.get("SMTP_USERNAME", ""),
            password=values.get("SMTP_PASSWORD", ""),
            implicit_tls=tls_value == "true",
            brevo_api_key=values.get("BREVO_API_KEY", ""),
        )
        mail.validate(secure_value == "true")
    return (
        WebSettings(
            database_path=values.get("DATABASE_PATH", "rentals.sqlite3").strip(),
            bot_username=bot_username,
            secure_cookies=secure_value == "true",
            status_page_token=status_page_token,
            accounts_enabled=accounts_value == "true",
            mail=mail,
            telegram_bot_token=telegram_bot_token,
            idealista_email_webhook_token=idealista_email_webhook_token,
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
        stop_event = threading.Event()
        worker_environment = dict(values)
        # Account-link commands have their own responsive poller below. Keeping the
        # username out of the property worker prevents duplicate Bot API reads.
        worker_environment.pop("TELEGRAM_BOT_USERNAME", None)

        def worker_target() -> None:
            worker_exit_code.append(run_worker(worker_settings, worker_environment))
            stop_event.set()
            server.shutdown()

        def telegram_link_target() -> None:
            run_telegram_link_worker(
                worker_settings,
                values,
                stop_event=stop_event,
            )

        worker_thread = threading.Thread(
            target=worker_target,
            name="rental-worker",
            daemon=True,
        )
        worker_thread.start()
        telegram_link_thread = threading.Thread(
            target=telegram_link_target,
            name="telegram-link-worker",
            daemon=True,
        )
        telegram_link_thread.start()
        print(f"Combined service listening on http://0.0.0.0:{port}", flush=True)
        try:
            server.serve_forever()
        finally:
            stop_event.set()
        worker_thread.join(timeout=5)
        telegram_link_thread.join(timeout=5)
        return worker_exit_code[0] if worker_exit_code else 1
