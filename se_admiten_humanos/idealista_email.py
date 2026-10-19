"""Ingest authenticated Idealista alert emails without reading user mailboxes."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .models import Listing, RentalDuration
from .persistence import RentalStore
from .pipeline import ListingPipeline
from .property_type import classify_property_type
from .rental_duration import classify_rental_duration
from .telegram import TelegramNotifier


_PROPERTY_URL = re.compile(r"/inmueble/(\d+)(?:/|$)")
_PRICE = re.compile(r"(?<!\d)(\d{1,3}(?:[.\s]\d{3})*|\d+)\s*€(?:\s*/\s*mes)?", re.I)
_BEDROOMS = re.compile(r"(?<!\d)(\d+)\s*(?:hab(?:\.|itaciones?)|dormitorios?)", re.I)
_AREA = re.compile(r"(?<!\d)(\d+)\s*m(?:²|2)\b", re.I)
_AUTHENTICATED_IDEALISTA = re.compile(
    r"dkim=pass[^\n]*(?:header\.(?:from|d)=|\bd=)(?:[\w-]+\.)*idealista\.com\b",
    re.I,
)
_MAX_ITEMS = 20
_MAX_BODY_CHARS = 1_500_000


@dataclass(frozen=True, slots=True)
class IdealistaEmailResult:
    emails_seen: int = 0
    emails_accepted: int = 0
    listings_seen: int = 0
    notifications_sent: int = 0
    notifications_failed: int = 0


class _VisibleEmailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.link_labels: dict[str, list[str]] = {}
        self._href = ""
        self._anchor_text: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._anchor_text = []
            if self._href:
                self.parts.extend(("\n", self._href, "\n"))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._href:
            label = " ".join("".join(self._anchor_text).split())
            if label:
                self.link_labels.setdefault(self._href, []).append(label)
            self._href = ""
            self._anchor_text = []
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.parts.append(data)
        if self._href:
            self._anchor_text.append(data)


def parse_brevo_idealista_payload(payload: object) -> tuple[Listing, ...]:
    """Return normalized listings from authenticated Idealista messages."""

    if not isinstance(payload, Mapping):
        raise ValueError("Inbound email payload must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > _MAX_ITEMS:
        raise ValueError("Inbound email payload has an invalid items array")
    listings: dict[str, Listing] = {}
    for item in items:
        if not isinstance(item, Mapping) or not _is_authenticated_idealista(item):
            continue
        raw_html = item.get("RawHtmlBody")
        raw_text = item.get("RawTextBody") or item.get("ExtractedMarkdownMessage") or ""
        if not isinstance(raw_html, str):
            raw_html = ""
        if not isinstance(raw_text, str):
            raw_text = ""
        if len(raw_html) + len(raw_text) > _MAX_BODY_CHARS:
            raise ValueError("Inbound email body is too large")
        for listing in _parse_message(raw_html, raw_text):
            listings.setdefault(listing.external_id, listing)
    return tuple(listings.values())


def process_brevo_idealista_payload(
    store: RentalStore,
    notifier: TelegramNotifier,
    payload: object,
) -> IdealistaEmailResult:
    """Parse, match, deduplicate and notify for one Brevo callback."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise ValueError("Inbound email payload has an invalid items array")
    listings = parse_brevo_idealista_payload(payload)
    sent = failed = 0
    pipeline = ListingPipeline(store)
    for listing in listings:
        for attempt in notifier.deliver(pipeline.process(listing)):
            sent += attempt.status == "sent"
            failed += attempt.status == "failed"
    accepted = sum(
        isinstance(item, Mapping) and _is_authenticated_idealista(item)
        for item in payload["items"]
    )
    return IdealistaEmailResult(
        emails_seen=len(payload["items"]),
        emails_accepted=accepted,
        listings_seen=len(listings),
        notifications_sent=sent,
        notifications_failed=failed,
    )


def _is_authenticated_idealista(item: Mapping[object, object]) -> bool:
    sender = item.get("From")
    if not isinstance(sender, Mapping):
        return False
    address = str(sender.get("Address", "")).strip().casefold()
    domain = address.rpartition("@")[2]
    if domain != "idealista.com" and not domain.endswith(".idealista.com"):
        return False
    headers = item.get("Headers")
    if not isinstance(headers, Mapping):
        return False
    authentication_values: list[str] = []
    for key, value in headers.items():
        if str(key).casefold() not in {"authentication-results", "arc-authentication-results"}:
            continue
        if isinstance(value, str):
            authentication_values.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            authentication_values.extend(str(part) for part in value)
    return bool(_AUTHENTICATED_IDEALISTA.search("\n".join(authentication_values)))


def _parse_message(raw_html: str, raw_text: str) -> tuple[Listing, ...]:
    parser = _VisibleEmailParser()
    if raw_html:
        parser.feed(raw_html)
    visible = "\n".join(part.strip() for part in "".join(parser.parts).splitlines() if part.strip())
    combined = f"{visible}\n{html.unescape(raw_text)}"
    urls = _idealista_urls(combined)
    results: list[Listing] = []
    for index, url in enumerate(urls):
        match = _PROPERTY_URL.search(urlsplit(url).path)
        if match is None:
            continue
        next_url = urls[index + 1] if index + 1 < len(urls) else ""
        start = combined.find(url)
        end = combined.find(next_url, start + len(url)) if next_url else -1
        context = combined[start : end if end >= 0 else start + 1600]
        labels = parser.link_labels.get(url, [])
        title = _title(labels, context)
        price_match = _PRICE.search(context)
        if price_match is None or "madrid" not in context.casefold():
            continue
        price_eur = int(re.sub(r"[^0-9]", "", price_match.group(1)))
        bedrooms = _integer_match(_BEDROOMS, context)
        area = _integer_match(_AREA, context)
        duration, duration_evidence = classify_rental_duration(
            context, category_default=RentalDuration.LONG_TERM
        )
        results.append(
            Listing(
                source_id="idealista_email",
                external_id=match.group(1),
                title=title,
                price=f"{price_eur:,} €".replace(",", "."),
                price_eur=price_eur,
                location="Madrid",
                municipality="Madrid",
                property_type=classify_property_type(title, url),
                bedrooms=bedrooms,
                area_sqm=area,
                rental_duration=duration,
                rental_duration_evidence=duration_evidence,
                description=" ".join(context.split())[:4000],
                url=url,
            )
        )
    return tuple(results)


def _idealista_urls(text: str) -> list[str]:
    found: list[str] = []
    for candidate in re.findall(r"https?://[^\s<>\"')]+", text):
        cleaned = html.unescape(candidate).rstrip(".,;]")
        parsed = urlsplit(cleaned)
        host = (parsed.hostname or "").casefold()
        if host not in {"idealista.com", "www.idealista.com"}:
            continue
        if _PROPERTY_URL.search(parsed.path) is None:
            continue
        normalized = urlunsplit(("https", "www.idealista.com", parsed.path, "", ""))
        if normalized not in found:
            found.append(normalized)
    return found


def _title(labels: list[str], context: str) -> str:
    for candidate in labels:
        if len(candidate) >= 8 and not _PRICE.fullmatch(candidate):
            return candidate[:300]
    for line in context.splitlines()[1:8]:
        candidate = " ".join(line.split())
        if len(candidate) >= 8 and not _PRICE.search(candidate):
            return candidate[:300]
    return "Nueva vivienda en Idealista"


def _integer_match(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1)) if match else None
