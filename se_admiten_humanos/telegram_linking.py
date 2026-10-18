"""Secure one-time account linking through Telegram deep links."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from .persistence import RentalStore


_BOT_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_START_COMMAND = re.compile(
    r"^/start(?:@[A-Za-z][A-Za-z0-9_]{4,31})?(?: ([A-Za-z0-9_-]{20,64}))?$"
)

# Kept together so the copy pass can revise bot voice without touching command routing.
_WELCOME = "Configura tu búsqueda en Se admiten humanos y conecta Telegram para recibir avisos."
_INVALID_LINK = "Este enlace ya no es válido. Entra en tu cuenta para generar uno nuevo."
_HELP = "Este bot envía avisos de Se admiten humanos. Gestiona tu búsqueda desde tu cuenta."
_CONNECTED = "✅ Telegram conectado. Recibirás aquí solo los pisos que encajen con tu búsqueda."


class TelegramLinkClient(Protocol):
    """The small Bot API surface required by the link processor."""

    def get_updates(self, offset: int, *, limit: int = 100) -> tuple[dict[str, object], ...]: ...

    def send_message(
        self,
        chat_id: str,
        text: str,
        action_url: str = "",
        action_text: str = "Abrir anuncio",
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class TelegramLink:
    """A short-lived capability returned once to an authenticated user."""

    user_id: str
    token: str
    expires_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class TelegramLinkPollResult:
    updates_seen: int = 0
    links_created: int = 0
    ignored_updates: int = 0
    rejected_links: int = 0
    confirmation_failures: int = 0


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_telegram_link(
    store: RentalStore,
    user_id: str,
    bot_username: str,
    *,
    ttl: timedelta = timedelta(minutes=10),
    now: datetime | None = None,
) -> TelegramLink:
    """Create a single-use deep link while persisting only its digest."""

    normalized_username = bot_username.strip().removeprefix("@")
    if not _BOT_USERNAME.fullmatch(normalized_username):
        raise ValueError("Telegram bot username is invalid")
    if ttl <= timedelta(0) or ttl > timedelta(hours=24):
        raise ValueError("Telegram link lifetime must be between 1 second and 24 hours")
    issued_at = datetime.now(UTC) if now is None else now
    if issued_at.tzinfo is None:
        raise ValueError("Telegram link creation time must include a timezone")
    expires_at = issued_at.astimezone(UTC) + ttl
    token = secrets.token_urlsafe(24)
    store.save_telegram_link_token(user_id, _token_hash(token), expires_at)
    return TelegramLink(
        user_id=user_id.strip(),
        token=token,
        expires_at=expires_at,
        url=f"https://t.me/{normalized_username}?start={token}",
    )


def process_telegram_link_updates(
    store: RentalStore,
    client: TelegramLinkClient,
    public_base_url: str = "",
) -> TelegramLinkPollResult:
    """Consume private-chat `/start` links and durably advance the update cursor."""

    offset = store.telegram_next_update_id()
    updates = client.get_updates(offset, limit=100)
    linked = ignored = rejected = confirmation_failures = 0
    seen = 0
    for update in sorted(updates, key=_update_sort_key):
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < offset:
            ignored += 1
            continue
        seen += 1
        message = update.get("message")
        if not isinstance(message, dict):
            ignored += 1
            store.save_telegram_next_update_id(update_id + 1)
            continue
        chat = message.get("chat")
        text = message.get("text")
        if (
            not isinstance(chat, dict)
            or chat.get("type") != "private"
            or not isinstance(chat.get("id"), int)
            or not isinstance(text, str)
        ):
            ignored += 1
            store.save_telegram_next_update_id(update_id + 1)
            continue
        chat_id = str(chat["id"])
        normalized_text = text.strip()
        match = _START_COMMAND.fullmatch(normalized_text)
        try:
            if match is not None and match.group(1):
                try:
                    store.consume_telegram_link_token(_token_hash(match.group(1)), chat_id)
                except ValueError:
                    rejected += 1
                    client.send_message(
                        chat_id,
                        _INVALID_LINK,
                        _account_url(public_base_url, "/account"),
                        "Ir a mi cuenta",
                    )
                else:
                    linked += 1
                    client.send_message(chat_id, _CONNECTED)
            elif match is not None:
                client.send_message(
                    chat_id,
                    _WELCOME,
                    _account_url(public_base_url, "/account/register"),
                    "Crear mi cuenta",
                )
            elif normalized_text.startswith("/start"):
                rejected += 1
                client.send_message(
                    chat_id,
                    _INVALID_LINK,
                    _account_url(public_base_url, "/account"),
                    "Ir a mi cuenta",
                )
            else:
                ignored += 1
                client.send_message(
                    chat_id,
                    _HELP,
                    _account_url(public_base_url, "/account"),
                    "Ir a mi cuenta",
                )
        except Exception:
            confirmation_failures += 1
        finally:
            store.save_telegram_next_update_id(update_id + 1)
    return TelegramLinkPollResult(
        updates_seen=seen,
        links_created=linked,
        ignored_updates=ignored,
        rejected_links=rejected,
        confirmation_failures=confirmation_failures,
    )


def _update_sort_key(update: dict[str, object]) -> int:
    update_id = update.get("update_id")
    return update_id if isinstance(update_id, int) else -1


def _account_url(public_base_url: str, path: str) -> str:
    """Return an HTTPS account URL, or no action when the public origin is unavailable."""

    origin = public_base_url.strip()
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return urljoin(origin.rstrip("/") + "/", path.lstrip("/"))
