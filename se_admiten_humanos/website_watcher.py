"""Shared, baseline-safe acquisition for public rental websites."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .models import Listing, WebsiteDiscovery
from .persistence import RentalStore
from .telegram import DeliveryAttempt


class WebsiteSource(Protocol):
    """A source with cheap discovery and detail fetches only for new identities."""

    source_id: str

    def discover(self) -> Iterable[WebsiteDiscovery]: ...

    def fetch_listing(self, discovery: WebsiteDiscovery) -> Listing | None: ...


@dataclass(frozen=True, slots=True)
class BaselineResult:
    source_id: str
    discoveries_recorded: int


class WebsiteDetailBatchError(RuntimeError):
    """One or more detail pages failed while later discoveries still ran."""


class WebsiteWatcherAdapter:
    """Turn a two-stage public website source into a normalized source adapter."""

    def __init__(
        self,
        store: RentalStore,
        source: WebsiteSource,
        *,
        max_detail_failures: int = 3,
    ) -> None:
        if max_detail_failures <= 0:
            raise ValueError("Detail failure limit must be greater than zero")
        self.store = store
        self.source = source
        self.source_id = source.source_id.strip().casefold()
        self.max_detail_failures = max_detail_failures
        self._detail_errors: list[str] = []
        if not self.source_id:
            raise ValueError("Source id cannot be empty")

    def initialize_baseline(self) -> BaselineResult:
        """Record current inventory without fetching details or sending alerts."""

        discoveries = tuple(self.source.discover())
        count = self.store.baseline_website_discoveries(
            self.source_id,
            discoveries,
        )
        return BaselineResult(self.source_id, count)

    def fetch(self) -> Iterable[Listing]:
        """Fetch detail pages only for discoveries not previously processed."""

        self._detail_errors = []
        discoveries = tuple(self.source.discover())
        pending = self.store.sync_website_discoveries(self.source_id, discoveries)
        for discovery in pending:
            try:
                listing = self.source.fetch_listing(discovery)
            except Exception as error:
                self._detail_errors.append(
                    f"{discovery.external_id}: {type(error).__name__}: {error}"[:500]
                )
                if len(self._detail_errors) >= self.max_detail_failures:
                    break
                continue
            if listing is None:
                self.store.mark_website_discovery_processed(
                    self.source_id,
                    discovery.external_id,
                )
                continue
            if listing.external_id.strip() != discovery.external_id.strip():
                raise ValueError(
                    "Website detail parser emitted an identity different from discovery"
                )
            yield listing

    def raise_detail_errors(self) -> None:
        """Fail the run after healthy pending details had a chance to complete."""

        if not self._detail_errors:
            return
        count = len(self._detail_errors)
        summary = "; ".join(self._detail_errors)
        raise WebsiteDetailBatchError(
            f"{count} detail request(s) failed; successful details continued: {summary}"
        )

    def acknowledge(
        self,
        listing: Listing,
        attempts: tuple[DeliveryAttempt, ...],
    ) -> None:
        """Acknowledge only completed work; delivery failures remain retryable."""

        should_retry = any(
            attempt.status == "failed"
            or (attempt.status == "skipped" and attempt.detail == "no chat configured")
            for attempt in attempts
        )
        if not should_retry:
            self.store.mark_website_discovery_processed(
                self.source_id,
                listing.external_id,
            )
