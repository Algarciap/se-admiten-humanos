"""Generate configurable Spanish outreach messages."""

from __future__ import annotations

from .models import Listing, PetDecision, PetStatus, RenterProfile


def listing_reference(listing: Listing) -> str:
    parts = [value for value in (listing.title, listing.price, listing.location) if value]
    return " · ".join(parts) if parts else "la vivienda anunciada"


def build_message(
    listing: Listing,
    pet_decision: PetDecision,
    profile: RenterProfile,
) -> str:
    """Build the approved message variant for a normalized listing."""

    reference = listing_reference(listing)
    greeting = f"Hola, {listing.contact_name}:" if listing.contact_name else "Hola:"
    opening = (
        f"Mi nombre es {profile.sender_name} y les escribo en nombre de "
        f"{profile.renter_name}, que está interesada en {reference} y busca mudarse "
        "lo antes posible."
    )
    renter_summary = (
        f"{profile.renter_name} tiene contrato indefinido como {profile.job}, con ingresos "
        f"mensuales de {profile.monthly_income} €, y viviría con {profile.pets}."
    )

    if pet_decision.status is PetStatus.ACCEPTED:
        question = (
            "Hemos visto que la vivienda admite mascotas. "
            "¿Sería posible concertar una visita lo antes posible?"
        )
    else:
        question = (
            f"¿Podrían confirmarnos si se admiten {profile.pets} y si sería posible "
            "concertar una visita?"
        )

    return "\n".join(
        (
            greeting,
            "",
            opening,
            "",
            renter_summary,
            "",
            question,
            "",
            "Muchas gracias. Quedo pendiente de su respuesta.",
            "",
            "Un saludo,",
            profile.sender_name,
        )
    )


def subject(listing: Listing) -> str:
    return f"Interés en alquiler: {listing_reference(listing)}"
