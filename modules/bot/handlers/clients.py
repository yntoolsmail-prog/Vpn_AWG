import os, tempfile, shutil, re, asyncio, subprocess, html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.ext import ContextTypes, ConversationHandler
from awg_core import (
    ADMIN_ID, AWG_IFACE, CLIENTS_DIR, EXCL_EXT, QRENCODE_BIN,
    SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP, SERVER_PORT, TMA_URL,
    can_access_device, create_client, device_short_name,
    gen_obfs, get_all_clients, get_allowed_ips_for_client,
    get_awg_dump, get_combined_awg_dump, get_client_keys, get_client_pub, get_user_clients,
    get_user_name, is_approved, load_client_excl, load_servers,
    make_conf_for_client, make_conf_for_client_ep, make_vpn_link, vpn_qr_texts,
    remove_client_from_awg, resolve_endpoint, save_client_excl,
    fmt_handshake, fmt_bytes, SERVER_PUBLIC,
)
import awg_cascade as cas
from .common import _md, back_kb, sites_keyboard, _tma_button, IMG_BASE, WAITING_DEVICE_NAME, BTN_BACK, BTN_BACK_MENU, BTN_BACK_CARD, BTN_CANCEL, BTN_MY_DEVICES
import logging

logger = logging.getLogger(__name__)


