"""Same-origin account routes, rendered in Spanish without browser-side scripts."""

import html
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from .accounts import Accounts, AccountMailError, digest, normalize_email, send_account_email
from .web import _CSRF_COOKIE, _SESSION_COOKIE, _layout, _read_form, _valid_csrf


def _field(name, label, kind="text", autocomplete=""):
    extra = ' minlength="12" maxlength="128"' if kind == "password" else ' maxlength="254"'
    return f'<label>{label}<input required name="{name}" type="{kind}" autocomplete="{autocomplete}"{extra}></label>'


def _form(path, csrf, fields, button):
    return f'<form method="post" action="{path}"><input type="hidden" name="csrf_token" value="{html.escape(csrf, quote=True)}">{fields}<button class="primary" type="submit">{button}</button></form>'


def handle_accounts(app, store, environ, start_response, user_id, cookies):
    path = str(environ.get("PATH_INFO", "/"))
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    accounts = Accounts(store)
    row = accounts.account(user_id)
    if not path.startswith("/account"):
        if path in {"/", "/profile", "/telegram-link"} and not user_id:
            return app._redirect(start_response, "/account/login")
        return None

    csrf = cookies.get(_CSRF_COOKIE) or secrets.token_urlsafe(24)

    def page(title, content, status="200 OK"):
        return app._respond(start_response, status, _layout(title, f'<header><a class="brand" href="/">Se admiten humanos</a><a class="account-header-link" href="/account">Mi cuenta</a></header><main class="center status-login account-page"><section class="success"><p class="eyebrow">Tu espacio</p><h1>{title}</h1>{content}</section></main>'),
                            cookies=(() if cookies.get(_CSRF_COOKIE) else (app._csrf_cookie(csrf),)))

    def signed_in(new_user_id):
        token = secrets.token_urlsafe(32)
        store.save_web_session(digest(token), new_user_id, datetime.now(UTC) + timedelta(days=app.settings.session_days))
        old = cookies.get(_SESSION_COOKIE)
        if old:
            store.delete_web_session(digest(old))
        return app._redirect(start_response, "/", cookies=(app._session_cookie(token), app._csrf_cookie(secrets.token_urlsafe(24))))

    email_field = _field("email", "Correo electrónico", "email", "email")
    password_field = _field("password", "Contraseña", "password", "current-password")
    routes = {"/account", "/account/login", "/account/register", "/account/recover", "/account/verify", "/account/reset", "/account/pause", "/account/disconnect", "/account/export", "/account/delete", "/account/logout-all"}
    if path not in routes:
        return app._error(start_response, "404 Not Found", "Página no encontrada.")
    if method == "GET":
        if path == "/account/login":
            return page("Entra en tu cuenta", _form(path, csrf, email_field + password_field, "Entrar") + '<nav class="account-actions" aria-label="Otras opciones de acceso"><a class="account-action account-action-main" href="/account/register">Crear cuenta</a><a class="account-action" href="/account/recover">Recuperar contraseña</a></nav>')
        if path in {"/account/register", "/account/recover"}:
            title = "Verifica tu correo" if path.endswith("register") else "Recupera tu cuenta"
            note = "Recibirás un enlace de un solo uso para continuar."
            if user_id and not row:
                note += " Vincularemos la búsqueda de este navegador a tu cuenta."
            return page(title, f"<p>{note}</p>" + _form(path, csrf, email_field, "Enviar enlace"))
        if path in {"/account/verify", "/account/reset"}:
            try:
                token = parse_qs(str(environ.get("QUERY_STRING", "")), max_num_fields=4).get("token", [""])[0]
            except ValueError:
                token = ""
            if not 32 <= len(token) <= 128:
                return page("Enlace no válido", '<p>Solicita un nuevo enlace.</p><a href="/account/recover">Recuperar cuenta</a>', "400 Bad Request")
            fields = f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">' + _field("password", "Elige una contraseña (12–128 caracteres)", "password", "new-password")
            return page("Confirma tu cuenta" if path.endswith("verify") else "Nueva contraseña", _form(path, csrf, fields, "Guardar y entrar"))
        if path != "/account":
            return app._error(start_response, "405 Method Not Allowed", "Usa el formulario de tu cuenta.")
        if not user_id:
            return app._redirect(start_response, "/account/login")
        if not row or not row[3]:
            return page("Protege tu búsqueda", '<p>Verifica tu correo para acceder desde otros dispositivos y recuperar tu cuenta.</p><a href="/account/register">Vincular esta búsqueda</a>')
        paused = bool(row[4])
        content = f'<p>Correo verificado: {html.escape(row[1])}</p><p>Avisos: {"en pausa" if paused else "activos"}. Telegram: {"conectado" if bool(store.telegram_chat_id(user_id)) else "desconectado"}.</p>'
        content += _form("/account/pause", csrf, f'<input type="hidden" name="paused" value="{0 if paused else 1}">', "Reanudar avisos" if paused else "Pausar avisos")
        content += '<p>La pausa conserva tus criterios. Al reanudar, se avisará de próximos anuncios; no se reenvía el historial.</p>'
        if store.telegram_chat_id(user_id):
            content += _form("/account/disconnect", csrf, "", "Desconectar Telegram")
        else:
            content += _form("/telegram-link", csrf, "", "Conectar Telegram")
            content += '<p>Abre Telegram y pulsa Iniciar / Start. La confirmación puede tardar hasta 3 minutos.</p><a href="/telegram-status">Comprobar conexión</a>'
        content += _form("/account/logout-all", csrf, password_field, "Cerrar todas las sesiones")
        content += '<h2>Tus datos</h2><p>Introduce tu contraseña para descargar tus datos o eliminar tu cuenta.</p>'
        content += _form("/account/export", csrf, password_field, "Descargar mis datos (JSON)")
        content += '<p>La eliminación borra tu perfil, búsquedas, conexión a Telegram, sesiones e historial personal de esta aplicación. No elimina mensajes ya recibidos en Telegram. Las copias de seguridad se conservan hasta su caducidad.</p>'
        content += _form("/account/delete", csrf, password_field + '<label>Escribe ELIMINAR<input required name="confirmation" autocomplete="off"></label>', "Eliminar mi cuenta y mis datos")
        content += '<p><a href="/">Editar búsqueda</a> · <a href="/account/recover">Cambiar contraseña por correo</a></p>'
        content += _form("/logout", csrf, "", "Cerrar sesión")
        return page("Mi cuenta", content)

    if method != "POST":
        return app._error(start_response, "405 Method Not Allowed", "Método no permitido.")
    form, error = _read_form(environ)
    if error or not _valid_csrf(form, cookies):
        return app._error(start_response, "403 Forbidden", "Recarga la página y vuelve a intentarlo.")
    # REMOTE_ADDR is the direct peer. Never trust client-supplied forwarding headers.
    peer = str(environ.get("REMOTE_ADDR", "unknown"))[:200]
    if not accounts.allow("request:" + peer, 60):
        return page("Espera un momento", "<p>Demasiados intentos. Vuelve a intentarlo en 15 minutos.</p>", "429 Too Many Requests")
    if path in {"/account/login", "/account/register", "/account/recover"}:
        try:
            email = normalize_email(form.get("email", ""))
        except ValueError as error:
            return page("Revisa el correo", f"<p>{html.escape(str(error))}</p>", "422 Unprocessable Entity")
        # One limit covers all entry points so switching routes cannot bypass it.
        if not accounts.allow("email:" + email, 8):
            return page("Espera un momento", "<p>Demasiados intentos. Vuelve a intentarlo en 15 minutos.</p>", "429 Too Many Requests")
        if path == "/account/login":
            found = accounts.login(email, form.get("password", ""))
            if found:
                return signed_in(found)
            return page("No se pudo entrar", '<p>Revisa el correo y la contraseña. Primero debes verificar el correo.</p><a href="/account/login">Volver</a>', "401 Unauthorized")
        purpose = "verify" if path.endswith("register") else "reset"
        token = accounts.issue(email, purpose, user_id if purpose == "verify" else None)
        if token:
            try:
                send_account_email(app.settings.mail, email, purpose, token)
            except AccountMailError as error:
                # Same public response for existing and unknown accounts; operators see only type.
                import logging
                logging.getLogger(__name__).error("account_email_delivery_failed category=%s status=%s", error.category, error.status)
        return page("Revisa tu correo", '<p>Si la solicitud es válida, recibirás un enlace que caduca en 30 minutos. Comprueba Spam y Promociones y espera unos minutos.</p><p>Si ya verificaste tu cuenta, entra con tu contraseña o solicita recuperar el acceso. Conectar Telegram no envía ningún correo.</p>' + _form(path, csrf, email_field, "Volver a solicitar el enlace") + '<p>Al solicitar otro enlace, el anterior deja de funcionar.</p><nav class="account-actions account-recovery-actions" aria-label="Opciones para acceder a tu cuenta"><a class="account-action" href="/account/register">Corregir correo o verificar cuenta</a><a class="account-action" href="/account/recover">Recuperar acceso</a><a class="account-action" href="/account/login">Volver a entrar</a></nav>')
    if path in {"/account/verify", "/account/reset"}:
        try:
            found = accounts.consume(form.get("token", "")[:128], path.rsplit("/", 1)[1], form.get("password", ""))
        except ValueError as error:
            return page("Revisa la contraseña", f"<p>{html.escape(str(error))} Abre de nuevo el enlace para continuar.</p>", "422 Unprocessable Entity")
        if found:
            return signed_in(found)
        return page("Enlace caducado o ya utilizado", '<p>Solicita otro enlace de verificación o recuperación.</p><a href="/account/register">Verificar</a> · <a href="/account/recover">Recuperar</a>', "400 Bad Request")
    if not user_id or not row or not row[3]:
        return app._error(start_response, "403 Forbidden", "Verifica tu cuenta para continuar.")
    if path in {"/account/delete", "/account/export", "/account/logout-all"}:
        if not accounts.require_password(user_id, form.get("password", "")):
            return app._error(start_response, "401 Unauthorized", "La contraseña no es correcta.")
    if path == "/account/pause":
        if form.get("paused") not in {"0", "1"}:
            return app._error(start_response, "400 Bad Request", "Preferencia no válida.")
        accounts.pause(user_id, form["paused"] == "1")
    elif path == "/account/disconnect":
        accounts.disconnect(user_id)
    elif path == "/account/export":
        return app._respond(start_response, "200 OK", accounts.export(user_id), content_type="application/json; charset=utf-8", extra_headers=(("Content-Disposition", 'attachment; filename="mis-datos.json"'),))
    elif path == "/account/delete":
        if form.get("confirmation") != "ELIMINAR":
            return app._error(start_response, "422 Unprocessable Entity", "Escribe ELIMINAR para confirmar.")
        accounts.delete(user_id)
        return app._redirect(start_response, "/account/login", cookies=(app._expired_cookie(_SESSION_COOKIE), app._expired_cookie(_CSRF_COOKIE)))
    elif path == "/account/logout-all":
        with store.connection:
            store.connection.execute("delete from web_sessions where user_id = ?", (user_id,))
        return app._redirect(start_response, "/account/login", cookies=(app._expired_cookie(_SESSION_COOKIE), app._expired_cookie(_CSRF_COOKIE)))
    else:
        return app._error(start_response, "405 Method Not Allowed", "Método no permitido.")
    return app._redirect(start_response, "/account")
