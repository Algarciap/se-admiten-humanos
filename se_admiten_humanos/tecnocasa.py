"""Conservative watcher for Tecnocasa's published Madrid rental inventory."""

from __future__ import annotations

import gzip
import html
import json
import re
import time
from collections.abc import Callable, Iterable
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import Listing, WebsiteDiscovery


SOURCE_ID = "tecnocasa"
BASE_URL = "https://www.tecnocasa.es"
SITEMAP_URLS = (
    f"{BASE_URL}/sitemap/inmuebles-0.xml.gz",
    f"{BASE_URL}/sitemap/inmuebles-1.xml.gz",
)
USER_AGENT = "SeAdmitenHumanos/0.11 (+https://github.com/Algarciap/se-admiten-humanos)"
_RESIDENTIAL_TYPES = {"atico", "casa", "chalet", "duplex", "estudio", "loft", "piso"}
_LISTING_PATH = re.compile(
    r"/alquiler/(?P<kind>[^/]+)/madrid/madrid/(?P<id>[1-9][0-9]*)\.html"
)


class TecnocasaTransportError(RuntimeError):
    """A bounded first-party request failed."""


class _EstateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_estate = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.raw_estate:
            return
        values = dict(attrs)
        candidate = values.get(":estate", "")
        if candidate:
            self.raw_estate = candidate


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


def parse_sitemap(xml_text: str) -> tuple[WebsiteDiscovery, ...]:
    """Accept only residential rentals in Madrid city from the public sitemap."""

    return _parse_sitemap(xml_text, require_discoveries=True)


def _parse_sitemap(
    xml_text: str, *, require_discoveries: bool
) -> tuple[WebsiteDiscovery, ...]:

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise ValueError("Tecnocasa sitemap is invalid XML") from error
    discoveries: list[WebsiteDiscovery] = []
    seen: set[str] = set()
    for element in root.findall("{*}url"):
        location = (element.findtext("{*}loc") or "").strip()
        last_modified = (element.findtext("{*}lastmod") or "").strip()
        parsed = urlsplit(location)
        match = _LISTING_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.tecnocasa.es"
            or parsed.query
            or parsed.fragment
            or match is None
            or match.group("kind") not in _RESIDENTIAL_TYPES
        ):
            continue
        external_id = match.group("id")
        if external_id in seen:
            raise ValueError(f"Duplicate Tecnocasa listing id: {external_id}")
        seen.add(external_id)
        discoveries.append(WebsiteDiscovery(external_id, location, last_modified))
    if require_discoveries and not discoveries:
        raise ValueError("Tecnocasa sitemap contained no Madrid residential rentals")
    if len(discoveries) > 5_000:
        raise ValueError("Tecnocasa sitemap exceeded the safety limit")
    return tuple(discoveries)


def _bedrooms(estate: dict[str, object]) -> int | None:
    for item in estate.get("data", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("label", "")).casefold() == "dormitorios":
            value = item.get("valore")
            if isinstance(value, int) and value >= 0:
                return value
    match = re.search(r"\b(?P<count>[0-9]+)\s+dorm", str(estate.get("rooms", "")), re.I)
    return int(match.group("count")) if match else None


def parse_listing(html_text: str, discovery: WebsiteDiscovery) -> Listing:
    """Normalize Tecnocasa's embedded, server-rendered listing object."""

    parser = _EstateParser()
    parser.feed(html_text)
    if not parser.raw_estate:
        raise ValueError("Tecnocasa detail page is missing the estate payload")
    try:
        estate = json.loads(parser.raw_estate)
    except json.JSONDecodeError as error:
        raise ValueError("Tecnocasa estate payload is invalid JSON") from error
    if not isinstance(estate, dict):
        raise ValueError("Tecnocasa estate payload must be an object")
    external_id = str(estate.get("id", ""))
    if external_id != discovery.external_id:
        raise ValueError("Tecnocasa detail identity does not match discovery")
    title = str(estate.get("title", "")).strip()
    price_eur = estate.get("numeric_price")
    city = estate.get("city")
    municipality = str(city.get("title", "")).strip() if isinstance(city, dict) else ""
    description = _plain_text(str(estate.get("description", "")))
    if not title or not isinstance(price_eur, int) or not municipality or not description:
        raise ValueError("Tecnocasa detail is missing title, price, municipality, or description")
    district_data = estate.get("district")
    agency = estate.get("agency")
    district = str(district_data.get("title", "")).strip() if isinstance(district_data, dict) else ""
    address = str(estate.get("address", "")).strip()
    contact_name = str(agency.get("name", "")).strip() if isinstance(agency, dict) else "Tecnocasa"
    contact_email = str(agency.get("email", "")).strip() if isinstance(agency, dict) else ""
    area_value = estate.get("numeric_surface")
    try:
        area_sqm = int(float(str(area_value))) if area_value not in {None, ""} else None
    except ValueError:
        area_sqm = None
    normalized = description.casefold()
    furnished: bool | None = None
    if "sin amueblar" in normalized:
        furnished = False
    elif re.search(r"\bamueblad[ao]s?\b", normalized):
        furnished = True
    return Listing(
        source_id=SOURCE_ID,
        external_id=external_id,
        title=title,
        price=str(estate.get("price", "")).strip(),
        price_eur=price_eur,
        location=municipality,
        municipality=municipality,
        district=district,
        address=address,
        bedrooms=_bedrooms(estate),
        area_sqm=area_sqm,
        is_furnished=furnished,
        description=description,
        url=discovery.url,
        contact_name=contact_name or "Tecnocasa",
        contact_email=contact_email,
    )


