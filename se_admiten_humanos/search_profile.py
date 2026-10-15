"""Eligibility rules for a renter's search profile."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date

from .models import Listing, PetDecision, PetStatus, PropertyType, RentalDuration


def _normalize_place(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return " ".join(without_accents.casefold().split())


@dataclass(frozen=True, slots=True)
class SearchProfile:
    """User-owned hard filters and preferences for one reusable search."""

    id: str
    user_id: str
    name: str
    max_rent_eur: int
    municipality: str
    renter_profile_id: str | None = None
    acceptable_districts: tuple[str, ...] = ()
    accepted_property_types: tuple[PropertyType, ...] = (PropertyType.HOME,)
    preferred_min_bedrooms: int = 1
    allow_studios: bool = True
    allow_unknown_bedrooms: bool = True
    furnished: bool | None = None
    move_in_asap: bool = True
    move_in_by: date | None = None
    accepted_rental_durations: tuple[RentalDuration, ...] = (RentalDuration.LONG_TERM,)
    requires_pet_acceptance: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        required = {
            "Search profile id": self.id,
            "User id": self.user_id,
            "Search profile name": self.name,
            "Municipality": self.municipality,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ValueError(f"{field_name} cannot be empty")
        if self.max_rent_eur <= 0:
            raise ValueError("Maximum rent must be greater than zero")
        if self.preferred_min_bedrooms < 0:
            raise ValueError("Preferred bedroom count cannot be negative")
        if not self.accepted_rental_durations:
            raise ValueError("At least one rental duration must be accepted")
        if not self.accepted_property_types:
            raise ValueError("At least one property type must be accepted")
        residential_types = {PropertyType.HOME, PropertyType.ROOM}
        if any(value not in residential_types for value in self.accepted_property_types):
            raise ValueError("Only homes and rooms can be selected")
        if len(self.accepted_property_types) != len(set(self.accepted_property_types)):
            raise ValueError("Accepted property types must be unique")
        if len(self.accepted_rental_durations) != len(set(self.accepted_rental_durations)):
            raise ValueError("Accepted rental durations must be unique")
        normalized_districts = [
            _normalize_place(value) for value in self.acceptable_districts
        ]
        if any(not district for district in normalized_districts):
            raise ValueError("Acceptable districts cannot contain empty values")
        if len(normalized_districts) != len(set(normalized_districts)):
            raise ValueError("Acceptable districts must be unique")


@dataclass(frozen=True, slots=True)
class SearchDecision:
    """Explain why a normalized listing is or is not eligible."""

    eligible: bool
    rejection_reasons: tuple[str, ...]
    preference_notes: tuple[str, ...]


def evaluate_listing(
    listing: Listing,
    pet_decision: PetDecision,
    profile: SearchProfile,
) -> SearchDecision:
    """Apply hard filters without turning preferences into false exclusions."""

    rejections: list[str] = []
    notes: list[str] = []

    if not profile.enabled:
        rejections.append("profile_disabled")

    if listing.property_type not in profile.accepted_property_types:
        rejections.append("property_type_mismatch")

    if listing.price_eur is None:
        rejections.append("rent_not_disclosed")
    elif listing.price_eur > profile.max_rent_eur:
        rejections.append("rent_above_maximum")

    expected_municipality = _normalize_place(profile.municipality)
    actual_municipality = _normalize_place(listing.municipality)
    accepted_places = {
        expected_municipality,
        f"{expected_municipality} capital",
        f"{expected_municipality} ciudad",
    }
    if not actual_municipality:
        rejections.append("municipality_not_disclosed")
    elif actual_municipality not in accepted_places:
        rejections.append("outside_selected_municipality")

    if profile.acceptable_districts:
        actual_district = _normalize_place(listing.district)
        accepted_districts = {
            _normalize_place(district) for district in profile.acceptable_districts
        }
        if not actual_district:
            rejections.append("district_not_disclosed")
        elif actual_district not in accepted_districts:
            rejections.append("outside_selected_districts")

    if profile.requires_pet_acceptance and pet_decision.status is PetStatus.REJECTED:
        rejections.append("pets_rejected")

    if listing.bedrooms is None:
        if profile.allow_unknown_bedrooms:
            notes.append("bedrooms_not_disclosed")
        else:
            rejections.append("bedrooms_not_disclosed")
    elif listing.bedrooms == 0:
        if profile.allow_studios:
            notes.append("studio")
        else:
            rejections.append("studio")
    elif listing.bedrooms < profile.preferred_min_bedrooms:
        notes.append("below_preferred_bedrooms")

    if profile.furnished is not None:
        if listing.is_furnished is None:
            notes.append("furnished_status_not_disclosed")
        elif listing.is_furnished is not profile.furnished:
            rejections.append("furnished_status_mismatch")

    if profile.move_in_asap and listing.available_from is None:
        notes.append("availability_not_disclosed")
    if (
        profile.move_in_by is not None
        and listing.available_from is not None
        and listing.available_from > profile.move_in_by
    ):
        rejections.append("available_too_late")

    if listing.rental_duration is RentalDuration.NOT_SPECIFIED:
        notes.append("rental_duration_not_disclosed")
    elif listing.rental_duration not in profile.accepted_rental_durations:
        rejections.append("rental_duration_mismatch")

    return SearchDecision(
        eligible=not rejections,
        rejection_reasons=tuple(rejections),
        preference_notes=tuple(notes),
    )