async def show_my_devices(query, user_id: int):
    clients = get_user_clients(user_id)
    peers   = get_combined_awg_dump()

    back_target = "clients_menu" if user_id == ADMIN_ID else "back"

    if not clients:
        kb = [
            [InlineKeyboardButton("➕ Добавить первое устройство", callback_data="add")],
            [InlineKeyboardButton(BTN_BACK_MENU, callback_data=back_target)],
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
    kb.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data=back_target)])
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
        [InlineKeyboardButton("🔑 Получить ключ (AmneziaVPN)", callback_data=f"share_{name}")],
        [InlineKeyboardButton("📱 QR-код (AmneziaVPN)",       callback_data=f"vq1_{name}")],
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

    lines = [f"🌍 Все устройства ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
        ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
        lines.append(f"• {name} | {hs} | ↓{dl} ↑{ul}")

    kb = [[InlineKeyboardButton(f"📋 {name}", callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data="clients_menu")])
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
    return {"conf": "📄", "qr": "📱", "share": "📤", "vq1": "📱", "vq2": "📱"}.get(action, "📄")

def _action_label(action: str) -> str:
    return {"conf": "Скачать .conf", "qr": "QR-код", "share": "Получить ключ",
            "vq1": "QR-код для AmneziaVPN", "vq2": "QR-код для AmneziaVPN — 2 кода"}.get(action, action)

def _usable_options(servers: list = None) -> list:
    """Пункты выдачи (awg_cascade.issue_options), у которых есть адрес: [(пункт, адрес)]."""
    out = []
    for o in cas.issue_options(servers):
        ep = cas.auto_endpoint(o["srv"])
        if ep:
            out.append((o, ep))
    return out


def _option_cb(action: str, opt: dict, name: str) -> str:
    """Данные кнопки пункта: прямой сервер — прежний {action}_auto_{si}_…, пункты
    каскада — {action}_k{K}_… (k0 — «Авто»)."""
    if opt["kind"] == "direct":
        return f"{action}_auto_{opt['si']}_{name}"
    return f"{action}_{opt['key']}_{name}"


async def _show_server_select(query, name: str, user_id: int, action: str):
    """Первый экран выбора: кнопка на каждый пункт выдачи + Расширенная настройка.
    Без каскада пункты — серверы, как раньше; с каскадом — «🌍 Авто», выходы
    через вход и прямые серверы (awg_cascade.issue_options)."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    short = device_short_name(name)
    icon  = _action_icon(action)

    usable = _usable_options(servers)
    if not usable:
        await query.answer("Нет доступных эндпоинтов.", show_alert=True)
        return

    # Один пункт с одним адресом — сразу отправляем без лишних экранов
    if len(usable) == 1 and len(usable[0][0]["srv"].get("endpoints", [])) == 1:
        opt, ep = usable[0]
        await _do_send_action(query, name, action, ep, opt["srv"], opt["slot"])
        return

    rows = []
    # Все пункты разом — для .conf и ссылок; QR так не отдаём: картинка на
    # каждый сервер — это экраны прокрутки
    if action in ("conf", "share") and len(usable) > 1:
        rows.append([InlineKeyboardButton("📦 Получить ВСЕ",
                        callback_data=f"{action}_all_{name}")])
    for opt, _ in usable:
        rows.append([InlineKeyboardButton(opt["label"], callback_data=_option_cb(action, opt, name))])

    rows.append([InlineKeyboardButton("🔧 Расширенная настройка",
                    callback_data=f"{action}_adv_{name}")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data=f"device_{name}")])

    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n\nВыберите сервер:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )


async def _show_ep_select(query, name: str, user_id: int, action: str):
    """Расширенная настройка — полный плоский список всех эндпоинтов."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    short = device_short_name(name)
    icon  = _action_icon(action)

    # Адреса тех серверов, что есть в выдаче: прямые и входы (адрес входа — это «Авто»)
    active = cas.cascade_active(servers)
    all_eps = []
    for si, srv in enumerate(servers):
        if cas.is_entry(srv) and not active:
            continue
        if not cas.is_entry(srv) and not cas.issue_of(srv)["direct"]:
            continue
        for ei, ep in enumerate(srv.get("endpoints", [])):
            all_eps.append((si, ei, srv, ep))

    if not all_eps:
        await query.answer("Нет доступных эндпоинтов.", show_alert=True)
        return

    rows = []
    for si, ei, srv, ep in all_eps:
        emoji  = srv.get("emoji", "🖥")
        sname  = srv.get("name", f"Сервер {si + 1}")
        if cas.is_entry(srv):
            emoji, sname = "🌍", f"вход {sname} → Авто"
        elif active:
            sname += " · напрямую"
        ep_val = ep["value"]
        is_domain = ep.get("type") == "domain"
        type_icon = "🌐" if is_domain else "🔢"
        rows.append([InlineKeyboardButton(
            f"{type_icon} {emoji} {ep_val}  ({sname})",
            callback_data=f"{action}_s{si}_e{ei}_{name}"
        )])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data=f"{action}_{name}")])

    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n\n"
        f"🔧 *Расширенная настройка* — выберите эндпоинт:\n\n"
        f"🌐 *Домен* — рекомендуется: универсальный способ подключения, обеспечит работу даже в случае смены реального IP сервера или переезда на другой сервер.\n\n"
        f"🔢 *IP* — только если домен не работает: прямое подключение к серверу, при смене IP потребуется заново скачать файл конфигурации.\n\n"
        f"_Флаг и название в скобках — страна/сервер, к которому относится эндпоинт._",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )


async def show_conf_ep_select(query, name: str, user_id: int):
    await _show_server_select(query, name, user_id, "conf")

async def show_qr_ep_select(query, name: str, user_id: int):
    await _show_server_select(query, name, user_id, "qr")

async def show_share_ep_select(query, name: str, user_id: int):
    await _show_server_select(query, name, user_id, "share")

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
    srv_clean = re.sub(r'[^\w-]', '', srv_name.replace(' ', '_'))
    parts = name.split(".", 1)
    if len(parts) == 2:
        return f"{parts[0]}.{srv_clean}.{parts[1]}.conf"
    return f"{name}.{srv_clean}.conf"


async def _deny_foreign(query, name: str) -> bool:
    """True — устройство не этого пользователя, выдачу останавливаем. Экран выбора
    сервера права проверяет, но кнопки выдачи (conf_auto_…, qr_s0_e1_…,
    share_all_…) несут имя устройства в своих данных, а данные кнопки приходят
    от клиента — полагаться, что их не подменили, нельзя: в конфиге приватный ключ."""
    if can_access_device(query.from_user.id, name):
        return False
    await query.answer("⛔ Это не ваше устройство.", show_alert=True)
    return True


