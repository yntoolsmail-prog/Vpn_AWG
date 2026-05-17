#!/usr/bin/env python3
# tma_server.py — HTTP-сервер для TMA (Telegram Mini App)
# Version: 3.1  —  Flask-based, full CRUD API
# Запускается отдельно от bot.py.
# Вся бизнес-логика — в awg_core.py.

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from functools import wraps
from urllib.parse import unquote

from flask import Flask, jsonify, request, Response, send_file

from awg_core import (
    ADMIN_ID, AWG_CONF, AWG_IFACE, AWG_SERVICE, BACKUP_DIR, BOT_SERVICE, CLIENTS_DIR, BOT_TOKEN,
    ENV_FILE, QRENCODE_BIN, SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP, SERVER_IP, SERVER_PORT, SERVER_PUBLIC,
    PRIMARY_DNS, SECONDARY_DNS, USERS_FILE,
    build_allowed_ips, can_access_device, collect_stats_basic, collect_stats_full,
    create_backup, create_client, device_short_name, fmt_bytes,
    get_all_clients, get_allowed_ips_for_client, get_awg_dump, get_combined_awg_dump, get_bw_histogram,
    get_client_keys, get_client_pub,
    get_maintenance, get_sites_json, get_system_stats, get_user_clients, get_user_name,
    is_approved, load_client_excl, load_users, make_conf_for_client, make_conf_for_client_ep,
    make_vpn_link, make_wg_conf, process_domain,
    remove_client_from_awg, save_client_excl, save_users, set_maintenance,
    load_servers,
)
from awg_ssh import PARAMIKO_AVAILABLE, ssh_sync_peer_to_slave
from awg_stats import _histogram_for_tma
from sites_data import DEFAULT_SELECTED

logging.basicConfig(level=logging.WARNING)

TMA_DIR     = "/etc/amnezia/amneziawg/tma"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080

app = Flask(__name__, static_folder=TMA_DIR, static_url_path="")

# ── Авторизация через Telegram initData ───────────────────────────────────────

def verify_telegram_init_data(init_data_raw: str) -> int | None:
    """Проверяет HMAC-подпись initData Telegram WebApp.
    Возвращает user_id (int) если подпись верна и не истёк 1 час, иначе None."""
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
        if time.time() - int(params.get("auth_date", "0")) > 3600:
            return None
        user = json.loads(params.get("user", "{}"))
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
        "endpoint":        stats.get("endpoint", ""),
        "server_label":    stats.get("server", ""),
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


def _make_conf_filename(name: str, srv_name: str = "") -> str:
    """Формирует имя файла конфига: User.ServerName.Device.conf — аналогично боту."""
    if not srv_name:
        return f"{name}.conf"
    srv_clean = re.sub(r"[^\w]", "", srv_name.replace(" ", "_"))
    parts = name.split(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.{srv_clean}.{parts[1]}.conf"
    return f"{name}.{srv_clean}.conf"


def _sync_new_peer_to_slaves(name: str, keys: dict) -> None:
    """Синхронизирует нового peer на все slave-серверы в фоне (fire-and-forget).
    Идентично боту: bot/handlers/servers.py _sync_peer_to_all_slaves."""
    if not PARAMIKO_AVAILABLE:
        return
    slaves = [s for s in load_servers() if not s.get("is_primary")]
    if not slaves:
        return
    pub = keys["pub"]
    psk = keys["psk"]
    ip  = keys["ip"]  # без /32, ssh_sync_peer_to_slave добавляет сам
    for srv in slaves:
        threading.Thread(
            target=ssh_sync_peer_to_slave,
            args=(srv, name, pub, psk, ip),
            daemon=True,
        ).start()


def _resolve_allowed_ips(name: str, use_excl: bool) -> str:
    """Возвращает AllowedIPs для .conf: если use_excl=True и есть .excl.json — применяем исключения."""
    if not use_excl:
        return "0.0.0.0/0"
    return get_allowed_ips_for_client(name)


def _send_file_via_bot(chat_id: int, filename: str, content: str,
                       caption: str = "") -> bool:
    """Отправляет текстовый файл пользователю через Telegram sendDocument."""
    if not BOT_TOKEN:
        return False
    with tempfile.NamedTemporaryFile(mode="w", suffix=".conf",
                                     delete=False, encoding="utf-8") as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                "-F", f"chat_id={chat_id}",
                "-F", f"caption={caption}",
                "-F", f"document=@{tmp_path};filename={filename}",
            ],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── Статика ───────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/index.html")
