"""Plan broad Idealista email alerts from precise user search profiles."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .models import PropertyType
from .search_profile import SearchProfile


def _place_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.casefold().split())


@dataclass(frozen=True, slots=True)
class IdealistaSourceSearch:
    """One deliberately broad alert that feeds the per-user matcher."""

    municipality: str
    property_type: PropertyType
    max_price_eur: int
    profile_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "municipality": self.municipality,
            "property_type": self.property_type.value,
            "max_price_eur": self.max_price_eur,
            "profile_count": self.profile_count,
            "source_filters": {
                "municipality": self.municipality,
                "property_type": self.property_type.value,
                "max_price_eur": self.max_price_eur,
            },
            "matched_after_ingestion": [
                "exact_budget",
                "districts",
                "bedrooms",
                "pets",
                "furnished",
                "move_in_date",
                "rental_duration",
            ],
        }


def plan_idealista_source_searches(
    profiles: tuple[SearchProfile, ...],
) -> tuple[IdealistaSourceSearch, ...]:
    """Consolidate profiles without losing listings needed by any user."""

    groups: dict[tuple[str, PropertyType], dict[str, object]] = {}
    for profile in profiles:
        if not profile.enabled:
            continue
        ceiling = profile.stretch_max_rent_eur or profile.max_rent_eur
        for property_type in profile.accepted_property_types:
            key = (_place_key(profile.municipality), property_type)
            group = groups.setdefault(
                key,
                {
                    "municipality": profile.municipality.strip(),
                    "max_price_eur": ceiling,
                    "profile_ids": set(),
                },
            )
            group["max_price_eur"] = max(int(group["max_price_eur"]), ceiling)
            profile_ids = group["profile_ids"]
            assert isinstance(profile_ids, set)
            profile_ids.add(profile.id)

    planned = [
        IdealistaSourceSearch(
            municipality=str(values["municipality"]),
            property_type=property_type,
            max_price_eur=int(values["max_price_eur"]),
            profile_count=len(values["profile_ids"]),
        )
        for (_, property_type), values in groups.items()
    ]
    return tuple(
        sorted(
            planned,
            key=lambda item: (_place_key(item.municipality), item.property_type.value),
        )
    )
