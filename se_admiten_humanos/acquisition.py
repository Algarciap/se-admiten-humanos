"""Source adapters and an auditable acquisition-to-notification runner."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Protocol

from .models import Listing
from .persistence import RentalStore, SourceRunRecord
from .pipeline import ListingPipeline, ProcessingResult
from .telegram import DeliveryAttempt


class SourceAdapter(Protocol):
    """A bounded source that emits normalized listings."""

    source_id: str

    def fetch(self) -> Iterable[Listing]: ...


class Notifier(Protocol):
    """Delivery boundary shared by Telegram and test implementations."""

    def deliver(self, result: ProcessingResult) -> tuple[DeliveryAttempt, ...]: ...


class AcquisitionRunError(RuntimeError):
    """A source run failed after its failure was persisted."""

    def __init__(self, record: SourceRunRecord) -> None:
        super().__init__(record.error)
        self.record = record


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """Compact outcome suitable for CLI output and monitoring."""

    source_id: str
    status: str
    run_id: int | None = None
    listings_seen: int = 0
    eligible_matches: int = 0
    notifications_sent: int = 0
    notifications_failed: int = 0
    notifications_skipped: int = 0
    duration_ms: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class JsonFileAdapter:
    """Strict local adapter for fixtures and manually exported listings."""

    def __init__(self, path: str | Path, source_id: str) -> None:
        self.path = Path(path)
        self.source_id = source_id.strip().casefold()
        if not self.source_id:
            raise ValueError("Source id cannot be empty")

    def fetch(self) -> Iterable[Listing]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as error:
            raise ValueError(f"Listing file does not exist: {self.path}") from error
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Listing file is invalid JSON at line {error.lineno}, column {error.colno}"
            ) from error

        records = payload.get("listings") if isinstance(payload, Mapping) else payload
        if not isinstance(records, list):
            raise ValueError("Listing JSON must be an array or an object with a listings array")

        allowed_fields = {field.name for field in fields(Listing)}
        for index, raw_record in enumerate(records, start=1):
            if not isinstance(raw_record, Mapping):
                raise ValueError(f"Listing {index} must be a JSON object")
            unknown_fields = sorted(set(raw_record) - allowed_fields)
            if unknown_fields:
                raise ValueError(
                    f"Listing {index} has unknown field(s): {', '.join(unknown_fields)}"
                )
            values = dict(raw_record)
            record_source = values.get("source_id", self.source_id)
            if not isinstance(record_source, str):
                raise ValueError(f"Listing {index} source_id must be text")
            if record_source.strip().casefold() != self.source_id:
                raise ValueError(
                    f"Listing {index} belongs to {record_source!r}, not {self.source_id!r}"
                )
            values["source_id"] = self.source_id
            available_from = values.get("available_from")
            if available_from is not None:
                if not isinstance(available_from, str):
                    raise ValueError(f"Listing {index} available_from must be an ISO date")
                try:
                    values["available_from"] = date.fromisoformat(available_from)
                except ValueError as error:
                    raise ValueError(
                        f"Listing {index} available_from must be an ISO date"
                    ) from error
            try:
                yield Listing(**values)
            except TypeError as error:
                raise ValueError(f"Listing {index} has invalid normalized fields") from error


class AcquisitionRunner:
    """Fetch, normalize, process, notify, and record one source execution."""

    def __init__(
        self,
        store: RentalStore,
        notifier: Notifier,
        *,
        max_listings_per_run: int = 500,
        stale_after_seconds: int = 900,
    ) -> None:
        if max_listings_per_run <= 0:
            raise ValueError("Per-run listing limit must be greater than zero")
        self.store = store
        self.notifier = notifier
        self.max_listings_per_run = max_listings_per_run
        self.stale_after_seconds = stale_after_seconds

    def run(self, adapter: SourceAdapter) -> AcquisitionResult:
        source_id = adapter.source_id.strip().casefold()
        claim = self.store.begin_source_run(source_id, self.stale_after_seconds)
        if claim is None:
            return AcquisitionResult(source_id=source_id, status="already_running")

        listings_seen = 0
        eligible_matches = 0
        sent = 0
        failed = 0
        skipped = 0
        pipeline = ListingPipeline(self.store)
        try:
            for position, listing in enumerate(adapter.fetch(), start=1):
                if position > self.max_listings_per_run:
                    raise ValueError(
                        f"Source exceeded the {self.max_listings_per_run}-listing run limit"
                    )
                if listing.source_id.strip().casefold() != source_id:
                    raise ValueError(
                        f"Adapter {source_id!r} emitted listing for {listing.source_id!r}"
                    )
                listings_seen += 1
                processed = pipeline.process(listing)
                eligible_matches += sum(
                    outcome.decision.eligible for outcome in processed.profile_outcomes
                )
                for attempt in self.notifier.deliver(processed):
                    if attempt.status == "sent":
                        sent += 1
                    elif attempt.status == "failed":
                        failed += 1
                    else:
                        skipped += 1
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"[:1000]
            record = self.store.finish_source_run(
                claim.run_id,
                "failed",
                listings_seen=listings_seen,
                eligible_matches=eligible_matches,
                notifications_sent=sent,
                notifications_failed=failed,
                notifications_skipped=skipped,
                error=detail,
            )
            raise AcquisitionRunError(record) from error

        status = "empty" if listings_seen == 0 else "succeeded"
        error = ""
        if failed:
            status = "failed"
            error = f"{failed} notification delivery attempt(s) failed"
        record = self.store.finish_source_run(
            claim.run_id,
            status,
            listings_seen=listings_seen,
            eligible_matches=eligible_matches,
            notifications_sent=sent,
            notifications_failed=failed,
            notifications_skipped=skipped,
            error=error,
        )
        return AcquisitionResult(
            source_id=record.source_id,
            status=record.status,
            run_id=record.run_id,
            listings_seen=record.listings_seen,
            eligible_matches=record.eligible_matches,
            notifications_sent=record.notifications_sent,
            notifications_failed=record.notifications_failed,
            notifications_skipped=record.notifications_skipped,
            duration_ms=record.duration_ms,
            error=record.error,
        )
