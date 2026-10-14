"""Eligibility rules for a renter's search profile."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .models import Listing, PetDecision, PetStatus


def _normalize_place(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(without_accents.casefold().split())


@dataclass(frozen=True, slots=True)
class SearchProfile:
    """Hard filters and preferences for the current Madrid search."""

    max_rent_eur: int = 950
    municipality: str = "Madrid"
    preferred_min_bedrooms: int = 1
    allow_studios: bool = True
    allow_unknown_bedrooms: bool = True
    furnished: bool | None = None
    move_in_asap: bool = True

    def __post_init__(self) -> None:
        if self.max_rent_eur <= 0:
            raise ValueError("Maximum rent must be greater than zero")
        if self.preferred_min_bedrooms < 0:
            raise ValueError("Preferred bedroom count cannot be negative")
        if not self.municipality.strip():
            raise ValueError("Municipality cannot be empty")


@dataclass(frozen=True, slots=True)
class SearchDecision:
    """Explain why a normalized listing is or is not eligible."""

    eligible: bool
    rejection_reasons: tuple[str, ...]
    preference_notes: tuple[str, ...]


MADRID_ALIASES = frozenset({"madrid", "madrid capital", "madrid ciudad"})


def evaluate_listing(
    listing: Listing,
    pet_decision: PetDecision,
    profile: SearchProfile | None = None,
) -> SearchDecision:
    """Apply hard filters without turning preferences into false exclusions."""

    selected_profile = profile or SearchProfile()
    rejections: list[str] = []
    notes: list[str] = []

    if listing.price_eur is None:
        rejections.append("rent_not_disclosed")
    elif listing.price_eur > selected_profile.max_rent_eur:
        rejections.append("rent_above_maximum")

    expected_municipality = _normalize_place(selected_profile.municipality)
    actual_municipality = _normalize_place(listing.municipality)
    accepted_places = (
        MADRID_ALIASES
        if expected_municipality == "madrid"
        else {expected_municipality}
    )
    if not actual_municipality:
        rejections.append("municipality_not_disclosed")
    elif actual_municipality not in accepted_places:
        rejections.append("outside_madrid_city")

    if pet_decision.status is PetStatus.REJECTED:
        rejections.append("pets_rejected")

    if listing.bedrooms is None:
        if selected_profile.allow_unknown_bedrooms:
            notes.append("bedrooms_not_disclosed")
        else:
            rejections.append("bedrooms_not_disclosed")
    elif listing.bedrooms == 0:
        if selected_profile.allow_studios:
            notes.append("studio")
        else:
            rejections.append("studio")
    elif listing.bedrooms < selected_profile.preferred_min_bedrooms:
        notes.append("below_preferred_bedrooms")

    if selected_profile.furnished is not None:
        if listing.is_furnished is None:
            notes.append("furnished_status_not_disclosed")
        elif listing.is_furnished is not selected_profile.furnished:
            rejections.append("furnished_status_mismatch")

    if selected_profile.move_in_asap and listing.available_from is None:
        notes.append("availability_not_disclosed")

    return SearchDecision(
        eligible=not rejections,
        rejection_reasons=tuple(rejections),
        preference_notes=tuple(notes),
    )