def index():
    return send_file(os.path.join(TMA_DIR, "index.html"))


# ── Healthcheck ───────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time())})


# ── Статистика ────────────────────────────────────────────────────────────────

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
        # Устройства пользователя в монитор
        dump  = get_combined_awg_dump()
        now   = int(time.time())
        names = get_user_clients(uid)
        data["peers"] = [_device_info(n, dump, now) for n in names]
        # Кольцо онлайн — только свои устройства
        data["user_peers_total"]  = len(names)
        data["user_peers_online"] = sum(
            1 for n in names
            if (pub := get_client_pub(n)) and
               dump.get(pub, {}).get("handshake") and
               now - dump[pub]["handshake"] < 180
        )
    return jsonify(data)


# ── Устройства ────────────────────────────────────────────────────────────────

@app.route("/api/devices")
@require_auth
def list_devices(user_id):
    """Список устройств текущего пользователя."""
    dump  = get_combined_awg_dump()
    now   = int(time.time())
    names = get_user_clients(user_id)
    return jsonify([_device_info(n, dump, now) for n in names])


@app.route("/api/bw_histogram")
@require_auth
def api_bw_histogram(user_id):
    """Гистограмма нагрузки за N дней (admin). days=0 — всё время."""
    days = request.args.get("days", "7")
    try:
        days = int(days)
    except ValueError:
        days = 7
    hist = get_bw_histogram(0 if days == 0 else days)
    return jsonify(_histogram_for_tma(hist) or {})


@app.route("/api/devices/all")
@require_admin
def list_all_devices(user_id):
    """Все устройства всех пользователей (admin)."""
    dump  = get_combined_awg_dump()
    now   = int(time.time())
    names = get_all_clients()
    return jsonify([_device_info(n, dump, now) for n in names])


@app.route("/api/devices", methods=["POST"])
@require_auth
def create_device(user_id):
    """Создать новое устройство."""
    body     = request.get_json(silent=True) or {}
    dev_name = (body.get("name") or "").strip()

    if not dev_name or not re.match(r"^[A-Za-z0-9_-]+$", dev_name):
        return jsonify({"error": "Некорректное имя устройства"}), 400

    username  = get_user_name(user_id)
    full_name = f"{username}.{dev_name}"

    if os.path.exists(f"{CLIENTS_DIR}/{full_name}.conf"):
        return jsonify({"error": "Устройство с таким именем уже существует"}), 409

    try:
        keys = _run_async(create_client(full_name))
        dump = get_combined_awg_dump()
        _sync_new_peer_to_slaves(full_name, keys)
        return jsonify(_device_info(full_name, dump, int(time.time()))), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/devices/<path:name>", methods=["DELETE"])
