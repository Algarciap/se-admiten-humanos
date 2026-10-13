"""Explainable Spanish and English pet-policy classification."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from .models import PetDecision, PetStatus


@dataclass(frozen=True, slots=True)
class _Match:
    evidence: str
    index: int


def _compile(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


ACCEPTED_PATTERNS = _compile(
    (
        r"(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:las?\s+)?(?:mascotas?|animales?)",
        r"(?:mascotas?|animales?)\s+(?:admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)",
        r"(?:pet[\s-]?friendly|pets?\s+(?:are\s+)?(?:welcome|allowed))",
        r"(?:gatos?|perros?)\s+(?:si|sí|admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)",
        r"no\s+(?:hay|habria|habría|existe)\s+(?:ningun\s+|ningún\s+)?problema\s+con\s+(?:las?\s+)?(?:mascotas?|animales?)",
    )
)

CONDITIONAL_PATTERNS = _compile(
    (
        r"(?:mascotas?|animales?)\s+(?:a\s+)?consultar",
        r"consultar\s+(?:por|sobre|si\s+se\s+admiten)?\s*(?:las?\s+)?(?:mascotas?|animales?)",
        r"(?:segun|según|dependiendo\s+de)\s+(?:la\s+)?mascota",
        r"(?:solo|sólo|unicamente|únicamente)\s+(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:mascotas?|animales?)\s+(?:pequenos|pequeños)",
        r"(?:mascotas?|animales?)\s+(?:pequenos|pequeños)\s+(?:admitid[oa]s?|aceptad[oa]s?)",
        r"pets?\s+(?:on\s+request|subject\s+to\s+approval)",
    )
)

CAT_EXCEPTION_PATTERNS = _compile(
    (
        r"(?:excepto|salvo)\s+(?:los\s+)?gatos?",
        r"gatos?\s+(?:si|sí|admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)",
    )
)

SIZE_RESTRICTION_PATTERNS = _compile(
    (
        r"no\s+(?:se\s+)?(?:admiten|aceptan|permiten)\s+(?:las?\s+)?(?:mascotas?|animales?)\s+(?:grandes|de\s+gran\s+tamaño)",
    )
)

REJECTED_PATTERNS = _compile(
    (
        r"no\s+(?:se\s+)?(?:admiten|aceptan|permiten|admitimos|aceptamos|permitimos)\s+(?:las?\s+)?(?:mascotas?|animales?)",
        r"(?:mascotas?|animales?)\s+(?:no\s+)?(?:admitid[oa]s?|aceptad[oa]s?|permitid[oa]s?)\s*:\s*no",
        r"(?:sin|prohibid[oa]s?)\s+(?:mascotas?|animales?)",
        r"(?:mascotas?|animales?)\s+no(?:\b|\s)",
        r"abstenerse\s+(?:personas?\s+)?con\s+(?:mascotas?|animales?)",
        r"no\s+gatos?(?:\b|\s)",
        r"(?:no\s+pets?|pets?\s+(?:are\s+)?not\s+allowed)",
    )
)


def normalize(value: str | None) -> str:
    """Normalize typography and whitespace without removing useful evidence."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_matches(text: str, patterns: Iterable[re.Pattern[str]]) -> list[_Match]:
    matches: list[_Match] = []
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            matches.append(_Match(evidence=match.group(0), index=match.start()))
    return matches


def _decision(
    status: PetStatus,
    source: str,
    evidence: str,
    confidence: float,
) -> PetDecision:
    return PetDecision(
        status=status,
        source=source,
        evidence=evidence,
        confidence=confidence,
    )


def classify(
    text: str | None,
    structured_pet_friendly: bool | None = None,
) -> PetDecision:
    """Classify pet wording while preserving the phrase used as evidence."""

    normalized = normalize(text)

    if structured_pet_friendly is True:
        return _decision(PetStatus.ACCEPTED, "structured_field", "pet-friendly field: yes", 1.0)
    if structured_pet_friendly is False:
        return _decision(PetStatus.REJECTED, "structured_field", "pet-friendly field: no", 1.0)

    accepted = _find_matches(normalized, ACCEPTED_PATTERNS)
    conditional = _find_matches(normalized, CONDITIONAL_PATTERNS)
    cat_exceptions = _find_matches(normalized, CAT_EXCEPTION_PATTERNS)
    size_restrictions = _find_matches(normalized, SIZE_RESTRICTION_PATTERNS)
    rejected = _find_matches(normalized, REJECTED_PATTERNS)

    if cat_exceptions:
        return _decision(PetStatus.ACCEPTED, "text", cat_exceptions[0].evidence, 0.99)
    if size_restrictions:
        return _decision(PetStatus.CONDITIONAL, "text", size_restrictions[0].evidence, 0.95)
    if rejected:
        return _decision(PetStatus.REJECTED, "text", rejected[0].evidence, 0.99)
    if conditional:
        return _decision(PetStatus.CONDITIONAL, "text", conditional[0].evidence, 0.95)
    if accepted:
        return _decision(PetStatus.ACCEPTED, "text", accepted[0].evidence, 0.99)

    return _decision(
        PetStatus.NOT_SPECIFIED,
        "absence",
        "No pet policy found in the available text",
        0.6,
    )
