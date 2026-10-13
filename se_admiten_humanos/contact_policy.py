"""Fail-closed rules for selecting direct email recipients."""

from __future__ import annotations

import re
from collections.abc import Iterable


EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@(?:[A-Z0-9-]+\.)+[A-Z]{2,}\b",
    re.IGNORECASE,
)


def normalize_allowlist(domains: str | Iterable[str]) -> frozenset[str]:
    """Normalize a comma-separated value or an iterable of domain names."""

    values = domains.split(",") if isinstance(domains, str) else domains
    return frozenset(
        value.strip().lower().lstrip("@")
        for value in values
        if value and value.strip()
    )


def email_domain_allowed(email: str, allowed_domains: str | Iterable[str]) -> bool:
    """Return true only for an exact, explicitly approved recipient domain."""

    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain in normalize_allowlist(allowed_domains)


def approved_contact_email(text: str, allowed_domains: str | Iterable[str]) -> str:
    """Return the first email whose exact domain is approved, or an empty string."""

    for match in EMAIL_PATTERN.finditer(text or ""):
        candidate = match.group(0).lower()
        if email_domain_allowed(candidate, allowed_domains):
            return candidate
    return ""