@require_auth
def delete_device(user_id, name):
    """Удалить устройство."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        return jsonify({"error": "Устройство не найдено"}), 404
    try:
        remove_client_from_awg(name)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/endpoints")
@require_auth
def api_endpoints(user_id):
    """Список доступных эндпоинтов (обратная совместимость — эндпоинты первичного сервера)."""
    servers = load_servers()
    primary = next((s for s in servers if s.get("is_primary")), servers[0] if servers else None)
    if primary:
        eps = []
        for ep in primary.get("endpoints", []):
            t = "Домен" if ep.get("type") == "domain" else "IP"
            eps.append({"key": ep["value"], "label": f"{t} · {ep['value']}", "value": ep["value"]})
        return jsonify(eps)
    # Fallback to env values
    eps = [{"key": "main", "label": f"Домен · {SERVER_ENDPOINT}", "value": SERVER_ENDPOINT}]
    if SERVER_IP and SERVER_IP != SERVER_ENDPOINT:
        eps.append({"key": "ip", "label": f"IP · {SERVER_IP}", "value": SERVER_IP})
    if SERVER_ENDPOINT_BACKUP:
        eps.append({"key": "backup", "label": f"Резервный · {SERVER_ENDPOINT_BACKUP}", "value": SERVER_ENDPOINT_BACKUP})
    return jsonify(eps)


@app.route("/api/servers")
@require_auth
def api_servers(user_id):
    """Список серверов с эндпоинтами (для выбора при генерации конфига)."""
    servers = load_servers()
    result = []
    for srv in servers:
        result.append({
            "id":        srv.get("id", ""),
            "name":      srv.get("name", ""),
            "emoji":     srv.get("emoji", "🖥"),
            "is_primary": srv.get("is_primary", False),
            "endpoints": [
                {
                    "value":    ep["value"],
                    "type":     ep.get("type", "ip"),
                    "verified": ep.get("verified", False),
                    "label":    ep["value"],
                }
                for ep in srv.get("endpoints", [])
            ],
        })
    return jsonify(result)


@app.route("/api/devices/<path:name>/qr")
@require_auth
def device_qr(user_id, name):
    """QR-код в виде base64 PNG. Принимает ?endpoint=X для генерации на лету."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    endpoint    = request.args.get("endpoint") or SERVER_ENDPOINT
    use_excl    = request.args.get("use_excl", "").lower() in ("1", "true")
    allowed_ips = _resolve_allowed_ips(name, use_excl)
    conf_text   = make_conf_for_client(name, endpoint, allowed_ips)
    if conf_text is None:
        return jsonify({"error": "Устройство не найдено"}), 404
    too_large = use_excl and len(conf_text.encode()) > 2900
    if too_large:
        # Генерируем QR без исключений — только базовое подключение
        allowed_ips = "0.0.0.0/0"
        conf_text   = make_conf_for_client(name, endpoint, allowed_ips)
    try:
        png_bytes = subprocess.check_output(
            [QRENCODE_BIN, "-t", "PNG", "-s", "6", "-o", "-"],
            input=conf_text.encode(),
        )
        b64 = base64.b64encode(png_bytes).decode()
        return jsonify({"qr": f"data:image/png;base64,{b64}", "too_large": too_large})
    except Exception as e:
        return jsonify({"error": f"qrencode: {e}"}), 500


@app.route("/api/devices/<path:name>/vpnlink")
@require_auth
def device_vpnlink(user_id, name):
    """Ссылка vpn:// для AmneziaVPN. Принимает ?endpoint=X&srv_id=Y."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    keys = get_client_keys(name)
    if not keys:
        return jsonify({"error": "Устройство не найдено"}), 404
    endpoint  = request.args.get("endpoint") or SERVER_ENDPOINT
    srv_id    = request.args.get("srv_id", "")
    srv_emoji = request.args.get("srv_emoji", "")
    spub, sprt = SERVER_PUBLIC, SERVER_PORT
    if srv_id:
        for s in load_servers():
            if s.get("id") == srv_id:
                spub = s.get("awg_public_key") or SERVER_PUBLIC
                sprt = str(s.get("awg_port") or SERVER_PORT)
                break
    vpn_display_name = f"{srv_emoji} {name}".strip() if srv_emoji else name
    try:
        link = make_vpn_link(
            keys["priv"], keys["pub"], keys["ip"], keys["psk"], keys["obfs"],
            name=vpn_display_name, endpoint=endpoint,
            server_public=spub, server_port=sprt,
        )
        return jsonify({"link": link})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/devices/<path:name>/send", methods=["POST"])
@require_auth
def device_send(user_id, name):
    """Отправляет .conf файл в Telegram-чат пользователя через бота."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    body        = request.get_json(silent=True) or {}
    endpoint    = body.get("endpoint") or SERVER_ENDPOINT
    use_excl    = bool(body.get("use_excl", False))
    srv_name    = body.get("srv_name", "")
    allowed_ips = _resolve_allowed_ips(name, use_excl)
    conf_text   = make_conf_for_client(name, endpoint, allowed_ips)
    if conf_text is None:
        return jsonify({"error": "Устройство не найдено"}), 404
    short      = device_short_name(name)
    excl_note  = "\n🌐 С исключениями сайтов" if use_excl and allowed_ips != "0.0.0.0/0" else ""
    filename   = _make_conf_filename(name, srv_name)
    caption    = (
        f"📄 Конфиг {short}\n"
        f"🌐 Endpoint: {endpoint}:{SERVER_PORT}{excl_note}\n\n"
        f"Импортируйте в AmneziaWG."
    )
    ok = _send_file_via_bot(user_id, filename, conf_text, caption)
    if ok:
        return jsonify({"ok": True})
    return jsonify({"error": "Ошибка отправки через Telegram"}), 500


