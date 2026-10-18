"""Small same-origin onboarding website for user-owned rental searches."""

from __future__ import annotations

import hashlib
import html
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http.cookies import SimpleCookie
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qs
from uuid import uuid4

from .models import Pet, PropertyType, RentalDuration, RenterProfile, User
from .monitoring import source_health_age_seconds
from .persistence import RentalStore
from .search_profile import SearchProfile
from .telegram_linking import create_telegram_link
from .accounts import Accounts, MailSettings


StartResponse = Callable[[str, list[tuple[str, str]]], object]
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SESSION_COOKIE = "humanos_session"
_CSRF_COOKIE = "humanos_csrf"
_STATUS_COOKIE = "humanos_status"
_STATUS_SESSION_SECONDS = 8 * 60 * 60
_MAX_BODY_BYTES = 65_536
_MADRID_DISTRICTS = (
    "Arganzuela", "Barajas", "Carabanchel", "Centro", "Chamartín", "Chamberí",
    "Ciudad Lineal", "Fuencarral-El Pardo", "Hortaleza", "Latina", "Moncloa-Aravaca",
    "Moratalaz", "Puente de Vallecas", "Retiro", "Salamanca", "San Blas-Canillejas",
    "Tetuán", "Usera", "Vicálvaro", "Villa de Vallecas", "Villaverde",
)


@dataclass(frozen=True, slots=True)
class WebSettings:
    database_path: str
    bot_username: str
    secure_cookies: bool = True
    session_days: int = 30
    status_page_token: str = ""
    accounts_enabled: bool = False
    mail: MailSettings | None = None


@dataclass(frozen=True, slots=True)
class PageState:
    user: User | None = None
    renter: RenterProfile | None = None
    search: SearchProfile | None = None


