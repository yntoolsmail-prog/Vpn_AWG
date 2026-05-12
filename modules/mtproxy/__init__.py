#!/usr/bin/env python3
"""modules/mtproxy/__init__.py — модуль управления MTProxy для Telegram.

Кнопка "📡 Прокси Telegram" видна ВСЕМ одобренным пользователям (readonly) и
администратору (полное управление).

Поддержка нескольких серверов: если установлен на slave, показывает ссылки
для каждого сервера (один секрет — разные IP/домены). Смена секрета
автоматически синхронизируется на все slave через SSH.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import subprocess
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

logger = logging.getLogger(__name__)

# ── Пути и константы ───────────────────────────────────────────────────────────
PROXY_CONF   = "/etc/proxy-bot/proxy_bot.env"
MTP_DIR      = "/opt/mtproxy"
MTP_BIN      = f"{MTP_DIR}/objs/bin/mtproto-proxy"
MTP_SECRET_F = f"{MTP_DIR}/proxy-secret"
MTP_MULTI_F  = f"{MTP_DIR}/proxy-multi.conf"
MTP_SERVICE  = "mtproxy"
MTP_STATS_F  = "/etc/proxy-bot/mtp_stats.json"
_IPT_CMT_IN  = "mtp_count_in"
_IPT_CMT_OUT = "mtp_count_out"


# ── Конфиг ────────────────────────────────────────────────────────────────────

def _load_conf() -> dict:
    conf = {}
    try:
        with open(PROXY_CONF) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip()
    except Exception:
        pass
    return conf


def _save_conf(data: dict):
    os.makedirs(os.path.dirname(PROXY_CONF), exist_ok=True)
    existing = _load_conf()
    existing.update(data)
    with open(PROXY_CONF, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    os.chmod(PROXY_CONF, 0o600)


def _get_server_ip() -> str:
    try:
        from awg_core import SERVER_IP
        if SERVER_IP:
            return SERVER_IP
    except Exception:
        pass
    conf = _load_conf()
    if conf.get("SERVER_IP"):
        return conf["SERVER_IP"]
    try:
        return subprocess.check_output(
            ["curl", "-sf", "--max-time", "5", "https://api.ipify.org"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "—"


def _get_proxy_address() -> str:
    conf = _load_conf()
    if conf.get("MTP_SERVER"):
        return conf["MTP_SERVER"]
    return _get_server_ip()


# ── Статус и хелперы ──────────────────────────────────────────────────────────

def _mtp_installed() -> bool:
    return os.path.exists(MTP_BIN)


def _mtp_get_secret() -> str:
    return _load_conf().get("MTP_SECRET", "")


def _mtp_get_port() -> str:
    return _load_conf().get("MTP_PORT", "443")


def _mtp_build_link(secret: str, port: str, ip: str) -> str:
    return f"https://t.me/proxy?server={ip}&port={port}&secret={secret}"


def _mtp_generate_secret(mode: str = "ee") -> str:
    raw = secrets.token_hex(16)
    if mode == "ee":
        domain_hex = "www.google.com".encode().hex()
        return f"ee{raw}{domain_hex}"
    elif mode == "dd":
        return f"dd{raw}"
    return raw


def _svc_action(action: str, ok_msg: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", action, MTP_SERVICE],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return True, ok_msg
        return False, f"❌ Ошибка: {r.stderr.strip() or f'journalctl -u {MTP_SERVICE}'}"
    except Exception as e:
        return False, f"❌ {e}"


def _svc_status() -> dict:
    try:
        active = subprocess.check_output(
            ["systemctl", "is-active", MTP_SERVICE],
            text=True, stderr=subprocess.DEVNULL
        ).strip() == "active"
    except Exception:
        active = False
    uptime_str = ""
    if active:
        try:
            import datetime
            raw = subprocess.check_output(
                ["systemctl", "show", MTP_SERVICE,
                 "--property=ActiveEnterTimestamp", "--value"],
                text=True
            ).strip()
            parts = raw.split()
            if len(parts) >= 3:
                started = datetime.datetime.strptime(
                    f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S"
                )
                delta = datetime.datetime.utcnow() - started
                h, r = divmod(int(delta.total_seconds()), 3600)
                uptime_str = f"{h}ч {r // 60}м"
        except Exception:
            pass
    return {"running": active, "uptime": uptime_str}


def _status_line(running: bool, uptime: str) -> str:
    icon = "🟢 Работает" if running else "🔴 Остановлен"
    return f"{icon}{f' ({uptime})' if uptime else ''}"


# ── Статистика ────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _ensure_counters(port: str) -> None:
    """Добавляет iptables правила-счётчики для порта MTProxy если их нет."""
    rules = [
        ("INPUT",  f"--dport {port}", _IPT_CMT_IN),
        ("OUTPUT", f"--sport {port}", _IPT_CMT_OUT),
    ]
    for chain, flag, comment in rules:
        r = subprocess.run(
            ["iptables", "-C", chain, "-p", "tcp"] + flag.split() +
            ["-m", "comment", "--comment", comment],
            capture_output=True
        )
        if r.returncode != 0:
            subprocess.run(
                ["iptables", "-I", chain, "1", "-p", "tcp"] + flag.split() +
                ["-m", "comment", "--comment", comment],
                capture_output=True
            )


def _read_ipt_bytes(chain: str, comment: str) -> int:
    try:
        out = subprocess.check_output(
            ["iptables", "-nvxL", chain], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if comment in line:
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1])
    except Exception:
        pass
    return 0


def _get_current_conns(port: str) -> int:
    try:
        out = subprocess.check_output(["ss", "-tn"], text=True, stderr=subprocess.DEVNULL)
        return sum(1 for line in out.splitlines() if f":{port}" in line)
    except Exception:
        return 0


def _load_mtp_stats() -> dict:
    try:
        with open(MTP_STATS_F) as f:
            return json.load(f)
    except Exception:
        return {
            "snapshots": [],
            "peak_conns": 0,
            "acc_in": 0,
            "acc_out": 0,
            "last_raw_in": 0,
            "last_raw_out": 0,
        }


def _save_mtp_stats(data: dict) -> None:
    os.makedirs(os.path.dirname(MTP_STATS_F), exist_ok=True)
    with open(MTP_STATS_F, "w") as f:
        json.dump(data, f)
    os.chmod(MTP_STATS_F, 0o600)


def _record_snapshot(port: str) -> dict:
    """Снимает snapshot трафика, возвращает текущую сводку."""
    _ensure_counters(port)
    data = _load_mtp_stats()

    raw_in  = _read_ipt_bytes("INPUT",  _IPT_CMT_IN)
    raw_out = _read_ipt_bytes("OUTPUT", _IPT_CMT_OUT)
    conns   = _get_current_conns(port)
    now     = int(time.time())

    # Если счётчики были сброшены (перезагрузка / пересоздание правил)
    if raw_in < data.get("last_raw_in", 0):
        data["acc_in"] = data.get("acc_in", 0) + data.get("last_raw_in", 0)
    if raw_out < data.get("last_raw_out", 0):
        data["acc_out"] = data.get("acc_out", 0) + data.get("last_raw_out", 0)

    data["last_raw_in"]  = raw_in
    data["last_raw_out"] = raw_out

    total_in  = data.get("acc_in", 0) + raw_in
    total_out = data.get("acc_out", 0) + raw_out

    if conns > data.get("peak_conns", 0):
        data["peak_conns"] = conns

    data.setdefault("snapshots", []).append(
        {"t": now, "ti": total_in, "to": total_out, "c": conns}
    )
    # Храним максимум 72 часа
    cutoff = now - 72 * 3600
    data["snapshots"] = [s for s in data["snapshots"] if s["t"] > cutoff]

    _save_mtp_stats(data)
    return {
        "conns":     conns,
        "peak":      data["peak_conns"],
        "total_in":  total_in,
        "total_out": total_out,
        "snapshots": data["snapshots"],
    }


def _period_traffic(snapshots: list, seconds: int) -> tuple[int, int]:
    """Трафик (in, out) за последние seconds секунд."""
    if len(snapshots) < 2:
        return 0, 0
    cutoff  = int(time.time()) - seconds
    recent  = [s for s in snapshots if s["t"] >= cutoff]
    if not recent:
        return 0, 0
    oldest = recent[0]
    newest = snapshots[-1]
    return (
        max(0, newest["ti"] - oldest["ti"]),
        max(0, newest["to"] - oldest["to"]),
    )


def _peak_conns_period(snapshots: list, seconds: int) -> int:
    """Пиковое количество подключений за период."""
    cutoff = int(time.time()) - seconds
    vals = [s["c"] for s in snapshots if s["t"] >= cutoff]
    return max(vals) if vals else 0


def _mtp_update_tg_configs() -> tuple[bool, str]:
    try:
        r1 = subprocess.run(
            ["curl", "-sf", "--max-time", "15",
             "https://core.telegram.org/getProxySecret", "-o", MTP_SECRET_F],
            capture_output=True, timeout=20
        )
        r2 = subprocess.run(
            ["curl", "-sf", "--max-time", "15",
             "https://core.telegram.org/getProxyConfig", "-o", MTP_MULTI_F],
            capture_output=True, timeout=20
        )
        if r1.returncode == 0 and r2.returncode == 0:
            return True, "✅ Конфиги Telegram обновлены"
        return False, "❌ Не удалось скачать конфиги"
    except Exception as e:
        return False, f"❌ {e}"


def _write_mtp_service(port: str, secret: str):
    stored = secret
    if stored.startswith("ee"):
        clean = stored[2:34]
        faketls_flag = "-D www.google.com"
    elif stored.startswith("dd"):
        clean = stored[2:]
        faketls_flag = ""
    else:
        clean = stored
        faketls_flag = ""
    extra = f" {faketls_flag}" if faketls_flag else ""
    with open(f"/etc/systemd/system/{MTP_SERVICE}.service", "w") as f:
        f.write(
            f"[Unit]\nDescription=MTProxy for Telegram\n"
            f"After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\n"
            f"ExecStartPre=/bin/sh -c 'curl -sf https://core.telegram.org/getProxySecret"
            f" -o {MTP_SECRET_F}; curl -sf https://core.telegram.org/getProxyConfig"
            f" -o {MTP_MULTI_F}'\n"
            f"ExecStart={MTP_BIN} -u nobody -p 8888 -H {port} -S {clean}"
            f"{extra} --aes-pwd {MTP_SECRET_F} {MTP_MULTI_F} -M 1\n"
            f"Restart=on-failure\nRestartSec=10\n"
            f"StandardOutput=journal\nStandardError=journal\n\n"
            f"[Install]\nWantedBy=multi-user.target\n"
        )
    subprocess.run(["systemctl", "daemon-reload"])


def _mtp_apply_secret(new_secret: str) -> tuple[bool, str]:
    _save_conf({"MTP_SECRET": new_secret})
    _write_mtp_service(_mtp_get_port(), new_secret)
    return _svc_action("restart", "🔄 MTProxy перезапущен с новым секретом")


# ── Мультисерверные хелперы ────────────────────────────────────────────────────

def _get_slaves() -> list:
    try:
        from awg_core import load_servers
        return [s for s in load_servers() if not s.get("is_primary")]
    except Exception:
        return []


def _server_best_endpoint(server: dict) -> str:
    """Возвращает лучший эндпоинт сервера: верифицированный домен > домен > IP."""
    for ep in server.get("endpoints", []):
        if ep.get("type") == "domain" and ep.get("verified"):
            return ep["value"]
    for ep in server.get("endpoints", []):
        if ep.get("type") == "domain":
            return ep["value"]
    for ep in server.get("endpoints", []):
        if ep.get("value"):
            return ep["value"]
    return server.get("ssh", {}).get("ip", "")


def _build_all_server_links(secret: str, port: str) -> list[dict]:
    """Строит список ссылок для всех серверов (primary + slaves).
    Возвращает [{name, emoji, is_primary, address, link}]."""
    if not secret:
        return []
    result = []
    try:
        from awg_core import load_servers
        servers = load_servers()
    except Exception:
        return []

    for srv in servers:
        addr = _server_best_endpoint(srv)
        if not addr:
            continue
        result.append({
            "name":       srv.get("name", "Сервер"),
            "emoji":      srv.get("emoji", "🖥"),
            "is_primary": srv.get("is_primary", False),
            "address":    addr,
            "link":       _mtp_build_link(secret, port, addr),
        })
    return result


async def _sync_secret_to_slaves(secret: str, port: str) -> list[str]:
    """SSH-синхронизирует секрет MTProxy на все slave. Возвращает список ошибок."""
    errors: list[str] = []
    loop   = asyncio.get_event_loop()
    slaves = _get_slaves()
    if not slaves:
        return errors

    try:
        from awg_core import ssh_sync_mtproxy_secret
    except ImportError:
        return ["ssh_sync_mtproxy_secret недоступен"]

    for slave in slaves:
        sname = f"{slave.get('emoji', '')} {slave.get('name', 'Slave')}".strip()
        try:
            ok, msg = await loop.run_in_executor(
                None, ssh_sync_mtproxy_secret, slave, secret, port
            )
            if not ok:
                errors.append(f"{sname}: {msg}")
        except Exception as e:
            errors.append(f"{sname}: {e}")
    return errors


# ── UI — пользовательский (readonly) ──────────────────────────────────────────

async def _show_mtp_user(query):
    if not _mtp_installed():
        await query.edit_message_text(
            "📡 *Прокси Telegram*\n\n"
            "Прокси сейчас недоступен.\n"
            "Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ В меню", callback_data="back")]
            ]),
            parse_mode="Markdown"
        )
        return

    secret = _mtp_get_secret()
    port   = _mtp_get_port()

    if not secret:
        await query.edit_message_text(
            "📡 *Прокси Telegram*\n\n"
            "Прокси ещё не настроен.\n"
            "Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ В меню", callback_data="back")]
            ]),
            parse_mode="Markdown"
        )
        return

    st = _svc_status()
    status_text = _status_line(st["running"], st["uptime"])

    servers = _build_all_server_links(secret, port)

    if servers:
        links_text = ""
        for s in servers:
            tag = " _(основной)_" if s["is_primary"] else ""
            links_text += f"\n{s['emoji']} *{s['name']}*{tag}\n`{s['link']}`\n"
    else:
        ip = _get_proxy_address()
        links_text = f"\n`{_mtp_build_link(secret, port, ip)}`\n"

    text = (
        f"📡 *Прокси Telegram*\n\n"
        f"Статус: {status_text}\n"
        f"Нажмите на ссылку нужного сервера или скопируйте вручную:\n"
        f"{links_text}\n"
        f"_Telegram → Настройки → Данные → Тип соединения_"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="proxy_mtp_menu")],
            [InlineKeyboardButton("◀️ В меню",   callback_data="back")],
        ]),
        parse_mode="Markdown"
    )


# ── UI — административный (полное управление) ─────────────────────────────────

async def _show_mtp_admin(query):
    if not _mtp_installed():
        await query.edit_message_text(
            "⚫ MTProxy не установлен.\n\n"
            "Запустите на сервере:\n"
            "`bash <(curl -s https://raw.githubusercontent.com/"
            "yntoolsmail-prog/Proxy-Telegram-Whatsapp/main/setup_proxy.sh)`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ В меню", callback_data="back")]
            ]),
            parse_mode="Markdown"
        )
        return

    st     = _svc_status()
    secret = _mtp_get_secret()
    port   = _mtp_get_port()

    if secret.startswith("ee"):
        mode = "EE (TLS 1.3)"
    elif secret.startswith("dd"):
        mode = "fake-TLS (DD)"
    else:
        mode = "plain"

    servers = _build_all_server_links(secret, port)

    if servers:
        links_text = ""
        for s in servers:
            tag = " _(primary)_" if s["is_primary"] else ""
            links_text += f"\n{s['emoji']} *{s['name']}*{tag}\n`{s['link']}`\n"
    else:
        ip = _get_proxy_address()
        links_text = f"\n`{_mtp_build_link(secret, port, ip) if secret and ip != '—' else '—'}`\n"

    slaves   = _get_slaves()
    sync_row = []
    if slaves:
        sync_row = [[InlineKeyboardButton(
            f"🔄 Синхронизировать секрет → {len(slaves)} slave",
            callback_data="proxy_mtp_sync_slaves"
        )]]

    toggle_btn = (
        InlineKeyboardButton("⏹ Остановить", callback_data="proxy_mtp_stop")
        if st["running"] else
        InlineKeyboardButton("▶️ Запустить",  callback_data="proxy_mtp_start")
    )
    # Быстрая сводка подключений для заголовка
    conns = _get_current_conns(port)
    conns_str = f"  |  👥 {conns} клиент{'ов' if conns not in (1, 21) else ''}" if conns else ""

    await query.edit_message_text(
        f"📡 *MTProxy*\n\n"
        f"Статус: {_status_line(st['running'], st['uptime'])}{conns_str}\n"
        f"Порт: `{port}`  |  🔑 {mode}\n\n"
        f"*Ссылки по серверам:*{links_text}\n"
        f"_Telegram → Настройки → Данные → Тип соединения_",
        reply_markup=InlineKeyboardMarkup([
            [toggle_btn,
             InlineKeyboardButton("🔄 Рестарт",             callback_data="proxy_mtp_restart")],
            [InlineKeyboardButton("🔑 Сменить секрет",      callback_data="proxy_mtp_secret_ask")],
            *sync_row,
            [InlineKeyboardButton("📊 Статистика",          callback_data="proxy_mtp_stats")],
            [InlineKeyboardButton("📥 Обновить конфиги TG", callback_data="proxy_mtp_update_cfg")],
            [InlineKeyboardButton("📖 Инструкция",          callback_data="proxy_mtp_help")],
            [InlineKeyboardButton("🔄 Обновить",            callback_data="proxy_mtp_menu")],
            [InlineKeyboardButton("◀️ В меню",              callback_data="back")],
        ]),
        parse_mode="Markdown"
    )


async def _show_mtp_secret_ask(query):
    await query.edit_message_text(
        "🔑 *Смена секрета MTProxy*\n\n"
        "⚠️ *Внимание!* После смены секрета:\n"
        "• Все подключённые пользователи отвалятся\n"
        "• Старая ссылка перестанет работать\n"
        "• Нужно разослать новую ссылку всем\n"
        "• Секрет будет автоматически синхронизирован на slave\n\n"
        "Выберите тип нового секрета:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 EE — TLS 1.3 (рекомендуется)",
                                  callback_data="proxy_mtp_secret_ee")],
            [InlineKeyboardButton("🔑 fake-TLS (DD)",
                                  callback_data="proxy_mtp_secret_dd")],
            [InlineKeyboardButton("🔓 plain",
                                  callback_data="proxy_mtp_secret_plain")],
            [InlineKeyboardButton("❌ Отмена",
                                  callback_data="proxy_mtp_menu")],
        ]),
        parse_mode="Markdown"
    )


async def _show_mtp_secret_confirm(query, mode: str):
    labels = {"ee": "EE (TLS 1.3)", "dd": "fake-TLS (DD)", "plain": "plain"}
    label  = labels.get(mode, mode)
    await query.edit_message_text(
        f"🔑 *Подтверждение смены секрета*\n\n"
        f"Тип: *{label}*\n\n"
        f"❗ Все пользователи будут отключены.\n"
        f"Вы уверены?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, сменить",
                                  callback_data=f"proxy_mtp_secret_confirm_{mode}")],
            [InlineKeyboardButton("❌ Отмена",
                                  callback_data="proxy_mtp_menu")],
        ]),
        parse_mode="Markdown"
    )


async def _show_mtp_help(query):
    await query.edit_message_text(
        "📖 *MTProxy — инструкция*\n\n"
        "*▶️ Старт / ⏹ Стоп*\n"
        "Запускает или останавливает прокси.\n\n"
        "*🔄 Рестарт*\n"
        "Перезапуск без смены настроек.\n\n"
        "*🔑 Сменить секрет*\n"
        "Генерирует новый секрет и перезапускает прокси на primary и всех slave. "
        "Все подключённые пользователи отвалятся — нужно разослать новую ссылку.\n\n"
        "*🔄 Синхронизировать секрет → N slave*\n"
        "Принудительная синхронизация текущего секрета на slave без его смены.\n\n"
        "*📥 Обновить конфиги TG*\n"
        "Скачивает свежие конфиги с серверов Telegram.\n\n"
        "*Как подключиться:*\n"
        "• Android/iOS: Настройки → Данные → Тип соединения → Прокси\n"
        "• ПК: Настройки → Продвинутые настройки → Тип соединения\n"
        "Или просто нажмите на ссылку из главного меню.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="proxy_mtp_menu")]
        ]),
        parse_mode="Markdown"
    )


async def _show_mtp_stats(query):
    port = _mtp_get_port()
    await query.edit_message_text("⏳ Считаю трафик...")

    try:
        snap  = _record_snapshot(port)
    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка сбора статистики: {e}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="proxy_mtp_menu")]
            ])
        )
        return

    conns = snap["conns"]
    peak  = snap["peak"]
    snaps = snap["snapshots"]

    in_1h,  out_1h  = _period_traffic(snaps, 3600)
    in_24h, out_24h = _period_traffic(snaps, 86400)
    in_72h, out_72h = _period_traffic(snaps, 72 * 3600)
    in_tot  = snap["total_in"]
    out_tot = snap["total_out"]

    peak_1h  = _peak_conns_period(snaps, 3600)
    peak_24h = _peak_conns_period(snaps, 86400)

    if conns and (in_1h + out_1h) > 0:
        avg_str = f"`{_fmt_bytes((in_1h + out_1h) // conns)}/клиент` за 1ч"
    else:
        avg_str = "_нет данных за 1ч_"

    max_spike = 0
    for i in range(1, len(snaps)):
        delta = (snaps[i]["ti"] + snaps[i]["to"]) - (snaps[i-1]["ti"] + snaps[i-1]["to"])
        if delta > max_spike:
            max_spike = delta
    spike_str = f"`{_fmt_bytes(max_spike)}`" if max_spike > 0 else "_нет данных_"

    text = (
        "📊 *Статистика MTProxy*\n\n"
        f"👥 *Подключений сейчас:* `{conns}`\n"
        f"📈 *Пик (1ч / 24ч / всё время):* `{peak_1h}` / `{peak_24h}` / `{peak}`\n\n"
        "*Трафик (вх / исх):*\n"
        f"• 1 час:   `{_fmt_bytes(in_1h)}` / `{_fmt_bytes(out_1h)}`\n"
        f"• 24 ч:    `{_fmt_bytes(in_24h)}` / `{_fmt_bytes(out_24h)}`\n"
        f"• 72 ч:    `{_fmt_bytes(in_72h)}` / `{_fmt_bytes(out_72h)}`\n"
        f"• Всего:   `{_fmt_bytes(in_tot)}` / `{_fmt_bytes(out_tot)}`\n\n"
        f"⚡ *Среднее:* {avg_str}\n"
        f"🌊 *Макс. всплеск:* {spike_str}\n\n"
        "_Трафик считается с первого открытия статистики_"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="proxy_mtp_stats")],
            [InlineKeyboardButton("◀️ Назад",    callback_data="proxy_mtp_menu")],
        ]),
        parse_mode="Markdown"
    )


# ── Главный callback роутер ────────────────────────────────────────────────────

async def _mtp_callback(update, context):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data
    await query.answer()

    try:
        from awg_core import ADMIN_ID
        is_admin = (user_id == ADMIN_ID)
    except Exception:
        is_admin = False

    if data == "proxy_mtp_menu":
        if is_admin:
            await _show_mtp_admin(query)
        else:
            await _show_mtp_user(query)
        return

    if not is_admin:
        await query.answer("⛔ Только для администратора", show_alert=True)
        return

    if data == "proxy_mtp_start":
        ok, msg = _svc_action("start", "▶️ MTProxy запущен")
        await query.answer(msg, show_alert=not ok)
        await _show_mtp_admin(query)

    elif data == "proxy_mtp_stop":
        ok, msg = _svc_action("stop", "⏹ MTProxy остановлен")
        await query.answer(msg, show_alert=not ok)
        await _show_mtp_admin(query)

    elif data == "proxy_mtp_restart":
        ok, msg = _svc_action("restart", "🔄 MTProxy перезапущен")
        await query.answer(msg, show_alert=not ok)
        await _show_mtp_admin(query)

    elif data == "proxy_mtp_secret_ask":
        await _show_mtp_secret_ask(query)

    elif data in ("proxy_mtp_secret_ee", "proxy_mtp_secret_dd", "proxy_mtp_secret_plain"):
        mode_map = {
            "proxy_mtp_secret_ee":    "ee",
            "proxy_mtp_secret_dd":    "dd",
            "proxy_mtp_secret_plain": "plain",
        }
        await _show_mtp_secret_confirm(query, mode_map[data])

    elif data.startswith("proxy_mtp_secret_confirm_"):
        mode  = data.replace("proxy_mtp_secret_confirm_", "")
        new_s = _mtp_generate_secret(mode)
        await query.edit_message_text("⏳ Применяю новый секрет...")
        ok, msg = _mtp_apply_secret(new_s)
        await query.answer(msg, show_alert=True)

        # Синхронизируем на slave
        port   = _mtp_get_port()
        errors = await _sync_secret_to_slaves(new_s, port)
        if errors:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="⚠️ Ошибки синхронизации MTProxy на slave:\n" +
                     "\n".join(f"• {e}" for e in errors)
            )
        await _show_mtp_admin(query)

    elif data == "proxy_mtp_sync_slaves":
        secret = _mtp_get_secret()
        port   = _mtp_get_port()
        if not secret:
            await query.answer("❌ Секрет не задан", show_alert=True)
            return
        await query.edit_message_text("⏳ Синхронизирую секрет на slave-серверы...")
        errors = await _sync_secret_to_slaves(secret, port)
        if errors:
            result = "⚠️ Ошибки:\n" + "\n".join(f"• {e}" for e in errors)
        else:
            slaves = _get_slaves()
            result = f"✅ Синхронизировано на {len(slaves)} slave"
        await query.answer(result, show_alert=True)
        await _show_mtp_admin(query)

    elif data == "proxy_mtp_stats":
        await _show_mtp_stats(query)

    elif data == "proxy_mtp_help":
        await _show_mtp_help(query)

    elif data == "proxy_mtp_update_cfg":
        await query.edit_message_text("⏳ Загружаю конфиги Telegram...")
        ok, msg = _mtp_update_tg_configs()
        if ok:
            _svc_action("restart", "")
        await query.answer(msg, show_alert=True)
        await _show_mtp_admin(query)


# ── Интерфейс модуля ──────────────────────────────────────────────────────────

def get_user_menu_buttons(user_id: int) -> list:
    return [[InlineKeyboardButton("📡 Прокси Telegram", callback_data="proxy_mtp_menu")]]


def get_admin_menu_buttons() -> list:
    return []


def register_handlers(app) -> None:
    app.add_handler(
        CallbackQueryHandler(_mtp_callback, pattern=r"^proxy_mtp"),
        group=-1
    )
