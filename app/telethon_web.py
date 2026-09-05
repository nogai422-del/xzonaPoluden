from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
import secrets
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import quote
from typing import TYPE_CHECKING

from aiohttp import web
import qrcode

if TYPE_CHECKING:
    from .telethon_manager import TelethonManager


@dataclass(slots=True)
class LoginTicket:
    owner_id: int
    expires_at: float
    phase: str = "credentials"
    used: bool = False
    qr_id: str | None = None


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
        app.router.add_get("/telethon/qr/status", self._qr_status)
        app.router.add_post("/telethon/qr/refresh", self._qr_refresh)
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
        connected = self.telethon.connected
        body = self._shell(
            "<h1>🤖 XZONA Group Bot</h1>"
            "<p class='ok'>Сервис запущен.</p>"
            f"<p>Telethon: <b>{'подключён' if connected else 'не подключён'}</b></p>"
            "<p class='hint'>Авторизация Telethon открывается только по одноразовой ссылке из /admin в Telegram.</p>"
        )
        return self._html(body)

    async def _health(self, request: web.Request) -> web.Response:
        connected = self.telethon.connected
        return web.json_response({"ok": True, "ready": True, "telethon_connected": connected})

    async def _show(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request)
        if not loaded:
            return self._html(self._expired_page(), status=403)
        token, ticket = loaded
        if ticket.qr_id:
            info = self.telethon.qr_status(ticket.qr_id)
            if info['state'] == 'connected':
                ticket.used = True
                return self._html(self._page('✅ Telethon подключён', 'Вход по QR завершён. Можно закрыть это окно.', token, done=True))
            if info['state'] == 'password':
                ticket.phase = 'password'
                return self._html(self._password_page(token))
            if ticket.phase == 'qr':
                return self._html(self._qr_page(token, info))
        connected = await self.telethon.is_connected()
        if connected:
            return self._html(self._page("Telethon уже подключён", "Аккаунт уже авторизован. Можно закрыть это окно.", token, done=True))
        if ticket.phase == "credentials":
            body = f"""
            <h1>🔐 Подключение Telethon</h1>
            <p>Введите API ID и API HASH приложения Telegram, затем выберите вход по QR-коду или по номеру телефона.</p>
            <form method="post" action="/telethon/start?t={escape(token)}">
              <label>API ID<input name="api_id" inputmode="numeric" autocomplete="off" required></label>
              <label>API HASH<input name="api_hash" autocomplete="off" required></label>
              <button type="submit" name="method" value="qr">Войти по QR-коду</button>
              <label>Телефон — только для входа по коду<input name="phone" type="tel" placeholder="+79990000000" autocomplete="tel"></label>
              <button type="submit" name="method" value="phone">Получить код Telegram</button>
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
        method = str(data.get('method', 'phone'))
        if method not in {'phone','qr'} or not raw_api_id.isdigit() or int(raw_api_id) <= 0 or len(api_hash) < 16 or (method == 'phone' and not phone.startswith("+")):
            return self._html(self._error_page(token, "Проверьте API ID, API HASH и телефон в международном формате."), status=400)
        try:
            async with self._lock:
                if self._ticket(request, expected_phase='credentials') is None:
                    return self._html(self._expired_page(), status=403)
                if method == 'qr':
                    ticket.qr_id = await self.telethon.begin_qr_login(int(raw_api_id), api_hash)
                    ticket.phase = 'qr'
                    return self._html(self._qr_page(token, self.telethon.qr_status(ticket.qr_id)))
                await self.telethon.begin_login(int(raw_api_id), api_hash, phone)
                ticket.phase = 'code'
        except Exception as exc:
            return self._html(self._error_page(token, f"Не удалось запросить код: {type(exc).__name__}: {exc}"), status=400)
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
                if self._ticket(request, expected_phase='password') is None:
                    return self._html(self._expired_page(), status=403)
                await self.telethon.submit_password(password, qr_id=ticket.qr_id)
        except Exception as exc:
            return self._html(self._password_page(token, error=f"Не удалось войти: {exc}"), status=400)
        ticket.used = True
        return self._html(self._page("✅ Telethon подключён", "Авторизация завершена. Пароль 2FA не сохранён. Окно можно закрыть.", token, done=True))

    async def _qr_status(self, request: web.Request) -> web.Response:
        loaded = self._ticket(request)
        if not loaded or not loaded[1].qr_id:
            return web.json_response({'state':'expired'}, status=403, headers={'Cache-Control':'no-store'})
        _, ticket = loaded
        info = self.telethon.qr_status(ticket.qr_id)
        if info['state'] == 'password':
            ticket.phase = 'password'
        # The QR token itself never leaves the protected HTML page.
        return web.json_response({'state':info['state'], 'expires':info.get('expires',0)}, headers={'Cache-Control':'no-store'})

    async def _qr_refresh(self, request: web.Request) -> web.Response:
        async with self._lock:
            loaded = self._ticket(request, expected_phase='qr')
            if not loaded or not loaded[1].qr_id:
                return self._html(self._expired_page(),status=403)
            token,ticket = loaded
            info = self.telethon.qr_status(ticket.qr_id)
            if info['state'] in ('connected','password'):
                return web.HTTPSeeOther('/telethon?t='+quote(token))
            try:
                ticket.qr_id = await self.telethon.refresh_qr_login(ticket.qr_id)
            except Exception:
                return self._html(self._error_page(token,'Не удалось обновить QR-код. Откройте новую ссылку из бота.'),status=400)
            return self._html(self._qr_page(token,self.telethon.qr_status(ticket.qr_id)))

    def _qr_page(self, token: str, info: dict) -> str:
        picture = ''
        if info['state'] == 'waiting':
            stream = BytesIO()
            qrcode.make(info['url'],box_size=8,border=4).save(stream,format='PNG')
            png = base64.b64encode(stream.getvalue()).decode('ascii')
            picture = f'<img id="qr" src="data:image/png;base64,{png}" alt="QR-код входа Telegram" style="width:280px;max-width:100%;image-rendering:pixelated">'
        status = 'Ожидаю подтверждения в Telegram…' if picture else info.get('error') or 'QR-код истёк. Обновите его.'
        status_url = json.dumps('/telethon/qr/status?t='+quote(token))
        next_url = json.dumps('/telethon?t='+quote(token))
        return self._shell(f'''
        <h1>Вход по QR-коду</h1>
        <p>На телефоне откройте Telegram → Настройки → Устройства → Подключить устройство.
        Отсканируйте QR-код и подтвердите вход. Удобнее открыть эту страницу на компьютере.</p>
        {picture}<p id="status" class="ok">{escape(status)}</p>
        <form method="post" action="/telethon/qr/refresh?t={escape(token)}">
          <button type="submit">Обновить QR-код</button>
        </form>
        <p class="hint">Если включён облачный пароль, после сканирования появится форма 2FA.</p>
        <script>
        async function checkQR() {{
          try {{
            const r = await fetch({status_url}, {{cache:'no-store'}});
            const s = await r.json();
            if (s.state === 'connected' || s.state === 'password') {{location.replace({next_url}); return;}}
            if (!r.ok || s.state === 'expired' || s.state === 'error') {{
              const image = document.getElementById('qr'); if(image) image.remove();
              document.getElementById('status').textContent = r.ok ? 'QR-код недействителен. Обновите его.' : 'Ссылка истекла. Запросите новую ссылку в боте.';
              return;
            }}
          }} catch(e) {{ document.getElementById('status').textContent='Связь прервалась. Повторяю проверку…'; }}
          setTimeout(checkQR, 2000);
        }}
        setTimeout(checkQR, 1000);
        </script>''')

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
        return web.Response(text=text, status=status, content_type="text/html", charset="utf-8",
                            headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

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
