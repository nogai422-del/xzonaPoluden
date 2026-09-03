from __future__ import annotations

import asyncio
import json
import os
import socketserver
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler


def _safe_port() -> int:
    raw = (os.getenv("PORT") or "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        port = 8080
    return port if 1 <= port <= 65535 else 8080


def _fallback_health(error: BaseException) -> None:
    """Keep the container alive and expose a safe startup error on /health.

    This is only used when the real application cannot start. Secrets and
    environment values are never returned by this endpoint.
    """

    error_type = type(error).__name__
    message = str(error).replace("\n", " ")[:400]
    payload = {
        "ok": False,
        "ready": False,
        "service": "xzona-group-bot",
        "startup_error_type": error_type,
        "startup_error": message,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in {"", "/health"}:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(503)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt, *args):
            return

    port = _safe_port()
    print(f"[XZONA BOOT] Fatal startup error: {error_type}: {message}", flush=True)
    print(f"[XZONA BOOT] Fallback health endpoint: 0.0.0.0:{port}/health", flush=True)
    try:
        with socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler) as server:
            server.allow_reuse_address = True
            server.serve_forever()
    except OSError as bind_error:
        print(f"[XZONA BOOT] Could not start fallback health server: {bind_error}", flush=True)
        # Do not exit immediately: leave a visible runtime process/log for Bothost.
        threading.Event().wait()


def run() -> None:
    print("[XZONA BOOT] Starting XZONA Group Bot v7.4.2 Bothost-safe", flush=True)
    print(
        "[XZONA BOOT] env: "
        f"BOT_TOKEN={'yes' if any(os.getenv(k) for k in ('BOT_TOKEN','TELEGRAM_BOT_TOKEN','TOKEN','API_TOKEN')) else 'NO'}, "
        f"OWNER_ID={'yes' if os.getenv('OWNER_ID') else 'no'}, "
        f"PORT={_safe_port()}, DB_PATH={os.getenv('DB_PATH','/app/data/bot.db')}",
        flush=True,
    )
    try:
        from bot import main
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[XZONA BOOT] Stopped by signal", flush=True)
    except BaseException as exc:
        traceback.print_exc()
        _fallback_health(exc)


if __name__ == "__main__":
    run()
