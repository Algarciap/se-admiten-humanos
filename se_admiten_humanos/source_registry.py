"""Load and validate the machine-readable source registry."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REQUIRED_FIELDS = frozenset(
    {
        "id",
        "name",
        "kind",
        "priority",
        "base_url",
        "search_url",
        "coverage",
        "acquisition",
        "automation_status",
        "pet_signal",
        "direct_contact",
        "evidence_url",
        "notes",
        "verified_on",
    }
)


def _normalized_domain(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid source URL: {url}")
    return parsed.hostname.lower().removeprefix("www.")


class SourceRegistry:
    """Recognize supported domains and reject unregistered listing URLs."""

    def __init__(self, sources: Iterable[Mapping[str, Any]]) -> None:
        self.sources = tuple(dict(source) for source in sources)
        self._by_id: dict[str, dict[str, Any]] = {}
        self._domains: dict[str, str] = {}
        self._validate_and_index()

    @classmethod
    def from_file(cls, path: str | Path) -> "SourceRegistry":
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError("Source registry must contain a JSON array")
        return cls(payload)

    @property
    def count(self) -> int:
        return len(self.sources)

    def source_id_from_text(self, text: str) -> str:
        normalized = str(text or "").lower()
        for domain, source_id in self._domains.items():
            boundary_pattern = rf"(?<![a-z0-9-])(?:[a-z0-9-]+\.)*{re.escape(domain)}(?![a-z0-9-])"
            if re.search(boundary_pattern, normalized):
                return source_id
        return "unknown"

    def allowed_listing_url(self, url: str) -> bool:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().removeprefix("www.")
        return any(host == domain or host.endswith(f".{domain}") for domain in self._domains)

    def _validate_and_index(self) -> None:
        for index, source in enumerate(self.sources):
            missing = REQUIRED_FIELDS.difference(source)
            if missing:
                fields = ", ".join(sorted(missing))
                raise ValueError(f"Source at index {index} is missing: {fields}")

            empty = sorted(
                field
                for field in REQUIRED_FIELDS
                if source[field] is None
                or (isinstance(source[field], str) and not source[field].strip())
            )
            if empty:
                fields = ", ".join(empty)
                raise ValueError(f"Source at index {index} has empty values: {fields}")

            source_id = str(source["id"])
            if source_id in self._by_id:
                raise ValueError(f"Duplicate source id: {source_id}")

            domain = _normalized_domain(str(source["base_url"]))
            _normalized_domain(str(source["search_url"]))
            _normalized_domain(str(source["evidence_url"]))
            if domain in self._domains:
                raise ValueError(f"Duplicate source domain: {domain}")

            self._by_id[source_id] = source
            self._domains[domain] = source_id


def default_registry() -> SourceRegistry:
    repository_root = Path(__file__).resolve().parent.parent
    return SourceRegistry.from_file(repository_root / "data" / "sources.json")
