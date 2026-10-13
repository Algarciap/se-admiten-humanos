"""Command-line entry points for local diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .pet_policy import classify
from .source_registry import default_registry


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="se-admiten-humanos")
    subparsers = parser.add_subparsers(dest="command", required=True)
    classify_parser = subparsers.add_parser("classify", help="classify pet-policy text")
    classify_parser.add_argument("text", nargs="?", help="text to classify; stdin is used if omitted")
    subparsers.add_parser(
        "validate-sources",
        help="validate the source registry and report its source count",
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
                    "should_notify": decision.should_notify,
                    "should_contact": decision.should_contact,
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
    return 2
