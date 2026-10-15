"""Command-line entry points for local diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from .acquisition import (
    AcquisitionResult,
    AcquisitionRunError,
    AcquisitionRunner,
    JsonFileAdapter,
)
from .alquiler_seguro import AlquilerSeguroSource
from .idealista import (
    MADRID_CENTER,
    IdealistaApiAdapter,
    IdealistaApiClient,
    IdealistaSearchConfig,
)
from .pet_policy import classify
from .persistence import RentalStore
from .profile_config import load_profile_bundle
from .source_registry import default_registry
from .telegram import (
    TelegramBotClient,
    TelegramDeliveryError,
    notifier_from_environment,
)
from .telegram_linking import create_telegram_link
from .website_watcher import WebsiteWatcherAdapter
from .worker import WorkerConfigurationError, WorkerSettings, run_worker


def _load_environment_file(path: str | Path) -> None:
    """Load simple KEY=VALUE entries without evaluating shell syntax."""

    environment_path = Path(path)
    if not environment_path.exists():
        return
    for line_number, raw_line in enumerate(
        environment_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid environment entry on line {line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid environment name on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _report_source_failure(error: AcquisitionRunError) -> int:
    record = error.record
    print(
        json.dumps(
            {
                "source_id": record.source_id,
                "status": record.status,
                "run_id": record.run_id,
                "listings_seen": record.listings_seen,
                "eligible_matches": record.eligible_matches,
                "notifications_sent": record.notifications_sent,
                "notifications_failed": record.notifications_failed,
                "notifications_skipped": record.notifications_skipped,
                "duration_ms": record.duration_ms,
                "error": record.error,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stderr,
    )
    return 1


def _report_source_result(result: AcquisitionResult) -> int:
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if result.status == "already_running":
        return 3
    return 1 if result.status == "failed" else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="se-admiten-humanos")
    subparsers = parser.add_subparsers(dest="command", required=True)
    classify_parser = subparsers.add_parser("classify", help="classify pet-policy text")
    classify_parser.add_argument(
        "text",
        nargs="?",
        help="text to classify; stdin is used if omitted",
    )
    subparsers.add_parser(
        "validate-sources",
        help="validate the source registry and report its source count",
    )
    import_parser = subparsers.add_parser(
        "import-profiles",
        help="import a private user/profile bundle into the local database",
    )
    import_parser.add_argument("--file", required=True, help="private profile JSON file")
    import_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    telegram_parser = subparsers.add_parser(
        "test-telegram",
        help="send a test alert using TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
    )
    telegram_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    link_parser = subparsers.add_parser(
        "create-telegram-link",
        help="create a short-lived, single-use Telegram account link",
    )
    link_parser.add_argument("--user-id", required=True, help="application user id")
    link_parser.add_argument(
        "--bot-username",
        default="",
        help="Telegram bot username (or set TELEGRAM_BOT_USERNAME)",
    )
    link_parser.add_argument(
        "--ttl-minutes",
        type=int,
        default=10,
        help="link lifetime in minutes (default: 10; maximum: 1440)",
    )
    link_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    link_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    run_parser = subparsers.add_parser(
        "run-json-source",
        help="process a normalized JSON source and send matching Telegram alerts",
    )
    run_parser.add_argument("--file", required=True, help="normalized listing JSON")
    run_parser.add_argument("--source-id", required=True, help="source adapter id")
    run_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    run_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    run_parser.add_argument(
        "--max-listings",
        type=int,
        default=500,
        help="hard safety limit per run (default: 500)",
    )
    idealista_parser = subparsers.add_parser(
        "run-idealista",
        help="query the approved Idealista Search API and send matching alerts",
    )
    idealista_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    idealista_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    idealista_parser.add_argument(
        "--center",
        default=MADRID_CENTER,
        help=f"latitude,longitude search center (default: {MADRID_CENTER})",
    )
    idealista_parser.add_argument(
        "--distance",
        type=int,
        default=20_000,
        help="search radius in meters (default: 20000)",
    )
    idealista_parser.add_argument(
        "--since-date",
        choices=("T", "Y", "W", "M"),
        default="T",
        help="official API recency filter (default: T)",
    )
    idealista_parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="API page limit per run (default: 1 until quota is known)",
    )
    idealista_parser.add_argument(
        "--max-listings",
        type=int,
        default=500,
        help="hard processing limit per run (default: 500)",
    )
    alquiler_baseline_parser = subparsers.add_parser(
        "baseline-alquiler-seguro",
        help="record current Alquiler Seguro inventory without sending alerts",
    )
    alquiler_baseline_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    alquiler_parser = subparsers.add_parser(
        "run-alquiler-seguro",
        help="check Alquiler Seguro for unseen listings and send matching alerts",
    )
    alquiler_parser.add_argument(
        "--database",
        default="rentals.sqlite3",
        help="SQLite database path (default: rentals.sqlite3)",
    )
    alquiler_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    alquiler_parser.add_argument(
        "--max-listings",
        type=int,
        default=50,
        help="hard processing limit per run (default: 50)",
    )
    worker_parser = subparsers.add_parser(
        "run-worker",
        help="continuously poll configured sources for a hosted deployment",
    )
    worker_parser.add_argument(
        "--env-file",
        default=".env",
        help="environment file to load when present (default: .env)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "classify":
        text = args.text if args.text is not None else sys.stdin.read()
        decision = classify(text)
        print(
            json.dumps(
                {
                    "status": decision.status.value,
                    "source": decision.source,
                    "evidence": decision.evidence,
                    "confidence": decision.confidence,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "validate-sources":
        registry = default_registry()
        print(f"Validated {registry.count} sources.")
        return 0
    if args.command == "import-profiles":
        bundle = load_profile_bundle(args.file)
        with RentalStore(args.database) as store:
            store.save_user(bundle.user)
            for renter_profile in bundle.renter_profiles:
                store.save_renter_profile(renter_profile)
            for search_profile in bundle.search_profiles:
                store.save_search_profile(search_profile)
        print(
            f"Imported user {bundle.user.id}: "
            f"{len(bundle.renter_profiles)} renter profile(s), "
            f"{len(bundle.search_profiles)} search profile(s)."
        )
        return 0
    if args.command == "test-telegram":
        try:
            _load_environment_file(args.env_file)
        except ValueError as error:
            print(f"Environment file is invalid: {error}", file=sys.stderr)
            return 2
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            print(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required",
                file=sys.stderr,
            )
            return 2
        try:
            message_id = TelegramBotClient(token).send_message(
                chat_id,
                "✅ Se admiten humanos puede enviarte alertas.",
            )
        except (ValueError, TelegramDeliveryError) as error:
            print(f"Telegram test failed: {error}", file=sys.stderr)
            return 1
        print(f"Telegram test message sent (message id {message_id}).")
        return 0
    if args.command == "create-telegram-link":
        try:
            _load_environment_file(args.env_file)
            bot_username = args.bot_username or os.environ.get(
                "TELEGRAM_BOT_USERNAME", ""
            )
            with RentalStore(args.database) as store:
                link = create_telegram_link(
                    store,
                    args.user_id,
                    bot_username,
                    ttl=timedelta(minutes=args.ttl_minutes),
                )
        except (ValueError, OSError) as error:
            print(f"Telegram link could not be created: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "user_id": link.user_id,
                    "url": link.url,
                    "expires_at": link.expires_at.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "run-json-source":
        try:
            _load_environment_file(args.env_file)
            with RentalStore(args.database) as store:
                notifier = notifier_from_environment(store)
                runner = AcquisitionRunner(
                    store,
                    notifier,
                    max_listings_per_run=args.max_listings,
                )
                result = runner.run(JsonFileAdapter(args.file, args.source_id))
        except AcquisitionRunError as error:
            return _report_source_failure(error)
        except (ValueError, OSError) as error:
            print(f"Source run could not start: {error}", file=sys.stderr)
            return 2
        return _report_source_result(result)
    if args.command == "run-idealista":
        try:
            _load_environment_file(args.env_file)
            api_key = os.environ.get("IDEALISTA_API_KEY", "")
            api_secret = os.environ.get("IDEALISTA_API_SECRET", "")
            if not api_key or not api_secret:
                raise ValueError(
                    "IDEALISTA_API_KEY and IDEALISTA_API_SECRET are required"
                )
            with RentalStore(args.database) as store:
                profiles = store.load_enabled_search_profiles()
                if not profiles:
                    raise ValueError("At least one enabled search profile is required")
                config = IdealistaSearchConfig(
                    center=args.center,
                    distance_meters=args.distance,
                    max_price_eur=max(profile.max_rent_eur for profile in profiles),
                    since_date=args.since_date,
                    max_pages=args.max_pages,
                )
                notifier = notifier_from_environment(store)
                runner = AcquisitionRunner(
                    store,
                    notifier,
                    max_listings_per_run=args.max_listings,
                )
                adapter = IdealistaApiAdapter(
                    IdealistaApiClient(api_key, api_secret),
                    config,
                )
                result = runner.run(adapter)
        except AcquisitionRunError as error:
            return _report_source_failure(error)
        except (ValueError, OSError) as error:
            print(f"Source run could not start: {error}", file=sys.stderr)
            return 2
        return _report_source_result(result)
    if args.command == "baseline-alquiler-seguro":
        try:
            with RentalStore(args.database) as store:
                result = WebsiteWatcherAdapter(
                    store,
                    AlquilerSeguroSource(),
                ).initialize_baseline()
        except (ValueError, OSError, RuntimeError) as error:
            print(f"Website baseline failed: {error}", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "source_id": result.source_id,
                    "status": "baseline_created",
                    "discoveries_recorded": result.discoveries_recorded,
                    "alerts_sent": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "run-alquiler-seguro":
        try:
            _load_environment_file(args.env_file)
            with RentalStore(args.database) as store:
                notifier = notifier_from_environment(store)
                runner = AcquisitionRunner(
                    store,
                    notifier,
                    max_listings_per_run=args.max_listings,
                )
                result = runner.run(
                    WebsiteWatcherAdapter(store, AlquilerSeguroSource())
                )
        except AcquisitionRunError as error:
            return _report_source_failure(error)
        except (ValueError, OSError, RuntimeError) as error:
            print(f"Source run could not start: {error}", file=sys.stderr)
            return 2
        return _report_source_result(result)
    if args.command == "run-worker":
        try:
            _load_environment_file(args.env_file)
            settings = WorkerSettings.from_environment()
            return run_worker(settings)
        except WorkerConfigurationError as error:
            print(f"Worker configuration is invalid: {error}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("Worker stopped.", file=sys.stderr)
            return 130
    return 2
