"""Small same-origin onboarding website for user-owned rental searches."""

from __future__ import annotations

import hashlib
import html
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
from .persistence import RentalStore
from .search_profile import SearchProfile
from .telegram_linking import create_telegram_link


StartResponse = Callable[[str, list[tuple[str, str]]], object]
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SESSION_COOKIE = "humanos_session"
_CSRF_COOKIE = "humanos_csrf"
_MAX_BODY_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class WebSettings:
    database_path: str
    bot_username: str
    secure_cookies: bool = True
    session_days: int = 30


@dataclass(frozen=True, slots=True)
class PageState:
    user: User | None = None
    renter: RenterProfile | None = None
    search: SearchProfile | None = None


class WebApplication:
    """Dependency-free WSGI application sharing the worker's SQLite store."""

    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings

    def __call__(self, environ: Mapping[str, object], start_response: StartResponse):
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "/"))
        cookies = _cookies(str(environ.get("HTTP_COOKIE", "")))
        session_token = cookies.get(_SESSION_COOKIE, "")
        session_hash = _digest(session_token) if session_token else ""
        with RentalStore(self.settings.database_path) as store:
            user_id = store.web_session_user_id(session_hash) if session_hash else None
            if path == "/health" and method == "GET":
                return self._respond(
                    start_response,
                    "200 OK",
                    json.dumps({"status": "ok"}),
                    content_type="application/json; charset=utf-8",
                )
            if path == "/" and method == "GET":
                csrf = cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(24)
                state = self._load_state(store, user_id)
                return self._respond(
                    start_response,
                    "200 OK",
                    _profile_page(state, csrf),
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
                        _profile_page(PageState(), csrf, str(error_value), form),
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
                    _saved_page(state, telegram_url, telegram_error),
                    cookies=tuple(response_cookies),
                )
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
    ) -> list[bytes]:
        payload = body.encode("utf-8")
        headers = _security_headers(content_type, len(payload))
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
        values = parse_qs(payload, keep_blank_values=True, max_num_fields=40)
    except (UnicodeDecodeError, ValueError):
        return {}, "El formulario contiene datos que no se pueden procesar."
    return {key: items[-1] for key, items in values.items()}, ""


def _valid_csrf(form: Mapping[str, str], cookies: Mapping[str, str]) -> bool:
    submitted = form.get("csrf_token", "")
    expected = cookies.get(_CSRF_COOKIE, "")
    return bool(submitted and expected and secrets.compare_digest(submitted, expected))


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