@app.route("/api/devices/<path:name>/send_qr", methods=["POST"])
@require_auth
def device_send_qr(user_id, name):
    """Генерирует QR-код и отправляет PNG в Telegram-чат пользователя."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    body        = request.get_json(silent=True) or {}
    endpoint    = body.get("endpoint") or SERVER_ENDPOINT
    use_excl    = bool(body.get("use_excl", False))
    allowed_ips = _resolve_allowed_ips(name, use_excl)
    conf_text   = make_conf_for_client(name, endpoint, allowed_ips)
    if conf_text is None:
        return jsonify({"error": "Устройство не найдено"}), 404
    conf_bytes = conf_text.encode()
    if len(conf_bytes) > 2900:
        return jsonify({"error": "Конфиг слишком большой для QR — используйте .conf файл"}), 400
    try:
        png_bytes = subprocess.check_output(
            [QRENCODE_BIN, "-t", "PNG", "-s", "6", "-o", "-"],
            input=conf_bytes,
        )
    except Exception as e:
        return jsonify({"error": f"Ошибка генерации QR: {e}"}), 500
    short      = device_short_name(name)
    excl_note  = "\n🌐 С исключениями сайтов" if use_excl and allowed_ips != "0.0.0.0/0" else ""
    caption    = (
        f"📱 QR-код AmneziaWG — {short}\n"
        f"🌐 Endpoint: {endpoint}:{SERVER_PORT}{excl_note}"
    )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tf.write(png_bytes)
        tmp_path = tf.name
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                "-F", f"chat_id={user_id}",
                "-F", f"caption={caption}",
                "-F", f"photo=@{tmp_path};type=image/png",
            ],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        return jsonify({"error": "Ошибка отправки в Telegram"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route("/api/devices/<path:name>/sites", methods=["GET"])
@require_auth
def device_sites_get(user_id, name):
    """Текущие site exclusions устройства."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    conf_text = _get_conf_text(name)
    if conf_text is None:
        return jsonify({"error": "Не найдено"}), 404
    m = re.search(r"^AllowedIPs = (.+)$", conf_text, re.MULTILINE)
    current_allowed = m.group(1).strip() if m else "0.0.0.0/0"
    return jsonify({
        "allowed_ips": current_allowed,
        "sites":       get_sites_json(),
    })


# ── Исключения клиента (.excl.json) ──────────────────────────────────────────

