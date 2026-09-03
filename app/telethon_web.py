from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import quote
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .telethon_manager import TelethonManager


@dataclass(slots=True)
class LoginTicket:
    owner_id: int
    expires_at: float
    phase: str = "credentials"
    used: bool = False


class TelethonWebAuth:
    """Small owner-only browser wizard for Telethon authorization.

    The URL contains a random, short-lived ticket. The ticket is created only
    after the configured OWNER_ID presses the button in Telegram and is sent
    to that owner's private chat. API hash, login code and 2FA password are
    therefore entered in the browser, not posted into a group topic.
    """

    def __init__(
        self,
        telethon: "TelethonManager",
        *,
        host: str,
        port: int,
        public_url: str,
        ticket_ttl_seconds: int = 900,
    ) -> None:
        self.telethon = telethon
        self.host = host
        self.port = port
        self.public_url = public_url.rstrip("/")
        self.ticket_ttl_seconds = max(120, int(ticket_ttl_seconds))
        self._tickets: dict[str, LoginTicket] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        app = web.Application(client_max_size=128 * 1024)
        app.router.add_get("/", self._root)
        app.router.add_get("/health", self._health)
        app.router.add_get("/telethon", self._show)
        app.router.add_post("/telethon/start", self._start_login)
        app.router.add_post("/telethon/code", self._submit_code)
        app.router.add_post("/telethon/password", self._submit_password)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    def create_login_url(self, owner_id: int) -> str:
        self._purge_expired()
        # Only the latest ticket for this owner remains active.
        for token, ticket in list(self._tickets.items()):
            if ticket.owner_id == owner_id:
                self._tickets.pop(token, None)
        token = secrets.token_urlsafe(32)
        self._tickets[token] = LoginTicket(
            owner_id=owner_id,
            expires_at=time.time() + self.ticket_ttl_seconds,
        )
        return f"{self.public_url}/telethon?t={quote(token)}"

    def _purge_expired(self) -> None:
        now = time.time()
        for token, ticket in list(self._tickets.items()):
            if ticket.expires_at <= now or ticket.used:
                self._tickets.pop(token, None)

    def _ticket(self, request: web.Request, *, expected_phase: str | None = None) -> tuple[str, LoginTicket] | None:
        self._purge_expired()
        token = (request.query.get("t") or "").strip()
        if not token:
            return None
        ticket = self._tickets.get(token)
        if not ticket or ticket.used or ticket.expires_at <= time.time():
            return None
        if expected_phase is not None and ticket.phase != expected_phase:
            return None
        return token, ticket

    async def _root(self, request: web.Request) -> web.Response:
        connected = await self.telethon.is_connected()
        body = self._shell(
            "<h1>🤖 XZONA Group Bot</h1>"
            "<p class='ok'>Сервис запущен.</p>"
            f"<p>Telethon: <b>{'подключён' if connected else 'не подключён'}</b></p>"
            "<p class='hint'>Авторизация Telethon открывается только по одноразовой ссылке из /admin в Telegram.</p>"
        )
        return self._html(body)

    async def _health(self, request: web.Request) -> web.Response:
        connected = await self.telethon.is_connected()
        return web.json_response({"ok": True, "ready": True, "telethon_connected": connected})

    async def _show(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request)
        if not loaded:
            return self._html(self._expired_page(), status=403)
        token, ticket = loaded
        connected = await self.telethon.is_connected()
        if connected:
            return self._html(self._page("Telethon уже подключён", "Аккаунт уже авторизован. Можно закрыть это окно.", token, done=True))
        if ticket.phase == "credentials":
            body = f"""
            <h1>🔐 Подключение Telethon</h1>
            <p>Введите данные приложения Telegram и номер аккаунта, от имени которого бот прочитает старую историю тем.</p>
            <form method="post" action="/telethon/start?t={escape(token)}">
              <label>API ID<input name="api_id" inputmode="numeric" autocomplete="off" required></label>
              <label>API HASH<input name="api_hash" autocomplete="off" required></label>
              <label>Телефон<input name="phone" type="tel" placeholder="+79990000000" autocomplete="tel" required></label>
              <button type="submit">Получить код Telegram</button>
            </form>
            <p class="hint">API HASH и код входа не публикуются в Telegram. Сессия после входа хранится в базе в зашифрованном виде.</p>
            """
            return self._html(self._shell(body))
        if ticket.phase == "code":
            return self._html(self._code_page(token))
        if ticket.phase == "password":
            return self._html(self._password_page(token))
        return self._html(self._expired_page(), status=403)

    async def _start_login(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request, expected_phase="credentials")
        if not loaded:
            return self._html(self._expired_page(), status=403)
        token, ticket = loaded
        data = await request.post()
        raw_api_id = str(data.get("api_id", "")).strip()
        api_hash = str(data.get("api_hash", "")).strip()
        phone = str(data.get("phone", "")).strip().replace(" ", "")
        if not raw_api_id.isdigit() or len(api_hash) < 16 or not phone.startswith("+"):
            return self._html(self._error_page(token, "Проверьте API ID, API HASH и телефон в международном формате."), status=400)
        try:
            async with self._lock:
                await self.telethon.begin_login(int(raw_api_id), api_hash, phone)
        except Exception as exc:
            return self._html(self._error_page(token, f"Не удалось запросить код: {type(exc).__name__}: {exc}"), status=400)
        ticket.phase = "code"
        return self._html(self._code_page(token, notice="Код отправлен Telegram. Введите его ниже."))

    async def _submit_code(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request, expected_phase="code")
        if not loaded:
            return self._html(self._expired_page(), status=403)
        token, ticket = loaded
        data = await request.post()
        code = str(data.get("code", "")).replace(" ", "").replace("-", "").strip()
        if not code:
            return self._html(self._code_page(token, error="Введите код из Telegram."), status=400)
        try:
            async with self._lock:
                result = await self.telethon.submit_code(code)
        except Exception as exc:
            return self._html(self._code_page(token, error=str(exc)), status=400)
        if result == "password":
            ticket.phase = "password"
            return self._html(self._password_page(token))
        ticket.used = True
        return self._html(self._page("✅ Telethon подключён", "Авторизация завершена. Это окно можно закрыть.", token, done=True))

    async def _submit_password(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request, expected_phase="password")
        if not loaded:
            return self._html(self._expired_page(), status=403)
        token, ticket = loaded
        data = await request.post()
        password = str(data.get("password", ""))
        if not password:
            return self._html(self._password_page(token, error="Введите пароль двухэтапной аутентификации."), status=400)
        try:
            async with self._lock:
                await self.telethon.submit_password(password)
        except Exception as exc:
            return self._html(self._password_page(token, error=f"Не удалось войти: {exc}"), status=400)
        ticket.used = True
        return self._html(self._page("✅ Telethon подключён", "Авторизация завершена. Пароль 2FA не сохранён. Окно можно закрыть.", token, done=True))

    def _code_page(self, token: str, *, notice: str = "", error: str = "") -> str:
        extra = ""
        if notice:
            extra += f'<p class="ok">{escape(notice)}</p>'
        if error:
            extra += f'<p class="err">{escape(error)}</p>'
        body = f"""
        <h1>📨 Код Telegram</h1>
        {extra}
        <p>Введите код подтверждения, который Telegram отправил вашему аккаунту.</p>
        <form method="post" action="/telethon/code?t={escape(token)}">
          <label>Код<input name="code" inputmode="numeric" autocomplete="one-time-code" autofocus required></label>
          <button type="submit">Продолжить</button>
        </form>
        """
        return self._shell(body)

    def _password_page(self, token: str, *, error: str = "") -> str:
        err = f'<p class="err">{escape(error)}</p>' if error else ""
        body = f"""
        <h1>🔑 Облачный пароль</h1>
        {err}
        <p>На аккаунте включена двухэтапная аутентификация Telegram.</p>
        <form method="post" action="/telethon/password?t={escape(token)}">
          <label>Пароль 2FA<input name="password" type="password" autocomplete="current-password" autofocus required></label>
          <button type="submit">Авторизоваться</button>
        </form>
        <p class="hint">Пароль используется только для текущего входа и не сохраняется.</p>
        """
        return self._shell(body)

    def _error_page(self, token: str, error: str) -> str:
        body = f"""
        <h1>⚠️ Ошибка подключения</h1>
        <p class="err">{escape(error)}</p>
        <p><a class="button" href="/telethon?t={escape(token)}">Вернуться к форме</a></p>
        """
        return self._shell(body)

    def _page(self, title: str, text: str, token: str, *, done: bool = False) -> str:
        body = f"<h1>{escape(title)}</h1><p>{escape(text)}</p>"
        if done:
            body += '<p class="ok">Секретные данные не нужно отправлять сообщениями боту.</p>'
        return self._shell(body)

    def _expired_page(self) -> str:
        return self._shell(
            "<h1>Ссылка недействительна</h1>"
            "<p>Срок одноразовой ссылки истёк. Откройте /admin в группе и запросите новое окно авторизации Telethon.</p>"
        )

    @staticmethod
    def _html(text: str, *, status: int = 200) -> web.Response:
        return web.Response(text=text, status=status, content_type="text/html", charset="utf-8")

    @staticmethod
    def _shell(body: str) -> str:
        return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XZONA — Telethon</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0e1116;color:#e8edf3;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:560px;margin:7vh auto;padding:28px;background:#171c24;border:1px solid #2a3340;border-radius:18px;box-shadow:0 20px 60px #0007}}
h1{{font-size:26px;margin:0 0 18px}}p{{margin:12px 0}}label{{display:block;margin:16px 0 6px;font-weight:650}}input{{width:100%;margin-top:7px;padding:13px 14px;border:1px solid #3b4656;border-radius:11px;background:#0f141b;color:#fff;font-size:16px;outline:none}}input:focus{{border-color:#7aa2f7}}button,.button{{display:inline-block;margin-top:18px;padding:12px 18px;border:0;border-radius:11px;background:#4f7cff;color:white;font-weight:700;text-decoration:none;cursor:pointer}}.hint{{color:#9ba8b8;font-size:14px}}.ok{{padding:10px 12px;border-radius:10px;background:#153a2a;color:#a9f0c8}}.err{{padding:10px 12px;border-radius:10px;background:#442026;color:#ffc2c8}}code{{word-break:break-all}}
</style></head><body><main>{body}</main></body></html>"""