class WebApplication:
    """Dependency-free WSGI application sharing the worker's SQLite store."""

    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings
        if settings.accounts_enabled:
            if settings.mail is None:
                raise ValueError("Verified accounts require SMTP configuration")
            settings.mail.validate(settings.secure_cookies)

    def __call__(self, environ: Mapping[str, object], start_response: StartResponse):
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        cookies = _cookies(str(environ.get("HTTP_COOKIE", "")))
        session_token = cookies.get(_SESSION_COOKIE, "")
        session_hash = _digest(session_token) if session_token else ""
        with RentalStore(self.settings.database_path) as store:
            user_id = store.web_session_user_id(session_hash) if session_hash else None
            if self.settings.accounts_enabled:
                from .account_web import handle_accounts
                response = handle_accounts(self, store, environ, start_response, user_id, cookies)
                if response is not None:
                    return response
            if path == "/health" and method == "GET":
                return self._respond(
                    start_response,
                    "200 OK",
                    json.dumps({"status": "ok"}),
                    content_type="application/json; charset=utf-8",
                )
            if path == "/form.js" and method == "GET":
                return self._respond(
                    start_response,
                    "200 OK",
                    _FORM_JS,
                    content_type="application/javascript; charset=utf-8",
                )
            if path == "/status" and method == "GET":
                if not self.settings.status_page_token:
                    return self._error(start_response, "404 Not Found", "Página no encontrada.")
                csrf = cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(24)
                if not _valid_status_cookie(
                    cookies.get(_STATUS_COOKIE, ""), self.settings.status_page_token
                ):
                    return self._respond(
                        start_response,
                        "200 OK",
                        _status_login_page(csrf),
                        cookies=(
                            ()
                            if cookies.get(_CSRF_COOKIE)
                            else (self._csrf_cookie(csrf),)
                        ),
                    )
                return self._respond(
                    start_response,
                    "200 OK",
                    _status_page(store.list_source_health(), csrf),
                    cookies=(
                        () if cookies.get(_CSRF_COOKIE) else (self._csrf_cookie(csrf),)
                    ),
                )
            if path == "/status/login" and method == "POST":
                if not self.settings.status_page_token:
                    return self._error(start_response, "404 Not Found", "Página no encontrada.")
                form, error = _read_form(environ)
                if error or not _valid_csrf(form, cookies):
                    return self._error(
                        start_response,
                        "403 Forbidden",
                        "La sesión del formulario ha caducado. Recarga la página.",
                    )
                password = form.get("password", "")
                if not secrets.compare_digest(password, self.settings.status_page_token):
                    return self._respond(
                        start_response,
                        "401 Unauthorized",
                        _status_login_page(
                            cookies.get(_CSRF_COOKIE, ""),
                            "La contraseña no es correcta.",
                        ),
                    )
                return self._redirect(
                    start_response,
                    "/status",
                    cookies=(self._status_cookie(self.settings.status_page_token),),
                )
            if path == "/status/logout" and method == "POST":
                form, error = _read_form(environ)
                if error or not _valid_csrf(form, cookies):
                    return self._error(
                        start_response,
                        "403 Forbidden",
                        "No se pudo cerrar la sesión.",
                    )
                return self._redirect(
                    start_response,
                    "/status",
                    cookies=(self._expired_cookie(_STATUS_COOKIE),),
                )
            if path == "/" and method == "GET":
                csrf = cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(24)
                state = self._load_state(store, user_id)
                page = (
                    _search_review_page(
                        state,
                        csrf=csrf,
                        connected=bool(user_id and store.telegram_chat_id(user_id)),
                        accounts_enabled=self.settings.accounts_enabled,
                    )
                    if state.user and state.renter and state.search
                    else _profile_page(
                        state, csrf, accounts_enabled=self.settings.accounts_enabled
                    )
                )
                return self._respond(
                    start_response,
                    "200 OK",
                    page,
                    cookies=(() if cookies.get(_CSRF_COOKIE) else (self._csrf_cookie(csrf),)),
                )
            if path == "/profile/edit" and method == "GET":
                if user_id is None:
                    return self._redirect(start_response, "/")
                csrf = cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(24)
                return self._respond(
                    start_response,
                    "200 OK",
                    _profile_page(
                        self._load_state(store, user_id),
                        csrf,
                        accounts_enabled=self.settings.accounts_enabled,
                    ),
                    cookies=(() if cookies.get(_CSRF_COOKIE) else (self._csrf_cookie(csrf),)),
                )
            if path == "/profile" and method == "POST":
                form, error = _read_form(environ)
                if error:
                    return self._error(start_response, "400 Bad Request", error)
                if not _valid_csrf(form, cookies):
                    return self._error(start_response, "403 Forbidden", "La sesión del formulario ha caducado. Recarga la página.")
                try:
                    state = self._save_profile(store, form, user_id)
                except ValueError as error_value:
                    csrf = cookies.get(_CSRF_COOKIE, "")
                    return self._respond(
                        start_response,
                        "422 Unprocessable Entity",
                        _profile_page(self._load_state(store, user_id), csrf, str(error_value), form, accounts_enabled=self.settings.accounts_enabled),
                    )
                response_cookies: list[str] = []
                if user_id is None:
                    session_token = secrets.token_urlsafe(32)
                    store.save_web_session(
                        _digest(session_token),
                        state.user.id,
                        datetime.now(UTC) + timedelta(days=self.settings.session_days),
                    )
                    response_cookies.append(self._session_cookie(session_token))
                telegram_url = ""
                telegram_error = ""
                try:
                    telegram_url = create_telegram_link(
                        store,
                        state.user.id,
                        self.settings.bot_username,
                    ).url
                except ValueError:
                    telegram_error = "Tu búsqueda está guardada, pero Telegram no está disponible ahora mismo."
                return self._respond(
                    start_response,
                    "200 OK",
                    _saved_page(state, telegram_url, telegram_error, connected=bool(store.telegram_chat_id(state.user.id))),
                    cookies=tuple(response_cookies),
                )
            if path == "/telegram-status" and method == "GET":
                if user_id is None:
                    return self._redirect(start_response, "/")
                connected = bool(store.telegram_chat_id(user_id))
                message = (
                    "Telegram está conectado. Si tus avisos están activos, recibirás allí los anuncios que encajen contigo."
                    if connected else
                    "Todavía no hemos confirmado la conexión. Abre el enlace de Telegram y pulsa Iniciar / Start. La confirmación puede tardar hasta 3 minutos. No enviamos un correo por conectar Telegram."
                )
                return self._respond(start_response, "200 OK", _layout(
                    "Estado de Telegram", f'<main class="center"><section class="success"><h1>{"Telegram conectado" if connected else "Conexión pendiente"}</h1><p>{message}</p><a class="primary button" href="/telegram-status">Comprobar de nuevo</a><a href="/account">Mi cuenta</a><a href="/">Volver a mi búsqueda</a></section></main>'
                ))
            if path == "/telegram-link" and method == "POST":
                if user_id is None:
                    return self._redirect(start_response, "/")
                form, error = _read_form(environ)
                if error or not _valid_csrf(form, cookies):
                    return self._error(start_response, "403 Forbidden", "La sesión del formulario ha caducado. Recarga la página.")
                link = create_telegram_link(store, user_id, self.settings.bot_username)
                return self._respond(
                    start_response,
                    "200 OK",
                    _telegram_page(link.url),
                )
            if path == "/logout" and method == "POST":
                form, error = _read_form(environ)
                if error or not _valid_csrf(form, cookies):
                    return self._error(start_response, "403 Forbidden", "No se pudo cerrar la sesión.")
                if session_hash:
                    store.delete_web_session(session_hash)
                return self._redirect(
                    start_response,
                    "/",
                    cookies=(self._expired_cookie(_SESSION_COOKIE),),
                )
        return self._error(start_response, "404 Not Found", "Página no encontrada.")

    def _load_state(self, store: RentalStore, user_id: str | None) -> PageState:
        if user_id is None:
            return PageState()
        return PageState(
            user=store.load_user(user_id),
            renter=store.load_primary_renter_profile(user_id),
            search=store.load_primary_search_profile(user_id),
        )

    def _save_profile(
        self,
        store: RentalStore,
        form: Mapping[str, str],
        existing_user_id: str | None,
    ) -> PageState:
        display_name = _required(form, "display_name", "Tu nombre", 100)
        email = form.get("email", "").strip()
        account = Accounts(store).account(existing_user_id)
        if account and account[3]:
            email = account[1]
        if email and (len(email) > 254 or not _EMAIL.fullmatch(email)):
            raise ValueError("Escribe un correo electrónico válido.")
        renter_name = _required(form, "renter_name", "Nombre de la persona inquilina", 100)
        sender_name = _required(form, "sender_name", "Nombre de contacto", 100)
        max_rent = _integer(form, "max_rent_eur", "Alquiler máximo", 300, 10_000)
        urgent = "urgent" in form
        stretch_max_rent = _optional_integer(
            form,
            "stretch_max_rent_eur",
            "Máximo excepcional",
            300,
            10_000,
        )
        if not urgent:
            stretch_max_rent = None
        elif stretch_max_rent is not None and stretch_max_rent <= max_rent:
            raise ValueError(
                "El máximo excepcional debe ser mayor que tu presupuesto preferido."
            )
        min_bedrooms = _integer(form, "preferred_min_bedrooms", "Dormitorios", 0, 10)
        income = _optional_integer(form, "monthly_income_eur", "Nómina", 1, 100_000)
        household_size = _integer(
            form, "household_size", "Número de personas", 1, 10
        )
        pet_count = _optional_integer(
            form, "pet_count", "Número de animales", 0, 10
        ) or 0
        pet_species = form.get("pet_species", "").strip()
        if pet_count and not pet_species:
            raise ValueError("Indica qué tipo de animales vivirán en el piso.")
        districts = _districts(form.get("acceptable_districts", ""))
        furnished_value = form.get("furnished", "any")
        if furnished_value not in {"any", "yes", "no"}:
            raise ValueError("La preferencia de mobiliario no es válida.")
        furnished = None if furnished_value == "any" else furnished_value == "yes"
        duration_value = form.get("rental_duration", "")
        duration_options = {
            "long_term": (RentalDuration.LONG_TERM,),
            "temporary": (RentalDuration.TEMPORARY,),
            "both": (RentalDuration.LONG_TERM, RentalDuration.TEMPORARY),
        }
        if duration_value not in duration_options:
            raise ValueError("El tipo de alquiler no es válido.")
        accepted_property_types = tuple(
            value
            for field, value in (
                ("property_type_home", PropertyType.HOME),
                ("property_type_room", PropertyType.ROOM),
            )
            if field in form
        )
        if not accepted_property_types:
            raise ValueError("Selecciona vivienda completa, habitación o ambas.")
        move_in_value = form.get("move_in_by", "").strip()
        try:
            move_in_by = date.fromisoformat(move_in_value) if move_in_value else None
        except ValueError as error:
            raise ValueError("La fecha de entrada no es válida.") from error

        user_id = existing_user_id or f"user-{uuid4().hex}"
        existing_renter = store.load_primary_renter_profile(user_id) if existing_user_id else None
        existing_search = store.load_primary_search_profile(user_id) if existing_user_id else None
        renter_id = existing_renter.id if existing_renter else f"renter-{uuid4().hex}"
        search_id = existing_search.id if existing_search else f"search-{uuid4().hex}"
        pets = ()
        if pet_count:
            pet_id = existing_renter.pets[0].id if existing_renter and existing_renter.pets else f"pet-{uuid4().hex}"
            pets = (
                Pet(
                    id=pet_id,
                    species=pet_species[:80],
                    count=pet_count,
                    notes=form.get("pet_notes", "").strip()[:300],
                ),
            )
        user = User(id=user_id, display_name=display_name, email=email)
        renter = RenterProfile(
            id=renter_id,
            user_id=user_id,
            label="Perfil principal",
            sender_name=sender_name,
            renter_name=renter_name,
            monthly_income_eur=income,
            household_size=household_size,
            job=form.get("job", "").strip()[:160],
            contract_type=form.get("contract_type", "").strip()[:120],
            pets=pets,
        )
        search = SearchProfile(
            id=search_id,
            user_id=user_id,
            name="Madrid",
            max_rent_eur=max_rent,
            municipality="Madrid",
            urgent=urgent,
            stretch_max_rent_eur=stretch_max_rent,
            renter_profile_id=renter_id,
            acceptable_districts=districts,
            accepted_property_types=accepted_property_types,
            preferred_min_bedrooms=min_bedrooms,
            allow_studios="allow_studios" in form,
            allow_unknown_bedrooms="allow_unknown_bedrooms" in form,
            furnished=furnished,
            move_in_asap="move_in_asap" in form,
            move_in_by=move_in_by,
            accepted_rental_durations=duration_options[duration_value],
            requires_pet_acceptance=pet_count > 0,
        )
        store.save_user(user)
        store.save_renter_profile(renter)
        store.save_search_profile(search)
        return PageState(user=user, renter=renter, search=search)

    def _respond(
        self,
        start_response: StartResponse,
        status: str,
        body: str,
        *,
        content_type: str = "text/html; charset=utf-8",
        cookies: tuple[str, ...] = (),
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> list[bytes]:
        payload = body.encode("utf-8")
        headers = _security_headers(content_type, len(payload))
        headers.extend(extra_headers)
        headers.extend(("Set-Cookie", cookie) for cookie in cookies)
        start_response(status, headers)
        return [payload]

    def _redirect(
        self,
        start_response: StartResponse,
        location: str,
        *,
        cookies: tuple[str, ...] = (),
    ) -> list[bytes]:
        headers = [("Location", location), ("Content-Length", "0")]
        headers.extend(("Set-Cookie", cookie) for cookie in cookies)
        start_response("303 See Other", headers)
        return [b""]

    def _error(self, start_response: StartResponse, status: str, message: str):
        return self._respond(start_response, status, _message_page("Algo no ha ido bien", message))

    def _session_cookie(self, value: str) -> str:
        return _cookie(_SESSION_COOKIE, value, self.settings.session_days * 86_400, self.settings.secure_cookies)

    def _csrf_cookie(self, value: str) -> str:
        return _cookie(_CSRF_COOKIE, value, 86_400, self.settings.secure_cookies)

    def _status_cookie(self, token: str) -> str:
        return _cookie(
            _STATUS_COOKIE,
            _status_cookie_value(token),
            _STATUS_SESSION_SECONDS,
            self.settings.secure_cookies,
        )

    def _expired_cookie(self, name: str) -> str:
        return _cookie(name, "", 0, self.settings.secure_cookies)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cookies(raw: str) -> dict[str, str]:
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return {}
    return {key: morsel.value for key, morsel in cookie.items()}


def _cookie(name: str, value: str, max_age: int, secure: bool) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _read_form(environ: Mapping[str, object]) -> tuple[dict[str, str], str]:
    content_type = str(environ.get("CONTENT_TYPE", ""))
    if not content_type.startswith("application/x-www-form-urlencoded"):
        return {}, "El formato del formulario no es válido."
    try:
        length = int(str(environ.get("CONTENT_LENGTH", "0") or "0"))
    except ValueError:
        return {}, "El tamaño del formulario no es válido."
    if length <= 0 or length > _MAX_BODY_BYTES:
        return {}, "El formulario está vacío o es demasiado grande."
    stream = environ.get("wsgi.input")
    if not hasattr(stream, "read"):
        return {}, "No se pudo leer el formulario."
    try:
        payload = stream.read(length).decode("utf-8", errors="strict")
        values = parse_qs(payload, keep_blank_values=True, max_num_fields=80)
    except (UnicodeDecodeError, ValueError):
        return {}, "El formulario contiene datos que no se pueden procesar."
    form = {key: items[-1] for key, items in values.items()}
    if "acceptable_districts" in values:
        form["acceptable_districts"] = ", ".join(
            item for item in values["acceptable_districts"] if item
        )
    return form, ""


def _valid_csrf(form: Mapping[str, str], cookies: Mapping[str, str]) -> bool:
    submitted = form.get("csrf_token", "")
    expected = cookies.get(_CSRF_COOKIE, "")
    return bool(submitted and expected and secrets.compare_digest(submitted, expected))


def _status_cookie_value(token: str) -> str:
    issued_at = str(int(datetime.now(UTC).timestamp()))
    signature = hmac.new(
        token.encode("utf-8"),
        f"status-session:{issued_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def _valid_status_cookie(value: str, token: str) -> bool:
    if not value or not token:
        return False
    issued_at, separator, signature = value.partition(".")
    if not separator:
        return False
    try:
        age = int(datetime.now(UTC).timestamp()) - int(issued_at)
    except ValueError:
        return False
    if age < 0 or age > _STATUS_SESSION_SECONDS:
        return False
    expected = hmac.new(
        token.encode("utf-8"),
        f"status-session:{issued_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return secrets.compare_digest(signature, expected)


def _required(form: Mapping[str, str], key: str, label: str, maximum: int) -> str:
    value = form.get(key, "").strip()
    if not value:
        raise ValueError(f"{label} es obligatorio.")
    if len(value) > maximum:
        raise ValueError(f"{label} es demasiado largo.")
    return value


def _integer(form: Mapping[str, str], key: str, label: str, minimum: int, maximum: int) -> int:
    try:
        value = int(form.get(key, ""))
    except ValueError as error:
        raise ValueError(f"{label} debe ser un número entero.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} debe estar entre {minimum} y {maximum}.")
    return value


def _optional_integer(
    form: Mapping[str, str], key: str, label: str, minimum: int, maximum: int
) -> int | None:
    raw = form.get(key, "").strip()
    return _integer(form, key, label, minimum, maximum) if raw else None


def _districts(raw: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if len(values) > 21 or any(len(item) > 80 for item in values):
        raise ValueError("La lista de distritos no es válida.")
    return values


def _security_headers(content_type: str, length: int) -> list[tuple[str, str]]:
    return [
        ("Content-Type", content_type),
        ("Content-Length", str(length)),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    ]


def _value(form: Mapping[str, str], key: str, fallback: object = "") -> str:
    return html.escape(str(form.get(key, fallback) if form else fallback), quote=True)


def _checked(value: bool) -> str:
    return " checked" if value else ""


def _count_control(name: str, value: str, label: str, minimum: int = 0, required: bool = False) -> str:
    safe_label = html.escape(label, quote=True)
    return (
        f'<span class="count-control" data-count-control>'
        f'<button type="button" data-count-delta="-1" aria-label="Reducir {safe_label}" hidden>−</button>'
        f'<input id="{name}" name="{name}" value="{value}" type="number" inputmode="numeric" min="{minimum}" max="10"'
        f'{" required" if required else ""}>'
        f'<button type="button" data-count-delta="1" aria-label="Aumentar {safe_label}" hidden>+</button></span>'
    )


def _profile_page(
    state: PageState,
    csrf: str,
    error: str = "",
    submitted: Mapping[str, str] | None = None,
    *,
    accounts_enabled: bool = False,
) -> str:
    form = dict(submitted or {})
    user, renter, search = state.user, state.renter, state.search
    if submitted is None:
        if user:
            form.update({"display_name": user.display_name, "email": user.email})
        if renter:
            pet = renter.pets[0] if renter.pets else None
            form.update({
                "renter_name": renter.renter_name,
                "sender_name": renter.sender_name,
                "monthly_income_eur": renter.monthly_income_eur or "",
                "household_size": renter.household_size,
                "job": renter.job,
                "contract_type": renter.contract_type,
                "pet_count": pet.count if pet else "",
                "pet_species": pet.species if pet else "",
                "pet_notes": pet.notes if pet else "",
            })
        if search:
            form.update({
                "max_rent_eur": search.max_rent_eur,
                "urgent": "on" if search.urgent else "",
                "stretch_max_rent_eur": search.stretch_max_rent_eur or "",
                "preferred_min_bedrooms": search.preferred_min_bedrooms,
                "acceptable_districts": ", ".join(search.acceptable_districts),
                "property_type_home": "on" if PropertyType.HOME in search.accepted_property_types else "",
                "property_type_room": "on" if PropertyType.ROOM in search.accepted_property_types else "",
                "furnished": "any" if search.furnished is None else ("yes" if search.furnished else "no"),
                "move_in_by": search.move_in_by.isoformat() if search.move_in_by else "",
                "allow_studios": "on" if search.allow_studios else "",
                "allow_unknown_bedrooms": "on" if search.allow_unknown_bedrooms else "",
                "move_in_asap": "on" if search.move_in_asap else "",
                "rental_duration": (
                    "both"
                    if len(search.accepted_rental_durations) == 2
                    else search.accepted_rental_durations[0].value
                ),
            })
    studios = form.get("allow_studios", "") == "on"
    unknown = form.get("allow_unknown_bedrooms", "") == "on"
    asap = form.get("move_in_asap", "") == "on"
    furnished = form.get("furnished", "any")
    rental_duration = form.get("rental_duration", "")
    property_type_home = form.get("property_type_home", "") == "on"
    property_type_room = form.get("property_type_room", "") == "on"
    urgent = form.get("urgent", "") == "on"
    selected_districts = {
        item.strip() for item in str(form.get("acceptable_districts", "")).split(",")
        if item.strip()
    }
    district_options = "".join(
        f'<label><input type="checkbox" name="acceptable_districts" value="{html.escape(district, quote=True)}"{_checked(district in selected_districts)}><span>{html.escape(district)}</span></label>'
        for district in _MADRID_DISTRICTS
    )
    district_summary = (
        ", ".join(sorted(selected_districts))
        if selected_districts else "Cualquier distrito de Madrid"
    )
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    session_note = ("Revisa tu búsqueda y cambia lo que necesites." if accounts_enabled else "Revisa tu búsqueda y cambia lo que necesites. Esta sesión está vinculada a este navegador.") if user else "Define tus criterios. Al terminar podrás conectar Telegram para recibir avisos."
    body = f"""
    <header><a class="brand" href="/">Se admiten humanos</a><span class="badge">Gratis y open source</span></header>
    <main>
      <section class="hero"><p class="eyebrow">Alertas de alquiler que van al grano</p><h1>Encontrar casa ya es bastante difícil.</h1><p class="lede">Cuéntanos qué buscas. Revisamos anuncios nuevos, aplicamos tus criterios y te avisamos por Telegram para que puedas reaccionar a tiempo.</p><div class="promise"><span>Tus criterios, tu búsqueda</span><span>Sin anuncios duplicados</span><span>Sin vender tus datos</span></div></section>
      <section class="panel"><div class="panel-head"><p class="step">Tu búsqueda</p><h2>{'Actualiza tus datos' if user else 'Cuéntanos qué necesitas'}</h2><p>{html.escape(session_note)}</p></div>{error_html}
      <form method="post" action="/profile" data-wizard><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">
        <p class="required-note"><span aria-hidden="true">*</span> Campos obligatorios</p>
        <ol class="form-progress" aria-label="Progreso del formulario"><li><button type="button" data-step-button="0" aria-current="step"><span>1</span> Búsqueda</button></li><li><button type="button" data-step-button="1"><span>2</span> Personas</button></li><li><button type="button" data-step-button="2"><span>3</span> Animales</button></li></ol>
        <fieldset class="wizard-step" data-step="0"><legend>1 · Presupuesto y zona</legend><div class="grid">
          <label><span class="field-title">Presupuesto mensual (€) <span class="required" aria-hidden="true">*</span></span><small>La cantidad que quieres pagar habitualmente.</small><input required type="number" inputmode="numeric" min="300" max="10000" name="max_rent_eur" value="{_value(form, 'max_rent_eur')}"></label>
          <label>Máximo excepcional (€) <small>Opcional · solo se usa en modo urgente.</small><input type="number" inputmode="numeric" min="300" max="10000" name="stretch_max_rent_eur" value="{_value(form, 'stretch_max_rent_eur')}" placeholder="Ej. 1000"></label>
          <div class="field-group"><label for="preferred_min_bedrooms"><span class="field-title">Número mínimo de dormitorios <span class="required" aria-hidden="true">*</span></span></label>{_count_control('preferred_min_bedrooms', _value(form, 'preferred_min_bedrooms'), 'dormitorios', 0, True)}</div>
          <div class="wide field-group"><span class="field-label">Distritos <small>Opcional · sin selección buscaremos en todo Madrid.</small></span><details class="district-picker"><summary data-district-summary>{html.escape(district_summary)}</summary><div class="district-options">{district_options}</div></details></div>
          <label>Mobiliario <small>Opcional</small><select name="furnished"><option value="any"{' selected' if furnished == 'any' else ''}>Me da igual</option><option value="yes"{' selected' if furnished == 'yes' else ''}>Amueblado</option><option value="no"{' selected' if furnished == 'no' else ''}>Sin amueblar</option></select></label>
          <label><span class="field-title">Tipo de alquiler <span class="required" aria-hidden="true">*</span></span><select required name="rental_duration"><option value="" disabled{' selected' if not rental_duration else ''}>Elige una opción</option><option value="long_term"{' selected' if rental_duration == 'long_term' else ''}>Larga duración</option><option value="temporary"{' selected' if rental_duration == 'temporary' else ''}>Temporal / por temporada</option><option value="both"{' selected' if rental_duration == 'both' else ''}>Ambos</option></select></label>
          <label class="date-field">Fecha límite para mudarte <small>Opcional</small><input type="date" name="move_in_by" value="{_value(form, 'move_in_by')}"></label>
        </div><div class="option-groups">
          <section class="option-group"><h3>Urgencia</h3><div class="option-list"><label><input type="checkbox" name="urgent"{_checked(urgent)}><span>Avisarme también de oportunidades hasta mi máximo excepcional</span></label></div></section>
          <section class="option-group"><h3>Tipo</h3><div class="option-list"><label><input type="checkbox" name="property_type_home"{_checked(property_type_home)}><span>Vivienda completa</span></label><label><input type="checkbox" name="property_type_room"{_checked(property_type_room)}><span>Habitación</span></label></div></section>
          <section class="option-group"><h3>Flexibilidad</h3><div class="option-list"><label><input type="checkbox" name="allow_studios"{_checked(studios)}><span>Incluir estudios</span></label><label><input type="checkbox" name="allow_unknown_bedrooms"{_checked(unknown)}><span>Incluir anuncios sin dormitorios indicados</span></label><label><input type="checkbox" name="move_in_asap"{_checked(asap)}><span>Necesito entrar cuanto antes</span></label></div></section>
        </div><small class="option-note">Las sugerencias urgentes se etiquetan claramente y nunca superan tu máximo excepcional. Garajes y locales comerciales se excluyen siempre.</small><div class="wizard-nav wizard-nav-end"><button class="primary next-step" type="button" data-next>Continuar →</button></div></fieldset>
        <fieldset class="wizard-step" data-step="1"><legend>2 · Personas y situación</legend><div class="grid">
          <label><span class="field-title">Tu nombre <span class="required" aria-hidden="true">*</span></span><input required maxlength="100" name="display_name" value="{_value(form, 'display_name')}"></label>
          {('<p class="account-note">Gestiona tu correo verificado en <a href="/account">Mi cuenta</a>.</p>' if accounts_enabled else f'<label>Correo <small>Opcional · aún no enviamos emails.</small><input type="email" maxlength="254" name="email" value="{_value(form, "email")}"></label>')}
          <div class="field-group"><label for="household_size"><span class="field-title">Personas que vivirán en la vivienda <span class="required" aria-hidden="true">*</span></span></label>{_count_control('household_size', _value(form, 'household_size', 1), 'personas', 1, True)}</div>
          <label><span class="field-title">Nombre de la persona o personas inquilinas <span class="required" aria-hidden="true">*</span></span><input required maxlength="100" name="renter_name" value="{_value(form, 'renter_name')}" placeholder="Ej. Ana y Luis"></label>
          <label><span class="field-title">Nombre para contactar a la agencia <span class="required" aria-hidden="true">*</span></span><input required maxlength="100" name="sender_name" value="{_value(form, 'sender_name')}"></label>
          <label>Ingresos mensuales (€) <small>Opcional</small><input type="number" inputmode="numeric" min="1" max="100000" name="monthly_income_eur" value="{_value(form, 'monthly_income_eur')}"></label>
          <label>Trabajo <small>Opcional</small><input maxlength="160" name="job" value="{_value(form, 'job')}" placeholder="Stage manager, docente…"></label>
          <label>Tipo de contrato <small>Opcional</small><input maxlength="120" name="contract_type" value="{_value(form, 'contract_type')}" placeholder="Indefinido, autónomo…"></label>
        </div><div class="wizard-nav"><button class="secondary-step" type="button" data-back>← Atrás</button><button class="primary next-step" type="button" data-next>Continuar →</button></div></fieldset>
        <fieldset class="wizard-step" data-step="2"><legend>3 · Animales <span class="optional">Opcional</span></legend><p class="section-intro">Si convivirán contigo, los tendremos en cuenta al revisar cada anuncio.</p><div class="grid">
          <div class="field-group"><label for="pet_count">¿Cuántos animales vivirán contigo? <small>Opcional</small></label>{_count_control('pet_count', _value(form, 'pet_count'), 'animales', 0, False)}</div>
          <label>¿Qué animales son? <small>Opcional</small><input maxlength="80" name="pet_species" value="{_value(form, 'pet_species')}" placeholder="Gato, perro…"></label>
          <label class="wide">Algo útil sobre ellos <small>Opcional</small><textarea maxlength="300" name="pet_notes" placeholder="Edad, tamaño, comportamiento…">{_value(form, 'pet_notes')}</textarea></label>
        </div><div class="wizard-nav"><button class="secondary-step" type="button" data-back>← Atrás</button><button class="primary" type="submit">Guardar y conectar Telegram →</button></div></fieldset>
      </form>
      {'<p><a href="/account">Mi cuenta · avisos, acceso y datos</a></p>' if accounts_enabled else ''}
      {f'<form method="post" action="/logout" class="logout"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}"><button type="submit">Cerrar sesión en este navegador</button></form>' if user else ''}
      </section>
      <section class="how"><p class="eyebrow">Cómo funciona</p><h2>Rápido, pero honesto.</h2><div class="cards"><article><b>01</b><h3>Buscamos</h3><p>Consultamos fuentes autorizadas y webs de agencias con controles para detectar cambios.</p></article><article><b>02</b><h3>Filtramos</h3><p>Aplicamos presupuesto, zona, tipo de alquiler y demás preferencias sin inventar información que el anuncio no da.</p></article><article><b>03</b><h3>Te avisamos</h3><p>Recibes el anuncio y la evidencia en Telegram para abrirlo y responder en dos toques.</p></article></div></section>
    </main><script src="/form.js" defer></script>"""
    return _layout("Tu búsqueda · Se admiten humanos", body)


def _search_review_page(
    state: PageState, *, csrf: str, connected: bool, accounts_enabled: bool
) -> str:
    """Render a warm, readable summary of an existing search."""
    assert state.user is not None
    assert state.renter is not None
    assert state.search is not None
    user, renter, search = state.user, state.renter, state.search

    home_types = []
    if PropertyType.HOME in search.accepted_property_types:
        home_types.append("una vivienda completa")
    if PropertyType.ROOM in search.accepted_property_types:
        home_types.append("una habitación")
    property_text = " o ".join(home_types)
    bedroom_text = (
        "sin un mínimo de dormitorios"
        if search.preferred_min_bedrooms == 0
        else f"con al menos {search.preferred_min_bedrooms} "
        f"{'dormitorio' if search.preferred_min_bedrooms == 1 else 'dormitorios'}"
    )
    budget_text = f"por un máximo de {search.max_rent_eur:,} € al mes".replace(",", ".")
    ideal = f"{property_text.capitalize()}, {bedroom_text}, {budget_text}."

    place = (
        _natural_list(search.acceptable_districts)
        if search.acceptable_districts
        else f"cualquier zona de {search.municipality}"
    )
    duration_names = {
        RentalDuration.LONG_TERM: "larga duración",
        RentalDuration.TEMPORARY: "alquiler temporal",
    }
    duration = _natural_list(
        tuple(duration_names[value] for value in search.accepted_rental_durations)
    )
    furnished = {
        True: "amueblada",
        False: "sin amueblar",
        None: "con o sin muebles",
    }[search.furnished]
    location = f"En {place}, para {duration} y {furnished}."

    pet_count = sum(pet.count for pet in renter.pets)
    household = f"{renter.household_size} {'persona' if renter.household_size == 1 else 'personas'}"
    if pet_count:
        species_values = tuple(
            _pluralize_species(pet.species, pet.count) for pet in renter.pets
        )
        species = _natural_list(species_values)
        animals = f"{pet_count} {species}"
        household += f" y {animals}"
    people_title = f"Un hogar para {household}."

    details: list[str] = []
    if search.move_in_asap:
        details.append(
            "Necesitas mudarte cuanto antes"
            if renter.household_size == 1
            else "Necesitáis mudaros cuanto antes"
        )
    elif search.move_in_by:
        details.append(
            f"{'Te gustaría mudarte' if renter.household_size == 1 else 'Os gustaría mudaros'} "
            f"antes del {_spanish_date(search.move_in_by)}"
        )
    if renter.pets:
        pet_notes = " ".join(pet.notes.strip() for pet in renter.pets if pet.notes.strip())
        if pet_notes:
            details.append(pet_notes.rstrip(".") + ".")
    detail_text = ". ".join(details).replace("..", ".")

    status_title = "Todo listo" if connected else "Falta conectar Telegram"
    status_copy = (
        "Telegram está conectado. Te avisaremos en cuanto aparezca algo que encaje."
        if connected
        else "Conecta Telegram para que podamos avisarte en cuanto aparezca algo que encaje."
    )
    status_action = (
        '<a class="review-status-link" href="/telegram-status">Comprobar conexión</a>'
        if connected else ""
    )
    primary_action = (
        '<a class="primary button" href="/telegram-status">Sí, esto es lo que busco</a>'
        if connected
        else f'<form method="post" action="/telegram-link"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}"><button class="primary button" type="submit">Sí, esto es lo que busco</button></form>'
    )
    account_link = (
        '<a class="review-back" href="/account">Volver a mi cuenta</a>'
        if accounts_enabled
        else ""
    )
    body = f"""
    <header><a class="brand" href="/">Se admiten humanos</a><span class="badge review-badge"><i></i>Búsqueda activa</span></header>
    <main class="review-main">
      <section class="review-intro"><p class="review-hello">Hola, {html.escape(user.display_name)}</p><h1>Así es la casa que buscas.</h1><p class="lede">Échale un vistazo para comprobar que seguimos buscando justo lo que necesitas.</p></section>
      <section class="review-card" aria-label="Resumen de tu búsqueda">
        <article class="review-row"><div class="review-icon" aria-hidden="true">⌂<span>♥</span></div><div><p class="review-label">Tu casa ideal</p><h2>{html.escape(ideal)}</h2><p>{html.escape(location)}</p></div></article>
        <article class="review-row"><div class="review-icon review-family" aria-hidden="true">♙<span>●</span></div><div><p class="review-label">Para vosotros</p><h2>{html.escape(people_title)}</h2>{f'<p>{html.escape(detail_text)}</p>' if detail_text else ''}</div></article>
        <aside class="review-status"><div class="review-plane" aria-hidden="true">➤</div><div><h2>{status_title}</h2><p>{status_copy} {status_action}</p></div></aside>
      </section>
      <nav class="review-actions" aria-label="Acciones de búsqueda">{primary_action}<a class="secondary button" href="/profile/edit">Quiero cambiar algo</a></nav>
      {account_link}
    </main>"""
    return _layout("Tu búsqueda actual · Se admiten humanos", body)


def _natural_list(values: tuple[str, ...]) -> str:
    escaped = [value.strip() for value in values if value.strip()]
    if len(escaped) < 2:
        return escaped[0] if escaped else ""
    return ", ".join(escaped[:-1]) + " o " + escaped[-1]


def _pluralize_species(value: str, count: int) -> str:
    species = value.strip()
    if count != 1 and species and not species.casefold().endswith("s"):
        return species + "s"
    return species


def _spanish_date(value: date) -> str:
    months = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    return f"{value.day} de {months[value.month - 1]}"


def _saved_page(state: PageState, telegram_url: str, error: str, connected: bool = False) -> str:
    assert state.search is not None
    action = f'<a class="primary button" href="{html.escape(telegram_url, quote=True)}">Conectar mi Telegram →</a>' if telegram_url else f'<div class="error">{html.escape(error)}</div><a class="secondary button" href="/">Volver a intentarlo</a>'
    if connected:
        action = '<p class="connection-status">✓ Telegram conectado. Tu búsqueda se ha actualizado.</p><a href="/telegram-status">Comprobar conexión</a>'
    else:
        action += '<p>Abre Telegram y pulsa <strong>Iniciar / Start</strong>. La confirmación puede tardar hasta 3 minutos. No enviamos un correo por este paso.</p><a href="/telegram-status">Comprobar si Telegram está conectado</a>'
    budget_text = f"Presupuesto preferido: <strong>{state.search.max_rent_eur} €</strong>"
    if state.search.urgent and state.search.stretch_max_rent_eur is not None:
        budget_text += f"; sugerencias urgentes hasta <strong>{state.search.stretch_max_rent_eur} €</strong>"
    body = f"""<header><a class="brand" href="/">Se admiten humanos</a><span class="badge" aria-label="Búsqueda guardada">Guardada</span></header><main class="center"><section class="success"><div class="success-mark">✓</div><p class="eyebrow">Ya sabemos qué buscar</p><h1>Tu búsqueda está lista.</h1><p>{budget_text} al mes en Madrid. Los avisos de anuncios se reciben por Telegram.</p>{action}{'' if connected else '<small>El enlace de Telegram caduca en 10 minutos y solo funciona una vez.</small>'}<a href="/">← Revisar mi búsqueda</a></section></main>"""
    return _layout("Búsqueda guardada · Se admiten humanos", body)


def _telegram_page(url: str) -> str:
    return _layout("Conectar Telegram · Se admiten humanos", f"""<header><a class="brand" href="/">Se admiten humanos</a></header><main class="center"><section class="success"><p class="eyebrow">Último paso</p><h1>Conecta Telegram.</h1><p>Abre Telegram y pulsa <strong>Iniciar / Start</strong>. La confirmación puede tardar hasta 3 minutos. No enviamos un correo por este paso.</p><p>Este enlace es personal, caduca en 10 minutos y solo se puede usar una vez.</p><a class="primary button" href="{html.escape(url, quote=True)}">Abrir Telegram →</a><a href="/telegram-status">Comprobar conexión</a><a href="/">← Volver a mi búsqueda</a></section></main>""")


def _message_page(title: str, message: str) -> str:
    return _layout(title, f"<main class=\"center\"><section class=\"success\"><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><a class=\"button secondary\" href=\"/\">Volver</a></section></main>")


def _status_login_page(csrf: str, error: str = "") -> str:
    error_box = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = f"""<header><a class="brand" href="/">Se admiten humanos</a><span class="badge">Monitorización</span></header>
    <main class="center status-login"><section class="success"><p class="eyebrow">Acceso privado</p><h1>Estado de las fuentes</h1><p>Introduce la contraseña de monitorización para continuar.</p>{error_box}<form method="post" action="/status/login"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}"><label for="status-password">Contraseña<input id="status-password" type="password" name="password" required autofocus autocomplete="current-password"></label><button class="primary" type="submit">Entrar →</button></form></section></main>"""
    return _layout("Estado protegido · Se admiten humanos", body)


def _status_page(records, csrf: str) -> str:
    rows = []
    labels = {
        "alquiler_seguro": "Alquiler Seguro",
        "tecnocasa": "Tecnocasa",
        "redpiso": "Redpiso",
        "nestar": "Nestar",
        "idealista": "Idealista",
    }
    state_labels = {
        "healthy": "Operativa",
        "failing": "Con fallos",
        "zero_results": "Cero viviendas",
        "stale": "Sin éxito reciente",
    }
    for record in records:
        age = source_health_age_seconds(record)
        if age is None:
            last_success = "Todavía ninguno"
        elif age < 60:
            last_success = "Hace menos de un minuto"
        elif age < 3600:
            last_success = f"Hace {age // 60} min"
        else:
            last_success = f"Hace {age // 3600} h"
        count = "—" if record.last_listing_count is None else str(record.last_listing_count)
        detail = record.last_error or "Sin errores"
        rows.append(
            "<tr>"
            f"<th data-label=\"Fuente\">{html.escape(labels.get(record.source_id, record.source_id))}</th>"
            f'<td data-label="Estado"><span class="health {html.escape(record.alert_state)}">{html.escape(state_labels.get(record.alert_state, record.alert_state))}</span></td>'
            f"<td data-label=\"Último éxito\">{html.escape(last_success)}</td>"
            f"<td data-label=\"Inventario\">{count}</td>"
            f"<td data-label=\"Fallos\">{record.consecutive_failures}</td>"
            f"<td data-label=\"Detalle\" class=\"detail\">{html.escape(detail)}</td>"
            "</tr>"
        )
    table = (
        "<div class=\"status-table-wrap\"><table><thead><tr>"
        "<th>Fuente</th><th>Estado</th><th>Último éxito</th>"
        "<th>Inventario</th><th>Fallos</th><th>Detalle</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else "<p class=\"empty-status\">Todavía no hay comprobaciones registradas.</p>"
    )
    body = (
        "<header><a class=\"brand\" href=\"/\">Se admiten humanos</a>"
        f'<form class="status-logout" method="post" action="/status/logout"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}"><button type="submit">Cerrar monitorización</button></form></header>'
        "<main class=\"status-main\"><p class=\"eyebrow\">Solo operaciones</p>"
        "<h1>Estado de las fuentes</h1>"
        "<p class=\"lede\">Últimos resultados técnicos. Esta página no muestra personas ni criterios de búsqueda.</p>"
        f"{table}</main>"
    )
    return _layout("Estado de fuentes · Se admiten humanos", body)


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><meta name="description" content="Alertas gratuitas y open source para encontrar alquiler antes y con menos ruido."><style>{_CSS}</style></head><body>{body}<footer><p>Hecho para ayudar, no para ganar dinero. <a href="https://github.com/Algarciap/se-admiten-humanos" target="_blank" rel="noopener noreferrer">Ver código en GitHub ↗</a>.</p><p>Tus preferencias mandan; no inventamos información que el anuncio no da.</p></footer></body></html>"""


_FORM_JS = """
(() => {
  const form = document.querySelector('[data-wizard]');
  if (!form) return;
  form.querySelectorAll('[data-count-control]').forEach((group) => {
    const input = group.querySelector('input');
    const buttons = [...group.querySelectorAll('button')];
    const update = () => buttons.forEach((button) => {
      button.disabled = input.value !== '' && (Number(button.dataset.countDelta) < 0
        ? Number(input.value) <= Number(input.min) : Number(input.value) >= Number(input.max));
    });
    buttons.forEach((button) => {
      button.hidden = false;
      button.addEventListener('click', () => {
        const current = input.value === '' || !Number.isFinite(input.valueAsNumber)
          ? Number(input.min) : input.valueAsNumber;
        input.value = Math.max(Number(input.min), Math.min(Number(input.max), current + Number(button.dataset.countDelta)));
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
      });
    });
    input.addEventListener('input', update);
    update();
  });
  const steps = [...form.querySelectorAll('[data-step]')];
  const stepButtons = [...form.querySelectorAll('[data-step-button]')];
  let current = 0;

  const valid = (step) => {
    const invalid = [...step.querySelectorAll('input, select, textarea')]
      .find((control) => !control.checkValidity());
    if (invalid) invalid.reportValidity();
    return !invalid;
  };
  const show = (index, focus = false) => {
    current = Math.max(0, Math.min(index, steps.length - 1));
    steps.forEach((step, position) => { step.hidden = position !== current; });
    stepButtons.forEach((button, position) => {
      if (position === current) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    if (focus) form.scrollIntoView({behavior: 'smooth', block: 'start'});
  };

  form.classList.add('wizard-ready');
  show(0);
  form.addEventListener('click', (event) => {
    const next = event.target.closest('[data-next]');
    const back = event.target.closest('[data-back]');
    const jump = event.target.closest('[data-step-button]');
    if (next && valid(steps[current])) show(current + 1, true);
    if (back) show(current - 1, true);
    if (jump) {
      const target = Number(jump.dataset.stepButton);
      if (target <= current || valid(steps[current])) show(target, true);
    }
  });
  form.addEventListener('submit', (event) => {
    const invalidStep = steps.findIndex((step) =>
      [...step.querySelectorAll('input, select, textarea')].some((control) => !control.checkValidity())
    );
    if (invalidStep >= 0) {
      event.preventDefault();
      show(invalidStep, true);
      valid(steps[invalidStep]);
    }
  });

  const districtInputs = [...form.querySelectorAll('[name="acceptable_districts"]')];
  const districtSummary = form.querySelector('[data-district-summary]');
  const updateDistricts = () => {
    const selected = districtInputs.filter((input) => input.checked).map((input) => input.value);
    if (districtSummary) districtSummary.textContent = selected.length ? selected.join(', ') : 'Cualquier distrito de Madrid';
  };
  districtInputs.forEach((input) => input.addEventListener('change', updateDistricts));
})();
"""


_CSS = """
a{color:var(--green);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:.2em}
a:where(:hover){color:var(--ink);text-decoration-thickness:2px}
a:focus-visible{outline:3px solid var(--green);outline-offset:4px;border-radius:2px}
:root{--ink:#17231c;--paper:#f5f1e8;--card:#fffdf8;--green:#1f5c45;--lime:#d9ef9f;--coral:#ef765b;--line:#cfd5c9;--muted:#5d685f}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}header{max-width:1180px;margin:auto;padding:28px 24px;display:flex;align-items:center;justify-content:space-between}.brand{font-family:Georgia,serif;font-size:1.35rem;font-weight:700;color:var(--ink);text-decoration:none;white-space:nowrap}.brand span{font-size:1rem}.badge{flex:0 0 auto;background:var(--lime);padding:7px 12px;border-radius:999px;font-size:.78rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}main{max-width:1180px;margin:auto;padding:30px 24px 90px}.hero{max-width:880px;padding:72px 0 62px}.eyebrow,.step{text-transform:uppercase;letter-spacing:.13em;font-weight:900;font-size:.75rem;color:var(--green)}h1,h2,h3{font-family:Georgia,serif;line-height:1.04;margin:.25em 0}h1{font-size:clamp(3rem,8vw,6.7rem);letter-spacing:-.055em}.lede{font-size:clamp(1.1rem,2vw,1.4rem);max-width:760px;color:var(--muted)}.promise{display:flex;flex-wrap:wrap;gap:9px;margin-top:28px}.promise span{border:1px solid var(--ink);border-radius:999px;padding:7px 12px;font-size:.85rem}.panel{background:var(--card);border:1px solid var(--ink);box-shadow:9px 9px 0 var(--ink);padding:clamp(22px,5vw,54px)}.panel-head{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:28px}.panel-head h2,.how h2{font-size:clamp(2rem,5vw,4rem)}.panel-head>p{color:var(--muted);max-width:450px}fieldset{border:0;padding:38px 0;margin:0;border-bottom:1px solid var(--line)}legend{font-weight:900;font-size:1.05rem;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;min-width:0}.wide{grid-column:1/-1}label{display:flex;flex-direction:column;gap:7px;min-width:0;font-size:.9rem;font-weight:750}label small,.success small{color:var(--muted);font-weight:400}input,select,textarea{width:100%;border:1px solid #99a298;background:#fff;padding:13px 14px;border-radius:4px;color:var(--ink);font:inherit}input[type=date]{min-width:0;max-width:100%}input:focus,select:focus,textarea:focus{outline:3px solid var(--lime);border-color:var(--green)}textarea{min-height:90px;resize:vertical}.checks{display:flex;flex-wrap:wrap;gap:18px;margin-top:22px}.checks label{flex-direction:row;align-items:center;font-weight:600}.checks input{width:auto}.primary,.button{display:inline-block;margin-top:30px;background:var(--green);color:white;border:1px solid var(--green);padding:15px 20px;border-radius:4px;font:inherit;font-weight:900;text-decoration:none;cursor:pointer}.primary:hover{background:#174534}.secondary{background:transparent;color:var(--ink);border:1px solid var(--ink)}.error{background:#ffe4dc;border-left:5px solid var(--coral);padding:14px 16px;margin:24px 0}.logout{margin-top:22px}.logout button{border:0;background:none;text-decoration:underline;color:var(--muted);cursor:pointer}.how{padding:110px 0 20px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.cards article{border-top:2px solid var(--ink);padding-top:20px}.cards b{color:var(--coral)}.cards h3{font-size:1.7rem}.cards p{color:var(--muted)}.center{min-height:70vh;display:grid;place-items:center}.success{max-width:700px;text-align:center;background:var(--card);border:1px solid var(--ink);box-shadow:9px 9px 0 var(--ink);padding:clamp(30px,7vw,70px)}.success h1{font-size:clamp(2.8rem,7vw,5rem)}.success p{font-size:1.15rem;color:var(--muted)}.success .button{display:block;margin:28px auto 16px;max-width:340px}.success>a:last-child{display:block;margin-top:25px;color:var(--green)}.success-mark{margin:auto;width:52px;height:52px;display:grid;place-items:center;border-radius:50%;background:var(--lime);font-size:1.7rem;font-weight:900}footer{border-top:1px solid var(--line);max-width:1180px;margin:auto;padding:35px 24px 60px;display:flex;justify-content:space-between;gap:30px;color:var(--muted);font-size:.85rem}footer a{color:var(--green)}@media(max-width:720px){header{align-items:center;gap:10px;padding:20px 16px}.brand{font-size:clamp(.95rem,4.3vw,1.12rem)}.badge{padding:6px 9px;font-size:.56rem;letter-spacing:.035em}.hero{padding-top:38px}.panel-head,.grid,.cards{grid-template-columns:1fr}.wide{grid-column:auto}.panel{box-shadow:5px 5px 0 var(--ink)}footer{display:block}h1{font-size:3.25rem}}
.panel{padding:clamp(22px,4vw,46px)}.panel-head{display:block;padding-bottom:22px}.panel-head>.step{color:var(--green);max-width:none;margin:0}.panel-head h2{font-size:clamp(2.2rem,5vw,3.7rem);margin:.18em 0}.panel-head>p:last-child{max-width:680px;margin:10px 0 0}fieldset{padding:0 0 22px}legend{float:left;width:100%;padding:0;margin:0 0 10px}.optional{margin-left:6px;color:var(--muted);font-size:.72rem;font-weight:500}.section-intro{clear:both;margin:0 0 12px;color:var(--muted);font-size:.86rem}legend+.grid{clear:both}.grid{gap:13px 18px}.grid>label{align-self:end}label{gap:5px}input,select,textarea{padding:10px 12px}textarea{min-height:78px}.option-groups{display:grid;grid-template-columns:1.25fr .8fr 1.25fr;gap:12px;margin-top:12px;align-items:start}.option-group{background:#fff;border:1px solid var(--line);border-radius:4px;padding:13px 14px}.option-group h3{font-family:inherit;font-size:.72rem;line-height:1.2;margin:0 0 10px;color:var(--green);text-transform:uppercase;letter-spacing:.1em}.option-list{display:grid;gap:9px}.option-list label{display:grid;grid-template-columns:17px minmax(0,1fr);gap:8px;align-items:start;font-weight:600;line-height:1.3}.option-list input{width:15px;height:15px;margin:2px 0 0;padding:0}.option-note{display:block;margin-top:11px;color:var(--muted);font-size:.8rem}.primary,.button{padding:13px 18px}@media(max-width:800px){.option-groups{grid-template-columns:1fr 1fr}.option-group:last-child{grid-column:1/-1}}@media(max-width:720px){.option-groups{grid-template-columns:1fr}.option-group:last-child{grid-column:auto}}
.cards{border-top:2px solid var(--ink);padding-top:20px}.cards article{border-top:0;padding-top:0}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}.date-field{overflow:hidden}.date-field input[type=date]{display:block;inline-size:100%;min-inline-size:0;max-inline-size:100%;width:100%;min-width:0;max-width:100%}@media(max-width:720px){header{display:grid;grid-template-columns:minmax(0,1fr) auto}.brand{overflow:hidden;text-overflow:ellipsis}.center{min-height:auto;display:block;padding-top:18px}.success{max-width:none;padding:28px 20px}.success .eyebrow{font-size:.72rem}.success h1{font-size:2.35rem}.success p{font-size:1rem;line-height:1.45}.date-field input[type=date]{-webkit-appearance:none;appearance:none;text-align:left}}
.status-main{padding-top:42px}.status-main h1{font-size:clamp(2.7rem,6vw,5rem)}.status-table-wrap{overflow-x:auto;margin-top:34px;border:1px solid var(--ink);background:var(--card);box-shadow:7px 7px 0 var(--ink)}table{width:100%;border-collapse:collapse;min-width:850px;text-align:left}th,td{padding:14px 16px;border-bottom:1px solid var(--line);vertical-align:top}thead th{background:var(--ink);color:white;font-size:.76rem;text-transform:uppercase;letter-spacing:.06em}tbody th{font-family:Georgia,serif;font-size:1.05rem}.detail{max-width:360px;color:var(--muted);font-size:.85rem;overflow-wrap:anywhere}.health{display:inline-block;padding:5px 9px;border-radius:999px;background:var(--lime);font-size:.75rem;font-weight:850;white-space:nowrap}.health.failing,.health.zero_results{background:#ffd5cb}.health.stale{background:#ffe8a3}.empty-status{margin-top:30px;padding:24px;border:1px solid var(--line);background:var(--card)}
.status-login .success{width:min(100%,560px);text-align:left}.status-login .success h1{font-size:clamp(2.5rem,7vw,4.2rem)}.status-login form{margin-top:24px}.status-login .primary{width:100%;margin-top:18px}.status-logout button{border:0;background:transparent;color:var(--ink);font:inherit;font-size:.82rem;font-weight:750;text-decoration:underline;cursor:pointer}
.account-header-link{color:var(--green);font-size:.86rem;font-weight:850;text-decoration:none;border:1px solid var(--green);border-radius:999px;padding:7px 12px}.account-header-link:hover{background:var(--lime)}.account-page .success>p:not(.eyebrow){color:var(--muted)}.account-page .success a{color:var(--green);font-weight:750;text-decoration-thickness:1px;text-underline-offset:3px}.account-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:24px}.account-actions.account-recovery-actions{grid-template-columns:minmax(0,1fr)}.account-actions .account-action{display:flex;align-items:center;justify-content:center;min-height:48px;border:1px solid var(--green);border-radius:4px;padding:11px 14px;text-align:center;text-decoration:none}.account-actions .account-action:hover{background:#edf5da}.account-actions .account-action-main{background:var(--lime);color:var(--ink);border-color:var(--ink);box-shadow:3px 3px 0 var(--ink)}
.account-project-intro{margin:28px 0 30px;padding:24px;border:1px solid var(--line);border-left:5px solid var(--coral);background:var(--paper)}.account-project-intro .account-project-kicker{margin:0 0 7px;color:var(--green);font-size:.72rem;font-weight:900;letter-spacing:.11em;text-transform:uppercase}.account-project-intro h2{font-size:clamp(1.65rem,4vw,2.25rem);margin:0 0 14px}.account-project-intro>p:not(.account-project-kicker){margin:0 0 15px;color:var(--muted);font-size:1rem}.account-project-intro ul{margin:0 0 17px;padding-left:20px;color:var(--muted)}.account-project-intro li+li{margin-top:9px}.account-project-intro strong{color:var(--ink)}.account-signin-note{margin:0 0 20px}.account-page .account-signin-note+form{margin-top:0}
.account-welcome .success{width:min(100%,1040px);max-width:1040px;display:grid;grid-template-columns:minmax(0,1.15fr) minmax(300px,.85fr);column-gap:clamp(32px,6vw,70px);text-align:left}.account-welcome .success>.eyebrow,.account-welcome .success>h1{grid-column:1/-1}.account-welcome .account-project-intro{grid-column:1;grid-row:3/6;margin-bottom:0}.account-welcome .account-signin-note{grid-column:2;grid-row:3;align-self:end}.account-welcome .account-signin-note+form{grid-column:2;grid-row:4}.account-welcome .account-actions{grid-column:2;grid-row:5;align-self:start}.account-welcome .success h1{font-size:clamp(3rem,6vw,5rem)}@media(max-width:760px){.account-welcome .success{display:block}.account-welcome .account-project-intro{margin-bottom:28px}.account-welcome .success h1{font-size:2.35rem}}
.account-dashboard{display:block;min-height:auto;padding-top:30px}.account-dashboard .success{width:100%;max-width:none;text-align:left;padding:clamp(22px,5vw,54px)}.account-dashboard .success .eyebrow{margin:0 0 8px}.account-dashboard .success h1{margin:0 0 28px}.account-dashboard .success>p{max-width:760px;margin:0 0 12px;font-size:1rem;line-height:1.5}.account-dashboard .success>form{margin:24px 0 0}.account-dashboard .success>form+.success-note,.account-dashboard .success>form+p{margin-top:14px}.account-dashboard .success form .primary{width:auto;margin:0}.account-dashboard .success form label+.primary{margin-top:16px}.account-dashboard .success>h2{font-size:clamp(2rem,4vw,3.4rem);margin:58px 0 14px}.account-dashboard .success>h2+p{margin-bottom:0}.account-dashboard .success>a{display:inline-block;margin-top:8px}.account-dashboard .success>p:last-of-type{margin-top:28px}@media(max-width:720px){.account-dashboard{padding-top:18px}.account-dashboard .success{padding:28px 20px}.account-dashboard .success h1{margin-bottom:22px}.account-dashboard .success>h2{margin-top:44px}.account-dashboard .success>form{margin-top:20px}.account-dashboard .success form .primary{width:100%}}
@media(max-width:520px){.account-actions{grid-template-columns:1fr}.account-header-link{font-size:.75rem;padding:6px 9px}}
@media(max-width:720px){.status-main{padding-top:24px}.status-main h1{font-size:2.7rem}.status-table-wrap{overflow:visible}.status-table-wrap table{min-width:0}.status-table-wrap thead{display:none}.status-table-wrap table,.status-table-wrap tbody,.status-table-wrap tr,.status-table-wrap th,.status-table-wrap td{display:block}.status-table-wrap tr{padding:15px 16px;border-bottom:1px solid var(--line)}.status-table-wrap tr:last-child{border-bottom:0}.status-table-wrap th,.status-table-wrap td{border:0;padding:4px 0}.status-table-wrap tbody th{font-size:1.35rem;margin-bottom:5px}.status-table-wrap td{display:grid;grid-template-columns:105px minmax(0,1fr);gap:10px}.status-table-wrap td:before{content:attr(data-label);color:var(--muted);font-size:.72rem;font-weight:850;text-transform:uppercase;letter-spacing:.05em}.status-table-wrap .detail{max-width:none}}
.promise span{border:0;border-left:3px solid var(--coral);border-radius:0;padding:2px 10px;color:var(--muted);font-size:.8rem}.required-note{text-align:right;color:var(--muted);font-size:.78rem;margin:18px 0 0}.required{color:var(--coral);font-weight:900}.field-group{display:flex;flex-direction:column;gap:5px;font-size:.9rem;font-weight:750}.field-label{display:flex;flex-direction:column;gap:3px}.field-label small{color:var(--muted);font-weight:400}.form-progress{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;list-style:none;margin:20px 0 0;padding:0}.form-progress button{width:100%;border:0;border-top:2px solid var(--line);background:transparent;color:var(--muted);padding:10px 4px;text-align:left;font:inherit;font-size:.78rem;font-weight:800;cursor:pointer}.form-progress button span{display:inline-grid;place-items:center;width:23px;height:23px;margin-right:5px;border-radius:50%;background:var(--line);color:var(--ink)}.form-progress button[aria-current=step]{border-color:var(--green);color:var(--green)}.form-progress button[aria-current=step] span{background:var(--lime)}.wizard-ready .wizard-step[hidden]{display:none}.wizard-step{padding:0 0 36px;margin-top:12px}.wizard-step legend{margin-bottom:12px}.wizard-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:28px}.wizard-nav-end{justify-content:flex-end}.wizard-nav .primary{width:auto;margin:0}.secondary-step{border:1px solid var(--ink);border-radius:4px;background:transparent;color:var(--ink);padding:12px 18px;font:inherit;font-weight:850;cursor:pointer}.district-picker{position:relative;border:1px solid #99a298;border-radius:4px;background:#fff}.district-picker summary{min-height:45px;padding:10px 40px 10px 12px;cursor:pointer;font-weight:500;list-style:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.district-picker summary::-webkit-details-marker{display:none}.district-picker summary:after{content:'⌄';position:absolute;right:14px;top:9px;color:var(--green);font-size:1.2rem}.district-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;border-top:1px solid var(--line);background:var(--line);max-height:250px;overflow:auto}.district-options label{display:grid;grid-template-columns:18px minmax(0,1fr);align-items:start;gap:8px;background:#fff;padding:10px;font-size:.82rem;font-weight:600}.district-options input{width:16px;height:16px;margin:1px 0 0;padding:0}.account-note{align-self:end;margin:0 0 9px}
@media(max-width:720px){.form-progress button{font-size:.68rem}.form-progress button span{display:grid;margin:0 0 4px}.district-options{grid-template-columns:1fr 1fr}.wizard-step{padding:0 0 32px}.wizard-nav{align-items:stretch}.wizard-nav button{flex:1;padding-left:10px;padding-right:10px}}

input[type=number]{appearance:textfield;-moz-appearance:textfield}input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.count-control{display:flex;align-items:stretch;gap:8px;min-width:0}.count-control input{flex:1;min-width:0;text-align:center}.count-control button{flex:0 0 46px;min-height:46px;border:1px solid var(--green);border-radius:4px;background:var(--paper);color:var(--green);font:inherit;font-size:1.4rem;cursor:pointer}.count-control button[hidden]{display:none}.count-control button:disabled{opacity:.4;cursor:default}.count-control button:focus-visible{outline:3px solid var(--lime)}
select{appearance:none;-webkit-appearance:none;min-height:46px;padding-right:42px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 16 16'%3E%3Cpath d='m4 6 4 4 4-4' fill='none' stroke='%231f5c45' stroke-width='2'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;background-size:16px}.district-picker summary{position:relative;min-height:46px}.district-picker summary:after{content:'';width:8px;height:8px;border-right:2px solid var(--green);border-bottom:2px solid var(--green);right:17px;top:50%;transform:translateY(-70%) rotate(45deg)}.district-picker[open] summary:after{transform:translateY(-30%) rotate(225deg)}
.review-badge{display:flex;align-items:center;gap:9px;text-transform:none;letter-spacing:0;font-size:.82rem}.review-badge i{width:9px;height:9px;border-radius:50%;background:var(--green)}.review-main{padding-top:35px}.review-intro{max-width:1080px;margin:0 auto 34px}.review-hello{position:relative;display:inline-block;margin:0;color:var(--coral);font-size:1.12rem;font-weight:900}.review-hello:after{content:' ';position:absolute;right:-27px;top:-7px;width:17px;height:18px;border-top:3px solid var(--coral);border-right:3px solid var(--coral);transform:rotate(-12deg)}.review-intro h1{font-size:clamp(3.1rem,6.3vw,5.6rem);margin:.12em 0}.review-intro .lede{max-width:950px;margin:.25em 0}.review-card{max-width:1080px;margin:auto;background:var(--card);border:1px solid var(--ink);box-shadow:9px 9px 0 var(--ink);padding:28px 34px 18px}.review-row{display:grid;grid-template-columns:125px minmax(0,1fr);align-items:center;gap:22px;padding:28px 0}.review-row+.review-row{border-top:1px solid var(--line)}.review-icon{position:relative;width:82px;height:70px;display:grid;place-items:center;color:var(--green);font-family:Georgia,serif;font-size:4.8rem;line-height:1}.review-icon span{position:absolute;right:4px;bottom:9px;color:var(--coral);font-family:inherit;font-size:1.05rem}.review-family{font-size:4rem}.review-family span{right:5px;bottom:14px}.review-label{margin:0 0 7px;color:var(--green);font-size:.76rem;font-weight:900;letter-spacing:.11em;text-transform:uppercase}.review-row h2,.review-status h2{font-size:clamp(1.55rem,3vw,2.15rem);margin:0}.review-row p:last-child{margin:8px 0 0;color:var(--muted);font-size:1.06rem}.review-status{display:grid;grid-template-columns:105px minmax(0,1fr);align-items:center;gap:18px;margin-top:5px;padding:22px 26px;background:var(--lime)}.review-status p{margin:5px 0 0;font-size:1.02rem}.review-status-link{white-space:nowrap;font-weight:800}.review-plane{color:var(--green);font-size:3.1rem;transform:rotate(-32deg);text-align:center}.review-actions{max-width:780px;margin:34px auto 0;display:grid;grid-template-columns:1fr 1fr;gap:22px}.review-actions form{margin:0}.review-actions form .button{width:100%;height:100%}.review-actions .button{display:flex;align-items:center;justify-content:center;margin:0;text-align:center}.review-actions .secondary:hover{background:var(--card)}.review-back{display:block;width:max-content;margin:17px auto 0;color:var(--ink)}
@media(max-width:720px){.review-main{padding:18px 16px 70px}.review-intro{margin-bottom:25px}.review-intro h1{font-size:3rem}.review-card{padding:13px 18px 18px;box-shadow:5px 5px 0 var(--ink)}.review-row{grid-template-columns:1fr;gap:8px;padding:24px 0}.review-icon{width:54px;height:45px;font-size:3.3rem;justify-self:start}.review-family{font-size:2.9rem}.review-status{grid-template-columns:52px minmax(0,1fr);padding:18px 16px}.review-plane{font-size:2.2rem}.review-actions{grid-template-columns:1fr;gap:11px}.review-actions .button{min-height:52px}.review-badge{font-size:.7rem}}
"""
