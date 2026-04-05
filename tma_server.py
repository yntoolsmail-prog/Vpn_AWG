#!/usr/bin/env python3
# tma_server.py — HTTP-сервер для TMA (Telegram Mini App)
# Version: 3.0  —  Flask-based, full CRUD API
# Запускается отдельно от bot.py.
# Вся бизнес-логика — в awg_core.py.

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import subprocess
import time
from functools import wraps
from urllib.parse import unquote

from flask import Flask, jsonify, request, Response, send_file

from awg_core import (
    ADMIN_ID, BACKUP_DIR, CLIENTS_DIR, BOT_TOKEN,
    SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP, SERVER_IP, SERVER_PORT,
    PRIMARY_DNS, SECONDARY_DNS,
    build_allowed_ips, collect_stats_basic, collect_stats_full,
    create_backup, create_client, fmt_bytes,
    get_all_clients, get_awg_dump, get_client_keys, get_client_pub,
    get_maintenance, get_system_stats, get_user_clients, get_user_name,
    is_approved, load_users, make_wg_conf, remove_client_from_awg,
    save_users, set_maintenance,
)
from sites_data import CATEGORIES, DEFAULT_SELECTED, SITES

logging.basicConfig(level=logging.WARNING)

TMA_DIR     = "/etc/amnezia/amneziawg/tma"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080

app = Flask(__name__, static_folder=TMA_DIR, static_url_path="")

# ── Авторизация через Telegram initData ───────────────────────────────────────

def verify_telegram_init_data(init_data_raw: str) -> int | None:
    """Проверяет подпись initData, возвращает user_id или None."""
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

        auth_date = int(params.get("auth_date", "0"))
        if time.time() - auth_date > 3600:
            return None

        user_json = params.get("user", "{}")
        user = json.loads(user_json)
        return int(user.get("id", 0)) or None
    except Exception:
        return None


def _get_uid() -> int | None:
    return verify_telegram_init_data(request.headers.get("X-Init-Data", ""))


def require_auth(f):
    """Декоратор: проверяет initData, передаёт user_id в аргументе."""
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = _get_uid()
        if not uid:
            return jsonify({"error": "unauthorized"}), 401
        if not is_approved(uid):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, user_id=uid, **kwargs)
    return decorated


