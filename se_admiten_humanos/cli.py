"""Command-line entry points for local diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Sequence

from .pet_policy import classify
from .persistence import RentalStore
from .profile_config import load_profile_bundle
from .source_registry import default_registry
from .telegram import TelegramBotClient, TelegramDeliveryError


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
    subparsers.add_parser(
        "test-telegram",
        help="send a test alert using TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
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
    return 2
