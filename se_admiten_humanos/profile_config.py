"""Load private local profile data without hardcoding one renter into the app."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .models import Pet, PropertyType, RentalDuration, RenterProfile, User
from .search_profile import SearchProfile


@dataclass(frozen=True, slots=True)
class ProfileBundle:
    """One user and all renter/search profiles imported together."""

    user: User
    renter_profiles: tuple[RenterProfile, ...]
    search_profiles: tuple[SearchProfile, ...]

    def __post_init__(self) -> None:
        renter_ids = {profile.id for profile in self.renter_profiles}
        if len(renter_ids) != len(self.renter_profiles):
            raise ValueError("Renter profile ids must be unique")
        search_ids = {profile.id for profile in self.search_profiles}
        if len(search_ids) != len(self.search_profiles):
            raise ValueError("Search profile ids must be unique")
        for profile in self.renter_profiles:
            if profile.user_id != self.user.id:
                raise ValueError("Renter profile belongs to a different user")
        for profile in self.search_profiles:
            if profile.user_id != self.user.id:
                raise ValueError("Search profile belongs to a different user")
            if (
                profile.renter_profile_id is not None
                and profile.renter_profile_id not in renter_ids
            ):
                raise ValueError("Search references an unknown renter profile")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return value


def _items(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    return value


def _boolean(values: Mapping[str, Any], field: str, default: bool) -> bool:
    value = values.get(field, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a JSON boolean")
    return value


def _optional_boolean(values: Mapping[str, Any], field: str) -> bool | None:
    value = values.get(field)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field} must be a JSON boolean or null")
    return value


def _pet(payload: Any) -> Pet:
    values = _mapping(payload, "pet")
    return Pet(
        id=str(values["id"]),
        species=str(values["species"]),
        count=int(values.get("count", 1)),
        name=str(values.get("name", "")),
        notes=str(values.get("notes", "")),
    )


def _renter_profile(payload: Any) -> RenterProfile:
    values = _mapping(payload, "renter profile")
    return RenterProfile(
        id=str(values["id"]),
        user_id=str(values["user_id"]),
        label=str(values["label"]),
        sender_name=str(values["sender_name"]),
        renter_name=str(values["renter_name"]),
        monthly_income_eur=(
            int(values["monthly_income_eur"])
            if values.get("monthly_income_eur") is not None
            else None
        ),
        job=str(values.get("job", "")),
        contract_type=str(values.get("contract_type", "")),
        pets=tuple(_pet(item) for item in _items(values.get("pets", []), "pets")),
    )


def _search_profile(payload: Any) -> SearchProfile:
    values = _mapping(payload, "search profile")
    move_in_by_value = values.get("move_in_by")
    duration_values = _items(
        values.get("accepted_rental_durations", ["long_term"]),
        "accepted_rental_durations",
    )
    try:
        accepted_durations = tuple(RentalDuration(str(item)) for item in duration_values)
    except ValueError as error:
        raise ValueError("accepted_rental_durations contains an unknown value") from error
    property_type_values = _items(
        values.get("accepted_property_types", ["home"]),
        "accepted_property_types",
    )
    try:
        accepted_property_types = tuple(
            PropertyType(str(item)) for item in property_type_values
        )
    except ValueError as error:
        raise ValueError("accepted_property_types contains an unknown value") from error
    return SearchProfile(
        id=str(values["id"]),
        user_id=str(values["user_id"]),
        name=str(values["name"]),
        max_rent_eur=int(values["max_rent_eur"]),
        municipality=str(values["municipality"]),
        renter_profile_id=(
            str(values["renter_profile_id"])
            if values.get("renter_profile_id") is not None
            else None
        ),
        acceptable_districts=tuple(
            str(item)
            for item in _items(
                values.get("acceptable_districts", []),
                "acceptable_districts",
            )
        ),
        accepted_property_types=accepted_property_types,
        preferred_min_bedrooms=int(values.get("preferred_min_bedrooms", 1)),
        allow_studios=_boolean(values, "allow_studios", True),
        allow_unknown_bedrooms=_boolean(
            values,
            "allow_unknown_bedrooms",
            True,
        ),
        furnished=_optional_boolean(values, "furnished"),
        move_in_asap=_boolean(values, "move_in_asap", True),
        move_in_by=(
            date.fromisoformat(str(move_in_by_value)) if move_in_by_value else None
        ),
        accepted_rental_durations=accepted_durations,
        requires_pet_acceptance=_boolean(
            values,
            "requires_pet_acceptance",
            False,
        ),
        enabled=_boolean(values, "enabled", True),
    )


def parse_profile_bundle(payload: Any) -> ProfileBundle:
    """Validate a decoded private profile bundle without logging its contents."""

    payload = _mapping(payload, "profile bundle")
    user_values = _mapping(payload["user"], "user")
    user = User(
        id=str(user_values["id"]),
        display_name=str(user_values["display_name"]),
        email=str(user_values.get("email", "")),
    )
    return ProfileBundle(
        user=user,
        renter_profiles=tuple(
            _renter_profile(item)
            for item in _items(payload.get("renter_profiles", []), "renter_profiles")
        ),
        search_profiles=tuple(
            _search_profile(item)
            for item in _items(payload.get("search_profiles", []), "search_profiles")
        ),
    )


def load_profile_bundle_json(value: str) -> ProfileBundle:
    """Read a private profile bundle from an environment-safe JSON string."""

    if not value.strip():
        raise ValueError("Profile bundle JSON cannot be empty")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(
            "Profile bundle is invalid JSON at "
            f"line {error.lineno}, column {error.colno}"
        ) from error
    return parse_profile_bundle(payload)


def load_profile_bundle(path: str | Path) -> ProfileBundle:
    """Read and validate one private profile bundle from JSON."""

    with Path(path).open(encoding="utf-8") as handle:
        return parse_profile_bundle(json.load(handle))
