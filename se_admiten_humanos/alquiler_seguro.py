"""Conservative public-sitemap adapter for Alquiler Seguro."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .http_transport import request_bytes
from .models import Listing, RentalDuration, WebsiteDiscovery
from .property_type import classify_property_type
from .rental_duration import classify_rental_duration


SOURCE_ID = "alquiler_seguro"
BASE_URL = "https://www.alquilerseguro.es"
SITEMAP_URL = f"{BASE_URL}/dynamics.xml"
USER_AGENT = (
    "se-admiten-humanos/0.6 "
    "(+https://github.com/Algarciap/se-admiten-humanos)"
)
_LISTING_PATH = re.compile(r"^/viviendas/(?P<id>[0-9]+)$")
_PRICE = re.compile(r"(?P<amount>[0-9]+(?:[.\s][0-9]{3})*)\s*€")
_BEDROOMS = re.compile(r"(?P<count>[0-9]+)\s+habitacion(?:es)?\b", re.IGNORECASE)
_AREA = re.compile(r"(?P<area>[0-9]+)\s*m\s*2\b", re.IGNORECASE)


class AlquilerSeguroTransportError(RuntimeError):
    """The public source could not be read safely."""


class _ListingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_description = ""
        self.og_title = ""
        self.values: dict[str, str] = {}
        self._captures: list[list[object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for capture in self._captures:
            capture[1] = int(capture[1]) + 1
        attributes = {name.casefold(): value or "" for name, value in attrs}
        if tag.casefold() == "meta":
            key = attributes.get("name", "").casefold()
            property_name = attributes.get("property", "").casefold()
            if key == "description":
                self.meta_description = attributes.get("content", "").strip()
            elif property_name == "og:title":
                self.og_title = attributes.get("content", "").strip()
            return
        class_tokens = set(attributes.get("class", "").casefold().split())
        target = ""
        if tag.casefold() == "h1" and "titulo-inmueble" in class_tokens:
            target = "title"
        elif (
            tag.casefold() in {"p", "span"}
            and "precio" in class_tokens
            and "card-precio-inmueble" not in class_tokens
        ):
            target = "price"
        elif "margen-caracteristicas" in class_tokens:
            target = "facts"
        if target and target not in self.values:
            self._captures.append([target, 1, []])

    def handle_endtag(self, _tag: str) -> None:
        completed: list[list[object]] = []
        for capture in self._captures:
            capture[1] = int(capture[1]) - 1
            if capture[1] == 0:
                completed.append(capture)
        for capture in completed:
            target = str(capture[0])
            parts = capture[2]
            assert isinstance(parts, list)
            self.values[target] = " ".join("".join(parts).split())
            self._captures.remove(capture)

    def handle_data(self, data: str) -> None:
        for capture in self._captures:
            parts = capture[2]
            assert isinstance(parts, list)
            parts.append(data)


def parse_sitemap(xml_text: str) -> tuple[WebsiteDiscovery, ...]:
    """Parse only exact first-party public listing URLs from the published sitemap."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise ValueError("Alquiler Seguro sitemap is invalid XML") from error
    discoveries: list[WebsiteDiscovery] = []
    seen_ids: set[str] = set()
    for url_element in root.findall("{*}url"):
        location = (url_element.findtext("{*}loc") or "").strip()
        last_modified = (url_element.findtext("{*}lastmod") or "").strip()
        parsed = urlsplit(location)
        match = _LISTING_PATH.fullmatch(parsed.path.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.alquilerseguro.es"
            or parsed.query
            or parsed.fragment
            or match is None
        ):
            continue
        external_id = match.group("id")
        if external_id in seen_ids:
            raise ValueError(f"Duplicate Alquiler Seguro listing id: {external_id}")
        seen_ids.add(external_id)
        discoveries.append(WebsiteDiscovery(external_id, location, last_modified))
    if not discoveries:
        raise ValueError("Alquiler Seguro sitemap contained no valid listing URLs")
    if len(discoveries) > 5_000:
        raise ValueError("Alquiler Seguro sitemap exceeded the safety limit")
    return tuple(discoveries)


