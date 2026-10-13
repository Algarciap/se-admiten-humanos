"""Shared immutable domain models."""

from __future__ import annotations

from dataclasses import dataclass
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

    @property
    def should_notify(self) -> bool:
        return self.status is not PetStatus.REJECTED

    @property
    def should_contact(self) -> bool:
        return self.status is not PetStatus.REJECTED


@dataclass(frozen=True, slots=True)
class Listing:
    """Normalized listing fields used by classification and outreach."""

    source_id: str = "unknown"
    title: str = ""
    price: str = ""
    location: str = ""
    url: str = ""
    contact_name: str = ""
    contact_email: str = ""


@dataclass(frozen=True, slots=True)
class RenterProfile:
    """Private renter details supplied at runtime, never committed as defaults."""

    sender_name: str
    renter_name: str
    monthly_income: str
    job: str
    pets: str
