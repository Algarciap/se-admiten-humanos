"""Conservative classification of an advert's rental duration."""

from __future__ import annotations

import re
import unicodedata

from .models import RentalDuration


_TEMPORARY = (
    re.compile(r"\balquiler\s+(?:de\s+)?temporad(?:a|as)\b"),
    re.compile(r"\barrendamiento\s+(?:de\s+)?temporad(?:a|as)\b"),
    re.compile(r"\b(?:estancia|contrato)\s+(?:de\s+)?tempor(?:al|ada)\b"),
    re.compile(r"\btemporal(?:mente)?\b"),
    re.compile(r"\bmaxim[oa]\s+(?:de\s+)?\d+\s+mes(?:es)?\b"),
    re.compile(r"\b(?:por|durante)\s+\d+\s+mes(?:es)?\b"),
)
_LONG_TERM = (
    re.compile(r"\blarga\s+(?:duracion|estancia)\b"),
    re.compile(r"\bvivienda\s+habitual\b"),
    re.compile(r"\bcontrato\s+(?:de\s+)?(?:larga\s+duracion|cinco\s+anos|5\s+anos)\b"),
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return " ".join(
        "".join(char for char in decomposed if not unicodedata.combining(char))
        .casefold()
        .split()
    )


def classify_rental_duration(
    text: str,
    *,
    category_default: RentalDuration = RentalDuration.NOT_SPECIFIED,
) -> tuple[RentalDuration, str]:
    """Return an explicit duration and evidence, falling back to the source category."""

    normalized = _normalize(text)
    for duration, patterns in (
        (RentalDuration.TEMPORARY, _TEMPORARY),
        (RentalDuration.LONG_TERM, _LONG_TERM),
    ):
        for pattern in patterns:
            match = pattern.search(normalized)
            if match:
                return duration, match.group(0)
    if category_default is RentalDuration.LONG_TERM:
        return category_default, "categoría de alquiler residencial"
    if category_default is RentalDuration.TEMPORARY:
        return category_default, "categoría de alquiler temporal"
    return RentalDuration.NOT_SPECIFIED, ""
