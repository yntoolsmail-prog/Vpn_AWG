#!/usr/bin/env python3
"""modules/socks5/__init__.py — модуль управления SOCKS5-прокси для AWG клиентов.

Кнопка "🧦 SOCKS5 прокси" видна ТОЛЬКО администратору.
Позволяет направить трафик выбранного VPN-клиента через внешний SOCKS5 прокси
с помощью redsocks2 + iptables.

Состояние хранится в /etc/awg-socks5/bot_state.json.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────
STATE_FILE       = "/etc/awg-socks5/bot_state.json"
REDSOCKS2_BIN    = "/usr/local/bin/redsocks2"
REDSOCKS2_CONF   = "/etc/redsocks2/redsocks2.conf"
REDSOCKS2_PORT   = 12345
DNS_LOCAL_PORT   = 5300

# ConversationHandler states (не пересекаются с bot.py: там 10-16)
_S_HOST = 50
_S_PORT = 51
_S_USER = 52
_S_PASS = 53


# ── Хранилище состояния ───────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(data: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(STATE_FILE, 0o600)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _is_redsocks_installed() -> bool:
    return os.path.exists(REDSOCKS2_BIN)


def _is_redsocks_running() -> bool:
    try:
        return subprocess.check_output(
            ["systemctl", "is-active", "redsocks2"],
            text=True, stderr=subprocess.DEVNULL
        ).strip() == "active"
    except Exception:
        return False


def _resolve_host(host: str) -> str:
    """Резолвит хост в IP-адрес."""
    try:
        return socket.gethostbyname(host)
    except Exception:
        return host


def _get_client_ip(name: str) -> str | None:
    """Возвращает IP-адрес VPN-клиента из его конфига."""
    try:
        from awg_core import get_client_keys
        data = get_client_keys(name)
        if data:
            return data.get("ip")
    except Exception:
        pass
    return None


def _write_redsocks_conf(host: str, port: int, user: str, pass_: str):
    os.makedirs(os.path.dirname(REDSOCKS2_CONF), exist_ok=True)
    auth_lines = ""
    if user:
        auth_lines = f"    login = \"{user}\";\n    password = \"{pass_}\";\n"
    conf = (
        "base {\n"
        "    log_debug = off;\n"
        "    log_info = on;\n"
        "    log = \"syslog:daemon\";\n"
        "    daemon = on;\n"
        "    redirector = iptables;\n"
        "}\n\n"
        "redsocks {\n"
        f"    local_ip = 0.0.0.0;\n"
        f"    local_port = {REDSOCKS2_PORT};\n"
        f"    ip = {host};\n"
        f"    port = {port};\n"
        "    type = socks5;\n"
        f"{auth_lines}"
        "}\n\n"
        "dnstc {\n"
        "    local_ip = 127.0.0.1;\n"
        f"    local_port = {DNS_LOCAL_PORT};\n"
        "}\n"
    )
    with open(REDSOCKS2_CONF, "w") as f:
        f.write(conf)
    os.chmod(REDSOCKS2_CONF, 0o600)


def _remove_iptables_for_client(client_ip: str):
    """Удаляет iptables правила для клиента."""
    if not client_ip:
        return
    chain = f"SOCKS5_{client_ip.replace('.', '_')}"
    cmds = [
        ["iptables", "-t", "nat", "-D", "PREROUTING",
         "-s", f"{client_ip}/32", "-p", "udp", "--dport", "53",
         "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"],
        ["iptables", "-t", "nat", "-D", "PREROUTING",
         "-s", f"{client_ip}/32", "-p", "tcp", "--dport", "53",
         "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"],
        ["iptables", "-t", "nat", "-D", "PREROUTING",
         "-s", f"{client_ip}/32", "-j", chain],
        ["iptables", "-t", "nat", "-F", chain],
        ["iptables", "-t", "nat", "-X", chain],
        ["iptables", "-t", "filter", "-D", "FORWARD",
         "-s", f"{client_ip}/32", "-p", "udp", "!", "--dport", "53", "-j", "REJECT"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, capture_output=True)


def _apply_iptables_for_client(client_ip: str, socks5_host_ip: str) -> tuple[bool, str]:
    """Настраивает iptables для маршрутизации трафика клиента через SOCKS5."""
    try:
        chain = f"SOCKS5_{client_ip.replace('.', '_')}"

        # DNS UDP → dnscrypt-proxy
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-p", "udp", "--dport", "53",
            "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"
        ], check=True, capture_output=True)

        # DNS TCP → dnscrypt-proxy
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-p", "tcp", "--dport", "53",
            "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"
        ], check=True, capture_output=True)

        # Создаём цепочку для TCP
        subprocess.run([
            "iptables", "-t", "nat", "-N", chain
        ], check=True, capture_output=True)

        # Исключение: IP прокси (не перенаправлять)
        subprocess.run([
            "iptables", "-t", "nat", "-A", chain,
            "-d", socks5_host_ip, "-j", "RETURN"
        ], check=True, capture_output=True)

        # Исключение: приватные сети
        private_nets = [
            "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8",
            "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
            "224.0.0.0/4", "240.0.0.0/4",
        ]
        for net in private_nets:
            subprocess.run([
                "iptables", "-t", "nat", "-A", chain,
                "-d", net, "-j", "RETURN"
            ], check=True, capture_output=True)

        # Весь TCP → redsocks2
        subprocess.run([
            "iptables", "-t", "nat", "-A", chain,
            "-p", "tcp", "-j", "REDIRECT", "--to-ports", str(REDSOCKS2_PORT)
        ], check=True, capture_output=True)

        # Применить цепочку к трафику клиента
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-j", chain
        ], check=True, capture_output=True)

        # Блокировать UDP кроме DNS
        subprocess.run([
            "iptables", "-t", "filter", "-A", "FORWARD",
            "-s", f"{client_ip}/32", "-p", "udp", "!", "--dport", "53",
            "-j", "REJECT"
        ], check=True, capture_output=True)

        # sysctl
        subprocess.run(["sysctl", "-w", "net.ipv4.conf.all.route_localnet=1"],
                       capture_output=True)
        subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"],
                       capture_output=True)

        # Сохранить правила
        os.makedirs("/etc/iptables", exist_ok=True)
        subprocess.run(
            ["bash", "-c", "iptables-save > /etc/iptables/rules.v4"],
            capture_output=True
        )
        return True, "✅ Правила iptables применены"
    except subprocess.CalledProcessError as e:
        return False, f"❌ iptables: {e.stderr.decode().strip() if e.stderr else str(e)}"
    except Exception as e:
        return False, f"❌ {e}"


def _redsocks_restart() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "restart", "redsocks2"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            return True, "✅ redsocks2 перезапущен"
        return False, f"❌ {r.stderr.strip()}"
    except Exception as e:
        return False, f"❌ {e}"


# ── UI — главное меню SOCKS5 ──────────────────────────────────────────────────

async def _show_socks5_menu(query):
    state = _load_state()

    if not _is_redsocks_installed():
        await query.edit_message_text(
            "🧦 *SOCKS5 прокси*\n\n"
            "❌ redsocks2 не установлен.\n\n"
            "Для установки запустите на сервере:\n"
            "`bash <(curl -s https://raw.githubusercontent.com/"
            "yntoolsmail-prog/Vpn_AWG/main/New/awg-socks5-installer-v3.sh)`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ В меню", callback_data="back")]
            ]),
            parse_mode="Markdown"
        )
        return

    svc_status = "🟢 Работает" if _is_redsocks_running() else "🔴 Остановлен"
    host = state.get("socks5_host", "—")
    port = state.get("socks5_port", "—")
    active_client = state.get("active_client", "")

    try:
        from awg_core import get_all_clients
        all_clients = get_all_clients()
    except Exception:
        all_clients = []

    header = (
        f"🧦 *SOCKS5 прокси*\n\n"
        f"redsocks2: {svc_status}\n"
        f"Прокси: `{host}:{port}`\n"
    )
    if active_client:
        header += f"Активен для: *{active_client}*\n"
    header += "\nВыберите клиента:"

    kb = []
    for name in all_clients:
        mark = " ✅" if name == active_client else ""
        kb.append([InlineKeyboardButton(
            f"👤 {name}{mark}", callback_data=f"socks5_client_{name}"
        )])
    kb.append([InlineKeyboardButton("⚙️ Настроить SOCKS5",  callback_data="socks5_setup")])
    if active_client:
        kb.append([InlineKeyboardButton("❌ Отключить SOCKS5",  callback_data="socks5_disable")])
    kb.append([InlineKeyboardButton("◀️ В меню",             callback_data="back")])

    await query.edit_message_text(
        header,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )


async def _show_client_card(query, name: str):
    state         = _load_state()
    active_client = state.get("active_client", "")
    client_ip     = _get_client_ip(name) or "неизвестен"

    if name == active_client:
        status_text = "✅ SOCKS5 активен"
        action_btn  = InlineKeyboardButton(
            "❌ Отключить SOCKS5", callback_data="socks5_disable"
        )
    else:
        status_text = "○ SOCKS5 не активен"
        action_btn  = InlineKeyboardButton(
            "✅ Включить SOCKS5", callback_data=f"socks5_enable_{name}"
        )

    await query.edit_message_text(
        f"👤 *{name}*\n"
        f"IP: `{client_ip}`\n"
        f"SOCKS5: {status_text}",
        reply_markup=InlineKeyboardMarkup([
            [action_btn],
            [InlineKeyboardButton("◀️ К списку", callback_data="socks5_list")],
        ]),
        parse_mode="Markdown"
    )


# ── Включение / отключение SOCKS5 ────────────────────────────────────────────

async def _enable_socks5(query, name: str):
    state = _load_state()
    host  = state.get("socks5_host")
    port  = state.get("socks5_port")

    if not host or not port:
        await query.answer(
            "⚠️ SOCKS5 не настроен. Нажмите ⚙️ Настроить SOCKS5.",
            show_alert=True
        )
        await _show_socks5_menu(query)
        return

    client_ip = _get_client_ip(name)
    if not client_ip:
        await query.answer(f"❌ Не удалось найти IP клиента {name}", show_alert=True)
        await _show_socks5_menu(query)
        return

    # Снимаем правила для предыдущего клиента
    prev_ip = state.get("active_client_ip")
    if prev_ip and prev_ip != client_ip:
        _remove_iptables_for_client(prev_ip)

    socks5_host_ip = _resolve_host(host)

    # Пишем конфиг redsocks2
    _write_redsocks_conf(
        host, int(port),
        state.get("socks5_user", ""),
        state.get("socks5_pass", "")
    )

    # Применяем iptables
    ok, msg = _apply_iptables_for_client(client_ip, socks5_host_ip)
    if not ok:
        await query.answer(msg, show_alert=True)
        await _show_socks5_menu(query)
        return

    # Перезапускаем redsocks2
    ok2, msg2 = _redsocks_restart()

    # Сохраняем состояние
    state["active_client"]    = name
    state["active_client_ip"] = client_ip
    _save_state(state)

    await query.answer(f"✅ SOCKS5 активирован для {name}" + (f"\n{msg2}" if not ok2 else ""),
                       show_alert=True)
    await _show_socks5_menu(query)


async def _disable_socks5(query):
    state = _load_state()
    prev_ip   = state.get("active_client_ip")
    prev_name = state.get("active_client", "")

    if prev_ip:
        _remove_iptables_for_client(prev_ip)
        # Сохраняем iptables
        subprocess.run(
            ["bash", "-c", "iptables-save > /etc/iptables/rules.v4"],
            capture_output=True
        )

    state.pop("active_client",    None)
    state.pop("active_client_ip", None)
    _save_state(state)

    await query.answer(
        f"✅ SOCKS5 отключён{f' для {prev_name}' if prev_name else ''}",
        show_alert=True
    )
    await _show_socks5_menu(query)


# ── ConversationHandler для настройки SOCKS5 ─────────────────────────────────

async def _socks5_setup_start(update, context):
    """Начало диалога настройки SOCKS5 (entry point из callback)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚙️ *Настройка SOCKS5*\n\n"
        "Введите адрес прокси-сервера (IP или домен):\n\n"
        "Для отмены — /cancel",
        parse_mode="Markdown"
    )
    return _S_HOST