def _link_html(label: str, ep: str, prt, link: str) -> str:
    """Ссылка vpn:// (~1500 символов) в сворачиваемой цитате: в чате — пара строк
    вместо нескольких экранов, по нажатию раскрывается. Внутри моноширинный текст —
    нажатие по нему копирует ссылку целиком."""
    return (f"{html.escape(label)} — {html.escape(ep)}:{prt}\n"
            f"<blockquote expandable><code>{html.escape(link)}</code></blockquote>")


_LINKS_FOOTER = ("Ссылка свёрнута: нажмите, чтобы раскрыть, нажатие по ссылке копирует её.\n"
                 "В AmneziaVPN: «+» → вставить ссылку.")


async def _send_docs(query, docs: list, caption: str):
    """Файлы одним сообщением-альбомом (по 10 — больше Telegram в альбом не
    берёт), подпись — под последним. Один файл — обычным документом: альбом
    бывает только из 2–10."""
    for i in range(0, len(docs), 10):
        chunk = docs[i:i + 10]
        last  = i + 10 >= len(docs)
        if len(chunk) == 1:
            content, filename = chunk[0]
            await query.message.reply_document(
                document=content, filename=filename,
                caption=caption if last else None,
                parse_mode="Markdown" if last else None)
            continue
        media = [InputMediaDocument(media=content, filename=filename) for content, filename in chunk]
        if last:
            media[-1] = InputMediaDocument(media=chunk[-1][0], filename=chunk[-1][1],
                                           caption=caption, parse_mode="Markdown")
        await query.message.reply_media_group(media=media)


async def send_all(query, name: str, action: str):
    """«📦 Получить ВСЕ»: конфиги на все пункты выдачи разом (_usable_options), у
    каждого — адрес по умолчанию. conf — альбом .conf-файлов одним сообщением;
    share — ссылки одним сообщением в сворачиваемых цитатах (не влезают в 4096
    символов — несколькими)."""
    if await _deny_foreign(query, name):
        return
    targets = []
    for opt, ep in _usable_options():
        srv = opt["srv"]
        targets.append((opt, ep, srv.get("awg_public_key") or SERVER_PUBLIC,
                        str(srv.get("awg_port") or SERVER_PORT)))
    if not targets:
        await query.answer("Нет доступных эндпоинтов.", show_alert=True)
        return
    short = device_short_name(name)

    if action == "conf":
        allowed_ips = await asyncio.get_running_loop().run_in_executor(
            None, get_allowed_ips_for_client, name)
        docs, lines = [], []
        for opt, ep, spub, sprt in targets:
            content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips,
                                               slot=opt["slot"]) or "").encode()
            if not content:
                continue
            docs.append((content, _make_conf_filename(name, opt["tag"])))
            lines.append(f"{_md(opt['label'])} — `{ep}:{sprt}`")
        if not docs:
            await query.message.reply_text(f"❌ Не удалось собрать конфиги для {name}")
            return
        excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
        await _send_docs(query, docs,
                         f"📄 Конфиги *{_md(short)}* для AmneziaWG:\n" + "\n".join(lines)
                         + excl_note + "\n\nИмпортируйте нужный в AmneziaWG.")
    else:
        keys = get_client_keys(name)
        if not keys:
            await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
            return
        blocks = []
        for opt, ep, spub, sprt in targets:
            emoji = opt["emoji"]
            link = make_vpn_link(
                keys["priv"], keys["pub"], cas.slot_ip(keys["ip"], opt["slot"]), keys["psk"],
                keys.get("obfs", gen_obfs()), _vpn_title(name, emoji, opt["kind"]),
                endpoint=ep, server_public=spub, server_port=sprt)
            label = opt["label"]
            blocks.append((len(link) + len(label) + len(ep) + 16, _link_html(label, ep, sprt, link)))
        # Предел сообщения — 4096 символов; ссылка ~1500, так что две-три на сообщение
        header = f"🔗 <b>Ссылки AmneziaVPN — {html.escape(short)}</b>"
        msgs, cur, size = [], [], len(header) + len(_LINKS_FOOTER)
        for n, block in blocks:
            if cur and size + n > 3800:
                msgs.append(cur)
                cur, size = [], len(header) + len(_LINKS_FOOTER)
            cur.append(block)
            size += n
        msgs.append(cur)
        for part in msgs:
            await query.message.reply_text(
                header + "\n\n" + "\n\n".join(part) + "\n\n" + _LINKS_FOOTER,
                parse_mode="HTML", disable_web_page_preview=True)
    await show_device(query, name, query.from_user.id)


