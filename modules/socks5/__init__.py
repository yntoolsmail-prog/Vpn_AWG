#!/usr/bin/env python3
"""modules/socks5/__init__.py — модуль управления SOCKS5-прокси для AWG клиентов.

Кнопка "🧦 SOCKS5 прокси" видна ТОЛЬКО администратору.
Позволяет направить трафик выбранного VPN-клиента через внешний SOCKS5 прокси
с помощью redsocks2 + iptables.

Правила применяются на PRIMARY и на всех slave-серверах где установлен redsocks2.
Состояние хранится в /etc/awg-socks5/bot_state.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
    try:
        return socket.gethostbyname(host)
    except Exception:
        return host


def _get_client_ip(name: str) -> str | None:
    try:
        from awg_core import get_client_keys
        data = get_client_keys(name)
        if data:
            return data.get("ip")
    except Exception:
        pass
    return None


def _get_slaves() -> list:
    """Возвращает список slave-серверов из servers.json."""
    try:
        from awg_core import load_servers
        return [s for s in load_servers() if not s.get("is_primary")]
    except Exception:
        return []


def _write_redsocks_conf(host: str, port: int, user: str, pass_: str):
    os.makedirs(os.path.dirname(REDSOCKS2_CONF), exist_ok=True)
    auth_lines = ""
    if user:
        auth_lines = f'    login = "{user}";\n    password = "{pass_}";\n'
    conf = (
        "base {\n"
        "    log_debug = off;\n"
        "    log_info = on;\n"
        "    log = \"syslog:daemon\";\n"
        "    daemon = off;\n"
        "    redirector = iptables;\n"
        "}\n\n"
        "redsocks {\n"
        f"    bind = \"0.0.0.0:{REDSOCKS2_PORT}\";\n"
        f"    relay = \"{host}:{port}\";\n"
        "    type = socks5;\n"
        "    autoproxy = 0;\n"
        "    timeout = 10;\n"
        f"{auth_lines}"
        "}\n"
    )
    with open(REDSOCKS2_CONF, "w") as f:
        f.write(conf)
    os.chmod(REDSOCKS2_CONF, 0o600)


def _remove_iptables_for_client(client_ip: str):
    if not client_ip:
        return
    chain = f"SOCKS5_{client_ip.replace('.', '_')}"
    # Снимаем INPUT-блокировку внешнего доступа к порту redsocks2
    try:
        from awg_core import get_host_iface
        ext_iface = get_host_iface()
        subprocess.run([
            "iptables", "-D", "INPUT",
            "-i", ext_iface, "-p", "tcp", "--dport", str(REDSOCKS2_PORT),
            "-j", "DROP"
        ], capture_output=True)
    except Exception:
        pass
    # Чистим оба порта (5300 — текущий dnscrypt-proxy, 5399 — старый dnstc) для идемпотентности
    for proto in ("udp", "tcp"):
        for dns_port in (5300, 5399):
            subprocess.run([
                "iptables", "-t", "nat", "-D", "PREROUTING",
                "-s", f"{client_ip}/32", "-p", proto, "--dport", "53",
                "-j", "DNAT", "--to-destination", f"127.0.0.1:{dns_port}",
            ], capture_output=True)
    for cmd in [
        ["iptables", "-t", "nat", "-D", "PREROUTING", "-s", f"{client_ip}/32", "-j", chain],
        ["iptables", "-t", "nat", "-F", chain],
        ["iptables", "-t", "nat", "-X", chain],
        ["iptables", "-t", "filter", "-D", "FORWARD",
         "-s", f"{client_ip}/32", "-p", "udp", "!", "--dport", "53", "-j", "REJECT"],
        ["iptables", "-t", "filter", "-D", "FORWARD",
         "-s", f"{client_ip}/32", "-p", "tcp", "--dport", "853", "-j", "REJECT"],
    ]:
        subprocess.run(cmd, capture_output=True)


def _apply_iptables_for_client(client_ip: str, socks5_host_ip: str) -> tuple[bool, str]:
    """Настраивает iptables для маршрутизации трафика клиента через SOCKS5.

    DNS (UDP и TCP) → dnscrypt-proxy (порт 5300, DoH).
    TCP → redsocks2 (порт 12345) → SOCKS5-прокси.
    UDP кроме DNS — заблокирован (нет WebRTC-утечек).
    Перед применением выполняется идемпотентная очистка."""
    try:
        chain = f"SOCKS5_{client_ip.replace('.', '_')}"

        # Идемпотентная очистка: удаляем старые правила если есть
        _remove_iptables_for_client(client_ip)

        # Блокируем прямые подключения к redsocks2 снаружи (0.0.0.0 bind — порт виден в интернете)
        try:
            from awg_core import get_host_iface
            ext_iface = get_host_iface()
            subprocess.run([
                "iptables", "-I", "INPUT",
                "-i", ext_iface, "-p", "tcp", "--dport", str(REDSOCKS2_PORT),
                "-j", "DROP"
            ], capture_output=True)
        except Exception:
            pass

        # DNS UDP → redsocks2 dnstc (конвертирует в TCP → уходит через SOCKS5)
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-p", "udp", "--dport", "53",
            "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"
        ], check=True, capture_output=True)
        # DNS TCP → redsocks2 dnstc
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-p", "tcp", "--dport", "53",
            "-j", "DNAT", "--to-destination", f"127.0.0.1:{DNS_LOCAL_PORT}"
        ], check=True, capture_output=True)

        # Создаём цепочку для TCP (после очистки выше цепочки нет — -N всегда успешен)
        subprocess.run(
            ["iptables", "-t", "nat", "-N", chain],
            check=True, capture_output=True
        )
        # Исключение: IP прокси (иначе петля)
        subprocess.run([
            "iptables", "-t", "nat", "-A", chain,
            "-d", socks5_host_ip, "-j", "RETURN"
        ], check=True, capture_output=True)
        # Исключения: приватные сети
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
        # Применяем цепочку к трафику клиента
        subprocess.run([
            "iptables", "-t", "nat", "-A", "PREROUTING",
            "-s", f"{client_ip}/32", "-j", chain
        ], check=True, capture_output=True)
        # Блокируем UDP кроме DNS
        subprocess.run([
            "iptables", "-t", "filter", "-A", "FORWARD",
            "-s", f"{client_ip}/32", "-p", "udp", "!", "--dport", "53",
            "-j", "REJECT"
        ], check=True, capture_output=True)
        # Блокируем DNS-over-TLS (TCP 853) — иначе утечка DNS через прокси
        subprocess.run([
            "iptables", "-t", "filter", "-A", "FORWARD",
            "-s", f"{client_ip}/32", "-p", "tcp", "--dport", "853",
            "-j", "REJECT"
        ], check=True, capture_output=True)

        subprocess.run(["sysctl", "-w", "net.ipv4.conf.all.route_localnet=1"], capture_output=True)
        subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], capture_output=True)
        os.makedirs("/etc/iptables", exist_ok=True)
        subprocess.run(["bash", "-c", "iptables-save > /etc/iptables/rules.v4"], capture_output=True)

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


# ── Диагностика прокси ────────────────────────────────────────────────────────

def _get_proxy_from_conf() -> tuple[str, int]:
    """Читает relay host:port напрямую из redsocks2.conf — источник правды."""
    try:
        with open(REDSOCKS2_CONF) as f:
            for line in f:
                m = re.search(r'relay\s*=\s*"([^"]+):(\d+)"', line)
                if m:
                    return m.group(1), int(m.group(2))
    except Exception:
        pass
    return "", 0


def _check_proxy_tcp(host: str, port: int, timeout: int = 5) -> bool:
    """TCP-пинг прокси-сервера: True если порт отвечает."""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _has_recent_auth_errors() -> bool:
    """True если в последних 50 строках лога redsocks2 есть 'server failure'."""
    try:
        r = subprocess.run(
            ["journalctl", "-u", "redsocks2", "-n", "50", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        return "server failure" in r.stdout
    except Exception:
        return False


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

    active_client = state.get("active_client", "")

    try:
        from awg_core import get_all_clients
        all_clients = get_all_clients()
    except Exception:
        all_clients = []

    # Адрес прокси: конфиг redsocks2.conf — источник правды; state — запасной
    conf_host, conf_port = _get_proxy_from_conf()
    display_host = conf_host or state.get("socks5_host") or "—"
    display_port = conf_port or state.get("socks5_port") or "—"

    # Статус сервиса
    running = _is_redsocks_running()
    svc_line = "🟢 redsocks2 работает" if running else "🔴 redsocks2 остановлен"

    # Доступность прокси-сервера (только если сервис запущен)
    if running and conf_host and conf_port:
        tcp_ok = _check_proxy_tcp(conf_host, conf_port)
        auth_errors = _has_recent_auth_errors()
        if not tcp_ok:
            proxy_line = f"⛔ Прокси недоступен ({display_host}:{display_port})"
        elif auth_errors:
            proxy_line = f"⚠️ Прокси отвечает, но есть ошибки авторизации\n   `{display_host}:{display_port}`"
        else:
            proxy_line = f"✅ Прокси работает: `{display_host}:{display_port}`"
    elif conf_host:
        proxy_line = f"💤 Прокси не проверен: `{display_host}:{display_port}`"
    else:
        proxy_line = "⚙️ Прокси не настроен"

    header = f"🧦 *SOCKS5 прокси*\n\n{svc_line}\n{proxy_line}\n"
    if active_client:
        header += f"Активен для: *{active_client}*\n"
    header += "\nВыберите клиента для управления SOCKS5:"

    kb = []
    for name in all_clients:
        mark = " ✅" if name == active_client else ""
        kb.append([InlineKeyboardButton(
            f"👤 {name}{mark}", callback_data=f"socks5_client_{name}"
        )])
    kb.append([InlineKeyboardButton("⚙️ Настроить SOCKS5", callback_data="socks5_setup")])
    if active_client:
        kb.append([InlineKeyboardButton("❌ Отключить SOCKS5", callback_data="socks5_disable")])
    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])

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
        action_btn  = InlineKeyboardButton("❌ Отключить SOCKS5", callback_data="socks5_disable")
    else:
        status_text = "○ SOCKS5 не активен"
        action_btn  = InlineKeyboardButton("✅ Включить SOCKS5", callback_data=f"socks5_enable_{name}")

    await query.edit_message_text(
        f"👤 *{name}*\n"
        f"IP: `{client_ip}`\n"
        f"SOCKS5: {status_text}",
        reply_markup=InlineKeyboardMarkup([
            [action_btn],
            [InlineKeyboardButton("◀️ К списку", callback_data="socks5_menu")],
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

    # Снимаем правила с предыдущего клиента (primary)
    prev_ip = state.get("active_client_ip")
    if prev_ip and prev_ip != client_ip:
        _remove_iptables_for_client(prev_ip)

    socks5_host_ip = _resolve_host(host)

    # 1. Пишем конфиг redsocks2
    _write_redsocks_conf(host, int(port), state.get("socks5_user", ""), state.get("socks5_pass", ""))

    # 2. Запускаем redsocks2 ПЕРВЫМ — его dnstc должен слушать DNS_LOCAL_PORT (5399)
    #    до того как iptables начнёт перенаправлять DNS на этот порт
    ok2, msg2 = _redsocks_restart()
    if not ok2:
        await query.answer(f"❌ redsocks2 не запустился:\n{msg2}", show_alert=True)
        await _show_socks5_menu(query)
        return

    # 3. Применяем iptables (idempotent — очищает старые правила внутри)
    ok, msg = _apply_iptables_for_client(client_ip, socks5_host_ip)
    if not ok:
        await query.answer(msg, show_alert=True)
        await _show_socks5_menu(query)
        return

    # Применяем на slave-серверах (асинхронно, не блокируем UI)
    slave_errors: list[str] = []
    loop = asyncio.get_event_loop()
    slaves = _get_slaves()
    for slave in slaves:
        sname = f"{slave.get('emoji', '')} {slave.get('name', 'Slave')}".strip()
        try:
            from awg_core import ssh_apply_socks5_on_slave
            s_ok, s_msg = await loop.run_in_executor(
                None,
                ssh_apply_socks5_on_slave,
                slave, client_ip, host, int(port),
                state.get("socks5_user", ""), state.get("socks5_pass", "")
            )
            if not s_ok:
                slave_errors.append(f"{sname}: {s_msg}")
        except Exception as e:
            slave_errors.append(f"{sname}: {e}")

    # Сохраняем состояние
    state["active_client"]    = name
    state["active_client_ip"] = client_ip
    _save_state(state)

    result_lines = [f"✅ SOCKS5 активирован для *{name}*"]
    if not ok2:
        result_lines.append(msg2)
    if slave_errors:
        result_lines.append("\n⚠️ Ошибки на slave:")
        result_lines.extend(f"  • {e}" for e in slave_errors)

    await query.answer("✅ SOCKS5 активирован" + (f"\nОшибки slave: {len(slave_errors)}" if slave_errors else ""))
    await query.edit_message_text(
        "\n".join(result_lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🧦 Меню SOCKS5", callback_data="socks5_menu")],
        ]),
        parse_mode="Markdown"
    )


async def _disable_socks5(query):
    state     = _load_state()
    prev_ip   = state.get("active_client_ip")
    prev_name = state.get("active_client", "")

    if prev_ip:
        _remove_iptables_for_client(prev_ip)
        subprocess.run(["bash", "-c", "iptables-save > /etc/iptables/rules.v4"], capture_output=True)

    # Снимаем с slave-серверов
    slave_errors: list[str] = []
    if prev_ip:
        loop   = asyncio.get_event_loop()
        slaves = _get_slaves()
        for slave in slaves:
            sname = f"{slave.get('emoji', '')} {slave.get('name', 'Slave')}".strip()
            try:
                from awg_core import ssh_remove_socks5_from_slave
                s_ok, s_msg = await loop.run_in_executor(
                    None, ssh_remove_socks5_from_slave, slave, prev_ip
                )
                if not s_ok:
                    slave_errors.append(f"{sname}: {s_msg}")
            except Exception as e:
                slave_errors.append(f"{sname}: {e}")

    state.pop("active_client",    None)
    state.pop("active_client_ip", None)
    _save_state(state)

    note = f" для {prev_name}" if prev_name else ""
    await query.answer(f"✅ SOCKS5 отключён{note}")
    await _show_socks5_menu(query)


# ── ConversationHandler для настройки SOCKS5 ─────────────────────────────────

async def _socks5_setup_start(update, context):
    query = update.callback_query
    await query.answer()

    state = _load_state()
    cur_host = state.get("socks5_host", "")
    cur_port = state.get("socks5_port", "")
    cur_note = ""
    if cur_host and cur_port:
        cur_note = f"\n\n_Текущий прокси: `{cur_host}:{cur_port}`_"

    await query.edit_message_text(
        f"⚙️ *Настройка SOCKS5*{cur_note}\n\n"
        "Введите адрес прокси-сервера (IP или домен):\n\n"
        "Для отмены — /cancel",
        parse_mode="Markdown"
    )
    return _S_HOST


async def _socks5_got_host(update, context):
    host = update.message.text.strip()
    context.user_data["socks5_host"] = host
    await update.message.reply_text(
        f"🌐 Хост: `{host}`\n\nВведите порт (например `1080`):",
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
    try:
        host  = context.user_data.get("socks5_host", "")
        port  = context.user_data.get("socks5_port", 1080)
        user  = context.user_data.get("socks5_user", "")
        pass_ = context.user_data.get("socks5_pass", "")

        state = _load_state()
        state.update({
            "socks5_host": host,
            "socks5_port": port,
            "socks5_user": user,
            "socks5_pass": pass_,
        })
        _save_state(state)
        context.user_data.clear()

        auth_info = f" (логин: {user})" if user else " (без авторизации)"
        await update.message.reply_text(
            f"✅ SOCKS5 настроен\n\n"
            f"Прокси: {host}:{port}{auth_info}\n\n"
            f"Выберите клиента в меню для активации.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧦 Открыть меню SOCKS5", callback_data="socks5_menu")]
            ])
        )
    except Exception as e:
        logger.exception("_socks5_save error: %s", e)
        try:
            await update.message.reply_text(f"❌ Ошибка сохранения настроек: {e}")
        except Exception:
            pass
    return ConversationHandler.END


async def _socks5_cancel(update, context):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "❌ Настройка отменена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧦 Меню SOCKS5", callback_data="socks5_menu")]
            ])
        )
    else:
        await update.message.reply_text(
            "❌ Настройка отменена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧦 Меню SOCKS5", callback_data="socks5_menu")]
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

    if data in ("socks5_menu", "socks5_list"):
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
    return [[InlineKeyboardButton("🧦 SOCKS5 прокси", callback_data="socks5_menu")]]


def get_user_menu_buttons(user_id: int) -> list:
    return []


def register_handlers(app) -> None:
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
            _S_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, _socks5_got_host)],
            _S_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, _socks5_got_port)],
            _S_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, _socks5_got_user)],
            _S_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND & admin_filter, _socks5_got_pass)],
        },
        fallbacks=[CommandHandler("cancel", _socks5_cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
        name="socks5_setup",
        persistent=True,
    )

    app.add_handler(conv, group=-1)
    app.add_handler(
        CallbackQueryHandler(_socks5_callback, pattern=r"^socks5_"),
        group=-1
    )
