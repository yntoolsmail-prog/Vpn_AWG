#!/usr/bin/env python3
"""modules/mtproxy/__init__.py — модуль управления MTProxy для Telegram.

Кнопка "📡 Прокси Telegram" видна ВСЕМ одобренным пользователям.
- Обычный пользователь: только ссылка (readonly)
- Администратор: полное управление (старт/стоп/рестарт/смена секрета)

Требует установленного MTProxy (/opt/mtproxy/objs/bin/mtproto-proxy).
Если не установлен — показывает инструкцию по установке.
"""

from __future__ import annotations

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
    """Читает IP сервера для диагностики."""
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
    """Адрес сервера для ссылки — может быть доменом если задан при установке."""
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


# ── UI — пользовательский (readonly) ──────────────────────────────────────────

async def _show_mtp_user(query):
    """Показывает ссылку для подключения (только для обычных пользователей)."""
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
    ip     = _get_proxy_address()
    if secret and ip != "—":
        link = _mtp_build_link(secret, port, ip)
        st   = _svc_status()
        status_text = _status_line(st["running"], st["uptime"])
        text = (
            f"📡 *Прокси Telegram*\n\n"
            f"Статус: {status_text}\n\n"
            f"🔗 Ссылка для подключения:\n`{link}`\n\n"
            f"_Нажмите на ссылку или скопируйте вручную_\n"
            f"_Telegram → Настройки → Данные → Тип соединения_"
        )
    else:
        text = (
            "📡 *Прокси Telegram*\n\n"
            "Прокси ещё не настроен.\n"
            "Обратитесь к администратору."
        )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить",  callback_data="proxy_mtp_menu")],
            [InlineKeyboardButton("◀️ В меню",    callback_data="back")],
        ]),
        parse_mode="Markdown"
    )


# ── UI — административный (полное управление) ─────────────────────────────────

async def _show_mtp_admin(query):
    """Показывает полное меню управления MTProxy (только для администратора)."""
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
    ip     = _get_proxy_address()
    link   = _mtp_build_link(secret, port, ip) if secret and ip != "—" else "—"
    if secret.startswith("ee"):
        mode = "EE (TLS 1.3)"
    elif secret.startswith("dd"):
        mode = "fake-TLS (DD)"
    else:
        mode = "plain"
    toggle_btn = (
        InlineKeyboardButton("⏹ Остановить", callback_data="proxy_mtp_stop")
        if st["running"] else
        InlineKeyboardButton("▶️ Запустить",  callback_data="proxy_mtp_start")
    )
    await query.edit_message_text(
        f"📡 MTProxy\n\n"
        f"Статус: {_status_line(st['running'], st['uptime'])}\n"
        f"🌐 {ip}:{port}  |  🔑 {mode}\n\n"
        f"🔗 Ссылка:\n`{link}`\n\n"
        f"_Telegram → Настройки → Данные → Тип соединения_",
        reply_markup=InlineKeyboardMarkup([
            [toggle_btn,
             InlineKeyboardButton("🔄 Рестарт",             callback_data="proxy_mtp_restart")],
            [InlineKeyboardButton("🔑 Сменить секрет",      callback_data="proxy_mtp_secret_ask")],
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
        "• Нужно разослать новую ссылку всем\n\n"
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
        "Генерирует новый секрет и перезапускает прокси. "
        "Все подключённые пользователи отвалятся — нужно разослать новую ссылку.\n\n"
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

    # Любой одобренный пользователь может открыть меню просмотра
    if data == "proxy_mtp_menu":
        if is_admin:
            await _show_mtp_admin(query)
        else:
            await _show_mtp_user(query)
        return

    # Все остальные действия — только для администратора
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
    """Кнопка видна ВСЕМ одобренным пользователям (и администратору тоже)."""
    return [[InlineKeyboardButton("📡 Прокси Telegram", callback_data="proxy_mtp_menu")]]


def get_admin_menu_buttons() -> list:
    """MTProxy кнопка через get_user_menu_buttons — здесь ничего не добавляем."""
    return []


def register_handlers(app) -> None:
    """Регистрируем в group=-1, чтобы срабатывать раньше главного button_handler."""
    app.add_handler(
        CallbackQueryHandler(_mtp_callback, pattern=r"^proxy_mtp"),
        group=-1
    )