def _profile_page(
    state: PageState,
    csrf: str,
    error: str = "",
    submitted: Mapping[str, str] | None = None,
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
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    session_note = "Revisa tu búsqueda y cambia lo que necesites. Esta sesión está vinculada a este navegador." if user else "Define tus criterios. Al terminar podrás conectar Telegram para recibir avisos."
    body = f"""
    <header><a class="brand" href="/">Se admiten humanos</a><span class="badge">Gratis y open source</span></header>
    <main>
      <section class="hero"><p class="eyebrow">Alertas de alquiler que van al grano</p><h1>Encontrar casa ya es bastante difícil.</h1><p class="lede">Cuéntanos qué buscas. Revisamos anuncios nuevos, aplicamos tus criterios y te avisamos por Telegram para que puedas reaccionar a tiempo.</p><div class="promise"><span>Tus criterios, tu búsqueda</span><span>Sin anuncios duplicados</span><span>Sin vender tus datos</span></div></section>
      <section class="panel"><div class="panel-head"><p class="step">Tu búsqueda</p><h2>{'Actualiza tus datos' if user else 'Cuéntanos qué necesitas'}</h2><p>{html.escape(session_note)}</p></div>{error_html}
      <form method="post" action="/profile"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">
        <fieldset><legend>1 · Presupuesto y zona</legend><div class="grid">
          <label>Presupuesto preferido al mes (€)<input required type="number" min="300" max="10000" name="max_rent_eur" value="{_value(form, 'max_rent_eur')}"></label>
          <label>Máximo excepcional (€)<small>Opcional; solo se usa en modo urgente.</small><input type="number" min="300" max="10000" name="stretch_max_rent_eur" value="{_value(form, 'stretch_max_rent_eur')}" placeholder="Ej. 1000"></label>
          <label>Dormitorios preferidos<input required type="number" min="0" max="10" name="preferred_min_bedrooms" value="{_value(form, 'preferred_min_bedrooms')}"></label>
          <label class="wide">Distritos aceptables <small>Déjalo vacío para todo Madrid; separa varios con comas.</small><input name="acceptable_districts" value="{_value(form, 'acceptable_districts')}" placeholder="Centro, Chamberí, Retiro…"></label>
          <label>Mobiliario<select name="furnished"><option value="any"{' selected' if furnished == 'any' else ''}>Me da igual</option><option value="yes"{' selected' if furnished == 'yes' else ''}>Amueblado</option><option value="no"{' selected' if furnished == 'no' else ''}>Sin amueblar</option></select></label>
          <label>Tipo de alquiler<select required name="rental_duration"><option value="" disabled{' selected' if not rental_duration else ''}>Elige una opción</option><option value="long_term"{' selected' if rental_duration == 'long_term' else ''}>Larga duración</option><option value="temporary"{' selected' if rental_duration == 'temporary' else ''}>Temporal / por temporada</option><option value="both"{' selected' if rental_duration == 'both' else ''}>Ambos</option></select></label>
          <label>Fecha límite de entrada <small>Opcional</small><input type="date" name="move_in_by" value="{_value(form, 'move_in_by')}"></label>
        </div><div class="option-groups">
          <section class="option-group"><h3>Urgencia</h3><div class="option-list"><label><input type="checkbox" name="urgent"{_checked(urgent)}><span>Avisarme también de oportunidades hasta mi máximo excepcional</span></label></div></section>
          <section class="option-group"><h3>Tipo</h3><div class="option-list"><label><input type="checkbox" name="property_type_home"{_checked(property_type_home)}><span>Vivienda completa</span></label><label><input type="checkbox" name="property_type_room"{_checked(property_type_room)}><span>Habitación</span></label></div></section>
          <section class="option-group"><h3>Flexibilidad</h3><div class="option-list"><label><input type="checkbox" name="allow_studios"{_checked(studios)}><span>Incluir estudios</span></label><label><input type="checkbox" name="allow_unknown_bedrooms"{_checked(unknown)}><span>Incluir anuncios sin dormitorios indicados</span></label><label><input type="checkbox" name="move_in_asap"{_checked(asap)}><span>Necesito entrar cuanto antes</span></label></div></section>
        </div><small class="option-note">Las sugerencias urgentes se etiquetan claramente y nunca superan tu máximo excepcional. Garajes y locales comerciales se excluyen siempre.</small></fieldset>
        <fieldset><legend>2 · Tú y tu situación</legend><div class="grid">
          <label>Tu nombre<input required maxlength="100" name="display_name" value="{_value(form, 'display_name')}"></label>
          <label>Correo <small>Opcional; aún no enviamos emails.</small><input type="email" maxlength="254" name="email" value="{_value(form, 'email')}"></label>
          <label>Nombre de la persona inquilina<input required maxlength="100" name="renter_name" value="{_value(form, 'renter_name')}"></label>
          <label>Nombre para contactar a la agencia<input required maxlength="100" name="sender_name" value="{_value(form, 'sender_name')}"></label>
          <label>Nómina mensual (€)<input type="number" min="1" max="100000" name="monthly_income_eur" value="{_value(form, 'monthly_income_eur')}"></label>
          <label>Trabajo<input maxlength="160" name="job" value="{_value(form, 'job')}" placeholder="Stage manager, docente…"></label>
          <label class="wide">Tipo de contrato<input maxlength="120" name="contract_type" value="{_value(form, 'contract_type')}" placeholder="Indefinido, autónomo…"></label>
        </div></fieldset>
        <fieldset><legend>3 · Animales <span class="optional">Opcional</span></legend><p class="section-intro">Si convivirán contigo, los tendremos en cuenta al revisar cada anuncio.</p><div class="grid">
          <label>¿Cuántos vivirían contigo?<input type="number" min="0" max="10" name="pet_count" value="{_value(form, 'pet_count')}"></label>
          <label>¿Qué animales son?<input maxlength="80" name="pet_species" value="{_value(form, 'pet_species')}" placeholder="Gato, perro…"></label>
          <label class="wide">Algo útil sobre ellos <small>Opcional</small><textarea maxlength="300" name="pet_notes" placeholder="Edad, tamaño, comportamiento…">{_value(form, 'pet_notes')}</textarea></label>
        </div></fieldset>
        <button class="primary" type="submit">Guardar y conectar Telegram →</button>
      </form>
      {f'<form method="post" action="/logout" class="logout"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}"><button type="submit">Cerrar sesión en este navegador</button></form>' if user else ''}
      </section>
      <section class="how"><p class="eyebrow">Cómo funciona</p><h2>Rápido, pero honesto.</h2><div class="cards"><article><b>01</b><h3>Buscamos</h3><p>Consultamos fuentes autorizadas y webs de agencias con controles para detectar cambios.</p></article><article><b>02</b><h3>Filtramos</h3><p>Aplicamos presupuesto, zona, tipo de alquiler y demás preferencias sin inventar información que el anuncio no da.</p></article><article><b>03</b><h3>Te avisamos</h3><p>Recibes el anuncio y la evidencia en Telegram para abrirlo y responder en dos toques.</p></article></div></section>
    </main>"""
    return _layout("Tu búsqueda · Se admiten humanos", body)


def _saved_page(state: PageState, telegram_url: str, error: str) -> str:
    assert state.search is not None
    action = f'<a class="primary button" href="{html.escape(telegram_url, quote=True)}">Conectar mi Telegram →</a>' if telegram_url else f'<div class="error">{html.escape(error)}</div><a class="secondary button" href="/">Volver a intentarlo</a>'
    budget_text = f"Presupuesto preferido: <strong>{state.search.max_rent_eur} €</strong>"
    if state.search.urgent and state.search.stretch_max_rent_eur is not None:
        budget_text += f"; sugerencias urgentes hasta <strong>{state.search.stretch_max_rent_eur} €</strong>"
    body = f"""<header><a class="brand" href="/">Se admiten humanos</a><span class="badge">Búsqueda guardada</span></header><main class="center"><section class="success"><div class="success-mark">✓</div><p class="eyebrow">Ya sabemos qué buscar</p><h1>Tu búsqueda está lista.</h1><p>{budget_text} al mes en Madrid. Ahora conecta Telegram para recibir solamente los anuncios que encajen contigo.</p>{action}<small>El enlace de Telegram caduca en 10 minutos y solo funciona una vez.</small><a href="/">← Revisar o cambiar mis datos</a></section></main>"""
    return _layout("Búsqueda guardada · Se admiten humanos", body)


def _telegram_page(url: str) -> str:
    return _layout("Conectar Telegram · Se admiten humanos", f"""<header><a class="brand" href="/">Se admiten humanos</a></header><main class="center"><section class="success"><p class="eyebrow">Último paso</p><h1>Conecta Telegram.</h1><p>Este enlace es personal, caduca en 10 minutos y solo se puede usar una vez.</p><a class="primary button" href="{html.escape(url, quote=True)}">Abrir Telegram →</a><a href="/">← Volver a mi búsqueda</a></section></main>""")


def _message_page(title: str, message: str) -> str:
    return _layout(title, f"<main class=\"center\"><section class=\"success\"><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><a class=\"button secondary\" href=\"/\">Volver</a></section></main>")


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><meta name="description" content="Alertas gratuitas y open source para encontrar alquiler antes y con menos ruido."><style>{_CSS}</style></head><body>{body}<footer><p>Hecho para ayudar, no para ganar dinero. <a href="https://github.com/Algarciap/se-admiten-humanos">Código libre y abierto</a>.</p><p>Tus preferencias mandan; no inventamos información que el anuncio no da.</p></footer></body></html>"""


_CSS = """
:root{--ink:#17231c;--paper:#f5f1e8;--card:#fffdf8;--green:#1f5c45;--lime:#d9ef9f;--coral:#ef765b;--line:#cfd5c9;--muted:#5d685f}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}header{max-width:1180px;margin:auto;padding:28px 24px;display:flex;align-items:center;justify-content:space-between}.brand{font-family:Georgia,serif;font-size:1.35rem;font-weight:700;color:var(--ink);text-decoration:none}.brand span{font-size:1rem}.badge{background:var(--lime);padding:7px 12px;border-radius:999px;font-size:.78rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em}main{max-width:1180px;margin:auto;padding:30px 24px 90px}.hero{max-width:880px;padding:72px 0 62px}.eyebrow,.step{text-transform:uppercase;letter-spacing:.13em;font-weight:900;font-size:.75rem;color:var(--green)}h1,h2,h3{font-family:Georgia,serif;line-height:1.04;margin:.25em 0}h1{font-size:clamp(3rem,8vw,6.7rem);letter-spacing:-.055em}.lede{font-size:clamp(1.1rem,2vw,1.4rem);max-width:760px;color:var(--muted)}.promise{display:flex;flex-wrap:wrap;gap:9px;margin-top:28px}.promise span{border:1px solid var(--ink);border-radius:999px;padding:7px 12px;font-size:.85rem}.panel{background:var(--card);border:1px solid var(--ink);box-shadow:9px 9px 0 var(--ink);padding:clamp(22px,5vw,54px)}.panel-head{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:end;border-bottom:1px solid var(--line);padding-bottom:28px}.panel-head h2,.how h2{font-size:clamp(2rem,5vw,4rem)}.panel-head>p{color:var(--muted);max-width:450px}fieldset{border:0;padding:38px 0;margin:0;border-bottom:1px solid var(--line)}legend{font-weight:900;font-size:1.05rem;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.wide{grid-column:1/-1}label{display:flex;flex-direction:column;gap:7px;font-size:.9rem;font-weight:750}label small,.success small{color:var(--muted);font-weight:400}input,select,textarea{width:100%;border:1px solid #99a298;background:#fff;padding:13px 14px;border-radius:4px;color:var(--ink);font:inherit}input:focus,select:focus,textarea:focus{outline:3px solid var(--lime);border-color:var(--green)}textarea{min-height:90px;resize:vertical}.checks{display:flex;flex-wrap:wrap;gap:18px;margin-top:22px}.checks label{flex-direction:row;align-items:center;font-weight:600}.checks input{width:auto}.primary,.button{display:inline-block;margin-top:30px;background:var(--green);color:white;border:1px solid var(--green);padding:15px 20px;border-radius:4px;font:inherit;font-weight:900;text-decoration:none;cursor:pointer}.primary:hover{background:#174534}.secondary{background:transparent;color:var(--ink);border:1px solid var(--ink)}.error{background:#ffe4dc;border-left:5px solid var(--coral);padding:14px 16px;margin:24px 0}.logout{margin-top:22px}.logout button{border:0;background:none;text-decoration:underline;color:var(--muted);cursor:pointer}.how{padding:110px 0 20px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.cards article{border-top:2px solid var(--ink);padding-top:20px}.cards b{color:var(--coral)}.cards h3{font-size:1.7rem}.cards p{color:var(--muted)}.center{min-height:70vh;display:grid;place-items:center}.success{max-width:700px;text-align:center;background:var(--card);border:1px solid var(--ink);box-shadow:9px 9px 0 var(--ink);padding:clamp(30px,7vw,70px)}.success h1{font-size:clamp(2.8rem,7vw,5rem)}.success p{font-size:1.15rem;color:var(--muted)}.success .button{display:block;margin:28px auto 16px;max-width:340px}.success>a:last-child{display:block;margin-top:25px;color:var(--ink)}.success-mark{margin:auto;width:52px;height:52px;display:grid;place-items:center;border-radius:50%;background:var(--lime);font-size:1.7rem;font-weight:900}footer{border-top:1px solid var(--line);max-width:1180px;margin:auto;padding:35px 24px 60px;display:flex;justify-content:space-between;gap:30px;color:var(--muted);font-size:.85rem}footer a{color:var(--ink)}@media(max-width:720px){header{align-items:flex-start}.badge{font-size:.65rem}.hero{padding-top:38px}.panel-head,.grid,.cards{grid-template-columns:1fr}.wide{grid-column:auto}.panel{box-shadow:5px 5px 0 var(--ink)}footer{display:block}h1{font-size:3.25rem}}
.panel{padding:clamp(22px,4vw,46px)}.panel-head{display:block;padding-bottom:22px}.panel-head>.step{color:var(--green);max-width:none;margin:0}.panel-head h2{font-size:clamp(2.2rem,5vw,3.7rem);margin:.18em 0}.panel-head>p:last-child{max-width:680px;margin:10px 0 0}fieldset{padding:0 0 22px}legend{padding:0 0 8px;margin:0}.optional{margin-left:6px;color:var(--muted);font-size:.72rem;font-weight:500}.section-intro{margin:0 0 12px;color:var(--muted);font-size:.86rem}.grid{gap:13px 18px}.grid>label{align-self:end}label{gap:5px}input,select,textarea{padding:10px 12px}textarea{min-height:78px}.option-groups{display:grid;grid-template-columns:1.25fr .8fr 1.25fr;gap:12px;margin-top:12px;align-items:start}.option-group{background:#fff;border:1px solid var(--line);border-radius:4px;padding:13px 14px}.option-group h3{font-family:inherit;font-size:.72rem;line-height:1.2;margin:0 0 10px;color:var(--green);text-transform:uppercase;letter-spacing:.1em}.option-list{display:grid;gap:9px}.option-list label{display:grid;grid-template-columns:17px minmax(0,1fr);gap:8px;align-items:start;font-weight:600;line-height:1.3}.option-list input{width:15px;height:15px;margin:2px 0 0;padding:0}.option-note{display:block;margin-top:11px;color:var(--muted);font-size:.8rem}.primary,.button{padding:13px 18px}@media(max-width:800px){.option-groups{grid-template-columns:1fr 1fr}.option-group:last-child{grid-column:1/-1}}@media(max-width:720px){.option-groups{grid-template-columns:1fr}.option-group:last-child{grid-column:auto}}
"""
