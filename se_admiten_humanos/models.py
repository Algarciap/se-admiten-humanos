"""Shared immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class PetStatus(str, Enum):
    """Supported pet-policy outcomes."""

    ACCEPTED = "accepted"
    CONDITIONAL = "conditional"
    NOT_SPECIFIED = "not_specified"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PetDecision:
    """Explainable result of evaluating a listing's pet policy."""

    status: PetStatus
    source: str
    evidence: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Listing:
    """Normalized listing fields used by classification and outreach."""

    source_id: str = "unknown"
    external_id: str = ""
    title: str = ""
    price: str = ""
    price_eur: int | None = None
    location: str = ""
    municipality: str = ""
    district: str = ""
    address: str = ""
    bedrooms: int | None = None
    area_sqm: int | None = None
    is_furnished: bool | None = None
    available_from: date | None = None
    description: str = ""
    url: str = ""
    contact_name: str = ""
    contact_email: str = ""


@dataclass(frozen=True, slots=True)
class WebsiteDiscovery:
    """A lightweight listing identity found on a public discovery page."""

    external_id: str
    url: str
    source_last_modified: str = ""

    def __post_init__(self) -> None:
        if not self.external_id.strip():
            raise ValueError("Website discovery external id cannot be empty")
        if not self.url.strip():
            raise ValueError("Website discovery URL cannot be empty")


@dataclass(frozen=True, slots=True)
class User:
    """Application account that owns renter and search profiles."""

    id: str
    display_name: str
    email: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("User id cannot be empty")
        if not self.display_name.strip():
            raise ValueError("User display name cannot be empty")


@dataclass(frozen=True, slots=True)
class Pet:
    """Structured pet details used by matching and outreach."""

    id: str
    species: str
    count: int = 1
    name: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Pet id cannot be empty")
        if not self.species.strip():
            raise ValueError("Pet species cannot be empty")
        if self.count <= 0:
            raise ValueError("Pet count must be greater than zero")


@dataclass(frozen=True, slots=True)
class RenterProfile:
    """Private outreach details owned by one user."""

    id: str
    user_id: str
    label: str
    sender_name: str
    renter_name: str
    monthly_income_eur: int | None = None
    job: str = ""
    contract_type: str = ""
    pets: tuple[Pet, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "Renter profile id": self.id,
            "User id": self.user_id,
            "Profile label": self.label,
            "Sender name": self.sender_name,
            "Renter name": self.renter_name,
        }
        for field_name, value in required.items():
            if not value.strip():
                raise ValueError(f"{field_name} cannot be empty")
        if self.monthly_income_eur is not None and self.monthly_income_eur <= 0:
            raise ValueError("Monthly income must be greater than zero")
        pet_ids = [pet.id for pet in self.pets]
        if len(pet_ids) != len(set(pet_ids)):
            raise ValueError("Pet ids must be unique within a renter profile")
