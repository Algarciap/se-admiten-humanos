"""SQLite persistence for users, searches, listings, matches, and delivery state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Listing, Pet, PetDecision, PropertyType, RentalDuration, RenterProfile, User, WebsiteDiscovery
from .search_profile import SearchDecision, SearchProfile


SCHEMA_VERSION = 11

_TELEGRAM_CHAT_ID = re.compile(r"-?[1-9][0-9]*")


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents.casefold()).strip()


def canonicalize_url(url: str) -> str:
    """Remove tracking details while retaining the listing's stable identity."""

    try:
        parsed = urlsplit(url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    host = hostname.casefold().removeprefix("www.")
    normalized_port = ""
    if port and not (parsed.scheme == "http" and port == 80):
        normalized_port = "" if parsed.scheme == "https" and port == 443 else f":{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
        )
    )
    return urlunsplit(
        (parsed.scheme.casefold(), f"{host}{normalized_port}", path, query, "")
    )


def _raw_identities(listing: Listing) -> tuple[str, ...]:
    identities: list[str] = []
    if listing.external_id.strip():
        identities.append(
            f"source-id|{listing.source_id.casefold()}|{listing.external_id.strip()}"
        )

    canonical_url = canonicalize_url(listing.url)
    if canonical_url:
        identities.append(f"url|{canonical_url}")

    address = _normalize_text(listing.address)
    municipality = _normalize_text(listing.municipality)
    if (
        address
        and municipality
        and listing.price_eur is not None
        and listing.bedrooms is not None
        and listing.area_sqm is not None
    ):
        identities.append(
            (
                f"property|{address}|{municipality}|{listing.price_eur}|"
                f"{listing.bedrooms}|{listing.area_sqm}"
            )
        )

    if not identities:
        raise ValueError(
            "A listing needs an external_id, valid URL, or complete property facts"
        )
    return tuple(dict.fromkeys(identities))


def _digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def listing_fingerprint(listing: Listing) -> str:
    """Return the listing's strongest stable identity fingerprint."""

    return _digest(_raw_identities(listing)[0])


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _listing_payload(listing: Listing) -> str:
    values = asdict(listing)
    if listing.available_from is not None:
        values["available_from"] = listing.available_from.isoformat()
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Result of storing one listing observation."""

    listing_id: int
    fingerprint: str
    is_new: bool


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Result of evaluating one shared listing for one search profile."""

    match_id: int
    listing_id: int
    search_profile_id: str
    eligible: bool
    is_new: bool


@dataclass(frozen=True, slots=True)
class SourceRunRecord:
    """Auditable state for one acquisition-source execution."""

    run_id: int
    source_id: str
    status: str
    started_at: str
    finished_at: str | None
    listings_seen: int
    eligible_matches: int
    notifications_sent: int
    notifications_failed: int
    notifications_skipped: int
    error: str

    @property
    def duration_ms(self) -> int | None:
        """Return elapsed wall time when the run has completed."""

        if self.finished_at is None:
            return None
        started = datetime.fromisoformat(self.started_at)
        finished = datetime.fromisoformat(self.finished_at)
        return max(0, round((finished - started).total_seconds() * 1000))


@dataclass(frozen=True, slots=True)
class SourceHealthRecord:
    """Durable operational health for one remote listing source."""

    source_id: str
    last_attempt_at: str
    last_success_at: str | None
    last_status: str
    last_listing_count: int | None
    consecutive_failures: int
    last_error: str
    alert_state: str
    alerted_at: str | None
    updated_at: str