def parse_listing(html_text: str, discovery: WebsiteDiscovery) -> Listing:
    """Normalize one public detail page and fail if its core contract changes."""

    parser = _ListingPageParser()
    parser.feed(html_text)
    title = parser.values.get("title", "").strip()
    price_text = parser.values.get("price", "").strip()
    description = parser.meta_description.strip()
    facts = parser.values.get("facts", "").strip()
    if not title or not price_text or not description or not facts:
        raise ValueError(
            "Alquiler Seguro detail page is missing title, price, description, or facts"
        )
    price_match = _PRICE.search(price_text)
    if price_match is None:
        raise ValueError("Alquiler Seguro detail price could not be parsed")
    price_eur = int(re.sub(r"[.\s]", "", price_match.group("amount")))
    bedrooms_match = _BEDROOMS.search(facts)
    area_match = _AREA.search(facts)

    og_parts = [part.strip() for part in parser.og_title.split(" - ") if part.strip()]
    address = og_parts[0] if og_parts else ""
    municipality = og_parts[-2].title() if len(og_parts) >= 2 else ""
    if not municipality:
        raise ValueError("Alquiler Seguro detail municipality could not be parsed")

    normalized_description = description.casefold()
    rental_duration, duration_evidence = classify_rental_duration(
        f"{title} {description}", category_default=RentalDuration.LONG_TERM
    )
    furnished: bool | None = None
    if "sin amueblar" in normalized_description:
        furnished = False
    elif re.search(r"\bamueblad[ao]s?\b", normalized_description):
        furnished = True

    return Listing(
        source_id=SOURCE_ID,
        external_id=discovery.external_id,
        title=title,
        price=price_text,
        price_eur=price_eur,
        location=municipality,
        municipality=municipality,
        address=address,
        property_type=classify_property_type(title, discovery.url),
        bedrooms=int(bedrooms_match.group("count")) if bedrooms_match else None,
        area_sqm=int(area_match.group("area")) if area_match else None,
        is_furnished=furnished,
        description=description,
        rental_duration=rental_duration,
        rental_duration_evidence=duration_evidence,
        url=discovery.url,
        contact_name="Alquiler Seguro",
    )


class AlquilerSeguroSource:
    """Discover through the public sitemap and fetch only new public detail pages."""

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
        self._minimum_detail_interval_seconds = minimum_detail_interval_seconds
        self._sleep = sleep
        self._last_detail_request_at: float | None = None

    def discover(self) -> tuple[WebsiteDiscovery, ...]:
        xml_text = self._fetch_text(SITEMAP_URL)
        if xml_text is None:
            raise AlquilerSeguroTransportError("Public sitemap unexpectedly returned 404")
        return parse_sitemap(xml_text)

    def fetch_listing(self, discovery: WebsiteDiscovery) -> Listing | None:
        expected_url = f"{BASE_URL}/viviendas/{discovery.external_id}"
        if discovery.url.rstrip("/") != expected_url:
            raise ValueError("Refusing an unexpected Alquiler Seguro detail URL")
        now = time.monotonic()
        if self._last_detail_request_at is not None:
            elapsed = now - self._last_detail_request_at
            remaining = self._minimum_detail_interval_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_detail_request_at = time.monotonic()
        html_text = self._fetch_text(discovery.url)
        if html_text is None:
            return None
        return parse_listing(html_text, discovery)

    @staticmethod
    def _request_text(url: str) -> str | None:
        parsed = urlsplit(url)
        match = _LISTING_PATH.fullmatch(parsed.path.rstrip("/"))
        stage = "sitemap discovery" if url == SITEMAP_URL else (
            f"detail {match.group('id')}" if match is not None else "detail"
        )
        payload = request_bytes(
            url,
            allowed_hostname="www.alquilerseguro.es",
            user_agent=USER_AGENT,
            source_label="Alquiler Seguro",
            stage=stage,
            error_type=AlquilerSeguroTransportError,
            timeout_seconds=15,
            max_bytes=2_000_000,
            accepted_content_types={"text/html", "application/xml", "text/xml"},
        )
        if payload is None:
            return None
        try:
            return payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise AlquilerSeguroTransportError(
                f"Alquiler Seguro {stage} response is not valid UTF-8"
            ) from error
