"""Long-running, baseline-safe worker for an SQLite-backed deployment."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .acquisition import AcquisitionResult, AcquisitionRunner, Notifier
from .alquiler_seguro import AlquilerSeguroSource
from .nestar import NestarSource
from .persistence import RentalStore
from .profile_config import ProfileBundle, load_profile_bundle_json
from .redpiso import RedpisoSource
from .telegram import TelegramBotClient, notifier_from_environment
from .telegram_linking import TelegramLinkPollResult, process_telegram_link_updates
from .tecnocasa import TecnocasaSource
from .website_watcher import WebsiteSource, WebsiteWatcherAdapter


class WorkerConfigurationError(ValueError):
    """A deployment setting requires operator intervention."""


class NotifierFactory(Protocol):
    def __call__(
        self,
        store: RentalStore,
        environment: Mapping[str, str] | None = None,
    ) -> Notifier: ...


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    database_path: str = "rentals.sqlite3"
    poll_interval_seconds: int = 180
    max_listings_per_run: int = 50
    max_consecutive_failures: int = 3

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "WorkerSettings":
        values = os.environ if environment is None else environment
        try:
            settings = cls(
                database_path=values.get("DATABASE_PATH", "rentals.sqlite3").strip(),
                poll_interval_seconds=int(
                    values.get("POLL_INTERVAL_SECONDS", "180").strip()
                ),
                max_listings_per_run=int(
                    values.get("MAX_LISTINGS_PER_RUN", "50").strip()
                ),
                max_consecutive_failures=int(
                    values.get("MAX_CONSECUTIVE_FAILURES", "3").strip()
                ),
            )
        except ValueError as error:
            raise WorkerConfigurationError(
                "Worker numeric settings must contain whole numbers"
            ) from error
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.database_path:
            raise WorkerConfigurationError("DATABASE_PATH cannot be empty")
        if self.poll_interval_seconds < 60:
            raise WorkerConfigurationError(
                "POLL_INTERVAL_SECONDS must be at least 60"
            )
        if self.max_listings_per_run <= 0:
            raise WorkerConfigurationError(
                "MAX_LISTINGS_PER_RUN must be greater than zero"
            )
        if self.max_consecutive_failures <= 0:
            raise WorkerConfigurationError(
                "MAX_CONSECUTIVE_FAILURES must be greater than zero"
            )
        parent = Path(self.database_path).expanduser().parent
        if not parent.exists():
            raise WorkerConfigurationError(
                f"Database directory does not exist: {parent}"
            )


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    source_id: str
    status: str
    profiles_imported: int = 0
    discoveries_recorded: int = 0
    telegram_linking: TelegramLinkPollResult | None = None
    telegram_linking_error: str = ""
    acquisition: AcquisitionResult | None = None

    def to_dict(self) -> dict[str, object]:
        values = asdict(self)
        if self.acquisition is not None:
            values["acquisition"] = self.acquisition.to_dict()
        return values


@dataclass(frozen=True, slots=True)
class WorkerBatchResult:
    """One hosted polling cycle across every enabled website source."""

    status: str
    sources: tuple[WorkerCycleResult, ...]
    profiles_imported: int = 0
    telegram_linking: TelegramLinkPollResult | None = None
    telegram_linking_error: str = ""
    source_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "profiles_imported": self.profiles_imported,
            "telegram_linking": (
                asdict(self.telegram_linking)
                if self.telegram_linking is not None
                else None
            ),
            "telegram_linking_error": self.telegram_linking_error,
            "sources": [source.to_dict() for source in self.sources],
            "source_errors": list(self.source_errors),
        }


def default_website_sources() -> tuple[WebsiteSource, ...]:
    """Return enabled first-party sources in a stable operational order."""

    return (
        AlquilerSeguroSource(),
        TecnocasaSource(),
        RedpisoSource(),
        NestarSource(),
    )


def _poll_telegram_links(
    store: RentalStore,
    environment: Mapping[str, str],
) -> tuple[TelegramLinkPollResult | None, str]:
    """Poll when a bot username opts the deployment into account linking."""

    if not environment.get("TELEGRAM_BOT_USERNAME", "").strip():
        return None, ""
    token = environment.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise WorkerConfigurationError(
            "TELEGRAM_BOT_TOKEN is required when TELEGRAM_BOT_USERNAME is configured"
        )
    try:
        return process_telegram_link_updates(store, TelegramBotClient(token)), ""
    except ValueError as error:
        raise WorkerConfigurationError(str(error)) from error
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"[:500]


def _save_bundle(store: RentalStore, bundle: ProfileBundle) -> None:
    store.save_user(bundle.user)
    for renter_profile in bundle.renter_profiles:
        store.save_renter_profile(renter_profile)
    for search_profile in bundle.search_profiles:
        store.save_search_profile(search_profile)


def _ensure_profiles(
    store: RentalStore,
    environment: Mapping[str, str],
) -> int:
    if store.load_enabled_search_profiles():
        return 0
    raw_bundle = environment.get("PROFILE_BUNDLE_JSON", "")
    if not raw_bundle.strip():
        raise WorkerConfigurationError(
            "The empty database needs PROFILE_BUNDLE_JSON with at least one enabled search"
        )
    try:
        bundle = load_profile_bundle_json(raw_bundle)
        _save_bundle(store, bundle)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkerConfigurationError(f"PROFILE_BUNDLE_JSON is invalid: {error}") from error
    if not store.load_enabled_search_profiles():
        raise WorkerConfigurationError(
            "PROFILE_BUNDLE_JSON must contain at least one enabled search"
        )
    return len(bundle.search_profiles)


def run_worker_cycle(
    settings: WorkerSettings,
    environment: Mapping[str, str],
    *,
    source: WebsiteSource | None = None,
    notifier_factory: NotifierFactory = notifier_from_environment,
) -> WorkerCycleResult:
    """Run one bootstrap or acquisition cycle against persistent state."""

    settings.validate()
    website_source = AlquilerSeguroSource() if source is None else source
    with RentalStore(settings.database_path) as store:
        profiles_imported = _ensure_profiles(store, environment)
        telegram_linking, telegram_linking_error = _poll_telegram_links(
            store, environment
        )
        adapter = WebsiteWatcherAdapter(store, website_source)
        if not store.website_source_initialized(adapter.source_id):
            baseline = adapter.initialize_baseline()
            return WorkerCycleResult(
                source_id=adapter.source_id,
                status="baseline_created",
                profiles_imported=profiles_imported,
                discoveries_recorded=baseline.discoveries_recorded,
                telegram_linking=telegram_linking,
                telegram_linking_error=telegram_linking_error,
            )
        try:
            notifier = notifier_factory(store, environment)
        except ValueError as error:
            raise WorkerConfigurationError(str(error)) from error
        acquisition = AcquisitionRunner(
            store,
            notifier,
            max_listings_per_run=settings.max_listings_per_run,
        ).run(adapter)
        if acquisition.status not in {"empty", "succeeded"}:
            raise RuntimeError(
                f"Source cycle finished with unhealthy status {acquisition.status!r}"
            )
        return WorkerCycleResult(
            source_id=adapter.source_id,
            status=acquisition.status,
            profiles_imported=profiles_imported,
            telegram_linking=telegram_linking,
            telegram_linking_error=telegram_linking_error,
            acquisition=acquisition,
        )


def run_worker_sources_cycle(
    settings: WorkerSettings,
    environment: Mapping[str, str],
    *,
    sources: tuple[WebsiteSource, ...] | None = None,
    notifier_factory: NotifierFactory = notifier_from_environment,
) -> WorkerBatchResult:
    """Run all enabled website sources, isolating one parser failure from the rest."""

    settings.validate()
    website_sources = default_website_sources() if sources is None else sources
    if not website_sources:
        raise WorkerConfigurationError("At least one website source is required")
    source_ids = [source.source_id.strip().casefold() for source in website_sources]
    if any(not source_id for source_id in source_ids) or len(source_ids) != len(
        set(source_ids)
    ):
        raise WorkerConfigurationError("Website source ids must be non-empty and unique")
    with RentalStore(settings.database_path) as store:
        profiles_imported = _ensure_profiles(store, environment)
        telegram_linking, telegram_linking_error = _poll_telegram_links(
            store, environment
        )
        results: list[WorkerCycleResult] = []
        errors: list[str] = []
        notifier: Notifier | None = None
        for website_source in website_sources:
            adapter = WebsiteWatcherAdapter(store, website_source)
            try:
                if not store.website_source_initialized(adapter.source_id):
                    baseline = adapter.initialize_baseline()
                    results.append(
                        WorkerCycleResult(
                            source_id=adapter.source_id,
                            status="baseline_created",
                            discoveries_recorded=baseline.discoveries_recorded,
                        )
                    )
                    continue
                if notifier is None:
                    try:
                        notifier = notifier_factory(store, environment)
                    except ValueError as error:
                        raise WorkerConfigurationError(str(error)) from error
                acquisition = AcquisitionRunner(
                    store,
                    notifier,
                    max_listings_per_run=settings.max_listings_per_run,
                ).run(adapter)
                if acquisition.status not in {"empty", "succeeded"}:
                    raise RuntimeError(
                        f"source finished with unhealthy status {acquisition.status!r}"
                    )
                results.append(
                    WorkerCycleResult(
                        source_id=adapter.source_id,
                        status=acquisition.status,
                        acquisition=acquisition,
                    )
                )
            except WorkerConfigurationError:
                raise
            except Exception as error:
                errors.append(
                    f"{adapter.source_id}: {type(error).__name__}: {error}"[:1000]
                )
        if errors:
            status = "partial_failure"
        elif all(result.status == "baseline_created" for result in results):
            status = "baseline_created"
        elif all(result.status == "empty" for result in results):
            status = "empty"
        else:
            status = "succeeded"
        return WorkerBatchResult(
            status=status,
            sources=tuple(results),
            profiles_imported=profiles_imported,
            telegram_linking=telegram_linking,
            telegram_linking_error=telegram_linking_error,
            source_errors=tuple(errors),
        )


def run_worker(
    settings: WorkerSettings,
    environment: Mapping[str, str] | None = None,
    *,
    max_cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    cycle: Callable[
        [WorkerSettings, Mapping[str, str]], WorkerCycleResult | WorkerBatchResult
    ] = run_worker_sources_cycle,
) -> int:
    """Poll forever, failing after repeated errors so the platform restarts us."""

    values = dict(os.environ if environment is None else environment)
    consecutive_failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        started = time.monotonic()
        try:
            result = cycle(settings, values)
            if (
                isinstance(result, WorkerBatchResult)
                and result.status == "partial_failure"
            ):
                print(
                    json.dumps(result.to_dict(), ensure_ascii=False),
                    file=sys.stderr,
                    flush=True,
                )
                raise RuntimeError("; ".join(result.source_errors))
        except WorkerConfigurationError:
            raise
        except Exception as error:
            consecutive_failures += 1
            print(
                json.dumps(
                    {
                        "status": "worker_cycle_failed",
                        "failure": consecutive_failures,
                        "error": f"{type(error).__name__}: {error}"[:1000],
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            if consecutive_failures >= settings.max_consecutive_failures:
                return 1
        else:
            consecutive_failures = 0
            print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
        cycles += 1
        if max_cycles is None or cycles < max_cycles:
            elapsed = time.monotonic() - started
            sleep(max(0.0, settings.poll_interval_seconds - elapsed))
    return 0