@app.route("/api/devices/<path:name>/excl", methods=["GET"])
@require_auth
def device_excl_get(user_id, name):
    """Возвращает сохранённые исключения клиента."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        return jsonify({"error": "Не найдено"}), 404
    excl = load_client_excl(name) or {"sites": [], "custom_domains": []}
    return jsonify(excl)


def _validate_domain_entry(entry: str) -> tuple[bool, str]:
    """Валидация домена/IP/CIDR — идентична боту (sites.py).
    Возвращает (True, нормализованный_вход) или (False, сообщение_об_ошибке)."""
    entry = entry.strip()
    for prefix in ("https://", "http://", "www."):
        if entry.startswith(prefix):
            entry = entry[len(prefix):]
    entry = entry.rstrip("/")
    try:
        ipaddress.ip_network(entry, strict=False)
        return True, entry
    except ValueError:
        pass
    if "." in entry and len(entry) >= 4 and not entry.startswith("."):
        return True, entry
    return False, f"Неверный формат: «{entry}». Укажите домен (site.ru) или IP/CIDR (1.2.3.0/24)."


@app.route("/api/devices/<path:name>/excl", methods=["PUT"])
@require_auth
def device_excl_put(user_id, name):
    """Сохраняет исключения клиента (не меняет сам .conf — применяется при создании QR/conf)."""
    if not can_access_device(user_id, name):
        return jsonify({"error": "Нет доступа"}), 403
    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        return jsonify({"error": "Не найдено"}), 404

    body = request.get_json(silent=True) or {}
    raw_domains = list(body.get("custom_domains", []))

    # Валидация кастомных доменов (идентично боту)
    validated = []
    for raw in raw_domains:
        ok, result = _validate_domain_entry(raw)
        if not ok:
            return jsonify({"error": result}), 400
        validated.append(result)

    # DNS-пробинг для новых доменов (не IP/CIDR) — как в боте
    existing = load_client_excl(name) or {}
    existing_domains = set(existing.get("custom_domains", []))
    for entry in validated:
        if entry not in existing_domains:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                threading.Thread(target=process_domain, args=(entry,), daemon=True).start()

    data = {
        "sites":          list(body.get("sites", [])),
        "custom_domains": validated,
    }
    try:
        save_client_excl(name, data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Список сайтов ─────────────────────────────────────────────────────────────

@app.route("/api/sites")
@require_auth
def api_sites(user_id):
    return jsonify(get_sites_json())


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
    dump     = get_combined_awg_dump()
    now      = int(time.time())
    approved = users.get("approved", {})

    result = []
    for uid, info in approved.items():
        uname   = info.get("name", "")
        clients = [c for c in get_all_clients() if c.startswith(uname + ".")]
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


@app.route("/api/backups/<filename>/send", methods=["POST"])
@require_admin
def backup_send(user_id, filename):
    """Отправляет бэкап в Telegram-чат администратора."""
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Недопустимое имя файла"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Файл не найден"}), 404
    if not BOT_TOKEN:
        return jsonify({"error": "BOT_TOKEN не настроен"}), 500
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                "-F", f"chat_id={user_id}",
                "-F", f"caption=💾 {filename}",
                "-F", f"document=@{path};filename={filename}",
            ],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            return jsonify({"ok": True})
        return jsonify({"error": "Ошибка curl"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backups/<filename>/restore", methods=["POST"])
@require_admin
def backup_restore(user_id, filename):
    """Восстанавливает конфиги из бэкапа. Перед восстановлением создаёт автобэкап."""
    if ".." in filename or "/" in filename:
        return jsonify({"error": "Недопустимое имя файла"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Файл не найден"}), 404

    # Валидация архива — идентично боту (maintenance.py)
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
    except Exception as e:
        return jsonify({"error": f"Не удалось открыть архив: {e}"}), 400

    has_conf = any(n.endswith(".conf") and "awg" in n for n in names)
    has_env  = "server.env" in names
    if not has_conf or not has_env:
        return jsonify({
            "error": "Файл не похож на бэкап AmneziaWG — не найдены обязательные файлы (конфиг интерфейса, server.env).",
            "has_conf": has_conf,
            "has_env":  has_env,
        }), 400

    # Автобэкап перед восстановлением
    try:
        auto_backup = create_backup(prefix="pre_restore")
    except Exception as e:
        return jsonify({"error": f"Не удалось создать автобэкап: {e}"}), 500

    try:
        # Останавливаем AWG
        subprocess.run(["systemctl", "stop", f"awg-quick@{AWG_IFACE}"],
                       capture_output=True)

        # Чистим clients/ чтобы не осталось мусора
        if os.path.exists(CLIENTS_DIR):
            shutil.rmtree(CLIENTS_DIR)
        os.makedirs(CLIENTS_DIR)

        # Распаковываем бэкап
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall("/etc/amnezia/amneziawg/")
    except Exception as e:
        return jsonify({"error": f"Ошибка при восстановлении: {e}",
                        "auto_backup": os.path.basename(auto_backup)}), 500

    # Поднимаем AWG и перезапускаем бота (через 2 сек, не блокируем ответ)
    subprocess.Popen(
        ["bash", "-c",
         f"sleep 2 && systemctl start awg-quick@{AWG_IFACE} && systemctl restart {BOT_SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return jsonify({
        "ok":          True,
        "auto_backup": os.path.basename(auto_backup),
    })


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
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
