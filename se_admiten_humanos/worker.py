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
from .idealista import IdealistaApiClient, IdealistaApiSource, IdealistaSearchConfig, MADRID_CENTER
from .models import PropertyType
from .monitoring import (
    MonitoringNotifier,
    MonitoringSettings,
    SourceHealthMonitor,
    monitoring_notifier_from_environment,
)
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


def _idealista_source(
    environment: Mapping[str, str],
    profiles,
    client: IdealistaApiClient | None = None,
) -> IdealistaApiSource | None:
    api_key = environment.get("IDEALISTA_API_KEY", "").strip()
    api_secret = environment.get("IDEALISTA_API_SECRET", "").strip()
    if not api_key and not api_secret:
        return None
    if not api_key or not api_secret:
        raise WorkerConfigurationError(
            "IDEALISTA_API_KEY and IDEALISTA_API_SECRET must be configured together"
        )
    try:
        distance = int(environment.get("IDEALISTA_DISTANCE_METERS", "20000"))
        max_pages = int(environment.get("IDEALISTA_MAX_PAGES", "1"))
        poll_interval = int(
            environment.get("IDEALISTA_POLL_INTERVAL_SECONDS", "900")
        )
    except ValueError as error:
        raise WorkerConfigurationError(
            "Idealista numeric settings must contain whole numbers"
        ) from error
    requested_types = {value for profile in profiles for value in profile.accepted_property_types}
    api_property_types = tuple(
        api_type
        for property_type, api_type in (
            (PropertyType.HOME, "homes"),
            (PropertyType.ROOM, "bedrooms"),
        )
        if property_type in requested_types
    )
    try:
        config = IdealistaSearchConfig(
            center=environment.get("IDEALISTA_CENTER", MADRID_CENTER).strip(),
            distance_meters=distance,
            max_price_eur=max(
                profile.stretch_max_rent_eur or profile.max_rent_eur
                for profile in profiles
            ),
            since_date=environment.get("IDEALISTA_SINCE_DATE", "T").strip(),
            max_pages=max_pages,
            property_types=api_property_types,
        )
        return IdealistaApiSource(
            client or IdealistaApiClient(api_key, api_secret),
            config,
            minimum_poll_interval_seconds=poll_interval,
        )
    except ValueError as error:
        raise WorkerConfigurationError(str(error)) from error


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


def _is_unexpected_zero_error(error: Exception) -> bool:
    detail = str(error).casefold()
    return "zero listing" in detail or "cero viviendas" in detail


def _ensure_profiles(
    store: RentalStore,
    environment: Mapping[str, str],
) -> int:
    if store.load_enabled_search_profiles():
        return 0
    if store.connection.execute("select 1 from accounts union all select 1 from bootstrap_blocks limit 1").fetchone():
        # Registered users may not have configured searches yet, or deleted them.
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
    idealista_client: IdealistaApiClient | None = None,
    monitoring_notifier: MonitoringNotifier | None = None,
) -> WorkerBatchResult:
    """Run all enabled website sources, isolating one parser failure from the rest."""

    settings.validate()
    use_default_sources = sources is None
    website_sources = default_website_sources() if use_default_sources else sources
    if not website_sources:
        raise WorkerConfigurationError("At least one website source is required")
    source_ids = [source.source_id.strip().casefold() for source in website_sources]
    if any(not source_id for source_id in source_ids) or len(source_ids) != len(
        set(source_ids)
    ):
        raise WorkerConfigurationError("Website source ids must be non-empty and unique")
    with RentalStore(settings.database_path) as store:
        profiles_imported = _ensure_profiles(store, environment)
        try:
            monitoring_settings = MonitoringSettings.from_environment(environment)
            operator_notifier = (
                monitoring_notifier
                if monitoring_notifier is not None
                else monitoring_notifier_from_environment(environment)
            )
        except ValueError as error:
            raise WorkerConfigurationError(str(error)) from error
        monitor = SourceHealthMonitor(
            store,
            monitoring_settings,
            operator_notifier,
        )
        if use_default_sources:
            idealista = _idealista_source(
                environment,
                store.load_enabled_search_profiles(),
                idealista_client,
            )
            if idealista is not None:
                website_sources = (*website_sources, idealista)
        telegram_linking, telegram_linking_error = _poll_telegram_links(
            store, environment
        )
        results: list[WorkerCycleResult] = []
        errors: list[str] = []
        notifier: Notifier | None = None
        for website_source in website_sources:
            adapter = WebsiteWatcherAdapter(store, website_source)
            try:
                minimum_interval = getattr(
                    website_source, "minimum_poll_interval_seconds", None
                )
                if (
                    minimum_interval is not None
                    and store.website_source_initialized(adapter.source_id)
                    and not store.website_source_due(adapter.source_id, minimum_interval)
                ):
                    results.append(
                        WorkerCycleResult(
                            source_id=adapter.source_id,
                            status="not_due",
                        )
                    )
                    continue
                if not store.website_source_initialized(adapter.source_id):
                    baseline = adapter.initialize_baseline()
                    results.append(
                        WorkerCycleResult(
                            source_id=adapter.source_id,
                            status="baseline_created",
                            discoveries_recorded=baseline.discoveries_recorded,
                        )
                    )
                    if baseline.discoveries_recorded == 0:
                        errors.append(
                            f"{adapter.source_id}: unexpected zero listings during baseline"
                        )
                        errors.extend(
                            monitor.failure(
                                adapter.source_id,
                                "Discovery returned zero total listings during baseline",
                                unexpected_zero=True,
                            )
                        )
                    else:
                        errors.extend(
                            monitor.success(
                                adapter.source_id,
                                "baseline_created",
                                baseline.discoveries_recorded,
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
                discovery_count = store.website_source_discovery_count(
                    adapter.source_id
                )
                if discovery_count == 0:
                    errors.append(
                        f"{adapter.source_id}: unexpected zero total listings"
                    )
                    errors.extend(
                        monitor.failure(
                            adapter.source_id,
                            "Discovery returned zero total listings",
                            unexpected_zero=True,
                        )
                    )
                elif discovery_count is not None:
                    errors.extend(
                        monitor.success(
                            adapter.source_id,
                            acquisition.status,
                            discovery_count,
                        )
                    )
            except WorkerConfigurationError:
                raise
            except Exception as error:
                detail = f"{adapter.source_id}: {type(error).__name__}: {error}"[:1000]
                errors.append(detail)
                errors.extend(
                    monitor.failure(
                        adapter.source_id,
                        detail,
                        unexpected_zero=_is_unexpected_zero_error(error),
                    )
                )
        errors.extend(
            monitor.check_stale(
                tuple(source.source_id.strip().casefold() for source in website_sources)
            )
        )
        if errors:
            status = "partial_failure"
        elif all(result.status == "baseline_created" for result in results):
            status = "baseline_created"
        elif all(result.status in {"empty", "not_due"} for result in results):
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
    idealista_client: IdealistaApiClient | None = None
    api_key = values.get("IDEALISTA_API_KEY", "").strip()
    api_secret = values.get("IDEALISTA_API_SECRET", "").strip()
    if api_key and api_secret and cycle is run_worker_sources_cycle:
        idealista_client = IdealistaApiClient(api_key, api_secret)
    consecutive_failures = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        started = time.monotonic()
        try:
            if cycle is run_worker_sources_cycle:
                result = run_worker_sources_cycle(
                    settings,
                    values,
                    idealista_client=idealista_client,
                )
            else:
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
