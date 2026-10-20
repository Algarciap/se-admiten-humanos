"""Conservative watcher for Nestar's published first-party inventory."""

from __future__ import annotations

import json
import math
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


SOURCE_ID = "nestar"
BASE_URL = "https://www.nestarhomes.es"
SITEMAP_URL = f"{BASE_URL}/sitemap-0.xml"
USER_AGENT = "SeAdmitenHumanos/0.13 (+https://github.com/Algarciap/se-admiten-humanos)"
_LISTING_PATH = re.compile(
    r"/vivienda/(?P<slug>[a-z0-9-]+)-(?P<portfolio>[0-9]{4})-"
    r"(?P<building>[0-9]{8})-(?P<unit>[0-9]{8})"
)


class NestarTransportError(RuntimeError):
    """A bounded first-party request failed."""


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.capturing = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "script" and values.get("id") == "__NEXT_DATA__":
            self.capturing = True

    def handle_data(self, data: str) -> None:
        if self.capturing:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.capturing:
            self.capturing = False


def _external_id(match: re.Match[str]) -> str:
    return "-".join(
        (match.group("portfolio"), match.group("building"), match.group("unit"))
    )


def parse_sitemap(xml_text: str) -> tuple[WebsiteDiscovery, ...]:
    """Accept exact public Nestar property detail URLs from its sitemap."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise ValueError("Nestar sitemap is invalid XML") from error
    discoveries: list[WebsiteDiscovery] = []
    seen: set[str] = set()
    for element in root.findall("{*}url"):
        location = (element.findtext("{*}loc") or "").strip()
        last_modified = (element.findtext("{*}lastmod") or "").strip()
        parsed = urlsplit(location)
        match = _LISTING_PATH.fullmatch(parsed.path)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.nestarhomes.es"
            or parsed.query
            or parsed.fragment
            or match is None
        ):
            continue
        external_id = _external_id(match)
        if external_id in seen:
            raise ValueError(f"Duplicate Nestar listing id: {external_id}")
        seen.add(external_id)
        discoveries.append(WebsiteDiscovery(external_id, location, last_modified))
    if not discoveries:
        raise ValueError("Nestar sitemap contained no property detail URLs")
    if len(discoveries) > 5_000:
        raise ValueError("Nestar sitemap exceeded the safety limit")
    return tuple(discoveries)


def _integer(value: object, field: str) -> int:
    try:
        result = int(float(str(value)))
    except (TypeError, ValueError) as error:
        raise ValueError(f"Nestar detail has invalid {field}") from error
    if result < 0:
        raise ValueError(f"Nestar detail has invalid {field}")
    return result


def parse_listing(html_text: str, discovery: WebsiteDiscovery) -> Listing | None:
    """Normalize public Next.js data, ignoring sale inventory explicitly."""

    parser = _NextDataParser()
    parser.feed(html_text)
    if not parser.parts:
        raise ValueError("Nestar detail is missing __NEXT_DATA__")
    try:
        payload = json.loads("".join(parser.parts))
        page_props = payload["props"]["pageProps"]
        property_data = page_props["property"]
        page = page_props["page"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Nestar detail data has changed or is invalid") from error
    if not isinstance(property_data, dict) or not isinstance(page, dict):
        raise ValueError("Nestar detail property payload is invalid")
    code = str(property_data.get("codigoUnidadAlquiler", "")).replace("/", "-")
    if code != discovery.external_id:
        raise ValueError("Nestar detail identity does not match discovery")
    operation = str(property_data.get("ventaAlquiler", "")).strip().casefold()
    if operation == "venta":
        return None
    if operation != "alquiler":
        raise ValueError("Nestar detail has an unknown transaction type")
    title = str(page.get("title", "")).strip()
    municipality = str(property_data.get("ciudad", "")).strip().title()
    price_value = property_data.get("importeRecibofinal")
    if (
        not title
        or not municipality
        or not isinstance(price_value, (int, float))
        or isinstance(price_value, bool)
        or price_value <= 0
    ):
        raise ValueError("Nestar rental is missing title, municipality, or rent")
    general = property_data.get("informacionGeneral")
    description = (
        str(general.get("argumentario", "")) if isinstance(general, dict) else ""
    )
    description = " ".join(description.replace("##", " ").replace("####", " ").split())
    if not description:
        raise ValueError("Nestar rental is missing its description")
    zone = property_data.get("tipoZona")
    district = str(zone.get("infoAdicional", "")).strip() if isinstance(zone, dict) else ""
    furnished_value = str(property_data.get("hasAmueblado", "")).strip().casefold()
    furnished = True if furnished_value == "si" else False if furnished_value == "no" else None
    duration, duration_evidence = classify_rental_duration(
        f"{title} {description}", category_default=RentalDuration.LONG_TERM
    )
    return Listing(
        source_id=SOURCE_ID,
        external_id=code,
        title=title,
        price=f"{float(price_value):.2f} €/mes",
        price_eur=math.ceil(float(price_value)),
        location=municipality,
        municipality=municipality,
        district=district,
        address=str(property_data.get("STREET", "")).strip(),
        property_type=classify_property_type(title, discovery.url),
        bedrooms=_integer(property_data.get("habitaciones"), "bedroom count"),
        area_sqm=_integer(property_data.get("metrosComputablesUA"), "area"),
        is_furnished=furnished,
        rental_duration=duration,
        rental_duration_evidence=duration_evidence,
        description=description,
        url=discovery.url,
        contact_name="Nestar",
    )


class NestarSource:
    """Discover published property pages and fetch only unseen details."""

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
            raise NestarTransportError("Published sitemap unexpectedly returned 404")
        return parse_sitemap(xml_text)

    def fetch_listing(self, discovery: WebsiteDiscovery) -> Listing | None:
        parsed = urlsplit(discovery.url)
        match = _LISTING_PATH.fullmatch(parsed.path)
        if (
            parsed.hostname != "www.nestarhomes.es"
            or match is None
            or _external_id(match) != discovery.external_id
        ):
            raise ValueError("Refusing an unexpected Nestar detail URL")
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
        match = _LISTING_PATH.fullmatch(parsed.path)
        stage = "sitemap discovery" if url == SITEMAP_URL else (
            f"detail {_external_id(match)}" if match is not None else "detail"
        )
        payload = request_bytes(
            url,
            allowed_hostname="www.nestarhomes.es",
            user_agent=USER_AGENT,
            source_label="Nestar",
            stage=stage,
            error_type=NestarTransportError,
            timeout_seconds=20,
            max_bytes=3_000_000,
        )
        if payload is None:
            return None
        try:
            return payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise NestarTransportError(
                f"Nestar {stage} response is not valid UTF-8"
            ) from error
