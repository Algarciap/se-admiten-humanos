"""SQLite persistence and conservative listing deduplication."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Listing


SCHEMA_VERSION = 1


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents.casefold()).strip()


def canonicalize_url(url: str) -> str:
    """Remove tracking details while retaining the listing's stable path."""

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


class ListingStore:
    """Persist listings and atomically coordinate notification delivery."""

    def __init__(self, path: str | Path = "rentals.sqlite3") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("pragma foreign_keys = on")
        self._initialize_schema()

    def __enter__(self) -> "ListingStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize_schema(self) -> None:
        version = self.connection.execute("pragma user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
            raise RuntimeError(
                f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}"
            )
        self.connection.executescript(
            """
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

            create table if not exists notifications (
                listing_id integer not null references listings(id) on delete cascade,
                channel text not null,
                status text not null check (status in ('pending', 'sent', 'failed')),
                attempts integer not null default 1,
                last_error text not null default '',
                updated_at text not null,
                primary key (listing_id, channel)
            );
            """
        )
        self.connection.execute(f"pragma user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def upsert(self, listing: Listing) -> UpsertResult:
        """Insert a new listing or update its latest observation."""

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
                    "Listing identities point to multiple records; manual reconciliation is required"
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

    def claim_notification(self, listing_id: int, channel: str) -> bool:
        """Claim one delivery; sent or in-flight messages cannot be claimed twice."""

        normalized_channel = channel.strip().casefold()
        if not normalized_channel:
            raise ValueError("Notification channel cannot be empty")
        updated_at = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                insert into notifications (listing_id, channel, status, updated_at)
                values (?, ?, 'pending', ?)
                on conflict(listing_id, channel) do update set
                    status = 'pending',
                    attempts = notifications.attempts + 1,
                    last_error = '',
                    updated_at = excluded.updated_at
                where notifications.status = 'failed'
                """,
                (listing_id, normalized_channel, updated_at),
            )
        return cursor.rowcount == 1

    def mark_notification_sent(self, listing_id: int, channel: str) -> None:
        """Mark a claimed notification as successfully delivered."""

        self._finish_notification(listing_id, channel, "sent", "")

    def mark_notification_failed(
        self,
        listing_id: int,
        channel: str,
        error: str,
    ) -> None:
        """Release a failed notification so a later run can retry it."""

        self._finish_notification(listing_id, channel, "failed", error)

    def _finish_notification(
        self,
        listing_id: int,
        channel: str,
        status: str,
        error: str,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                update notifications
                set status = ?, last_error = ?, updated_at = ?
                where listing_id = ? and channel = ? and status = 'pending'
                """,
                (status, error, _utc_now(), listing_id, channel.strip().casefold()),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Notification must be claimed before it can be completed"
            )
