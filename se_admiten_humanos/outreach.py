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
    household = (
        "Viviría una persona en la vivienda."
        if profile.household_size == 1
        else f"Vivirían {profile.household_size} personas en la vivienda."
    )
    if profile.household_size == 1:
        living_arrangement = household + (
            f" Viviría con {pets}." if profile.pets else " No viviría con mascotas."
        )
    else:
        living_arrangement = household + (
            f" Convivirían con {pets}." if profile.pets else " No vivirían con mascotas."
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


def build_copy_message(
    pet_decision: PetDecision,
    profile: RenterProfile,
    *,
    max_length: int = 256,
) -> str:
    """Build a concise, income-first Idealista message for a copy button."""

    if max_length <= 0:
        raise ValueError("Maximum message length must be greater than zero")

    income = (
        f"Tengo ingresos demostrables de {profile.monthly_income_eur:,} €/mes".replace(
            ",", "."
        )
        if profile.monthly_income_eur is not None
        else "Puedo aportar documentación económica"
    )
    employment_details = " y ".join(
        value.strip() for value in (profile.contract_type, profile.job) if value.strip()
    )
    employment = f" y {employment_details}" if employment_details else ""
    people = (
        "Viviría una persona"
        if profile.household_size == 1
        else f"Viviríamos {profile.household_size} personas"
    )
    pets = describe_pets(profile.pets)
    household = people + (f" con {pets}" if profile.pets else ", sin mascotas")
    if profile.pets and pet_decision.status is PetStatus.ACCEPTED:
        question = "He visto que se admiten mascotas. ¿Podríamos concertar una visita?"
    elif profile.pets:
        question = f"¿Se admiten {pets}? ¿Podríamos concertar una visita?"
    else:
        question = "¿Podríamos concertar una visita?"

    def compose(*, include_job: bool, include_household: bool) -> str:
        work = employment if include_job else ""
        living = f" {household}." if include_household else ""
        return (
            f"Hola, me interesa la vivienda. {income}{work}.{living} "
            f"{question} Muchas gracias."
        )

    for include_job, include_household in ((True, True), (False, True), (False, False)):
        message = compose(include_job=include_job, include_household=include_household)
        if len(message) <= max_length:
            return message
    raise ValueError("The required contact message exceeds Telegram's copy limit")


def subject(listing: Listing) -> str:
    return f"Interés en alquiler: {listing_reference(listing)}"
