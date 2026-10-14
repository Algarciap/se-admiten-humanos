"""SQLite persistence for users, searches, listings, matches, and delivery state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Listing, PetDecision, RenterProfile, User
from .search_profile import SearchDecision, SearchProfile


SCHEMA_VERSION = 2


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
        elif version == 1:
            self._migrate_v1_to_v2()
        elif version != SCHEMA_VERSION:
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
                    municipality, acceptable_districts_json,
                    preferred_min_bedrooms, allow_studios,
                    allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                    requires_pet_acceptance, enabled, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    user_id = excluded.user_id,
                    renter_profile_id = excluded.renter_profile_id,
                    name = excluded.name,
                    max_rent_eur = excluded.max_rent_eur,
                    municipality = excluded.municipality,
                    acceptable_districts_json = excluded.acceptable_districts_json,
                    preferred_min_bedrooms = excluded.preferred_min_bedrooms,
                    allow_studios = excluded.allow_studios,
                    allow_unknown_bedrooms = excluded.allow_unknown_bedrooms,
                    furnished = excluded.furnished,
                    move_in_asap = excluded.move_in_asap,
                    move_in_by = excluded.move_in_by,
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
                    profile.municipality,
                    json.dumps(profile.acceptable_districts, ensure_ascii=False),
                    profile.preferred_min_bedrooms,
                    int(profile.allow_studios),
                    int(profile.allow_unknown_bedrooms),
                    furnished,
                    int(profile.move_in_asap),
                    move_in_by,
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
                id, user_id, name, max_rent_eur, municipality,
                renter_profile_id, acceptable_districts_json,
                preferred_min_bedrooms, allow_studios,
                allow_unknown_bedrooms, furnished, move_in_asap, move_in_by,
                requires_pet_acceptance, enabled
            from search_profiles
            where id = ?
            """,
            (profile_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown search profile: {profile_id}")
        furnished = None if row[10] is None else bool(row[10])
        move_in_by = date.fromisoformat(row[12]) if row[12] else None
        return SearchProfile(
            id=row[0],
            user_id=row[1],
            name=row[2],
            max_rent_eur=row[3],
            municipality=row[4],
            renter_profile_id=row[5],
            acceptable_districts=tuple(json.loads(row[6])),
            preferred_min_bedrooms=row[7],
            allow_studios=bool(row[8]),
            allow_unknown_bedrooms=bool(row[9]),
            furnished=furnished,
            move_in_asap=bool(row[11]),
            move_in_by=move_in_by,
            requires_pet_acceptance=bool(row[13]),
            enabled=bool(row[14]),
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