class RentalStore:
    """Persist global listings and user-isolated matching and delivery state."""

    def __init__(self, path: str | Path = "rentals.sqlite3") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("pragma foreign_keys = on")
        self._initialize_schema()

    def __enter__(self) -> "RentalStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize_schema(self) -> None:
        version = self.connection.execute("pragma user_version").fetchone()[0]
        if version == 0:
            self._create_schema_v2()
            version = 2
        elif version == 1:
            self._migrate_v1_to_v2()
            version = 2
        if version == 2:
            self._migrate_v2_to_v3()
            version = 3
        if version == 3:
            self._migrate_v3_to_v4()
            version = 4
        if version == 4:
            self._migrate_v4_to_v5()
            version = 5
        if version == 5:
            self._migrate_v5_to_v6()
            version = 6
        if version == 6:
            self._migrate_v6_to_v7()
            version = 7
        if version == 7:
            self._migrate_v7_to_v8()
            version = 8
        if version == 8:
            self._migrate_v8_to_v9()
            version = 9
        if version == 9:
            self._migrate_v9_to_v10()
            version = 10
        if version == 10:
            self._migrate_v10_to_v11()
            version = 11
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}"
            )

    def _create_schema_v2(self) -> None:
        self.connection.executescript(
            """
            create table if not exists users (
                id text primary key,
                display_name text not null,
                email text not null default '',
                created_at text not null,
                updated_at text not null
            );

            create table if not exists renter_profiles (
                id text primary key,
                user_id text not null references users(id) on delete cascade,
                label text not null,
                sender_name text not null,
                renter_name text not null,
                monthly_income_eur integer,
                job text not null default '',
                contract_type text not null default '',
                created_at text not null,
                updated_at text not null,
                unique (user_id, label)
            );

            create table if not exists pets (
                id text primary key,
                renter_profile_id text not null
                    references renter_profiles(id) on delete cascade,
                species text not null,
                count integer not null check (count > 0),
                name text not null default '',
                notes text not null default ''
            );

            create table if not exists search_profiles (
                id text primary key,
                user_id text not null references users(id) on delete cascade,
                renter_profile_id text
                    references renter_profiles(id) on delete set null,
                name text not null,
                max_rent_eur integer not null check (max_rent_eur > 0),
                municipality text not null,
                acceptable_districts_json text not null default '[]',
                preferred_min_bedrooms integer not null,
                allow_studios integer not null,
                allow_unknown_bedrooms integer not null,
                furnished integer,
                move_in_asap integer not null,
                move_in_by text,
                requires_pet_acceptance integer not null,
                enabled integer not null,
                created_at text not null,
                updated_at text not null,
                unique (user_id, name)
            );

            create table if not exists listings (
                id integer primary key,
                fingerprint text not null unique,
                source_id text not null,
                external_id text not null,
                canonical_url text not null,
                payload_json text not null,
                first_seen_at text not null,
                last_seen_at text not null
            );

            create table if not exists listing_identities (
                identity text primary key,
                listing_id integer not null references listings(id) on delete cascade,
                identity_kind text not null
            );

            create table if not exists listing_matches (
                id integer primary key,
                listing_id integer not null references listings(id) on delete cascade,
                search_profile_id text not null
                    references search_profiles(id) on delete cascade,
                eligible integer not null,
                pet_status text not null,
                pet_evidence text not null,
                rejection_reasons_json text not null,
                preference_notes_json text not null,
                first_evaluated_at text not null,
                last_evaluated_at text not null,
                unique (listing_id, search_profile_id)
            );

            create table if not exists notifications (
                match_id integer not null
                    references listing_matches(id) on delete cascade,
                channel text not null,
                status text not null check (status in ('pending', 'sent', 'failed')),
                attempts integer not null default 1,
                last_error text not null default '',
                updated_at text not null,
                primary key (match_id, channel)
            );

            create index if not exists listing_matches_profile_idx
                on listing_matches(search_profile_id, eligible);

            pragma user_version = 2;
            """
        )
        self.connection.commit()

    def _migrate_v1_to_v2(self) -> None:
        with self.connection:
            self.connection.execute(
                "alter table notifications rename to legacy_notifications_v1"
            )
        self._create_schema_v2()

    def _migrate_v2_to_v3(self) -> None:
        self.connection.executescript(
            """
            create table if not exists source_runs (
                id integer primary key,
                source_id text not null,
                status text not null
                    check (status in ('running', 'succeeded', 'empty', 'failed')),
                started_at text not null,
                finished_at text,
                listings_seen integer not null default 0 check (listings_seen >= 0),
                eligible_matches integer not null default 0
                    check (eligible_matches >= 0),
                notifications_sent integer not null default 0
                    check (notifications_sent >= 0),
                notifications_failed integer not null default 0
                    check (notifications_failed >= 0),
                notifications_skipped integer not null default 0
                    check (notifications_skipped >= 0),
                error text not null default ''
            );

            create unique index if not exists source_runs_one_running_idx
                on source_runs(source_id)
                where status = 'running';

            create index if not exists source_runs_source_started_idx
                on source_runs(source_id, started_at desc);

            pragma user_version = 3;
            """
        )
        self.connection.commit()

    def _migrate_v3_to_v4(self) -> None:
        self.connection.executescript(
            """
            create table if not exists website_source_state (
                source_id text primary key,
                initialized_at text not null,
                last_scan_at text not null,
                last_discovery_count integer not null
                    check (last_discovery_count > 0)
            );

            create table if not exists website_discoveries (
                source_id text not null
                    references website_source_state(source_id) on delete cascade,
                external_id text not null,
                url text not null,
                source_last_modified text not null default '',
                first_seen_at text not null,
                last_seen_at text not null,
                processed_at text,
                primary key (source_id, external_id)
            );

            create index if not exists website_discoveries_pending_idx
                on website_discoveries(source_id, processed_at, first_seen_at);

            pragma user_version = 4;
            """
        )
        self.connection.commit()

    def _migrate_v4_to_v5(self) -> None:
        self.connection.executescript(
            """
            create table if not exists telegram_destinations (
                user_id text primary key
                    references users(id) on delete cascade,
                chat_id text not null unique,
                enabled integer not null check (enabled in (0, 1)),
                created_at text not null,
                updated_at text not null
            );

            pragma user_version = 5;
            """
        )
        self.connection.commit()

    def _migrate_v5_to_v6(self) -> None:
        self.connection.executescript(
            """
            create table if not exists telegram_link_tokens (
                token_hash text primary key,
                user_id text not null references users(id) on delete cascade,
                expires_at text not null,
                used_at text,
                created_at text not null
            );

            create index if not exists telegram_link_tokens_user_idx
                on telegram_link_tokens(user_id, created_at desc);

            create table if not exists telegram_bot_state (
                id integer primary key check (id = 1),
                next_update_id integer not null check (next_update_id >= 0),
                updated_at text not null
            );

            pragma user_version = 6;
            """
        )
        self.connection.commit()

    def _migrate_v6_to_v7(self) -> None:
        self.connection.executescript(
            """
            create table if not exists web_sessions (
                token_hash text primary key,
                user_id text not null references users(id) on delete cascade,
                expires_at text not null,
                created_at text not null,
                last_seen_at text not null
            );

            create index if not exists web_sessions_user_idx
                on web_sessions(user_id, expires_at);

            pragma user_version = 7;
            """
        )
        self.connection.commit()

    def _migrate_v7_to_v8(self) -> None:
        self.connection.executescript(
            """
            alter table search_profiles add column accepted_rental_durations_json
                text not null default '["long_term"]';

            pragma user_version = 8;
            """
        )
        self.connection.commit()

    def _migrate_v8_to_v9(self) -> None:
        self.connection.executescript(
            """
            alter table search_profiles add column accepted_property_types_json
                text not null default '["home"]';

            pragma user_version = 9;
            """
        )
        self.connection.commit()

    def _migrate_v9_to_v10(self) -> None:
        self.connection.executescript(
            """
            alter table search_profiles add column urgent
                integer not null default 0 check (urgent in (0, 1));
            alter table search_profiles add column stretch_max_rent_eur integer;

            pragma user_version = 10;
            """
        )
        self.connection.commit()

    def _migrate_v10_to_v11(self) -> None:
        self.connection.executescript(
            """
            create table if not exists source_health (
                source_id text primary key,
                last_attempt_at text not null,
                last_success_at text,
                last_status text not null,
                last_listing_count integer check (
                    last_listing_count is null or last_listing_count >= 0
                ),
                consecutive_failures integer not null default 0
                    check (consecutive_failures >= 0),
                last_error text not null default '',
                alert_state text not null default 'healthy'
                    check (alert_state in ('healthy', 'failing', 'zero_results', 'stale')),
                alerted_at text,
                updated_at text not null
            );

            pragma user_version = 11;
            """
        )
        self.connection.commit()

    @staticmethod
    def _source_health_record(row: sqlite3.Row | tuple[object, ...]) -> SourceHealthRecord:
        return SourceHealthRecord(
            source_id=str(row[0]),
            last_attempt_at=str(row[1]),
            last_success_at=None if row[2] is None else str(row[2]),
            last_status=str(row[3]),
            last_listing_count=None if row[4] is None else int(row[4]),
            consecutive_failures=int(row[5]),
            last_error=str(row[6]),
            alert_state=str(row[7]),
            alerted_at=None if row[8] is None else str(row[8]),
            updated_at=str(row[9]),
        )

    def source_health(self, source_id: str) -> SourceHealthRecord | None:
        normalized_source_id = source_id.strip().casefold()
        if not normalized_source_id:
            raise ValueError("Source id cannot be empty")
        row = self.connection.execute(
            """
            select source_id, last_attempt_at, last_success_at, last_status,
                   last_listing_count, consecutive_failures, last_error,
                   alert_state, alerted_at, updated_at
            from source_health where source_id = ?
            """,
            (normalized_source_id,),
        ).fetchone()
        return None if row is None else self._source_health_record(row)

    def list_source_health(self) -> tuple[SourceHealthRecord, ...]:
        rows = self.connection.execute(
            """
            select source_id, last_attempt_at, last_success_at, last_status,
                   last_listing_count, consecutive_failures, last_error,
                   alert_state, alerted_at, updated_at
            from source_health order by source_id
            """
        ).fetchall()
        return tuple(self._source_health_record(row) for row in rows)

    def record_source_success(
        self,
        source_id: str,
        status: str,
        listing_count: int,
        *,
        observed_at: datetime | None = None,
    ) -> tuple[SourceHealthRecord, str]:
        normalized_source_id = source_id.strip().casefold()
        if not normalized_source_id or not status.strip():
            raise ValueError("Source id and status cannot be empty")
        if listing_count < 0:
            raise ValueError("Listing count cannot be negative")
        previous = self.source_health(normalized_source_id)
        previous_alert_state = previous.alert_state if previous else "healthy"
        now = (observed_at or datetime.now(UTC)).isoformat()
        with self.connection:
            self.connection.execute(
                """
                insert into source_health (
                    source_id, last_attempt_at, last_success_at, last_status,
                    last_listing_count, consecutive_failures, last_error,
                    alert_state, alerted_at, updated_at
                ) values (?, ?, ?, ?, ?, 0, '', 'healthy', null, ?)
                on conflict(source_id) do update set
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    last_status = excluded.last_status,
                    last_listing_count = excluded.last_listing_count,
                    consecutive_failures = 0,
                    last_error = '',
                    alert_state = 'healthy',
                    alerted_at = null,
                    updated_at = excluded.updated_at
                """,
                (normalized_source_id, now, now, status.strip(), listing_count, now),
            )
        record = self.source_health(normalized_source_id)
        assert record is not None
        return record, previous_alert_state

    def record_source_failure(
        self,
        source_id: str,
        status: str,
        error: str,
        *,
        listing_count: int | None = None,
        observed_at: datetime | None = None,
    ) -> SourceHealthRecord:
        normalized_source_id = source_id.strip().casefold()
        if not normalized_source_id or not status.strip():
            raise ValueError("Source id and status cannot be empty")
        if listing_count is not None and listing_count < 0:
            raise ValueError("Listing count cannot be negative")
        now = (observed_at or datetime.now(UTC)).isoformat()
        detail = error.strip()[:1000]
        with self.connection:
            self.connection.execute(
                """
                insert into source_health (
                    source_id, last_attempt_at, last_success_at, last_status,
                    last_listing_count, consecutive_failures, last_error,
                    alert_state, alerted_at, updated_at
                ) values (?, ?, null, ?, ?, 1, ?, 'healthy', null, ?)
                on conflict(source_id) do update set
                    last_attempt_at = excluded.last_attempt_at,
                    last_status = excluded.last_status,
                    last_listing_count = coalesce(
                        excluded.last_listing_count,
                        source_health.last_listing_count
                    ),
                    consecutive_failures = source_health.consecutive_failures + 1,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (normalized_source_id, now, status.strip(), listing_count, detail, now),
            )
        record = self.source_health(normalized_source_id)
        assert record is not None
        return record

    def set_source_alert_state(
        self,
        source_id: str,
        alert_state: str,
        *,
        observed_at: datetime | None = None,
    ) -> SourceHealthRecord:
        if alert_state not in {"healthy", "failing", "zero_results", "stale"}:
            raise ValueError("Invalid source alert state")
        normalized_source_id = source_id.strip().casefold()
        now = (observed_at or datetime.now(UTC)).isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """
                update source_health
                set alert_state = ?, alerted_at = ?, updated_at = ?
                where source_id = ?
                """,
                (alert_state, None if alert_state == "healthy" else now, now, normalized_source_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown source health record {normalized_source_id!r}")
        record = self.source_health(normalized_source_id)
        assert record is not None
        return record

    def website_source_discovery_count(self, source_id: str) -> int | None:
        normalized_source_id = source_id.strip().casefold()
        row = self.connection.execute(
            "select last_discovery_count from website_source_state where source_id = ?",
            (normalized_source_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def website_source_initialized(self, source_id: str) -> bool:
        """Return whether a website source has an explicit no-alert baseline."""

        normalized_source_id = source_id.strip().casefold()
        if not normalized_source_id:
            raise ValueError("Source id cannot be empty")
        row = self.connection.execute(
            "select 1 from website_source_state where source_id = ?",
            (normalized_source_id,),
        ).fetchone()
        return row is not None

    def website_source_due(self, source_id: str, interval_seconds: int) -> bool:
        """Return whether an initialized source may consume its next remote request."""

        if interval_seconds < 60:
            raise ValueError("Source poll interval must be at least 60 seconds")
        normalized_source_id = source_id.strip().casefold()
        row = self.connection.execute(
            "select last_scan_at from website_source_state where source_id = ?",
            (normalized_source_id,),
        ).fetchone()
        if row is None:
            return True
        last_scan = datetime.fromisoformat(str(row[0]))
        if last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_scan >= timedelta(seconds=interval_seconds)

    def baseline_website_discoveries(
        self,
        source_id: str,
        discoveries: tuple[WebsiteDiscovery, ...],
    ) -> int:
        """Seed existing stock as processed so first use never floods users."""

        normalized_source_id = source_id.strip().casefold()
        validated = self._validate_website_discoveries(normalized_source_id, discoveries)
        if self.website_source_initialized(normalized_source_id):
            raise ValueError(f"Website source {normalized_source_id!r} is already initialized")
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                insert into website_source_state (
                    source_id, initialized_at, last_scan_at, last_discovery_count
                ) values (?, ?, ?, ?)
                """,
                (normalized_source_id, now, now, len(validated)),
            )
            self.connection.executemany(
                """
                insert into website_discoveries (
                    source_id, external_id, url, source_last_modified,
                    first_seen_at, last_seen_at, processed_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        normalized_source_id,
                        item.external_id.strip(),
                        item.url.strip(),
                        item.source_last_modified.strip(),
                        now,
                        now,
                        now,
                    )
                    for item in validated
                ],
            )
        return len(validated)

    def sync_website_discoveries(
        self,
        source_id: str,
        discoveries: tuple[WebsiteDiscovery, ...],
    ) -> tuple[WebsiteDiscovery, ...]:
        """Persist a complete discovery snapshot and return unseen identities."""

        normalized_source_id = source_id.strip().casefold()
        validated = self._validate_website_discoveries(normalized_source_id, discoveries)
        if not self.website_source_initialized(normalized_source_id):
            raise ValueError(
                f"Website source {normalized_source_id!r} needs a baseline before live runs"
            )
        now = _utc_now()
        with self.connection:
            for item in validated:
                self.connection.execute(
                    """
                    insert into website_discoveries (
                        source_id, external_id, url, source_last_modified,
                        first_seen_at, last_seen_at, processed_at
                    ) values (?, ?, ?, ?, ?, ?, null)
                    on conflict(source_id, external_id) do update set
                        url = excluded.url,
                        source_last_modified = excluded.source_last_modified,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        normalized_source_id,
                        item.external_id.strip(),
                        item.url.strip(),
                        item.source_last_modified.strip(),
                        now,
                        now,
                    ),
                )
            self.connection.execute(
                """
                update website_source_state
                set last_scan_at = ?, last_discovery_count = ?
                where source_id = ?
                """,
                (now, len(validated), normalized_source_id),
            )
        rows = self.connection.execute(
            """
            select external_id, url, source_last_modified
            from website_discoveries
            where source_id = ? and processed_at is null
            order by first_seen_at, external_id
            """,
            (normalized_source_id,),
        ).fetchall()
        return tuple(
            WebsiteDiscovery(str(row[0]), str(row[1]), str(row[2])) for row in rows
        )

    def mark_website_discovery_processed(self, source_id: str, external_id: str) -> None:
        """Acknowledge a discovery only after processing and delivery finish."""

        normalized_source_id = source_id.strip().casefold()
        normalized_external_id = external_id.strip()
        if not normalized_source_id or not normalized_external_id:
            raise ValueError("Source id and external id cannot be empty")
        with self.connection:
            cursor = self.connection.execute(
                """
                update website_discoveries
                set processed_at = ?
                where source_id = ? and external_id = ? and processed_at is null
                """,
                (_utc_now(), normalized_source_id, normalized_external_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Website discovery must be pending before acknowledgement")

    @staticmethod
    def _validate_website_discoveries(
        source_id: str,
        discoveries: tuple[WebsiteDiscovery, ...],
    ) -> tuple[WebsiteDiscovery, ...]:
        if not source_id:
            raise ValueError("Source id cannot be empty")
        if not discoveries:
            raise ValueError("Website discovery returned zero listings; refusing to continue")
        by_id: dict[str, WebsiteDiscovery] = {}
        for item in discoveries:
            external_id = item.external_id.strip()
            previous = by_id.get(external_id)
            if previous is not None and previous.url.strip() != item.url.strip():
                raise ValueError(
                    f"Website discovery id {external_id!r} has conflicting URLs"
                )
            by_id[external_id] = item
        return tuple(by_id.values())

    def begin_source_run(
        self,
        source_id: str,
        stale_after_seconds: int = 900,
    ) -> SourceRunRecord | None:
        """Atomically claim a source, replacing only an abandoned stale run."""

        normalized_source_id = source_id.strip().casefold()
        if not normalized_source_id:
            raise ValueError("Source id cannot be empty")
        if stale_after_seconds <= 0:
            raise ValueError("Stale-run timeout must be greater than zero")

        now_datetime = datetime.now(UTC)
        now = now_datetime.isoformat()
        with self.connection:
            self.connection.execute("begin immediate")
            running = self.connection.execute(
                """
                select id, started_at
                from source_runs
                where source_id = ? and status = 'running'
                """,
                (normalized_source_id,),
            ).fetchone()
            if running is not None:
                try:
                    started_at = datetime.fromisoformat(str(running[1]))
                    if started_at.tzinfo is None:
                        started_at = started_at.replace(tzinfo=UTC)
                except ValueError:
                    started_at = datetime.min.replace(tzinfo=UTC)
                if now_datetime - started_at < timedelta(seconds=stale_after_seconds):
                    return None
                self.connection.execute(
                    """
                    update source_runs
                    set status = 'failed', finished_at = ?,
                        error = 'Abandoned run exceeded the stale timeout'
                    where id = ? and status = 'running'
                    """,
                    (now, running[0]),
                )

            cursor = self.connection.execute(
                """
                insert into source_runs (source_id, status, started_at)
                values (?, 'running', ?)
                """,
                (normalized_source_id, now),
            )
            run_id = int(cursor.lastrowid)
        return self.source_run(run_id)

    def finish_source_run(
        self,
        run_id: int,
        status: str,
        *,
        listings_seen: int = 0,
        eligible_matches: int = 0,
        notifications_sent: int = 0,
        notifications_failed: int = 0,
        notifications_skipped: int = 0,
        error: str = "",
    ) -> SourceRunRecord:
        """Complete a claimed run while preserving its partial progress."""

        if status not in {"succeeded", "empty", "failed"}:
            raise ValueError("Completed source-run status is invalid")
        counts = (
            listings_seen,
            eligible_matches,
            notifications_sent,
            notifications_failed,
            notifications_skipped,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Source-run counts cannot be negative")
        with self.connection:
            cursor = self.connection.execute(
                """
                update source_runs
                set status = ?, finished_at = ?, listings_seen = ?,
                    eligible_matches = ?, notifications_sent = ?,
                    notifications_failed = ?, notifications_skipped = ?, error = ?
                where id = ? and status = 'running'
                """,
                (
                    status,
                    _utc_now(),
                    listings_seen,
                    eligible_matches,
                    notifications_sent,
                    notifications_failed,
                    notifications_skipped,
                    error[:1000],
                    run_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Source run must be active before it can be completed")
        return self.source_run(run_id)

    def source_run(self, run_id: int) -> SourceRunRecord:
        """Read one source run for diagnostics and monitoring."""

        row = self.connection.execute(
            """
            select id, source_id, status, started_at, finished_at, listings_seen,
                eligible_matches, notifications_sent, notifications_failed,
                notifications_skipped, error
            from source_runs
            where id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown source run: {run_id}")
        return SourceRunRecord(
            run_id=int(row[0]),
            source_id=str(row[1]),
            status=str(row[2]),
            started_at=str(row[3]),
            finished_at=str(row[4]) if row[4] is not None else None,
            listings_seen=int(row[5]),
            eligible_matches=int(row[6]),
            notifications_sent=int(row[7]),
            notifications_failed=int(row[8]),
            notifications_skipped=int(row[9]),
            error=str(row[10]),
        )

    def save_user(self, user: User) -> None:
        """Create or update an application account."""

        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                insert into users (id, display_name, email, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(id) do update set
                    display_name = excluded.display_name,
                    email = excluded.email,
                    updated_at = excluded.updated_at
                """,
                (user.id, user.display_name, user.email, now, now),
            )

    def load_user(self, user_id: str) -> User:
        """Load one account without exposing any other user's data."""

        row = self.connection.execute(
            "select id, display_name, email from users where id = ?",
            (user_id.strip(),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown user: {user_id}")
        return User(id=str(row[0]), display_name=str(row[1]), email=str(row[2]))

    def save_web_session(
        self,
        token_hash: str,
        user_id: str,
        expires_at: datetime,
    ) -> None:
        """Persist an opaque browser session by digest, never by raw token."""

        normalized_hash = token_hash.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            raise ValueError("Web session token hash must be SHA-256")
        self._require_user(user_id.strip())
        if expires_at.tzinfo is None:
            raise ValueError("Web session expiry must include a timezone")
        now = _utc_now()
        expiry = expires_at.astimezone(UTC).isoformat()
        if expiry <= now:
            raise ValueError("Web session expiry must be in the future")
        with self.connection:
            self.connection.execute(
                """
                insert into web_sessions (
                    token_hash, user_id, expires_at, created_at, last_seen_at
                ) values (?, ?, ?, ?, ?)
                """,
                (normalized_hash, user_id.strip(), expiry, now, now),
            )

    def web_session_user_id(
        self,
        token_hash: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Resolve and refresh one unexpired browser session."""

        normalized_hash = token_hash.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            return None
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None:
            raise ValueError("Web session lookup time must include a timezone")
        timestamp = current.astimezone(UTC).isoformat()
        with self.connection:
            row = self.connection.execute(
                """
                select user_id
                from web_sessions
                where token_hash = ? and expires_at > ?
                """,
                (normalized_hash, timestamp),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "delete from web_sessions where token_hash = ?",
                    (normalized_hash,),
                )
                return None
            self.connection.execute(
                "update web_sessions set last_seen_at = ? where token_hash = ?",
                (timestamp, normalized_hash),
            )
        return str(row[0])

    def delete_web_session(self, token_hash: str) -> None:
        """Revoke one browser session."""

        with self.connection:
            self.connection.execute(
                "delete from web_sessions where token_hash = ?",
                (token_hash.strip().casefold(),),
            )

    def save_telegram_destination(
        self,
        user_id: str,
        chat_id: str,
        *,
        enabled: bool = True,
    ) -> None:
        """Create or update one user's private Telegram destination."""

        normalized_user_id = user_id.strip()
        normalized_chat_id = chat_id.strip()
        if not normalized_user_id:
            raise ValueError("Telegram destination user id cannot be empty")
        if not _TELEGRAM_CHAT_ID.fullmatch(normalized_chat_id):
            raise ValueError("Telegram chat id must be numeric")
        self._require_user(normalized_user_id)
        now = _utc_now()
        try:
            with self.connection:
                self.connection.execute(
                    """
                    insert into telegram_destinations (
                        user_id, chat_id, enabled, created_at, updated_at
                    ) values (?, ?, ?, ?, ?)
                    on conflict(user_id) do update set
                        chat_id = excluded.chat_id,
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_user_id,
                        normalized_chat_id,
                        int(enabled),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "Telegram chat is already linked to a different user"
            ) from error

    def telegram_chat_id(self, user_id: str) -> str | None:
        """Return one enabled destination without exposing other users' chats."""

        row = self.connection.execute(
            """
            select chat_id
            from telegram_destinations
            where user_id = ? and enabled = 1
            """,
            (user_id.strip(),),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def telegram_destination_configured(self, user_id: str) -> bool:
        """Return whether a user has a destination, including a disabled one."""

        row = self.connection.execute(
            "select 1 from telegram_destinations where user_id = ?",
            (user_id.strip(),),
        ).fetchone()
        return row is not None

    def load_telegram_destinations(self) -> dict[str, str]:
        """Load enabled user-to-chat mappings for diagnostics and startup checks."""

        rows = self.connection.execute(
            """
            select user_id, chat_id
            from telegram_destinations
            where enabled = 1
            order by user_id
            """
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def save_telegram_link_token(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
    ) -> None:
        """Store one hashed, single-use Telegram linking capability."""

        normalized_user_id = user_id.strip()
        normalized_hash = token_hash.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            raise ValueError("Telegram link token hash must be SHA-256")
        self._require_user(normalized_user_id)
        if expires_at.tzinfo is None:
            raise ValueError("Telegram link expiry must include a timezone")
        now = _utc_now()
        expiry = expires_at.astimezone(UTC).isoformat()
        if expiry <= now:
            raise ValueError("Telegram link expiry must be in the future")
        with self.connection:
            self.connection.execute(
                "delete from telegram_link_tokens where user_id = ? and used_at is null",
                (normalized_user_id,),
            )
            self.connection.execute(
                """
                insert into telegram_link_tokens (
                    token_hash, user_id, expires_at, used_at, created_at
                ) values (?, ?, ?, null, ?)
                """,
                (normalized_hash, normalized_user_id, expiry, now),
            )

    def consume_telegram_link_token(
        self,
        token_hash: str,
        chat_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Atomically consume a valid token and bind its user to a private chat."""

        normalized_hash = token_hash.strip().casefold()
        normalized_chat_id = chat_id.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            raise ValueError("Invalid or expired Telegram link")
        if not _TELEGRAM_CHAT_ID.fullmatch(normalized_chat_id):
            raise ValueError("Telegram chat id must be numeric")
        current = datetime.now(UTC) if now is None else now
        if current.tzinfo is None:
            raise ValueError("Telegram link consumption time must include a timezone")
        timestamp = current.astimezone(UTC).isoformat()
        try:
            with self.connection:
                row = self.connection.execute(
                    """
                    select user_id
                    from telegram_link_tokens
                    where token_hash = ? and used_at is null and expires_at > ?
                    """,
                    (normalized_hash, timestamp),
                ).fetchone()
                if row is None:
                    raise ValueError("Invalid or expired Telegram link")
                user_id = str(row[0])
                self.connection.execute(
                    """
                    insert into telegram_destinations (
                        user_id, chat_id, enabled, created_at, updated_at
                    ) values (?, ?, 1, ?, ?)
                    on conflict(user_id) do update set
                        chat_id = excluded.chat_id,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, normalized_chat_id, timestamp, timestamp),
                )
                updated = self.connection.execute(
                    """
                    update telegram_link_tokens
                    set used_at = ?
                    where token_hash = ? and used_at is null and expires_at > ?
                    """,
                    (timestamp, normalized_hash, timestamp),
                ).rowcount
                if updated != 1:
                    raise ValueError("Invalid or expired Telegram link")
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "Telegram chat is already linked to a different user"
            ) from error
        return user_id

    def telegram_next_update_id(self) -> int:
        """Return the next Bot API update id, or zero before the first poll."""

        row = self.connection.execute(
            "select next_update_id from telegram_bot_state where id = 1"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def save_telegram_next_update_id(self, next_update_id: int) -> None:
        """Advance the durable Bot API cursor without ever moving it backwards."""

        if next_update_id < 0:
            raise ValueError("Telegram update id cannot be negative")
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                insert into telegram_bot_state (id, next_update_id, updated_at)
                values (1, ?, ?)
                on conflict(id) do update set
                    next_update_id = max(next_update_id, excluded.next_update_id),
                    updated_at = excluded.updated_at
                """,
                (next_update_id, now),
            )

    def save_renter_profile(self, profile: RenterProfile) -> None:
        """Create or update private renter details and structured pets."""

        self._require_user(profile.user_id)
        self._require_unchanged_owner("renter_profiles", profile.id, profile.user_id)
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                insert into renter_profiles (
                    id, user_id, label, sender_name, renter_name, monthly_income_eur,
                    job, contract_type, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    user_id = excluded.user_id,
                    label = excluded.label,
                    sender_name = excluded.sender_name,
                    renter_name = excluded.renter_name,
                    monthly_income_eur = excluded.monthly_income_eur,
                    job = excluded.job,
                    contract_type = excluded.contract_type,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.id,
                    profile.user_id,
                    profile.label,
                    profile.sender_name,
                    profile.renter_name,
                    profile.monthly_income_eur,
                    profile.job,
                    profile.contract_type,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                "delete from pets where renter_profile_id = ?", (profile.id,)
            )
            self.connection.executemany(
                """
                insert into pets (id, renter_profile_id, species, count, name, notes)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        pet.id,
                        profile.id,
                        pet.species,
                        pet.count,
                        pet.name,
                        pet.notes,
                    )
                    for pet in profile.pets
                ),
            )

    def load_primary_renter_profile(self, user_id: str) -> RenterProfile | None:
        """Load the first renter profile owned by a user for web editing."""

        row = self.connection.execute(
            """
            select id, user_id, label, sender_name, renter_name,
                   monthly_income_eur, job, contract_type
            from renter_profiles
            where user_id = ?
            order by created_at, id
            limit 1
            """,
            (user_id.strip(),),
        ).fetchone()
        if row is None:
            return None
        pet_rows = self.connection.execute(
            """
            select id, species, count, name, notes
            from pets
            where renter_profile_id = ?
            order by id
            """,
            (row[0],),
        ).fetchall()
        return RenterProfile(
            id=str(row[0]),
            user_id=str(row[1]),
            label=str(row[2]),
            sender_name=str(row[3]),
            renter_name=str(row[4]),
            monthly_income_eur=(int(row[5]) if row[5] is not None else None),
            job=str(row[6]),
            contract_type=str(row[7]),
            pets=tuple(
                Pet(
                    id=str(pet[0]),
                    species=str(pet[1]),
                    count=int(pet[2]),
                    name=str(pet[3]),
                    notes=str(pet[4]),
                )
                for pet in pet_rows
            ),
        )

    def save_search_profile(self, profile: SearchProfile) -> None:
        """Create or update one independently matchable search."""

        self._require_user(profile.user_id)
        self._require_unchanged_owner("search_profiles", profile.id, profile.user_id)
        if profile.renter_profile_id is not None:
            owner = self.connection.execute(
                "select user_id from renter_profiles where id = ?",
                (profile.renter_profile_id,),
            ).fetchone()
            if owner is None:
                raise ValueError("Renter profile does not exist")
            if owner[0] != profile.user_id:
                raise ValueError("Renter profile belongs to a different user")

        now = _utc_now()
        furnished = None if profile.furnished is None else int(profile.furnished)
        move_in_by = profile.move_in_by.isoformat() if profile.move_in_by else None
        with self.connection:
            self.connection.execute(
                """
                insert into search_profiles (
                    id, user_id, renter_profile_id, name, max_rent_eur,
                    urgent, stretch_max_rent_eur,
                    municipality, acceptable_districts_json,
                    accepted_property_types_json,
                    preferred_min_bedrooms, allow_studios,
                    allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                    accepted_rental_durations_json, requires_pet_acceptance,
                    enabled, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    user_id = excluded.user_id,
                    renter_profile_id = excluded.renter_profile_id,
                    name = excluded.name,
                    max_rent_eur = excluded.max_rent_eur,
                    urgent = excluded.urgent,
                    stretch_max_rent_eur = excluded.stretch_max_rent_eur,
                    municipality = excluded.municipality,
                    acceptable_districts_json = excluded.acceptable_districts_json,
                    accepted_property_types_json = excluded.accepted_property_types_json,
                    preferred_min_bedrooms = excluded.preferred_min_bedrooms,
                    allow_studios = excluded.allow_studios,
                    allow_unknown_bedrooms = excluded.allow_unknown_bedrooms,
                    furnished = excluded.furnished,
                    move_in_asap = excluded.move_in_asap,
                    move_in_by = excluded.move_in_by,
                    accepted_rental_durations_json = excluded.accepted_rental_durations_json,
                    requires_pet_acceptance = excluded.requires_pet_acceptance,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.id,
                    profile.user_id,
                    profile.renter_profile_id,
                    profile.name,
                    profile.max_rent_eur,
                    int(profile.urgent),
                    profile.stretch_max_rent_eur,
                    profile.municipality,
                    json.dumps(profile.acceptable_districts, ensure_ascii=False),
                    json.dumps([value.value for value in profile.accepted_property_types]),
                    profile.preferred_min_bedrooms,
                    int(profile.allow_studios),
                    int(profile.allow_unknown_bedrooms),
                    furnished,
                    int(profile.move_in_asap),
                    move_in_by,
                    json.dumps([value.value for value in profile.accepted_rental_durations]),
                    int(profile.requires_pet_acceptance),
                    int(profile.enabled),
                    now,
                    now,
                ),
            )

    def load_search_profile(self, profile_id: str) -> SearchProfile:
        """Load a stored search profile into the domain model."""

        row = self.connection.execute(
            """
            select
                id, user_id, name, max_rent_eur, urgent, stretch_max_rent_eur,
                municipality,
                renter_profile_id, acceptable_districts_json,
                accepted_property_types_json,
                preferred_min_bedrooms, allow_studios,
                allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                accepted_rental_durations_json, requires_pet_acceptance, enabled
            from search_profiles
            where id = ?
            """,
            (profile_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown search profile: {profile_id}")
        return self._search_profile_from_row(row)

    def load_primary_search_profile(self, user_id: str) -> SearchProfile | None:
        """Load the first search owned by a user for web editing."""

        row = self.connection.execute(
            """
            select
                id, user_id, name, max_rent_eur, urgent, stretch_max_rent_eur,
                municipality,
                renter_profile_id, acceptable_districts_json,
                accepted_property_types_json,
                preferred_min_bedrooms, allow_studios,
                allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                accepted_rental_durations_json, requires_pet_acceptance, enabled
            from search_profiles
            where user_id = ?
            order by created_at, id
            limit 1
            """,
            (user_id.strip(),),
        ).fetchone()
        return self._search_profile_from_row(row) if row is not None else None

    def load_enabled_search_profiles(self) -> tuple[SearchProfile, ...]:
        """Load every active search in deterministic order for matching."""

        rows = self.connection.execute(
            """
            select
                id, user_id, name, max_rent_eur, urgent, stretch_max_rent_eur,
                municipality,
                renter_profile_id, acceptable_districts_json,
                accepted_property_types_json,
                preferred_min_bedrooms, allow_studios,
                allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                accepted_rental_durations_json, requires_pet_acceptance, enabled
            from search_profiles
            where enabled = 1
            order by id
            """
        ).fetchall()
        return tuple(self._search_profile_from_row(row) for row in rows)

    @staticmethod
    def _search_profile_from_row(row: tuple[object, ...]) -> SearchProfile:
        furnished = None if row[13] is None else bool(row[13])
        move_in_by = date.fromisoformat(row[15]) if row[15] else None
        return SearchProfile(
            id=str(row[0]),
            user_id=str(row[1]),
            name=str(row[2]),
            max_rent_eur=int(row[3]),
            urgent=bool(row[4]),
            stretch_max_rent_eur=int(row[5]) if row[5] is not None else None,
            municipality=str(row[6]),
            renter_profile_id=str(row[7]) if row[7] is not None else None,
            acceptable_districts=tuple(json.loads(row[8])),
            accepted_property_types=tuple(
                PropertyType(value) for value in json.loads(str(row[9]))
            ),
            preferred_min_bedrooms=int(row[10]),
            allow_studios=bool(row[11]),
            allow_unknown_bedrooms=bool(row[12]),
            furnished=furnished,
            move_in_asap=bool(row[14]),
            move_in_by=move_in_by,
            accepted_rental_durations=tuple(
                RentalDuration(value) for value in json.loads(str(row[16]))
            ),
            requires_pet_acceptance=bool(row[17]),
            enabled=bool(row[18]),
        )

    def upsert(self, listing: Listing) -> UpsertResult:
        """Insert a global listing or update its latest observation."""

        raw_identities = _raw_identities(listing)
        identities = tuple(_digest(identity) for identity in raw_identities)
        fingerprint = identities[0]
        observed_at = _utc_now()
        canonical_url = canonicalize_url(listing.url)
        payload = _listing_payload(listing)
        with self.connection:
            placeholders = ", ".join("?" for _identity in identities)
            matched_rows = self.connection.execute(
                f"""
                select distinct listing_id
                from listing_identities
                where identity in ({placeholders})
                """,
                identities,
            ).fetchall()
            if len(matched_rows) > 1:
                raise RuntimeError(
                    "Listing identities point to multiple records; "
                    "manual reconciliation is required"
                )

            is_new = not matched_rows
            if is_new:
                cursor = self.connection.execute(
                    """
                    insert into listings (
                        fingerprint, source_id, external_id, canonical_url, payload_json,
                        first_seen_at, last_seen_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fingerprint,
                        listing.source_id,
                        listing.external_id,
                        canonical_url,
                        payload,
                        observed_at,
                        observed_at,
                    ),
                )
                listing_id = cursor.lastrowid
            else:
                listing_id = matched_rows[0][0]
                self.connection.execute(
                    """
                    update listings
                    set payload_json = ?, last_seen_at = ?
                    where id = ?
                    """,
                    (payload, observed_at, listing_id),
                )

            for raw_identity, identity in zip(raw_identities, identities, strict=True):
                self.connection.execute(
                    """
                    insert into listing_identities (identity, listing_id, identity_kind)
                    values (?, ?, ?)
                    on conflict(identity) do nothing
                    """,
                    (identity, listing_id, raw_identity.partition("|")[0]),
                )

            row = self.connection.execute(
                "select id from listings where id = ?", (listing_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Stored listing could not be read back")
        return UpsertResult(listing_id=row[0], fingerprint=fingerprint, is_new=is_new)

    def record_match(
        self,
        listing_id: int,
        search_profile_id: str,
        search_decision: SearchDecision,
        pet_decision: PetDecision,
    ) -> MatchResult:
        """Record one profile's evaluation without affecting any other profile."""

        now = _utc_now()
        with self.connection:
            existing = self.connection.execute(
                """
                select id from listing_matches
                where listing_id = ? and search_profile_id = ?
                """,
                (listing_id, search_profile_id),
            ).fetchone()
            self.connection.execute(
                """
                insert into listing_matches (
                    listing_id, search_profile_id, eligible, pet_status,
                    pet_evidence, rejection_reasons_json, preference_notes_json,
                    first_evaluated_at, last_evaluated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(listing_id, search_profile_id) do update set
                    eligible = excluded.eligible,
                    pet_status = excluded.pet_status,
                    pet_evidence = excluded.pet_evidence,
                    rejection_reasons_json = excluded.rejection_reasons_json,
                    preference_notes_json = excluded.preference_notes_json,
                    last_evaluated_at = excluded.last_evaluated_at
                """,
                (
                    listing_id,
                    search_profile_id,
                    int(search_decision.eligible),
                    pet_decision.status.value,
                    pet_decision.evidence,
                    json.dumps(search_decision.rejection_reasons),
                    json.dumps(search_decision.preference_notes),
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                """
                select id from listing_matches
                where listing_id = ? and search_profile_id = ?
                """,
                (listing_id, search_profile_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Stored listing match could not be read back")
        return MatchResult(
            match_id=row[0],
            listing_id=listing_id,
            search_profile_id=search_profile_id,
            eligible=search_decision.eligible,
            is_new=existing is None,
        )

    def claim_notification(self, match_id: int, channel: str) -> bool:
        """Claim one profile-specific delivery exactly once."""

        normalized_channel = channel.strip().casefold()
        if not normalized_channel:
            raise ValueError("Notification channel cannot be empty")
        eligible = self.connection.execute(
            "select eligible from listing_matches where id = ?", (match_id,)
        ).fetchone()
        if eligible is None:
            raise ValueError("Listing match does not exist")
        if not eligible[0]:
            raise ValueError("Cannot notify for an ineligible listing match")

        updated_at = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                insert into notifications (match_id, channel, status, updated_at)
                values (?, ?, 'pending', ?)
                on conflict(match_id, channel) do update set
                    status = 'pending',
                    attempts = notifications.attempts + 1,
                    last_error = '',
                    updated_at = excluded.updated_at
                where notifications.status = 'failed'
                """,
                (match_id, normalized_channel, updated_at),
            )
        return cursor.rowcount == 1

    def notification_status(self, match_id: int, channel: str) -> str | None:
        """Return delivery state for one profile match and channel."""

        normalized_channel = channel.strip().casefold()
        if not normalized_channel:
            raise ValueError("Notification channel cannot be empty")
        row = self.connection.execute(
            """
            select status from notifications
            where match_id = ? and channel = ?
            """,
            (match_id, normalized_channel),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def mark_notification_sent(self, match_id: int, channel: str) -> None:
        """Mark a claimed notification as successfully delivered."""

        self._finish_notification(match_id, channel, "sent", "")

    def mark_notification_failed(
        self,
        match_id: int,
        channel: str,
        error: str,
    ) -> None:
        """Release a failed notification so a later run can retry it."""

        self._finish_notification(match_id, channel, "failed", error)

    def _finish_notification(
        self,
        match_id: int,
        channel: str,
        status: str,
        error: str,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                update notifications
                set status = ?, last_error = ?, updated_at = ?
                where match_id = ? and channel = ? and status = 'pending'
                """,
                (status, error, _utc_now(), match_id, channel.strip().casefold()),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Notification must be claimed before it can be completed"
            )

    def _require_user(self, user_id: str) -> None:
        exists = self.connection.execute(
            "select 1 from users where id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            raise ValueError(f"Unknown user: {user_id}")

    def _require_unchanged_owner(
        self,
        table: str,
        record_id: str,
        user_id: str,
    ) -> None:
        if table not in {"renter_profiles", "search_profiles"}:
            raise ValueError("Unsupported owner table")
        existing = self.connection.execute(
            f"select user_id from {table} where id = ?", (record_id,)
        ).fetchone()
        if existing is not None and existing[0] != user_id:
            raise ValueError("Profile ownership cannot be changed")


# Backward-compatible import name while callers move to the broader repository name.
ListingStore = RentalStore
