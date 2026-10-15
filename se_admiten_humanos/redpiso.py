"""Conservative watcher for Redpiso's published Madrid rental inventory."""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import Listing, RentalDuration, WebsiteDiscovery
from .rental_duration import classify_rental_duration


SOURCE_ID = "redpiso"
BASE_URL = "https://www.redpiso.es"
SITEMAP_URL = f"{BASE_URL}/sitemaps/properties-1.xml"
USER_AGENT = "SeAdmitenHumanos/0.11 (+https://github.com/Algarciap/se-admiten-humanos)"
_LISTING_PATH = re.compile(r"/inmueble/(?P<slug>[a-z0-9-]+)-(?P<id>rp[0-9]+)", re.I)


class RedpisoTransportError(RuntimeError):
    """A bounded first-party request failed."""


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_kind = ""
        self.script_parts: list[str] = []
        self.json_ld = ""
        self.nuxt_data = ""
        self.property_meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "div" and values.get("id") == "property_meta":
            self.property_meta = values
        if tag != "script":
            return
        if values.get("type") == "application/ld+json":
            self.script_kind = "json_ld"
            self.script_parts = []
        elif values.get("id") == "__NUXT_DATA__":
            self.script_kind = "nuxt_data"
            self.script_parts = []

    def handle_data(self, data: str) -> None:
        if self.script_kind:
            self.script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self.script_kind:
            return
        value = "".join(self.script_parts)
        if self.script_kind == "json_ld":
            self.json_ld = value
        else:
            self.nuxt_data = value
        self.script_kind = ""
        self.script_parts = []


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _plain_text(markup: str) -> str:
    parser = _TextParser()
    parser.feed(html.unescape(markup))
    return " ".join(parser.parts)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def parse_sitemap(xml_text: str) -> tuple[WebsiteDiscovery, ...]:
    """Accept long-term residential rental details in the Madrid province."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise ValueError("Redpiso sitemap is invalid XML") from error
    discoveries: list[WebsiteDiscovery] = []
    seen: set[str] = set()
    for element in root.findall("{*}url"):
        location = (element.findtext("{*}loc") or "").strip()
        last_modified = (element.findtext("{*}lastmod") or "").strip()
        parsed = urlsplit(location)
        match = _LISTING_PATH.fullmatch(parsed.path)
        slug = match.group("slug").casefold() if match else ""
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.redpiso.es"
            or parsed.query
            or parsed.fragment
            or match is None
            or not any(marker in slug for marker in ("-en-alquiler-en-", "-alquiler-temporal-", "-alquiler-de-temporada-"))
            or "vacacional" in slug
            or not slug.endswith("-madrid")
        ):
            continue
        external_id = match.group("id").upper()
        if external_id in seen:
            raise ValueError(f"Duplicate Redpiso listing id: {external_id}")
        seen.add(external_id)
        discoveries.append(WebsiteDiscovery(external_id, location, last_modified))
    if not discoveries:
        raise ValueError("Redpiso sitemap contained no Madrid residential rentals")
    if len(discoveries) > 5_000:
        raise ValueError("Redpiso sitemap exceeded the safety limit")
    return tuple(discoveries)


def _real_estate_listing(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    graph = payload.get("@graph", [])
    if not isinstance(graph, list):
        return {}
    for item in graph:
        if isinstance(item, dict) and item.get("@type") == "RealEstateListing":
            return item
    return {}


def parse_listing(html_text: str, discovery: WebsiteDiscovery) -> Listing:
    """Normalize schema.org data and preserve the full advert text as evidence."""

    parser = _MetadataParser()
    parser.feed(html_text)
    try:
        schema = _real_estate_listing(json.loads(parser.json_ld))
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("Redpiso detail schema is invalid JSON") from error
    if not schema:
        raise ValueError("Redpiso detail is missing RealEstateListing schema")
    external_id = str(schema.get("identifier", "")).upper()
    if external_id != discovery.external_id.upper():
        raise ValueError("Redpiso detail identity does not match discovery")
    offers = schema.get("offers")
    address_data = schema.get("address")
    if not isinstance(offers, dict) or not isinstance(address_data, dict):
        raise ValueError("Redpiso detail is missing offers or address schema")
    title = str(schema.get("name", "")).strip()
    price = offers.get("price")
    municipality = str(address_data.get("addressLocality", "")).strip()
    if not title or not isinstance(price, int) or not municipality:
        raise ValueError("Redpiso detail is missing title, price, or municipality")
    description = str(schema.get("description", "")).strip()
    if parser.nuxt_data:
        try:
            nuxt_payload = json.loads(parser.nuxt_data)
        except json.JSONDecodeError as error:
            raise ValueError("Redpiso detail data is invalid JSON") from error
        candidates = [value for value in _strings(nuxt_payload) if "<p" in value and len(value) > 40]
        if candidates:
            description = _plain_text(max(candidates, key=len))
    if not description:
        raise ValueError("Redpiso detail is missing its description")
    schema_description = str(schema.get("description", ""))
    area_match = re.search(r"(?P<area>[0-9]+)\s*m", schema_description, re.I)
    bedroom_match = re.search(r"\b(?P<count>[0-9]+)\s+dorm", description, re.I)
    normalized = description.casefold()
    category_default = (
        RentalDuration.TEMPORARY
        if "temporal" in discovery.url.casefold() or "temporada" in discovery.url.casefold()
        else RentalDuration.LONG_TERM
    )
    rental_duration, duration_evidence = classify_rental_duration(
        f"{title} {description}", category_default=category_default
    )
    furnished: bool | None = None
    if "sin amueblar" in normalized:
        furnished = False
    elif re.search(r"\bamueblad[ao]s?\b", normalized):
        furnished = True
    return Listing(
        source_id=SOURCE_ID,
        external_id=external_id,
        title=title,
        price=f"{price} €",
        price_eur=price,
        location=municipality,
        municipality=municipality,
        address=title,
        bedrooms=int(bedroom_match.group("count")) if bedroom_match else None,
        area_sqm=int(area_match.group("area")) if area_match else None,
        is_furnished=furnished,
        description=description,
        rental_duration=rental_duration,
        rental_duration_evidence=duration_evidence,
        url=discovery.url,
        contact_name=parser.property_meta.get("data-office-name", "Redpiso"),
        contact_email=parser.property_meta.get("data-office-email", ""),
    )


class RedpisoSource:
    """Discover through the public property sitemap and fetch unseen details."""

    source_id = SOURCE_ID

    def __init__(
        self,
        fetch_text: Callable[[str], str | None] | None = None,
        *,
        minimum_detail_interval_seconds: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if minimum_detail_interval_seconds < 0:
            raise ValueError("Detail request interval cannot be negative")
        self._fetch_text = fetch_text or self._request_text
        self._interval = minimum_detail_interval_seconds
        self._sleep = sleep
        self._last_detail_request_at: float | None = None

    def discover(self) -> tuple[WebsiteDiscovery, ...]:
        xml_text = self._fetch_text(SITEMAP_URL)
        if xml_text is None:
            raise RedpisoTransportError("Published sitemap unexpectedly returned 404")
        return parse_sitemap(xml_text)

    def fetch_listing(self, discovery: WebsiteDiscovery) -> Listing | None:
        parsed = urlsplit(discovery.url)
        match = _LISTING_PATH.fullmatch(parsed.path)
        if parsed.hostname != "www.redpiso.es" or match is None or match.group("id").upper() != discovery.external_id.upper():
            raise ValueError("Refusing an unexpected Redpiso detail URL")
        self._throttle()
        html_text = self._fetch_text(discovery.url)
        return None if html_text is None else parse_listing(html_text, discovery)

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_detail_request_at is not None:
            remaining = self._interval - (now - self._last_detail_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_detail_request_at = time.monotonic()

    @staticmethod
    def _request_text(url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.redpiso.es":
            raise ValueError("Refusing a non-Redpiso URL")
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=20) as response:
                payload = response.read(5_000_001)
                if len(payload) > 5_000_000:
                    raise RedpisoTransportError("Response exceeded 5 MB safety limit")
                charset = response.headers.get_content_charset() or "utf-8"
                return payload.decode(charset)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise RedpisoTransportError(f"Redpiso returned HTTP {error.code}") from error
        except (URLError, TimeoutError, UnicodeDecodeError) as error:
            raise RedpisoTransportError(
                f"Redpiso request failed: {type(error).__name__}"
            ) from error
