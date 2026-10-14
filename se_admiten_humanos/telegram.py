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

from .models import Listing, PetDecision, PetStatus
from .persistence import RentalStore
from .pipeline import ProcessingResult, ProfileMatchOutcome


_TOKEN = re.compile(r"^[0-9]+:[A-Za-z0-9_-]+$")
_CHAT_ID = re.compile(r"^-?[0-9]+$")
_PET_LABELS = {
    PetStatus.ACCEPTED: "aceptadas",
    PetStatus.CONDITIONAL: "con condiciones",
    PetStatus.NOT_SPECIFIED: "no especificado",
    PetStatus.REJECTED: "no admitidas",
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
    lines = [f"🏠 {listing.title or 'Nueva vivienda'}"]
    compact_facts = " · ".join(value for value in facts if value)
    if compact_facts:
        lines.extend((compact_facts, ""))
    lines.append(f"Mascotas: {_PET_LABELS[pet_decision.status]}")
    if pet_decision.status is PetStatus.NOT_SPECIFIED:
        lines.append("Evidencia: el anuncio no menciona mascotas")
    elif pet_decision.evidence:
        lines.append(f"Evidencia: {pet_decision.evidence}")
    lines.extend(("", f"Búsqueda: {outcome.profile.name}", f"Fuente: {listing.source_id}"))
    return "\n".join(lines)[:4096]


class TelegramBotClient:
    """Small dependency-free client for the Telegram Bot API."""

    def __init__(self, token: str, timeout_seconds: float = 10) -> None:
        if not _TOKEN.fullmatch(token.strip()):
            raise ValueError("Invalid Telegram bot token format")
        self._token = token.strip()
        self.timeout_seconds = timeout_seconds

    def send_message(self, chat_id: str, text: str, listing_url: str = "") -> int:
        normalized_chat_id = chat_id.strip()
        if not _CHAT_ID.fullmatch(normalized_chat_id):
            raise ValueError("Telegram chat id must be numeric")
        payload: dict[str, object] = {"chat_id": normalized_chat_id, "text": text}
        safe_url = _listing_url(listing_url)
        if safe_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Abrir anuncio", "url": safe_url}]]
            }
        request = Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
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
        return int(result["result"]["message_id"])


class TelegramNotifier:
    """Deliver candidates only to explicitly mapped user chat ids."""

    channel = "telegram"

    def __init__(
        self,
        store: RentalStore,
        client: TelegramBotClient,
        user_chat_ids: Mapping[str, str],
    ) -> None:
        self.store = store
        self.client = client
        self.user_chat_ids = dict(user_chat_ids)

    def deliver(self, result: ProcessingResult) -> tuple[DeliveryAttempt, ...]:
        attempts: list[DeliveryAttempt] = []
        for outcome in result.delivery_candidates:
            match_id = outcome.match.match_id
            user_id = outcome.profile.user_id
            chat_id = self.user_chat_ids.get(user_id, "")
            if not chat_id:
                attempts.append(DeliveryAttempt(match_id, user_id, "skipped", "no chat configured"))
                continue
            if not self.store.claim_notification(match_id, self.channel):
                attempts.append(DeliveryAttempt(match_id, user_id, "skipped", "already claimed"))
                continue
            try:
                self.client.send_message(
                    chat_id,
                    format_alert(result.listing, result.pet_decision, outcome),
                    result.listing.url,
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
    """Build the single-user MVP notifier without storing secrets in Git."""

    values = os.environ if environment is None else environment
    required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "TELEGRAM_CHAT_ID")
    missing = [name for name in required if not values.get(name, "").strip()]
    if missing:
        raise ValueError(f"Missing Telegram configuration: {', '.join(missing)}")
    user_id = values["TELEGRAM_USER_ID"].strip()
    return TelegramNotifier(
        store,
        TelegramBotClient(values["TELEGRAM_BOT_TOKEN"]),
        {user_id: values["TELEGRAM_CHAT_ID"].strip()},
    )
