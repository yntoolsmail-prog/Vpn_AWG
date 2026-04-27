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
import logging
import os
import secrets
import subprocess

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
    await query.edit_message_text(
        f"📡 *MTProxy*\n\n"
        f"Статус: {_status_line(st['running'], st['uptime'])}\n"
        f"Порт: `{port}`  |  🔑 {mode}\n\n"
        f"*Ссылки по серверам:*{links_text}\n"
        f"_Telegram → Настройки → Данные → Тип соединения_",
        reply_markup=InlineKeyboardMarkup([
            [toggle_btn,
             InlineKeyboardButton("🔄 Рестарт",             callback_data="proxy_mtp_restart")],
            [InlineKeyboardButton("🔑 Сменить секрет",      callback_data="proxy_mtp_secret_ask")],
            *sync_row,
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