async def _socks5_got_host(update, context):
    host = update.message.text.strip()
    context.user_data["socks5_host"] = host
    await update.message.reply_text(
        f"🌐 Хост: `{host}`\n\n"
        "Введите порт (например `1080`):",
        parse_mode="Markdown"
    )
    return _S_PORT


async def _socks5_got_port(update, context):
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 65535):
        await update.message.reply_text("❌ Некорректный порт. Введите число от 1 до 65535:")
        return _S_PORT
    context.user_data["socks5_port"] = int(text)
    await update.message.reply_text(
        "Введите логин (или `-` если без авторизации):"
    )
    return _S_USER


async def _socks5_got_user(update, context):
    text = update.message.text.strip()
    context.user_data["socks5_user"] = "" if text == "-" else text
    if context.user_data["socks5_user"]:
        await update.message.reply_text("Введите пароль:")
        return _S_PASS
    context.user_data["socks5_pass"] = ""
    return await _socks5_save(update, context)


async def _socks5_got_pass(update, context):
    context.user_data["socks5_pass"] = update.message.text.strip()
    return await _socks5_save(update, context)


async def _socks5_save(update, context):
    host = context.user_data.get("socks5_host", "")
    port = context.user_data.get("socks5_port", 1080)
    user = context.user_data.get("socks5_user", "")
    pass_ = context.user_data.get("socks5_pass", "")

    state = _load_state()
    state.update({
        "socks5_host": host,
        "socks5_port": port,
        "socks5_user": user,
        "socks5_pass": pass_,
    })
    _save_state(state)

    auth_info = f" (логин: {user})" if user else " (без авторизации)"
    await update.message.reply_text(
        f"✅ *SOCKS5 настроен*\n\n"
        f"Хост: `{host}:{port}`{auth_info}\n\n"
        f"Теперь выберите клиента в меню для активации.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧦 Открыть меню SOCKS5", callback_data="socks5_menu")]
        ])
    )
    context.user_data.clear()
    return ConversationHandler.END


