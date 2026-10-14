"""Generate configurable Spanish outreach messages."""

from __future__ import annotations

from .models import Listing, Pet, PetDecision, PetStatus, RenterProfile


SPANISH_COUNTS = {1: "un", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco"}


def describe_pets(pets: tuple[Pet, ...]) -> str:
    """Describe structured pets for a Spanish outreach message."""

    descriptions: list[str] = []
    for pet in pets:
        count = SPANISH_COUNTS.get(pet.count, str(pet.count))
        species = pet.species.strip()
        if pet.count > 1 and not species.endswith("s"):
            species = f"{species}s"
        descriptions.append(f"{count} {species}")
    if not descriptions:
        return "ninguna mascota"
    if len(descriptions) == 1:
        return descriptions[0]
    return ", ".join(descriptions[:-1]) + f" y {descriptions[-1]}"


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
    pets = describe_pets(profile.pets)
    greeting = f"Hola, {listing.contact_name}:" if listing.contact_name else "Hola:"
    opening = (
        f"Mi nombre es {profile.sender_name} y les escribo en nombre de "
        f"{profile.renter_name}, que está interesada en {reference} y busca mudarse "
        "lo antes posible."
    )
    employment = ""
    if profile.contract_type and profile.job:
        employment = f"Tiene {profile.contract_type} como {profile.job}"
    elif profile.contract_type:
        employment = f"Tiene {profile.contract_type}"
    elif profile.job:
        employment = f"Trabaja como {profile.job}"
    income = (
        f", con ingresos mensuales de {profile.monthly_income_eur} €"
        if profile.monthly_income_eur is not None
        else ""
    )
    living_arrangement = (
        f"Viviría con {pets}." if profile.pets else "No viviría con mascotas."
    )
    renter_summary = f"{employment}{income}. {living_arrangement}".strip()

    if pet_decision.status is PetStatus.ACCEPTED or not profile.pets:
        question = (
            (
                "Hemos visto que la vivienda admite mascotas. "
                if profile.pets
                else ""
            )
            + "¿Sería posible concertar una visita lo antes posible?"
        )
    else:
        question = (
            f"¿Podrían confirmarnos si se admiten {pets} y si sería posible "
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