class TecnocasaSource:
    """Discover through published sitemaps and fetch only unseen detail pages."""

    source_id = SOURCE_ID

    def __init__(
        self,
        fetch_bytes: Callable[[str], bytes | None] | None = None,
        *,
        minimum_detail_interval_seconds: float = 1.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if minimum_detail_interval_seconds < 0:
            raise ValueError("Detail request interval cannot be negative")
        self._fetch_bytes = fetch_bytes or self._request_bytes
        self._interval = minimum_detail_interval_seconds
        self._sleep = sleep
        self._last_detail_request_at: float | None = None

    def discover(self) -> tuple[WebsiteDiscovery, ...]:
        combined: list[WebsiteDiscovery] = []
        seen: set[str] = set()
        for url in SITEMAP_URLS:
            payload = self._fetch_bytes(url)
            if payload is None:
                raise TecnocasaTransportError("Published sitemap unexpectedly returned 404")
            try:
                uncompressed = gzip.decompress(payload)
                if len(uncompressed) > 20_000_000:
                    raise ValueError("Tecnocasa sitemap exceeded the expanded safety limit")
                xml_text = uncompressed.decode("utf-8-sig")
            except (gzip.BadGzipFile, UnicodeDecodeError) as error:
                raise ValueError("Tecnocasa sitemap is not valid UTF-8 gzip") from error
            for discovery in _parse_sitemap(xml_text, require_discoveries=False):
                if discovery.external_id in seen:
                    raise ValueError(
                        f"Duplicate Tecnocasa listing id across sitemaps: {discovery.external_id}"
                    )
                seen.add(discovery.external_id)
                combined.append(discovery)
        if not combined:
            raise ValueError(
                "Tecnocasa sitemaps contained no Madrid residential rentals"
            )
        return tuple(combined)

    def fetch_listing(self, discovery: WebsiteDiscovery) -> Listing | None:
        parsed = urlsplit(discovery.url)
        match = _LISTING_PATH.fullmatch(parsed.path)
        if parsed.hostname != "www.tecnocasa.es" or match is None or match.group("id") != discovery.external_id:
            raise ValueError("Refusing an unexpected Tecnocasa detail URL")
        self._throttle()
        payload = self._fetch_bytes(discovery.url)
        if payload is None:
            return None
        try:
            return parse_listing(payload.decode("utf-8"), discovery)
        except UnicodeDecodeError as error:
            raise ValueError("Tecnocasa detail is not valid UTF-8") from error

    def _throttle(self) -> None:
        now = time.monotonic()
        if self._last_detail_request_at is not None:
            remaining = self._interval - (now - self._last_detail_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_detail_request_at = time.monotonic()

    @staticmethod
    def _request_bytes(url: str) -> bytes | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "www.tecnocasa.es":
            raise ValueError("Refusing a non-Tecnocasa URL")
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=20) as response:
                payload = response.read(8_000_001)
                if len(payload) > 8_000_000:
                    raise TecnocasaTransportError("Response exceeded 8 MB safety limit")
                return payload
        except HTTPError as error:
            if error.code == 404:
                return None
            raise TecnocasaTransportError(f"Tecnocasa returned HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise TecnocasaTransportError(
                f"Tecnocasa request failed: {type(error).__name__}"
            ) from error