def _vpn_title(name: str, emoji: str, kind: str = "") -> str:
    """Название в AmneziaVPN: «🇳🇱 Ivan.Phone», прямой при каскаде — с пометкой,
    чтобы не путать с тем же сервером через вход."""
    title = f"{emoji} {name}".strip() if emoji else name
    return title + " · напрямую" if kind == "direct" and cas.cascade_active(load_servers()) else title


async def _do_send_action(query, name: str, action: str, ep: str, server: dict, slot: int = 0):
    """Выполняет нужное действие (conf/qr/share) с конкретным эндпоинтом и сервером.
    slot — выход каскада: адрес устройства A.B.(C+K).N (awg_cascade.slot_ip)."""
    if await _deny_foreign(query, name):
        return
    spub      = server.get("awg_public_key") or SERVER_PUBLIC
    sprt      = str(server.get("awg_port") or SERVER_PORT)
    srv_name, srv_emoji, _ = cas.describe(server, slot)
    kind = "direct" if not slot and not cas.is_entry(server) else ""
    if action == "conf":
        await do_send_conf_direct(query, name, ep, spub, sprt, srv_name, slot)
    elif action == "qr":
        await do_send_qr_direct(query, name, ep, spub, sprt, srv_name, slot)
    elif action in ("vq1", "vq2"):
        await do_send_vpn_qr(query, name, ep, server, spub, sprt, srv_emoji, slot, kind,
                             series=action == "vq2")
    else:
        await do_send_share_direct(query, name, ep, spub, sprt, srv_name, srv_emoji, slot, kind)


async def send_option(query, name: str, action: str, key: str):
    """Кнопка пункта каскада ({action}_k{K}_{name}): адрес входа по умолчанию и слот K."""
    opt = cas.find_option(key)
    ep = cas.auto_endpoint(opt["srv"]) if opt else None
    if not ep:
        await query.answer("Этот вариант больше недоступен — откройте выбор сервера заново.",
                           show_alert=True)
        return
    await _do_send_action(query, name, action, ep, opt["srv"], opt["slot"])

# ── Финальная отправка .conf ──────────────────────────────────────────────────

