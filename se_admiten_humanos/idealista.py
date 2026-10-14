"""Credential-gated client and adapter for the official Idealista Search API."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import Listing


AUTH_URL = "https://api.idealista.com/oauth/token"
SEARCH_URL = "https://api.idealista.com/3.5/es/search"
MADRID_CENTER = "40.4168,-3.7038"


class IdealistaApiError(RuntimeError):
    """The official API failed or returned a response outside its contract."""


@dataclass(frozen=True, slots=True)
class IdealistaSearchConfig:
    """Bounded official-API query; profile filtering still happens downstream."""

    center: str = MADRID_CENTER
    distance_meters: int = 20_000
    max_price_eur: int = 950
    since_date: str = "T"
    max_items_per_page: int = 50
    max_pages: int = 1

    def __post_init__(self) -> None:
        coordinates = self.center.split(",")
        if len(coordinates) != 2:
            raise ValueError("Idealista center must be formatted as latitude,longitude")
        try:
            latitude, longitude = (float(value.strip()) for value in coordinates)
        except ValueError as error:
            raise ValueError(
                "Idealista center must be formatted as latitude,longitude"
            ) from error
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Idealista center coordinates are out of range")
        if not 1 <= self.distance_meters <= 50_000:
            raise ValueError("Idealista distance must be between 1 and 50000 meters")
        if self.max_price_eur <= 0:
            raise ValueError("Idealista maximum price must be greater than zero")
        if self.since_date not in {"T", "Y", "W", "M"}:
            raise ValueError("Idealista since-date filter is invalid")
        if not 1 <= self.max_items_per_page <= 50:
            raise ValueError("Idealista page size must be between 1 and 50")
        if not 1 <= self.max_pages <= 10:
            raise ValueError("Idealista page limit must be between 1 and 10")

    def parameters(self, page: int) -> dict[str, str]:
        if not 1 <= page <= self.max_pages:
            raise ValueError("Idealista page is outside the configured run limit")
        return {
            "center": self.center,
            "distance": str(self.distance_meters),
            "propertyType": "homes",
            "operation": "rent",
            "maxPrice": str(self.max_price_eur),
            "sinceDate": self.since_date,
            "order": "publicationDate",
            "sort": "desc",
            "maxItems": str(self.max_items_per_page),
            "numPage": str(page),
        }


class IdealistaApiClient:
    """Small dependency-free OAuth client for approved Search API access."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        timeout_seconds: float = 15,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Idealista API key and secret are required")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self.timeout_seconds = timeout_seconds
        self._access_token = ""
        self._token_expires_at = 0.0

    def search(self, parameters: Mapping[str, str]) -> Mapping[str, object]:
        token = self._token()
        return self._post_json(
            SEARCH_URL,
            parameters,
            {"Authorization": f"Bearer {token}"},
        )

    def _token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        credentials = base64.b64encode(
            f"{self._api_key}:{self._api_secret}".encode("utf-8")
        ).decode("ascii")
        payload = self._post_json(
            AUTH_URL,
            {"grant_type": "client_credentials", "scope": "read"},
            {"Authorization": f"Basic {credentials}"},
        )
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 300)
        if not isinstance(token, str) or not token:
            raise IdealistaApiError("Idealista authentication response has no access token")
        if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool):
            raise IdealistaApiError("Idealista authentication expiry is invalid")
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(1, float(expires_in) - 30)
        return token

    def _post_json(
        self,
        url: str,
        parameters: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        request = Request(
            url,
            data=urlencode(parameters).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "User-Agent": "se-admiten-humanos/0.5 (official Idealista API client)",
                **headers,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise IdealistaApiError(
                f"Idealista API returned HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise IdealistaApiError(
                f"Idealista API request failed: {type(error).__name__}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IdealistaApiError("Idealista API returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise IdealistaApiError("Idealista API response must be a JSON object")
        return payload


class IdealistaApiAdapter:
    """Turn official Search API results into the project's Listing model."""

    source_id = "idealista"

    def __init__(
        self,
        client: IdealistaApiClient,
        config: IdealistaSearchConfig,
    ) -> None:
        self.client = client
        self.config = config

    def fetch(self):
        for page in range(1, self.config.max_pages + 1):
            payload = self.client.search(self.config.parameters(page))
            elements = payload.get("elementList")
            if not isinstance(elements, list):
                raise IdealistaApiError(
                    "Idealista search response has no elementList array"
                )
            for index, element in enumerate(elements, start=1):
                if not isinstance(element, Mapping):
                    raise IdealistaApiError(
                        f"Idealista page {page} listing {index} is not an object"
                    )
                yield _normalize_listing(element, page, index)

            total_pages = payload.get("totalPages")
            if total_pages is not None:
                if not isinstance(total_pages, int) or isinstance(total_pages, bool):
                    raise IdealistaApiError("Idealista totalPages is invalid")
                if page >= total_pages:
                    break
            elif len(elements) < self.config.max_items_per_page:
                break


def _required_text(
    element: Mapping[str, object],
    field: str,
    page: int,
    index: int,
) -> str:
    value = element.get(field)
    if not isinstance(value, str) or not value.strip():
        raise IdealistaApiError(
            f"Idealista page {page} listing {index} has invalid {field}"
        )
    return value.strip()


def _optional_text(element: Mapping[str, object], field: str) -> str:
    value = element.get(field)
    return value.strip() if isinstance(value, str) else ""


def _optional_integer(
    element: Mapping[str, object],
    field: str,
    page: int,
    index: int,
) -> int | None:
    value = element.get(field)
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise IdealistaApiError(
            f"Idealista page {page} listing {index} has invalid {field}"
        )
    return round(value)


def _normalize_listing(
    element: Mapping[str, object],
    page: int,
    index: int,
) -> Listing:
    external_id = _required_text(element, "propertyCode", page, index)
    municipality = _required_text(element, "municipality", page, index)
    url = _required_text(element, "url", page, index)
    parsed_url = urlsplit(url)
    host = (parsed_url.hostname or "").casefold()
    if (
        parsed_url.scheme not in {"http", "https"}
        or (host != "idealista.com" and not host.endswith(".idealista.com"))
        or parsed_url.username
        or parsed_url.password
    ):
        raise IdealistaApiError(
            f"Idealista page {page} listing {index} has an invalid URL"
        )

    price = _optional_integer(element, "price", page, index)
    if price is None or price <= 0:
        raise IdealistaApiError(
            f"Idealista page {page} listing {index} has invalid price"
        )
    rooms = _optional_integer(element, "rooms", page, index)
    area = _optional_integer(element, "size", page, index)
    district = _optional_text(element, "district")
    neighborhood = _optional_text(element, "neighborhood")
    location_parts = tuple(
        dict.fromkeys(
            part for part in (neighborhood, district, municipality) if part
        )
    )
    suggested = element.get("suggestedTexts")
    suggested_title = (
        suggested.get("title") if isinstance(suggested, Mapping) else None
    )
    title = (
        suggested_title.strip()
        if isinstance(suggested_title, str) and suggested_title.strip()
        else f"Vivienda en {district or municipality}"
    )
    furnished = element.get("furnished")
    return Listing(
        source_id="idealista",
        external_id=external_id,
        title=title,
        price=f"{price} €/mes",
        price_eur=price,
        location=", ".join(location_parts),
        municipality=municipality,
        district=district,
        address=(
            _optional_text(element, "address")
            if element.get("showAddress") is True
            else ""
        ),
        bedrooms=rooms,
        area_sqm=area,
        is_furnished=furnished if isinstance(furnished, bool) else None,
        description=_optional_text(element, "description"),
        url=url,
    )
