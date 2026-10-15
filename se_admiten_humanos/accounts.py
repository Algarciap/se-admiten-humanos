"""Verified account credentials and expiring capabilities; no raw secrets at rest."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlsplit
from uuid import uuid4

from .persistence import RentalStore


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_email(value: str) -> str:
    value = value.strip().casefold()
    # Accept one ordinary mailbox only; reject address lists, display-name syntax
    # and control characters before constructing an SMTP envelope.
    local, _, _domain = value.partition("@")
    if (len(value) > 254 or len(local) > 64 or local.startswith(".")
            or local.endswith(".") or ".." in local
            or not re.fullmatch(
                r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)):
        raise ValueError("Escribe un correo electrónico válido.")
    return value


def password_hash(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Usa una contraseña de entre 12 y 128 caracteres.")
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return f"pbkdf2-sha256$600000${salt}${hashed.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    if len(password) > 128:
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2-sha256" or int(iterations) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = "pbkdf2-sha256$600000$" + "00" * 16 + "$" + "00" * 32


@dataclass(frozen=True, slots=True)
class MailSettings:
    public_url: str
    host: str
    sender: str
    port: int = 587
    username: str = ""
    password: str = field(default="", repr=False)
    implicit_tls: bool = False

    def validate(self, secure: bool = True) -> None:
        url = urlsplit(self.public_url)
        if (url.scheme not in ({"https"} if secure else {"http", "https"})
                or not url.netloc or url.username or url.password or url.query
                or url.fragment or url.path not in {"", "/"}):
            raise ValueError("PUBLIC_BASE_URL must be an origin (HTTPS in production)")
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("SMTP_HOST and a valid SMTP_PORT are required")
        normalize_email(self.sender)
        if bool(self.username) != bool(self.password):
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")


class AccountMailError(RuntimeError):
    """Sanitized transport failure, without credentials, addresses or tokens."""


def send_account_email(settings: MailSettings, recipient: str, purpose: str, token: str) -> None:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = recipient
    message["Subject"] = "Verifica tu cuenta" if purpose == "verify" else "Recupera tu cuenta"
    message.set_content(
        "Se admiten humanos\n\n"
        + ("Verifica tu correo y elige tu contraseña" if purpose == "verify" else "Elige una nueva contraseña")
        + f":\n{settings.public_url.rstrip('/')}/account/{purpose}?token={token}\n\n"
        + "El enlace es de un solo uso y caduca en 30 minutos. "
        + "Si no lo has solicitado, ignora este mensaje."
    )
    context = ssl.create_default_context()
    try:
        if settings.implicit_tls:
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=15)
        with client:
            if not settings.implicit_tls:
                client.starttls(context=context)
            if settings.username:
                client.login(settings.username, settings.password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise AccountMailError("No se pudo enviar el correo. Inténtalo más tarde.") from None


class Accounts:
    def __init__(self, store: RentalStore):
        self.store = store
        self.db = store.connection

    def account(self, user_id: str | None):
        return self.db.execute(
            "select user_id, email, password_hash, verified, alerts_paused from accounts where user_id = ?",
            (user_id,),
        ).fetchone()

    def allow(self, key: str, limit: int, seconds: int = 900) -> bool:
        """Persistent fixed windows shared by HTTP threads and service restarts."""
        now = int(time.time())
        with self.db:
            self.db.execute("delete from auth_limits where window_start < ?", (now - 86400,))
            row = self.db.execute("""
                insert into auth_limits (key_hash, window_start, attempts) values (?, ?, 1)
                on conflict(key_hash) do update set
                    attempts = case when window_start <= ? then 1 else attempts + 1 end,
                    window_start = case when window_start <= ? then excluded.window_start else window_start end
                returning attempts
            """, (digest(key), now, now - seconds, now - seconds)).fetchone()
        return row[0] <= limit

    def issue(self, email: str, purpose: str, legacy_user_id: str | None = None) -> str | None:
        """Never match a legacy profile by an unverified email address."""
        email = normalize_email(email)
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("begin immediate")
            row = self.db.execute("select user_id, verified from accounts where email = ?", (email,)).fetchone()
            if purpose == "verify":
                if row and row[1]:
                    return None
                if row and legacy_user_id and row[0] != legacy_user_id:
                    return None
                if row is None:
                    pending = self.account(legacy_user_id) if legacy_user_id else None
                    if pending and pending[3]:
                        return None
                    user_id = legacy_user_id or f"user-{uuid4().hex}"
                    if not legacy_user_id:
                        now = datetime.now(UTC).isoformat()
                        self.db.execute("insert into users (id, display_name, email, created_at, updated_at) values (?, 'Tu perfil', '', ?, ?)", (user_id, now, now))
                    if pending:
                        # Correct a mistyped pending email from the same trusted
                        # legacy session. Earlier links must immediately stop working.
                        self.db.execute("update accounts set email = ? where user_id = ?", (email, user_id))
                        self.db.execute("delete from account_tokens where user_id = ?", (user_id,))
                    else:
                        self.db.execute("insert into accounts (user_id, email, password_hash) values (?, ?, '')", (user_id, email))
                else:
                    user_id = row[0]
            elif purpose == "reset":
                if row is None or not row[1]:
                    return None
                user_id = row[0]
            else:
                raise ValueError("Unknown account capability")
            # Invalidate earlier links of this kind, including links delayed in email queues.
            self.db.execute("delete from account_tokens where user_id = ? and purpose = ?", (user_id, purpose))
            self.db.execute("insert into account_tokens values (?, ?, ?, ?)", (
                digest(token), user_id, purpose, (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            ))
        return token

    def consume(self, token: str, purpose: str, password: str) -> str | None:
        encoded = password_hash(password)
        with self.db:
            self.db.execute("begin immediate")
            row = self.db.execute("""
                select t.user_id, a.email from account_tokens t join accounts a on a.user_id = t.user_id
                where t.token_hash = ? and t.purpose = ? and t.expires_at > ?
            """, (digest(token), purpose, datetime.now(UTC).isoformat())).fetchone()
            if row is None:
                return None
            user_id, email = row
            self.db.execute("update accounts set verified = 1, password_hash = ? where user_id = ?", (encoded, user_id))
            self.db.execute("update users set email = ?, updated_at = ? where id = ?", (email, datetime.now(UTC).isoformat(), user_id))
            self.db.execute("delete from account_tokens where user_id = ?", (user_id,))
            self.db.execute("delete from web_sessions where user_id = ?", (user_id,))
        return user_id

    def login(self, email: str, password: str) -> str | None:
        row = self.db.execute("select user_id, password_hash, verified from accounts where email = ?", (normalize_email(email),)).fetchone()
        valid = password_matches(password, row[1] if row and row[1] else _DUMMY_HASH)
        return row[0] if row and row[2] and valid else None

    def require_password(self, user_id: str, password: str) -> bool:
        row = self.account(user_id)
        return bool(row and row[3] and password_matches(password, row[2]))

    def pause(self, user_id: str, paused: bool) -> None:
        with self.db:
            self.db.execute("update accounts set alerts_paused = ? where user_id = ?", (int(paused), user_id))

    def disconnect(self, user_id: str) -> None:
        with self.db:
            self.db.execute("insert or ignore into bootstrap_blocks values (?)", (digest(user_id),))
            self.db.execute("delete from telegram_destinations where user_id = ?", (user_id,))
            self.db.execute("delete from telegram_link_tokens where user_id = ?", (user_id,))

    def export(self, user_id: str) -> str:
        """Explicit columns; credentials, capabilities and other users are never exported."""
        def rows(sql, params=(user_id,)):
            cursor = self.db.execute(sql, params)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        return json.dumps({
            "format_version": 1,
            "user": rows("select id, display_name, email, created_at, updated_at from users where id = ?"),
            "account": rows("select email, verified, alerts_paused from accounts where user_id = ?"),
            "renter_profiles": rows("select * from renter_profiles where user_id = ?"),
            "pets": rows("select p.* from pets p join renter_profiles r on r.id = p.renter_profile_id where r.user_id = ?"),
            "searches": rows("select * from search_profiles where user_id = ?"),
            "telegram": rows("select chat_id, enabled, created_at, updated_at from telegram_destinations where user_id = ?"),
            "matches": rows("select m.* from listing_matches m join search_profiles s on s.id = m.search_profile_id where s.user_id = ?"),
            "listings": rows("select distinct l.* from listings l join listing_matches m on m.listing_id = l.id join search_profiles s on s.id = m.search_profile_id where s.user_id = ?"),
            "notifications": rows("select n.* from notifications n join listing_matches m on m.id = n.match_id join search_profiles s on s.id = m.search_profile_id where s.user_id = ?"),
        }, ensure_ascii=False, indent=2)

    def delete(self, user_id: str) -> None:
        with self.db:
            self.db.execute("insert or ignore into bootstrap_blocks values (?)", (digest(user_id),))
            self.db.execute("delete from users where id = ?", (user_id,))