async def do_send_conf(query, name: str, ep_key: str):
    """Генерирует .conf в памяти (с сохранёнными исключениями) и отправляет в чат."""
    if await _deny_foreign(query, name):
        return
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
    if await _deny_foreign(query, name):
        return
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    ep      = resolve_endpoint(ep_key)
    content = (make_conf_for_client(name, ep, allowed_ips) or "").encode()
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)

    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        # Ни конфиг, ни QR на диск не пишем: в конфиге приватный ключ, а /tmp
        # читается любым процессом. qrencode умеет stdin → stdout.
        safe_name = name.replace(".", "_")
        await query.message.reply_document(
            document=content,
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
            png = subprocess.check_output(
                [QRENCODE_BIN, "-t", "PNG", "-s", "6", "-o", "-"], input=content
            )
            await query.message.reply_photo(
                photo=png,
                caption=(
                    f"📱 QR для AmneziaWG — *{short}* ({ep_label})\n"
                    f"🌐 Endpoint: `{ep}:{SERVER_PORT}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    await show_device(query, name, query.from_user.id)

# ── Финальная отправка vpn:// (AmneziaVPN) ───────────────────────────────────

async def do_send_share(query, name: str, ep_key: str):
    """Генерирует vpn:// ссылку на лету, отправляет в чат."""
    if await _deny_foreign(query, name):
        return
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
    await query.message.reply_text(
        f"🔗 <b>Ссылка AmneziaVPN — {html.escape(short)}</b>\n\n"
        + _link_html(ep_label, ep, SERVER_PORT, vpn_link) + "\n\n" + _LINKS_FOOTER,
        parse_mode="HTML", disable_web_page_preview=True
    )
    await show_device(query, name, query.from_user.id)

# ── Прямая отправка с конкретным эндпоинтом (мультисервер) ───────────────────

async def do_send_conf_direct(query, name: str, ep: str,
                               spub: str = None, sprt: str = None,
                               srv_name: str = None, slot: int = 0):
    """Отправляет .conf с указанным эндпоинтом и параметрами сервера."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips, slot=slot) or "").encode()
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
                             srv_name: str = None, slot: int = 0):
    """Отправляет QR с указанным эндпоинтом."""
    allowed_ips = await asyncio.get_running_loop().run_in_executor(None, get_allowed_ips_for_client, name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips, slot=slot) or "").encode()
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        # Без временных файлов — в конфиге приватный ключ, а /tmp читаем всеми
        await query.message.reply_document(
            document=content,
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
            png = subprocess.check_output(
                [QRENCODE_BIN, "-t", "PNG", "-s", "6", "-o", "-"], input=content
            )
            await query.message.reply_photo(
                photo=png,
                caption=(
                    f"📱 QR для AmneziaWG — *{short}*\n"
                    f"🌐 Endpoint: `{ep}:{prt}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    await show_device(query, name, query.from_user.id)

def _device_vpn_link(name: str, ep: str, spub: str, sprt: str, srv_emoji: str,
                     slot: int, kind: str) -> str | None:
    """Ссылка vpn:// устройства; None — ключи не прочитать."""
    keys = get_client_keys(name)
    if not keys:
        return None
    # Название в приложении AmneziaVPN: "🇳🇱 Admin.Nout"
    return make_vpn_link(
        keys["priv"], keys["pub"], cas.slot_ip(keys["ip"], slot), keys["psk"],
        keys.get("obfs", gen_obfs()), _vpn_title(name, srv_emoji, kind),
        endpoint=ep, server_public=spub, server_port=sprt
    )


def _vpn_qr_series_cb(name: str, server: dict, ep: str, slot: int) -> str | None:
    """Кнопка «2 кода» под одиночным QR: тот же пункт и адрес. Слот K приходит
    только с кнопки пункта (kK — адрес входа по умолчанию), остальное — сервер и
    адрес из списка; не нашли или длиннее 64 байт (предел Telegram) — без кнопки."""
    if slot:
        cb = f"vq2_k{slot}_{name}"
    else:
        servers = load_servers()
        si = next((i for i, s in enumerate(servers) if s.get("id") == server.get("id")), None)
        eps = servers[si].get("endpoints", []) if si is not None else []
        ei = next((i for i, e in enumerate(eps) if e.get("value") == ep), None)
        if ei is None:
            return None
        cb = f"vq2_s{si}_e{ei}_{name}"
    return cb if len(cb.encode()) <= 64 else None


async def do_send_vpn_qr(query, name: str, ep: str, server: dict, spub: str, sprt: str,
                         srv_emoji: str, slot: int, kind: str, series: bool):
    """QR для сканера AmneziaVPN (vpn_qr_texts), содержимое — как у ссылки vpn://.
    Сначала один плотный код и под ним кнопка «не читается»: она присылает тот же
    ключ серией — каждый код отдельным сообщением во всю ширину, подпись только
    под последним, чтобы между кодами не было текста. Альбом ставил коды в ряд, а
    одна общая картинка — друг под другом, и в обоих случаях они мельчали. Сканер
    собирает куски сам: наводят на коды по очереди или разом, отдельного экрана
    «отсканировано 1 из 2» на Android нет."""
    link = _device_vpn_link(name, ep, spub, sprt, srv_emoji, slot, kind)
    if not link:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short = device_short_name(name)
    prt   = sprt or SERVER_PORT
    head  = (f"📱 QR для AmneziaVPN — *{_md(short)}*\n"
             f"🌐 Endpoint: `{ep}:{prt}`\n\n"
             "В AmneziaVPN: добавить сервер → «QR-код»")
    try:
        texts = vpn_qr_texts(link, series)
        # Без файлов на диске — в конфиге приватный ключ
        pngs = [subprocess.check_output([QRENCODE_BIN, "-t", "PNG", "-l", "L", "-s", "8", "-o", "-"],
                                        input=t.encode()) for t in texts]
        if len(pngs) == 1:
            # У 2.0 серия — тот же один код: кнопка «2 кода» ничего бы не дала
            cb = None if series or len(vpn_qr_texts(link, True)) < 2 else \
                _vpn_qr_series_cb(name, server, ep, slot)
            await query.message.reply_photo(
                photo=pngs[0], parse_mode="Markdown",
                caption=head + ", наведите камеру." + (
                    "\n\nНе считывается с экрана — нажмите кнопку ниже: тот же ключ "
                    "двумя кодами покрупнее." if cb else ""),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔍 Не читается — 2 кода крупнее", callback_data=cb)]]) if cb else None)
        else:
            for i, png in enumerate(pngs):
                last = i == len(pngs) - 1
                await query.message.reply_photo(
                    photo=png, parse_mode="Markdown" if last else None,
                    caption=f"{head} и наведите камеру на коды по очереди или на оба разом."
                    if last else None)
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    # Кнопка «2 кода» висит под фото: карточку там не перерисовать — в фото нет текста
    if not getattr(query.message, "photo", None):
        await show_device(query, name, query.from_user.id)


async def do_send_share_direct(query, name: str, ep: str,
                                spub: str = None, sprt: str = None,
                                srv_name: str = None, srv_emoji: str = "",
                                slot: int = 0, kind: str = ""):
    """Отправляет vpn:// ссылку с указанным эндпоинтом."""
    vpn_link = _device_vpn_link(name, ep, spub, sprt, srv_emoji, slot, kind)
    if not vpn_link:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short    = device_short_name(name)
    prt      = sprt or SERVER_PORT
    label = " ".join(p for p in (srv_emoji, srv_name) if p) or "Сервер"
    await query.message.reply_text(
        f"🔗 <b>Ссылка AmneziaVPN — {html.escape(short)}</b>\n\n"
        + _link_html(label, ep, prt, vpn_link) + "\n\n" + _LINKS_FOOTER,
        parse_mode="HTML", disable_web_page_preview=True
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
    # Раньше ошибка синка уходила только в лог — устройство молча не появлялось
    # на слейве, и узнать об этом было неоткуда.
    sync_note = ""
    if sync_errors:
        logger.warning(f"receive_device_name: slave sync errors: {sync_errors}")
        sync_note = (
            f"\n\n⚠️ Устройство не доехало до {len(sync_errors)} сервер(ов) — "
            f"через них подключиться не получится. Администратор уведомлён."
        )
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Устройство не синхронизировалось*\n\n"
                f"`{full_name}` создано на основном сервере, но не доехало:\n"
                + "\n".join(f"• {_md(e)}" for e in sync_errors)
                + "\n\nПочините связь и нажмите «Синхронизировать» в карточке сервера.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🖥 Серверы", callback_data="servers")
                ]]),
            )
        except Exception:
            pass

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Перейти к устройству", callback_data=f"device_{full_name}")],
        [InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")],
    ])
    await update.message.reply_text(
        f"✅ Устройство *{device_raw}* создано!\n\n"
        f"Перейдите в карточку и выберите способ подключения:\n"
        f"• 📄 *.conf* — для AmneziaWG (рекомендуется)\n"
        f"• 📱 *QR-код* — для AmneziaWG на телефоне\n"
        f"• 🔑 *Получить ключ* или 📱 *QR-код* — для AmneziaVPN\n\n"
        f"Для каждого варианта можно выбрать канал и настроить исключения сайтов.\n\n"
        f"💡 Первый раз? Загляните в 📖 *Инструкцию* в главном меню — "
        f"там есть важный раздел про настройку DNS на устройстве."
        f"{sync_note}",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END