def require_admin(f):
    """Декоратор: только для ADMIN_ID."""
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = _get_uid()
        if not uid:
            return jsonify({"error": "unauthorized"}), 401
        if uid != ADMIN_ID:
            return jsonify({"error": "forbidden"}), 403
        return f(*args, user_id=uid, **kwargs)
    return decorated


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_async(coro):
    """Запускает async-корутину из синхронного Flask-контекста."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _device_info(name: str, dump: dict, now: int) -> dict:
    """Собирает данные об устройстве из awg dump."""
    pub   = get_client_pub(name)
    stats = dump.get(pub, {}) if pub else {}
    hs    = stats.get("handshake", 0)
    return {
        "name":            name,
        "handshake":       hs,
        "online":          bool(hs and now - hs < 180),
        "client_upload":   fmt_bytes(stats.get("rx", 0)),
        "client_download": fmt_bytes(stats.get("tx", 0)),
        "rx_bytes":        stats.get("rx", 0),
        "tx_bytes":        stats.get("tx", 0),
    }


def _endpoint_for(conf_text: str) -> str:
    """Извлекает хост эндпоинта из текста .conf (без порта)."""
    m = re.search(r"^Endpoint = (.+):\d+\s*$", conf_text, re.MULTILINE)
    return m.group(1) if m else SERVER_ENDPOINT


def _get_conf_text(name: str) -> str | None:
    path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read()


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# ── Статика ───────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/index.html")
def index():
    return send_file(os.path.join(TMA_DIR, "index.html"))


# ── Healthcheck ───────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


# ── Статистика (совместимость с TMA v1) ──────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    uid = _get_uid()
    if not uid:
        return jsonify({"error": "unauthorized"}), 401
    if uid == ADMIN_ID:
        data = collect_stats_full()
        data["is_admin"] = True
    else:
        if not is_approved(uid):
            return jsonify({"error": "forbidden"}), 403
        data = collect_stats_basic()
        data["is_admin"] = False
    return jsonify(data)


# ── Устройства ────────────────────────────────────────────────────────────────

@app.route("/api/devices")
@require_auth
def list_devices(user_id):
    """Список устройств текущего пользователя."""
    dump  = get_awg_dump()
    now   = int(time.time())
    names = get_user_clients(user_id)
    return jsonify([_device_info(n, dump, now) for n in names])


@app.route("/api/devices/all")
@require_admin
def list_all_devices(user_id):
    """Все устройства всех пользователей (admin)."""
    dump  = get_awg_dump()
    now   = int(time.time())
    names = get_all_clients()
    return jsonify([_device_info(n, dump, now) for n in names])


@app.route("/api/devices", methods=["POST"])
@require_auth
def create_device(user_id):
    """Создать новое устройство."""
    body      = request.get_json(silent=True) or {}
    dev_name  = (body.get("name") or "").strip()
    selected  = set(body.get("sites", [])) | DEFAULT_SELECTED
    endpoint  = body.get("endpoint") or SERVER_ENDPOINT

    if not dev_name or not re.match(r"^[A-Za-z0-9_-]+$", dev_name):
        return jsonify({"error": "Некорректное имя устройства"}), 400

    username    = get_user_name(user_id)
    full_name   = f"{username}.{dev_name}"

    # Проверить — не занято ли имя
    if os.path.exists(f"{CLIENTS_DIR}/{full_name}.conf"):
        return jsonify({"error": "Устройство с таким именем уже существует"}), 409

    # Создаём клиента (async)
    async def _create():
        lock = asyncio.Lock()
        return await create_client(full_name, lock)

    try:
        keys        = _run_async(_create())
        allowed_ips = build_allowed_ips(selected)
        # Перезаписываем .conf с нужным эндпоинтом и AllowedIPs
        conf_text = make_wg_conf(
            keys["priv"], keys["ip"], keys["psk"], keys["obfs"],
            endpoint=endpoint, allowed_ips=allowed_ips,
        )
        with open(f"{CLIENTS_DIR}/{full_name}.conf", "w") as f:
            f.write(conf_text)
        dump = get_awg_dump()
        return jsonify(_device_info(full_name, dump, int(time.time()))), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/devices/<path:name>", methods=["DELETE"])
@require_auth
def delete_device(user_id, name):
    """Удалить устройство."""
    username = get_user_name(user_id)
    if user_id != ADMIN_ID and not name.startswith(username + "."):
        return jsonify({"error": "Нет доступа"}), 403
    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        return jsonify({"error": "Устройство не найдено"}), 404
    try:
        remove_client_from_awg(name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/devices/<path:name>/config")
@require_auth
def device_config(user_id, name):
    """Скачать .conf файл устройства."""
    username = get_user_name(user_id)
    if user_id != ADMIN_ID and not name.startswith(username + "."):
        return jsonify({"error": "Нет доступа"}), 403
    conf_text = _get_conf_text(name)
    if conf_text is None:
        return jsonify({"error": "Не найдено"}), 404
    dev_part = name.split(".", 1)[1] if "." in name else name
    return Response(
        conf_text,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{dev_part}.conf"'},
    )


@app.route("/api/devices/<path:name>/qr")
@require_auth
def device_qr(user_id, name):
    """QR-код (.conf) в виде base64 PNG."""
    username = get_user_name(user_id)
    if user_id != ADMIN_ID and not name.startswith(username + "."):
        return jsonify({"error": "Нет доступа"}), 403
    conf_text = _get_conf_text(name)
    if conf_text is None:
        return jsonify({"error": "Не найдено"}), 404
    try:
        png_bytes = subprocess.check_output(
            ["qrencode", "-t", "PNG", "-s", "6", "-o", "-"],
            input=conf_text.encode(),
        )
        b64 = base64.b64encode(png_bytes).decode()
        return jsonify({"qr": f"data:image/png;base64,{b64}"})
    except Exception as e:
        return jsonify({"error": f"qrencode: {e}"}), 500


@app.route("/api/devices/<path:name>/sites", methods=["GET"])
@require_auth
def device_sites_get(user_id, name):
    """Текущие site exclusions устройства."""
    username = get_user_name(user_id)
    if user_id != ADMIN_ID and not name.startswith(username + "."):
        return jsonify({"error": "Нет доступа"}), 403
    conf_text = _get_conf_text(name)
    if conf_text is None:
        return jsonify({"error": "Не найдено"}), 404
    # Определяем текущий AllowedIPs и считаем какие сайты включены
    m = re.search(r"^AllowedIPs = (.+)$", conf_text, re.MULTILINE)
    current_allowed = m.group(1).strip() if m else "0.0.0.0/0"
    return jsonify({
        "allowed_ips": current_allowed,
        "sites":       _sites_payload(),
    })


@app.route("/api/devices/<path:name>/sites", methods=["PUT"])
@require_auth
def device_sites_put(user_id, name):
    """Обновить site exclusions и перегенерировать .conf."""
    username = get_user_name(user_id)
    if user_id != ADMIN_ID and not name.startswith(username + "."):
        return jsonify({"error": "Нет доступа"}), 403

    body     = request.get_json(silent=True) or {}
    selected = set(body.get("sites", [])) | DEFAULT_SELECTED
    keys     = get_client_keys(name)
    if not keys:
        return jsonify({"error": "Устройство не найдено"}), 404

    conf_text   = _get_conf_text(name)
    endpoint    = _endpoint_for(conf_text) if conf_text else SERVER_ENDPOINT
    allowed_ips = build_allowed_ips(selected)
    new_conf    = make_wg_conf(
        keys["priv"], keys["ip"], keys["psk"], keys["obfs"],
        endpoint=endpoint, allowed_ips=allowed_ips,
    )
    try:
        with open(f"{CLIENTS_DIR}/{name}.conf", "w") as f:
            f.write(new_conf)
        return jsonify({"ok": True, "allowed_ips": allowed_ips})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Список сайтов для выбора ──────────────────────────────────────────────────

def _sites_payload() -> list:
    """Возвращает категории со списком сайтов для UI."""
    result = []
    for cat_label, keys in CATEGORIES.items():
        sites = []
        for k in keys:
            s = SITES.get(k)
            if not s:
                continue
            sites.append({
                "key":     k,
                "name":    s["name"],
                "emoji":   s.get("emoji", ""),
                "locked":  k in DEFAULT_SELECTED,
            })
        if sites:
            result.append({"category": cat_label, "sites": sites})
    return result


@app.route("/api/sites")
@require_auth
def api_sites(user_id):
    """Список доступных сайтов для выбора при создании / редактировании устройства."""
    return jsonify(_sites_payload())


# ── Пользователи (admin) ──────────────────────────────────────────────────────

@app.route("/api/users/pending")
@require_admin
def users_pending(user_id):
    users = load_users()
    result = [
        {"id": uid, **info}
        for uid, info in users.get("pending", {}).items()
    ]
    return jsonify(result)


@app.route("/api/users/pending/<target_id>/approve", methods=["POST"])
@require_admin
def user_approve(user_id, target_id):
    users = load_users()
    info  = users.get("pending", {}).get(target_id)
    if not info:
        return jsonify({"error": "Пользователь не найден в ожидающих"}), 404
    users["approved"][target_id] = info
    del users["pending"][target_id]
    save_users(users)
    return jsonify({"ok": True})


@app.route("/api/users/pending/<target_id>/reject", methods=["POST"])
@require_admin
def user_reject(user_id, target_id):
    users = load_users()
    if target_id not in users.get("pending", {}):
        return jsonify({"error": "Пользователь не найден в ожидающих"}), 404
    del users["pending"][target_id]
    save_users(users)
    return jsonify({"ok": True})


@app.route("/api/users")
@require_admin
def users_list(user_id):
    """Все одобренные пользователи с их устройствами."""
    users    = load_users()
    dump     = get_awg_dump()
    now      = int(time.time())
    approved = users.get("approved", {})

    result = []
    for uid, info in approved.items():
        uname   = info.get("name", "")
        clients = [c for c in get_all_clients() if c.startswith(uname + ".")]
        # суммарный трафик по всем устройствам
        rx_total = tx_total = 0
        for c in clients:
            pub = get_client_pub(c)
            if pub and pub in dump:
                rx_total += dump[pub].get("rx", 0)
                tx_total += dump[pub].get("tx", 0)
        result.append({
            "id":           uid,
            "name":         uname,
            "display":      info.get("display", uname),
            "requested_at": info.get("requested_at", 0),
            "devices":      len(clients),
            "upload":       fmt_bytes(rx_total),
            "download":     fmt_bytes(tx_total),
        })
    return jsonify(result)


@app.route("/api/users/<target_id>", methods=["DELETE"])
@require_admin
def user_delete(user_id, target_id):
    """Удалить пользователя и все его устройства."""
    users = load_users()
    info  = users.get("approved", {}).get(target_id)
    if not info:
        return jsonify({"error": "Пользователь не найден"}), 404

    uname   = info.get("name", "")
    clients = [c for c in get_all_clients() if c.startswith(uname + ".")]
    for c in clients:
        try:
            remove_client_from_awg(c)
        except Exception:
            pass

    del users["approved"][target_id]
    save_users(users)
    return jsonify({"ok": True, "devices_removed": len(clients)})


# ── Бэкапы (admin) ───────────────────────────────────────────────────────────

@app.route("/api/backups")
@require_admin
def backups_list(user_id):
    if not os.path.exists(BACKUP_DIR):
        return jsonify([])
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")],
        reverse=True,
    )
    result = []
    for f in files:
        path = os.path.join(BACKUP_DIR, f)
        stat = os.stat(path)
        result.append({
            "name":     f,
            "size":     stat.st_size,
            "size_fmt": fmt_bytes(stat.st_size),
            "ts":       int(stat.st_mtime),
            "date":     time.strftime("%d.%m.%Y %H:%M", time.localtime(stat.st_mtime)),
        })
    return jsonify(result)


@app.route("/api/backups", methods=["POST"])
@require_admin
def backup_create(user_id):
    try:
        path = create_backup()
        name = os.path.basename(path)
        stat = os.stat(path)
        return jsonify({
            "ok":       True,
            "name":     name,
            "size_fmt": fmt_bytes(stat.st_size),
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<filename>")
@require_admin
def backup_download(user_id, filename):
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Недопустимое имя файла"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Файл не найден"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/backups/<filename>", methods=["DELETE"])
@require_admin
def backup_delete(user_id, filename):
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Недопустимое имя файла"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Файл не найден"}), 404
    os.remove(path)
    return jsonify({"ok": True})


# ── Обслуживание (admin) ──────────────────────────────────────────────────────

@app.route("/api/maintenance")
@require_admin
def maintenance_get(user_id):
    return jsonify(get_maintenance())


@app.route("/api/maintenance", methods=["POST"])
@require_admin
def maintenance_set(user_id):
    body    = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled", False))
    message = str(body.get("message", ""))
    return jsonify(set_maintenance(enabled, message))


# ── Настройки сервера (admin) ─────────────────────────────────────────────────

@app.route("/api/settings")
@require_admin
def settings_get(user_id):
    sys = get_system_stats()
    return jsonify({
        "endpoint":         SERVER_ENDPOINT,
        "endpoint_backup":  SERVER_ENDPOINT_BACKUP,
        "server_ip":        SERVER_IP,
        "port":             SERVER_PORT,
        "dns_primary":      PRIMARY_DNS,
        "dns_secondary":    SECONDARY_DNS,
        "total_devices":    len(get_all_clients()),
        "uptime":           sys["uptime"],
        "load":             sys["load"],
        "ram_pct":          round(sys["ram_used"] / max(sys["ram_total"], 1) * 100),
        "disk_pct":         sys["disk_pct"],
    })


# ── CORS preflight ────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return Response(status=204)


# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n\033[0;32m✓ TMA сервер запущен: http://{LISTEN_HOST}:{LISTEN_PORT}\033[0m")
    print(f"  Admin ID : {ADMIN_ID}")
    print(f"  TMA dir  : {TMA_DIR}")
    print(f"  Endpoints: {len([r for r in app.url_map.iter_rules()])} routes\n")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=False)
