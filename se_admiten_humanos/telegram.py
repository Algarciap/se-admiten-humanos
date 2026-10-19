"""Telegram delivery with atomic notification claims and explicit failures."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import Listing, PetDecision, PetStatus, PropertyType, RentalDuration
from .outreach import build_copy_message
from .persistence import RentalStore
from .pipeline import ProcessingResult, ProfileMatchOutcome
from .search_profile import MatchTier


_TOKEN = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")
_CHAT_ID = re.compile(r"^-?[0-9]+$")
_PET_LABELS = {
    PetStatus.ACCEPTED: "aceptadas",
    PetStatus.CONDITIONAL: "con condiciones",
    PetStatus.NOT_SPECIFIED: "no especificado",
    PetStatus.REJECTED: "no admitidas",
}
_DURATION_LABELS = {
    RentalDuration.LONG_TERM: "larga duración",
    RentalDuration.TEMPORARY: "temporal",
    RentalDuration.NOT_SPECIFIED: "no especificado",
}
_PROPERTY_TYPE_LABELS = {
    PropertyType.HOME: "vivienda completa",
    PropertyType.ROOM: "habitación",
    PropertyType.GARAGE: "garaje",
    PropertyType.COMMERCIAL: "local comercial",
    PropertyType.NOT_SPECIFIED: "no especificado",
}


class TelegramDeliveryError(RuntimeError):
    """A Telegram request failed without exposing credentials."""


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    match_id: int
    user_id: str
    status: str
    detail: str = ""


def _listing_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    return url.strip() if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def format_alert(
    listing: Listing,
    pet_decision: PetDecision,
    outcome: ProfileMatchOutcome,
) -> str:
    """Create a compact Spanish alert with pet-policy evidence."""

    facts = [listing.price or (f"{listing.price_eur} €" if listing.price_eur else "")]
    facts.append(listing.location or listing.district or listing.municipality)
    if listing.bedrooms is not None:
        facts.append(
            "estudio" if listing.bedrooms == 0 else f"{listing.bedrooms} dormitorio(s)"
        )
    if listing.area_sqm is not None:
        facts.append(f"{listing.area_sqm} m²")
    if outcome.decision.match_tier is MatchTier.BUDGET_STRETCH:
        difference = (listing.price_eur or 0) - outcome.profile.max_rent_eur
        lines = [
            f"🟡 Sugerencia urgente: {difference} € sobre tu presupuesto preferido",
            f"🏠 {listing.title or 'Nueva vivienda'}",
        ]
    else:
        lines = [
            "🟢 Dentro de tu presupuesto",
            f"🏠 {listing.title or 'Nueva vivienda'}",
        ]
    compact_facts = " · ".join(value for value in facts if value)
    if compact_facts:
        lines.extend((compact_facts, ""))
    lines.append(f"Mascotas: {_PET_LABELS[pet_decision.status]}")
    if pet_decision.status is PetStatus.NOT_SPECIFIED:
        lines.append("Evidencia: el anuncio no menciona mascotas")
    elif pet_decision.evidence:
        lines.append(f"Evidencia: {pet_decision.evidence}")
    lines.append(f"Tipo de alquiler: {_DURATION_LABELS[listing.rental_duration]}")
    lines.append(f"Tipo de inmueble: {_PROPERTY_TYPE_LABELS[listing.property_type]}")
    if listing.rental_duration_evidence:
        lines.append(f"Evidencia de duración: {listing.rental_duration_evidence}")
    lines.extend(("", f"Búsqueda: {outcome.profile.name}", f"Fuente: {listing.source_id}"))
    return "\n".join(lines)[:4096]


class TelegramBotClient:
    """Small dependency-free client for the Telegram Bot API."""

    def __init__(self, token: str, timeout_seconds: float = 10) -> None:
        if not _TOKEN.fullmatch(token.strip()):
            raise ValueError("Invalid Telegram bot token format")
        self._token = token.strip()
        self.timeout_seconds = timeout_seconds

    def send_message(
        self,
        chat_id: str,
        text: str,
        action_url: str = "",
        action_text: str = "Abrir anuncio",
        copy_text: str = "",
    ) -> int:
        normalized_chat_id = chat_id.strip()
        if not _CHAT_ID.fullmatch(normalized_chat_id):
            raise ValueError("Telegram chat id must be numeric")
        payload: dict[str, object] = {"chat_id": normalized_chat_id, "text": text}
        safe_url = _listing_url(action_url)
        buttons: list[dict[str, object]] = []
        if safe_url:
            buttons.append({"text": action_text[:64], "url": safe_url})
        if copy_text:
            if len(copy_text) > 256:
                raise ValueError("Telegram copy text cannot exceed 256 characters")
            buttons.append(
                {
                    "text": "Copiar mensaje",
                    "copy_text": {"text": copy_text},
                }
            )
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        result = self._request("sendMessage", payload)
        return int(result["message_id"])

    def get_updates(self, offset: int, *, limit: int = 100) -> tuple[dict[str, object], ...]:
        """Fetch message updates from a durable offset without long polling."""

        if offset < 0:
            raise ValueError("Telegram update offset cannot be negative")
        if not 1 <= limit <= 100:
            raise ValueError("Telegram update limit must be between 1 and 100")
        result = self._request(
            "getUpdates",
            {
                "offset": offset,
                "limit": limit,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramDeliveryError("Telegram returned malformed updates")
        return tuple(result)

    def _request(self, method: str, payload: Mapping[str, object]) -> object:
        request = Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise TelegramDeliveryError(f"Telegram request failed: {type(error).__name__}") from error
        if result.get("ok") is not True:
            description = str(result.get("description", "unknown Telegram error"))
            raise TelegramDeliveryError(description[:500])
        if "result" not in result:
            raise TelegramDeliveryError("Telegram response omitted its result")
        return result["result"]


class TelegramNotifier:
    """Deliver candidates only to each user's database-backed chat id."""

    channel = "telegram"

    def __init__(
        self,
        store: RentalStore,
        client: TelegramBotClient,
    ) -> None:
        self.store = store
        self.client = client

    def deliver(self, result: ProcessingResult) -> tuple[DeliveryAttempt, ...]:
        attempts: list[DeliveryAttempt] = []
        for outcome in result.delivery_candidates:
            match_id = outcome.match.match_id
            user_id = outcome.profile.user_id
            chat_id = self.store.telegram_chat_id(user_id)
            if not chat_id:
                attempts.append(DeliveryAttempt(match_id, user_id, "skipped", "no chat configured"))
                continue
            if not self.store.claim_notification(match_id, self.channel):
                attempts.append(DeliveryAttempt(match_id, user_id, "skipped", "already claimed"))
                continue
            try:
                renter_profile = (
                    self.store.load_renter_profile(
                        outcome.profile.renter_profile_id,
                        user_id=user_id,
                    )
                    if outcome.profile.renter_profile_id
                    else self.store.load_primary_renter_profile(user_id)
                )
                copy_text = (
                    build_copy_message(result.pet_decision, renter_profile)
                    if renter_profile is not None
                    else ""
                )
                self.client.send_message(
                    chat_id,
                    format_alert(result.listing, result.pet_decision, outcome),
                    result.listing.url,
                    copy_text=copy_text,
                )
            except Exception as error:
                if isinstance(error, TelegramDeliveryError):
                    detail = f"TelegramDeliveryError: {error}"[:500]
                else:
                    detail = type(error).__name__
                self.store.mark_notification_failed(match_id, self.channel, detail)
                attempts.append(DeliveryAttempt(match_id, user_id, "failed", detail))
            else:
                self.store.mark_notification_sent(match_id, self.channel)
                attempts.append(DeliveryAttempt(match_id, user_id, "sent"))
        return tuple(attempts)


def notifier_from_environment(
    store: RentalStore,
    environment: Mapping[str, str] | None = None,
) -> TelegramNotifier:
    """Build a multi-user notifier and import the legacy one-user mapping once."""

    values = os.environ if environment is None else environment
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Missing Telegram configuration: TELEGRAM_BOT_TOKEN")

    user_id = values.get("TELEGRAM_USER_ID", "").strip()
    chat_id = values.get("TELEGRAM_CHAT_ID", "").strip()
    if bool(user_id) != bool(chat_id):
        missing = "TELEGRAM_CHAT_ID" if user_id else "TELEGRAM_USER_ID"
        raise ValueError(f"Missing Telegram configuration: {missing}")
    from .accounts import digest
    blocked = store.connection.execute("select 1 from bootstrap_blocks where user_hash = ?", (digest(user_id),)).fetchone()
    if user_id and chat_id and not blocked and not store.telegram_destination_configured(user_id):
        store.save_telegram_destination(user_id, chat_id)

    if (not store.load_telegram_destinations()
            and not store.connection.execute("select 1 from accounts union all select 1 from bootstrap_blocks limit 1").fetchone()):
        raise ValueError("At least one enabled Telegram destination is required")
    return TelegramNotifier(store, TelegramBotClient(token))