async def _socks5_cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Настройка отменена.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧦 Открыть меню SOCKS5", callback_data="socks5_menu")]
        ])
    )
    return ConversationHandler.END


# ── Главный callback роутер ────────────────────────────────────────────────────

async def _socks5_callback(update, context):
    query   = update.callback_query
    user_id = query.from_user.id
    data    = query.data
    await query.answer()

    try:
        from awg_core import ADMIN_ID
        is_admin = (user_id == ADMIN_ID)
    except Exception:
        is_admin = False

    if not is_admin:
        await query.answer("⛔ Только для администратора", show_alert=True)
        return

    if data == "socks5_menu" or data == "socks5_list":
        await _show_socks5_menu(query)
    elif data.startswith("socks5_client_"):
        name = data[len("socks5_client_"):]
        await _show_client_card(query, name)
    elif data.startswith("socks5_enable_"):
        name = data[len("socks5_enable_"):]
        await _enable_socks5(query, name)
    elif data == "socks5_disable":
        await _disable_socks5(query)


# ── Интерфейс модуля ──────────────────────────────────────────────────────────

def get_admin_menu_buttons() -> list:
    """Кнопка видна ТОЛЬКО администратору."""
    return [[InlineKeyboardButton("🧦 SOCKS5 прокси", callback_data="socks5_menu")]]


def get_user_menu_buttons(user_id: int) -> list:
    """SOCKS5 управление — только для администратора."""
    return []


def register_handlers(app) -> None:
    """Регистрируем обработчики в group=-1 (раньше главного button_handler)."""

    # ConversationHandler для настройки параметров SOCKS5
    try:
        from awg_core import ADMIN_ID
        admin_filter = filters.User(user_id=ADMIN_ID)
    except Exception:
        admin_filter = filters.ALL

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_socks5_setup_start, pattern=r"^socks5_setup$")
        ],
        states={
            _S_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                                     _socks5_got_host)],
            _S_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                                     _socks5_got_port)],
            _S_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                                     _socks5_got_user)],
            _S_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter,
                                     _socks5_got_pass)],
        },
        fallbacks=[CommandHandler("cancel", _socks5_cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv, group=-1)
    app.add_handler(
        CallbackQueryHandler(_socks5_callback, pattern=r"^socks5_"),
        group=-1
    )
