"""Conservative residential property-type classification."""

from __future__ import annotations

import re
import unicodedata

from .models import PropertyType


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(
        character for character in decomposed if not unicodedata.combining(character)
    ).casefold()


def classify_property_type(title: str, url: str = "") -> PropertyType:
    """Classify explicit non-home categories; ordinary homes remain homes."""

    evidence = _normalized(f"{title} {url}")
    if re.search(r"\b(garaje|garage|parking|plaza de aparcamiento)\b", evidence):
        return PropertyType.GARAGE
    if re.search(r"\b(local|oficina|nave industrial|commercial)\b", evidence):
        return PropertyType.COMMERCIAL
    if re.search(
        r"\b(habitacion en alquiler|alquiler de habitacion|room for rent)\b|/habitaciones?/",
        evidence,
    ):
        return PropertyType.ROOM
    return PropertyType.HOME
