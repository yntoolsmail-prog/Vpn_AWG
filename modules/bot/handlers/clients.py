import os, tempfile, shutil, re, asyncio, subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from awg_core import (
    ADMIN_ID, AWG_IFACE, CLIENTS_DIR, EXCL_EXT, QRENCODE_BIN,
    SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP, SERVER_PORT, TMA_URL,
    can_access_device, create_client, device_short_name,
    gen_obfs, get_all_clients, get_allowed_ips_for_client,
    get_awg_dump, get_combined_awg_dump, get_client_keys, get_client_pub, get_user_clients,
    get_user_name, is_approved, load_client_excl, load_servers,
    make_conf_for_client, make_conf_for_client_ep, make_vpn_link,
    remove_client_from_awg, resolve_endpoint, save_client_excl,
    fmt_handshake, fmt_bytes, SERVER_PUBLIC,
)
from .common import _md, back_kb, sites_keyboard, _tma_button, IMG_BASE, WAITING_DEVICE_NAME, BTN_BACK, BTN_BACK_MENU, BTN_BACK_CARD, BTN_CANCEL, BTN_MY_DEVICES
import logging

logger = logging.getLogger(__name__)


async def show_my_devices(query, user_id: int):
    clients = get_user_clients(user_id)
    peers   = get_combined_awg_dump()

    if not clients:
        kb = [
            [InlineKeyboardButton("➕ Добавить первое устройство", callback_data="add")],
            [InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")],
        ]
        await query.edit_message_text(
            "📱 У вас пока нет устройств.\nДобавьте первое!",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    lines = [f"📱 Ваши устройства ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
        ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
        lines.append(f"• {device_short_name(name)} | {hs} | ↓{dl} ↑{ul}")

    kb = [[InlineKeyboardButton(f"📋 {device_short_name(name)}",
           callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def show_device(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    peers = get_combined_awg_dump()
    pub   = get_client_pub(name)
    stats = peers.get(pub, {}) if pub else {}

    short = device_short_name(name)
    hs    = fmt_handshake(stats.get("handshake", 0))
    dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
    ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
    ep    = stats.get("endpoint", "—")
    srv_label = stats.get("server", "")
    srv_note  = f" _({srv_label})_" if srv_label else ""

    info = (
        f"📱 Устройство: *{short}*\n"
        f"👤 Пользователь: {name.split('.')[0]}\n\n"
        f"🕐 Хендшейк: {hs}{srv_note}\n"
        f"📍 Endpoint: {ep}\n"
        f"📶 Трафик: ↓{dl} ↑{ul}"
    )
    saved = load_client_excl(name)
    excl_count = len(saved.get("sites", [])) if saved else 0
    excl_label = f"🌐 Исключения сайтов [{excl_count}]" if excl_count else "🌐 Исключения сайтов [нет]"

    back_target = "my_devices" if user_id != ADMIN_ID else "all_clients"
    kb = [
        [InlineKeyboardButton("📄 Скачать .conf (AmneziaWG)", callback_data=f"conf_{name}")],
        [InlineKeyboardButton("📱 QR-код (AmneziaWG)",        callback_data=f"qr_{name}")],
        [InlineKeyboardButton("📤 Поделиться кодом (AmneziaVPN)", callback_data=f"share_{name}")],
        [InlineKeyboardButton(excl_label,                      callback_data=f"sites_{name}")],
        [InlineKeyboardButton("🗑 Удалить",                    callback_data=f"del_{name}")],
        [InlineKeyboardButton(BTN_BACK,                      callback_data=back_target)],
    ]
    await query.edit_message_text(info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def show_all_clients(query):
    clients = get_all_clients()
    peers   = get_combined_awg_dump()

    if not clients:
        await query.edit_message_text("👥 Клиентов нет.", reply_markup=back_kb())
        return

    lines = [f"🌍 Все клиенты ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
        ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
        lines.append(f"• {name} | {hs} | ↓{dl} ↑{ul}")

    kb = [[InlineKeyboardButton(f"📋 {name}", callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

def _server_kb(name: str, action: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора VPS-сервера. action = 'conf' | 'qr' | 'share'"""
    servers = load_servers()
    rows = []
    for i, srv in enumerate(servers):
        label = f"{srv.get('emoji', '🖥')} {srv.get('name', f'Сервер {i+1}')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{action}_srv_{i}_{name}")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data=f"device_{name}")])
    return InlineKeyboardMarkup(rows)

def _server_eps_kb(name: str, action: str, srv_idx: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора эндпоинта внутри конкретного сервера."""
    servers = load_servers()
    srv = servers[srv_idx] if srv_idx < len(servers) else None
    rows = []
    if srv:
        for ei, ep in enumerate(srv.get("endpoints", [])):
            rows.append([InlineKeyboardButton(
                ep["value"], callback_data=f"{action}_s{srv_idx}_e{ei}_{name}"
            )])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data=f"{action}_{name}")])
    return InlineKeyboardMarkup(rows)

def _action_icon(action: str) -> str:
    return {"conf": "📄", "qr": "📱", "share": "📤"}.get(action, "📄")

def _action_label(action: str) -> str:
    return {"conf": "Скачать .conf", "qr": "QR-код", "share": "Поделиться"}.get(action, action)

async def _show_ep_select(query, name: str, user_id: int, action: str):
    """Единый экран выбора эндпоинта — плоский список с меткой сервера."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    short = device_short_name(name)
    icon  = _action_icon(action)

    all_eps = []
    for si, srv in enumerate(servers):
        for ei, ep in enumerate(srv.get("endpoints", [])):
            all_eps.append((si, ei, srv, ep))

    if not all_eps:
        await query.answer("Нет доступных эндпоинтов.", show_alert=True)
        return

    # Один эндпоинт — сразу отправляем
    if len(all_eps) == 1:
        si, ei, srv, ep = all_eps[0]
        await _do_send_action(query, name, action, ep["value"], srv)
        return

    # Плоский список: каждая кнопка = флаг + эндпоинт + (сервер)
    rows = []
    for si, ei, srv, ep in all_eps:
        emoji  = srv.get("emoji", "🖥")
        sname  = srv.get("name", f"Сервер {si + 1}")
        ep_val = ep["value"]
        is_domain = ep.get("type") == "domain"
        type_icon = "🌐" if is_domain else "🔢"
        rows.append([InlineKeyboardButton(
            f"{type_icon} {emoji} {ep_val}  ({sname})",
            callback_data=f"{action}_s{si}_e{ei}_{name}"
        )])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data=f"device_{name}")])

    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n\n"
        f"Выберите сервер (страну) и способ подключения:\n\n"
        f"🌐 *Домен* — рекомендуется: универсальный способ подключения, обеспечит работу даже в случае смены реального IP сервера или переезда на другой сервер.\n\n"
        f"🔢 *IP* — только если домен не работает: прямое подключение к серверу, при смене IP потребуется заново скачать файл конфигурации.\n\n"
        f"_Флаг и название в скобках — страна/сервер, к которому относится эндпоинт._",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )

async def show_conf_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "conf")

async def show_qr_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "qr")

async def show_share_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "share")

async def show_server_eps(query, name: str, user_id: int, action: str, srv_idx: int):
    """Показывает список эндпоинтов выбранного сервера."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv  = servers[srv_idx]
    eps  = srv.get("endpoints", [])
    short = device_short_name(name)
    icon  = _action_icon(action)
    emoji = srv.get("emoji", "🖥")
    sname = srv.get("name", f"Сервер {srv_idx + 1}")

    if not eps:
        await query.answer("У этого сервера нет эндпоинтов.", show_alert=True)
        return
    if len(eps) == 1:
        await _do_send_action(query, name, action, eps[0]["value"], srv)
        return
    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n{emoji} {_md(sname)} — выберите эндпоинт:",
        reply_markup=_server_eps_kb(name, action, srv_idx),
        parse_mode="Markdown"
    )

def _make_conf_filename(name: str, srv_name: str = None) -> str:
    """Формирует имя файла: User.SERVER.Device.conf"""
    if not srv_name:
        return f"{name}.conf"
    srv_clean = re.sub(r'[^\w]', '', srv_name.replace(' ', '_'))
    parts = name.split(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.{srv_clean}.{parts[1]}.conf"
    return f"{name}.{srv_clean}.conf"


def _make_vpn_filename(name: str, srv_name: str = None) -> str:
    """Формирует имя .vpn файла: User.SERVER.Device.vpn"""
    return _make_conf_filename(name, srv_name).replace(".conf", ".vpn")


async def _do_send_action(query, name: str, action: str, ep: str, server: dict):
    """Выполняет нужное действие (conf/qr/share) с конкретным эндпоинтом и сервером."""
    spub      = server.get("awg_public_key") or SERVER_PUBLIC
    sprt      = str(server.get("awg_port") or SERVER_PORT)
    srv_name  = server.get("name", "")
    srv_emoji = server.get("emoji", "")
    if action == "conf":
        await do_send_conf_direct(query, name, ep, spub, sprt, srv_name)
    elif action == "qr":
        await do_send_qr_direct(query, name, ep, spub, sprt, srv_name)
    else:
        await do_send_share_direct(query, name, ep, spub, sprt, srv_name, srv_emoji)

# ── Финальная отправка .conf ──────────────────────────────────────────────────

async def do_send_conf(query, name: str, ep_key: str):
    """Генерирует .conf в памяти (с сохранёнными исключениями) и отправляет в чат."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short    = device_short_name(name)
    ep       = resolve_endpoint(ep_key)
    content  = (make_conf_for_client(name, ep, allowed_ips) or "").encode()
    filename = f"{name}.conf"
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    await query.message.reply_document(
        document=content,
        filename=filename,
        caption=(
            f"📄 Конфиг *{short}* ({ep_label})\n"
            f"🌐 Endpoint: `{ep}:{SERVER_PORT}`{excl_note}\n\n"
            f"Импортируйте в AmneziaWG."
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ── Финальная отправка QR ─────────────────────────────────────────────────────

async def do_send_qr(query, name: str, ep_key: str):
    """Генерирует .conf в памяти (с сохранёнными исключениями) → QR → отправляет."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    ep      = resolve_endpoint(ep_key)
    content = (make_conf_for_client(name, ep, allowed_ips) or "").encode()
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)

    # Пишем во временный файл только для qrencode — сразу удаляем
    tmp_conf = f"/tmp/qr_{name}_{ep_key}.conf"
    qr_path  = f"/tmp/qr_{name}_{ep_key}.png"
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        with open(tmp_conf, "wb") as f:
            f.write(content)
        # Сначала .conf файл, затем QR
        safe_name = name.replace(".", "_")
        await query.message.reply_document(
            document=open(tmp_conf, "rb"),
            filename=f"{safe_name}_{ep_key}.conf",
            caption=f"📄 .conf для AmneziaWG — *{short}* ({ep_label}){excl_note}",
            parse_mode="Markdown"
        )
        if len(content) > 2900:
            await query.message.reply_text(
                "ℹ️ QR-код недоступен: конфиг слишком большой из-за исключений сайтов.\n"
                "Используйте *.conf* файл выше.",
                parse_mode="Markdown"
            )
        else:
            subprocess.run([QRENCODE_BIN, "-o", qr_path, "-r", tmp_conf], check=True)
            await query.message.reply_photo(
                photo=open(qr_path, "rb"),
                caption=(
                    f"📱 QR для AmneziaWG — *{short}* ({ep_label})\n"
                    f"🌐 Endpoint: `{ep}:{SERVER_PORT}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    finally:
        for p in [tmp_conf, qr_path]:
            if os.path.exists(p):
                os.remove(p)
    await show_device(query, name, query.from_user.id)

# ── Финальная отправка vpn:// (AmneziaVPN) ───────────────────────────────────

async def do_send_share(query, name: str, ep_key: str):
    """Генерирует vpn:// ссылку и .vpn файл на лету, отправляет в чат."""
    keys = get_client_keys(name)
    if not keys:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short    = device_short_name(name)
    ep       = resolve_endpoint(ep_key)
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)
    vpn_link = make_vpn_link(
        keys["priv"], keys["pub"], keys["ip"], keys["psk"],
        keys.get("obfs", gen_obfs()), name, endpoint=ep
    )
    vpn_bytes = vpn_link.encode()
    # Сначала ссылка — потом файл
    await query.message.reply_text(
        f"🔗 Ссылка AmneziaVPN *{short}* ({ep_label})\n"
        f"🌐 Endpoint: `{ep}:{SERVER_PORT}`\n\n"
        f"Нажмите чтобы скопировать:\n`{vpn_link}`",
        parse_mode="Markdown"
    )
    await query.message.reply_document(
        document=vpn_bytes,
        filename=_make_vpn_filename(name),
        caption=(
            f"📤 Файл для AmneziaVPN — *{short}* ({ep_label})\n"
            f"Вставьте в приложении: + → Открыть файл"
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ── Прямая отправка с конкретным эндпоинтом (мультисервер) ───────────────────

async def do_send_conf_direct(query, name: str, ep: str,
                               spub: str = None, sprt: str = None,
                               srv_name: str = None):
    """Отправляет .conf с указанным эндпоинтом и параметрами сервера."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips) or "").encode()
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    await query.message.reply_document(
        document=content,
        filename=_make_conf_filename(name, srv_name),
        caption=(
            f"📄 Конфиг *{short}*\n"
            f"🌐 Endpoint: `{ep}:{prt}`{excl_note}\n\n"
            f"Импортируйте в AmneziaWG."
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

async def do_send_qr_direct(query, name: str, ep: str,
                             spub: str = None, sprt: str = None,
                             srv_name: str = None):
    """Отправляет QR с указанным эндпоинтом."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips) or "").encode()
    tmp_conf = f"/tmp/qr_{name}_direct.conf"
    qr_path  = f"/tmp/qr_{name}_direct.png"
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        with open(tmp_conf, "wb") as f:
            f.write(content)
        await query.message.reply_document(
            document=open(tmp_conf, "rb"),
            filename=_make_conf_filename(name, srv_name),
            caption=f"📄 .conf для AmneziaWG — *{short}*{excl_note}",
            parse_mode="Markdown"
        )
        if len(content) > 2900:
            await query.message.reply_text(
                "ℹ️ QR-код недоступен: конфиг слишком большой из-за исключений сайтов.\n"
                "Используйте *.conf* файл выше.",
                parse_mode="Markdown"
            )
        else:
            subprocess.run([QRENCODE_BIN, "-o", qr_path, "-r", tmp_conf], check=True)
            await query.message.reply_photo(
                photo=open(qr_path, "rb"),
                caption=(
                    f"📱 QR для AmneziaWG — *{short}*\n"
                    f"🌐 Endpoint: `{ep}:{prt}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    finally:
        for p in [tmp_conf, qr_path]:
            if os.path.exists(p):
                os.remove(p)
    await show_device(query, name, query.from_user.id)

async def do_send_share_direct(query, name: str, ep: str,
                                spub: str = None, sprt: str = None,
                                srv_name: str = None, srv_emoji: str = ""):
    """Отправляет vpn:// ссылку с указанным эндпоинтом."""
    keys = get_client_keys(name)
    if not keys:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short    = device_short_name(name)
    prt      = sprt or SERVER_PORT
    # Название в приложении AmneziaVPN: "🇳🇱 Admin.Nout"
    vpn_display_name = f"{srv_emoji} {name}".strip() if srv_emoji else name
    vpn_link = make_vpn_link(
        keys["priv"], keys["pub"], keys["ip"], keys["psk"],
        keys.get("obfs", gen_obfs()), vpn_display_name,
        endpoint=ep, server_public=spub, server_port=sprt
    )
    vpn_bytes = vpn_link.encode()
    await query.message.reply_text(
        f"🔗 Ссылка AmneziaVPN *{short}*\n"
        f"🌐 Endpoint: `{ep}:{prt}`\n\n"
        f"Нажмите чтобы скопировать:\n`{vpn_link}`",
        parse_mode="Markdown"
    )
    await query.message.reply_document(
        document=vpn_bytes,
        filename=_make_vpn_filename(name, srv_name),
        caption=(
            f"📤 Файл для AmneziaVPN — *{short}*\n"
            f"Вставьте в приложении: + → Открыть файл"
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ══════════════════════════════════════════════════════════════════════════════
# УДАЛЕНИЕ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════

async def do_delete(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        await query.edit_message_text("❌ Устройство не найдено.", reply_markup=back_kb())
        return

    short = device_short_name(name)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del_{name}")],
        [InlineKeyboardButton(BTN_CANCEL,      callback_data=f"device_{name}")],
    ])
    await query.edit_message_text(
        f"🗑 Удалить устройство *{short}*?\n\nЭто действие необратимо.",
        reply_markup=kb, parse_mode="Markdown"
    )

async def confirm_delete(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    remove_client_from_awg(name)
    short = device_short_name(name)
    await query.edit_message_text(
        f"✅ Устройство *{short}* удалено.",
        reply_markup=back_kb("my_devices"), parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНИЕ УСТРОЙСТВА (ConversationHandler)
# ══════════════════════════════════════════════════════════════════════════════

async def cancel_add_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена добавления устройства через кнопку Отмена"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("adding_user_id", None)
    # Late import to avoid circular dependency (bot -> handlers.clients -> bot)
    import importlib
    _bot_mod = importlib.import_module("bot")
    await _bot_mod.main_menu(query, query.from_user.id, edit=True)
    return ConversationHandler.END

async def add_device_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_CANCEL, callback_data="add_cancel")]
    ])
    await query.edit_message_text(
        f"🧲 Добавление устройства\n\n"
        f"Введите название устройства *латиницей*:\n"
        f"`Phone`, `PC`, `Nout`, `iPad`, `TV`\n\n"
        f"Или нажмите Отмена.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    context.user_data["adding_user_id"] = user_id
    return WAITING_DEVICE_NAME

async def receive_device_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_approved(user_id):
        return ConversationHandler.END

    raw        = update.message.text.strip()

    # Проверяем наличие кириллицы / не-ASCII символов ДО фильтрации
    has_non_ascii = any(not c.isascii() for c in raw)

    # Убираем всё лишнее — AmneziaWG не любит дефисы и спецсимволы
    device_raw = "".join(c for c in raw if c.isascii() and (c.isalnum() or c == "_"))
    device_raw = device_raw.capitalize()

    if not device_raw:
        await update.message.reply_text(
            "❌ Введите название *латиницей*, только буквы и цифры. Например: `Phone`",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    # Если была кириллица — не угадываем, просим переввести явно
    if has_non_ascii:
        await update.message.reply_text(
            f"❌ Название содержит кириллицу или недопустимые символы.\n\n"
            f"Используйте *только латинские буквы и цифры*, например: `Phone`, `PC`, `iPad`, `TV`\n\n"
            f"Кириллица, пробелы и спецсимволы не поддерживаются.",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    user_name = get_user_name(user_id)
    full_name = f"{user_name}.{device_raw}"

    if os.path.exists(f"{CLIENTS_DIR}/{full_name}.conf"):
        await update.message.reply_text(
            f"❌ Устройство *{device_raw}* уже существует. Введите другое название.",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    await update.message.reply_text(f"⏳ Создаю профиль *{device_raw}*...", parse_mode="Markdown")
    try:
        keys = await create_client(full_name)
    except Exception as e:
        logger.error(f"receive_device_name: create_client failed: {e}")
        await update.message.reply_text(
            f"❌ Не удалось создать устройство *{device_raw}*.\n\n"
            f"`{e}`\n\n"
            f"Попробуйте ещё раз или обратитесь к администратору.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    from handlers.servers import _sync_peer_to_all_slaves
    sync_errors = await _sync_peer_to_all_slaves(
        full_name, keys["pub"], keys["psk"], keys["ip"]
    )
    if sync_errors:
        logger.warning(f"receive_device_name: slave sync errors: {sync_errors}")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Перейти к устройству", callback_data=f"device_{full_name}")],
        [InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")],
    ])
    await update.message.reply_text(
        f"✅ Устройство *{device_raw}* создано!\n\n"
        f"Перейдите в карточку и выберите способ подключения:\n"
        f"• 📄 *.conf* — для AmneziaWG (рекомендуется)\n"
        f"• 📱 *QR-код* — для AmneziaWG на телефоне\n"
        f"• 📤 *Поделиться кодом* — для AmneziaVPN\n\n"
        f"Для каждого варианта можно выбрать канал и настроить исключения сайтов.\n\n"
        f"💡 Первый раз? Загляните в 📖 *Инструкцию* в главном меню — "
        f"там есть важный раздел про настройку DNS на устройстве.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END
