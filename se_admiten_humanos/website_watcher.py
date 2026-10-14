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


class WebsiteWatcherAdapter:
    """Turn a two-stage public website source into a normalized source adapter."""

    def __init__(self, store: RentalStore, source: WebsiteSource) -> None:
        self.store = store
        self.source = source
        self.source_id = source.source_id.strip().casefold()
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

        discoveries = tuple(self.source.discover())
        pending = self.store.sync_website_discoveries(self.source_id, discoveries)
        for discovery in pending:
            listing = self.source.fetch_listing(discovery)
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
