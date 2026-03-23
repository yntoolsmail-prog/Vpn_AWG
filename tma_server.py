#!/usr/bin/env python3
# tma_server.py — HTTP-сервер для TMA
# Version: 2.0
# Запускается отдельно от bot.py.
# Вся бизнес-логика — в awg_core.py.

import json, time, hashlib, hmac
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
import os

from awg_core import (
    BOT_TOKEN, ADMIN_ID,
    collect_stats_full, collect_stats_basic,
    is_approved,
)

TMA_DIR     = "/etc/amnezia/amneziawg/tma"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080

# ── Авторизация через Telegram initData ───────────────────────────────────────
def verify_telegram_init_data(init_data_raw: str) -> int | None:
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает user_id если подпись верна, иначе None.
    Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data_raw or not BOT_TOKEN:
        return None
    try:
        params = {}
        for part in init_data_raw.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[unquote(k)] = unquote(v)

        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        # Данные не должны быть старше 1 часа
        auth_date = int(params.get("auth_date", "0"))
        if time.time() - auth_date > 3600:
            return None

        user_json = params.get("user", "{}")
        user = json.loads(user_json)
        return int(user.get("id", 0)) or None

    except Exception:
        return None

# ── HTTP-обработчик ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # отключаем стандартный шумный лог

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
            ext  = path.rsplit(".", 1)[-1].lower()
            mime = {
                "html": "text/html; charset=utf-8",
                "js":   "application/javascript",
                "css":  "text/css",
                "png":  "image/png",
                "ico":  "image/x-icon",
            }.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Init-Data")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        # ── Статика ──
        if path in ("/", "/index.html"):
            self.send_file(os.path.join(TMA_DIR, "index.html"))
            return

        # ── API: статистика ──
        if path == "/api/stats":
            init_data = self.headers.get("X-Init-Data", "")
            user_id   = verify_telegram_init_data(init_data)
            if not user_id:
                self.send_json({"error": "unauthorized"}, 401)
                return
            if user_id == ADMIN_ID:
                data = collect_stats_full()
                data["is_admin"] = True
            else:
                if not is_approved(user_id):
                    self.send_json({"error": "forbidden"}, 403)
                    return
                data = collect_stats_basic()
                data["is_admin"] = False
            self.send_json(data)
            return

        # ── API: healthcheck (без авторизации) ──
        if path == "/api/health":
            self.send_json({"ok": True, "ts": int(time.time())})
            return

        self.send_response(404)
        self.end_headers()

# ── Запуск ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n\033[0;32m✓ TMA сервер запущен: http://{LISTEN_HOST}:{LISTEN_PORT}\033[0m")
    print(f"  Admin ID: {ADMIN_ID}")
    print(f"  TMA dir:  {TMA_DIR}\n")
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")
