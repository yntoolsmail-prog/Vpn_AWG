import asyncio, re, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from awg_core import (
    ADMIN_ID, AWG_IFACE, AWG_CONF, SERVER_IP, SERVER_PORT, SERVER_PUBLIC,
    SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP,
    PARAMIKO_AVAILABLE as _PARAMIKO_AVAILABLE,
    get_all_clients, get_client_pub, load_servers, save_servers,
    invalidate_servers_cache, resolve_endpoint,
    read_iface_bytes, get_system_stats, load_bw_peak, get_combined_awg_dump, fmt_bytes,
    ssh_read_slave_env      as _ssh_read_slave_env,
    ssh_clone_awg_to_slave  as _ssh_clone_awg_to_slave,
    ssh_sync_peer_to_slave  as _ssh_sync_peer_to_slave,
    ssh_sync_all_clients_to_slave as _ssh_sync_all_clients_to_slave,
    ssh_stop_slave_awg      as _ssh_stop_slave_awg,
    ssh_get_slave_peer_count as _ssh_get_slave_peer_count,
    ssh_get_slave_sys_stats as _ssh_get_slave_sys_stats,
)
from .common import _md, back_kb, WAITING_SRV_DOMAIN, WAITING_SRV_EDIT_NAME, WAITING_SRV_EDIT_EMOJI, WAITING_SRV_COUNTRY, BTN_BACK, BTN_BACK_MENU, BTN_CANCEL, BTN_BACK_CARD


def _count_peers_in_conf(conf_text: str) -> int:
    """Считает количество [Peer] блоков в awg0.conf."""
    return conf_text.count("\n[Peer]") + (1 if conf_text.startswith("[Peer]") else 0)


def _fmt_gb(b: int) -> str:
    if b < 1024 ** 3:
        return f"{b / 1024**2:.0f} MB"
    return f"{b / 1024**3:.2f} GB"


def _srv_block_primary() -> str:
    """Блок статистики для основного сервера."""
    from .bandwidth import get_primary_bw
    sys_s  = get_system_stats()
    bw     = get_primary_bw()
    rx, tx = read_iface_bytes(AWG_IFACE)
    now    = int(time.time())

    dump   = get_combined_awg_dump()
    online = sum(1 for p in dump.values()
                 if not p.get("server") and p.get("handshake") and now - p["handshake"] < 180)

    ram_pct = round(sys_s["ram_used"] / sys_s["ram_total"] * 100) if sys_s.get("ram_total") else 0

    awg_down = bw.get("awg_down", 0)
    awg_up   = bw.get("awg_up", 0)

    lines = [
        f"🟢 AWG: работает",
        f"🖥 IP: {SERVER_IP}:{SERVER_PORT}",
        f"⏱ Uptime: {sys_s['uptime']}",
        "",
        f"💾 RAM: {ram_pct}%  💿 Диск: {sys_s['disk_pct']}%",
        f"⬇️ {awg_down} / ⬆️ {awg_up} Mbit/s",
        f"🟢 Онлайн: {online}",
        f"📊 Трафик (с перезагрузки): ↓{_fmt_gb(tx)} ↑{_fmt_gb(rx)}",
    ]
    return "\n".join(lines)


async def _srv_block_slave(srv: dict, idx: int) -> str:
    """Блок статистики для slave-сервера (SSH)."""
    from .bandwidth import get_slave_bw_detail

    sid   = srv.get("id") or srv.get("name", "")
    now   = int(time.time())
    dump  = get_combined_awg_dump()
    label = f"{srv.get('emoji', '')} {srv.get('name', 'Slave')}".strip()
    online = sum(1 for p in dump.values()
                 if p.get("server") == label and p.get("handshake") and now - p["handshake"] < 180)

    bw_d   = get_slave_bw_detail().get(sid, {})
    awg_down = bw_d.get("awg_down", 0)
    awg_up   = bw_d.get("awg_up", 0)

    ssh_ip   = srv.get("ssh", {}).get("ip", "—")
    awg_port = srv.get("awg_port", 51820)

    if _PARAMIKO_AVAILABLE:
        try:
            stats = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _ssh_get_slave_sys_stats, srv),
                timeout=8.0
            )
        except Exception:
            stats = {}
    else:
        stats = {}

    if stats:
        awg_icon = "🟢" if stats["awg_ok"] else "🔴"
        awg_line = f"{awg_icon} AWG: {'работает' if stats['awg_ok'] else 'не работает'}"
        uptime   = stats.get("uptime", "—")
        ram_pct  = stats.get("ram_pct", 0)
        disk_pct = stats.get("disk_pct", 0)
        rx_b     = stats.get("rx_bytes", 0)
        tx_b     = stats.get("tx_bytes", 0)
        traffic  = f"↓{_fmt_gb(tx_b)} ↑{_fmt_gb(rx_b)}"
    else:
        awg_line = "⚠️ AWG: нет данных"
        uptime   = "—"
        ram_pct  = disk_pct = 0
        traffic  = "—"

    lines = [
        awg_line,
        f"🖥 IP: {ssh_ip}:{awg_port}",
        f"⏱ Uptime: {uptime}",
        "",
        f"💾 RAM: {ram_pct}%  💿 Диск: {disk_pct}%",
        f"⬇️ {awg_down} / ⬆️ {awg_up} Mbit/s",
        f"🟢 Онлайн: {online}",
        f"📊 Трафик (с перезагрузки): {traffic}",
    ]
    return "\n".join(lines)


async def show_servers_list(query):
    """Список всех VPS-серверов со статистикой в шапке."""
    try:
        from bot import _modules
    except ImportError:
        _modules = None

    servers = load_servers()

    # Собираем статблоки параллельно (основной — синхронно, slaves — async)
    slave_blocks: dict[int, str] = {}
    if _PARAMIKO_AVAILABLE:
        tasks = {
            i: asyncio.ensure_future(_srv_block_slave(srv, i))
            for i, srv in enumerate(servers)
            if not srv.get("is_primary")
        }
        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for (i, _), result in zip(tasks.items(), done):
                slave_blocks[i] = result if isinstance(result, str) else "⚠️ нет данных"

    lines = ["🖥 *Серверы*\n"]
    for i, srv in enumerate(servers):
        emoji   = srv.get("emoji", "🖥")
        name    = srv.get("name", f"Сервер {i+1}")
        is_pri  = srv.get("is_primary", False)
        role    = "Основной" if is_pri else "Слейв"

        eps         = srv.get("endpoints", [])
        domain_eps  = [e["value"] for e in eps if e.get("type") == "domain"]
        ep_text     = ", ".join(domain_eps) if domain_eps else "нет доменов"

        lines.append(f"{emoji} {name} *({role})*")
        if is_pri:
            lines.append(_srv_block_primary())
        else:
            lines.append(slave_blocks.get(i, "⚠️ нет данных"))
        lines.append(f"\nЕндпоинты: {ep_text}\n")

    rows = []
    for i, srv in enumerate(servers):
        emoji = srv.get("emoji", "🖥")
        sname = srv.get("name", f"Сервер {i+1}")
        rows.append([InlineKeyboardButton(
            f"{emoji} {sname}", callback_data=f"srv_card_{i}"
        )])
    if _modules and any(getattr(m, "__name__", "") == "modules.slave_servers" for m in _modules.modules):
        rows.append([InlineKeyboardButton("➕ Добавить сервер", callback_data="srv_add")])
    rows.append([InlineKeyboardButton("🔍 Проверить DNS", callback_data="srv_checkdns")])
    rows.append([InlineKeyboardButton("🗑 Удалить домен", callback_data="srv_deldomain_list")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="settings_menu")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )

async def srv_deldomain_list(query):
    """Показывает все домены во всех серверах для удаления."""
    servers = load_servers()
    rows = []
    for si, srv in enumerate(servers):
        for ep in srv.get("endpoints", []):
            emoji = srv.get("emoji", "🖥")
            sname = srv.get("name", f"Сервер {si+1}")
            val   = ep["value"]
            rows.append([InlineKeyboardButton(
                f"🗑 {emoji} {val}  ({sname})",
                callback_data=f"srv_deldomain_confirm_{val}"
            )])
    if not rows:
        await query.answer("Нет доменов для удаления.", show_alert=True)
        return
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="servers")])
    await query.edit_message_text(
        "🗑 *Удалить домен из системы*\n\nВыберите домен:",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )


async def srv_deldomain_confirm(query, domain: str):
    """Запрашивает подтверждение удаления домена."""
    servers = load_servers()
    # Находим сервер-владелец
    owner = None
    for srv in servers:
        if any(e["value"] == domain for e in srv.get("endpoints", [])):
            owner = f"{srv.get('emoji', '')} {srv.get('name', '')}".strip()
            break
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"srv_deldomain_ok_{domain}")],
        [InlineKeyboardButton(BTN_BACK,        callback_data="srv_deldomain_list")],
    ])
    owner_note = f"\nСейчас привязан к: *{owner}*" if owner else ""
    await query.edit_message_text(
        f"Удалить домен `{domain}`?{owner_note}",
        reply_markup=kb,
        parse_mode="Markdown"
    )


async def srv_deldomain_ok(query, domain: str):
    """Удаляет домен из всех серверов."""
    servers = load_servers()
    removed_from = []
    for si, srv in enumerate(servers):
        old_eps = srv.get("endpoints", [])
        new_eps = [e for e in old_eps if e["value"] != domain]
        if len(new_eps) < len(old_eps):
            removed_from.append(f"{srv.get('emoji', '')} {srv.get('name', '')}".strip())
            srv["endpoints"] = new_eps
            servers[si] = srv
    save_servers(servers)
    note = f" (был на: {', '.join(removed_from)})" if removed_from else ""
    await query.edit_message_text(
        f"✅ Домен `{domain}` удалён{note}.",
        reply_markup=back_kb("servers"),
        parse_mode="Markdown"
    )


async def show_server_card(query, srv_idx: int):
    """Карточка конкретного VPS."""
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv    = servers[srv_idx]
    emoji  = srv.get("emoji", "🖥")
    name   = srv.get("name", f"Сервер {srv_idx+1}")
    is_pri = srv.get("is_primary", False)
    ssh    = srv.get("ssh", {})
    port   = srv.get("awg_port", SERVER_PORT)

    eps = srv.get("endpoints", [])
    ep_lines = []
    for ep in eps:
        if ep.get("type") == "domain":
            v_mark = " ✅" if ep.get("verified") else ""
            ep_lines.append(f"  🌐 {ep['value']}{v_mark}")
    ep_text = "\n".join(ep_lines) if ep_lines else "  нет доменов"

    text = (
        f"{emoji} *{name}*" + (" _(Основной)_" if is_pri else " _(Слейв)_") + "\n"
        f"SSH: `{ssh.get('login', 'root')}@{ssh.get('ip', '—')}:{ssh.get('port', 22)}`\n"
        f"AWG-порт: `{port}`\n\n"
        f"*Эндпоинты:*\n{ep_text}"
    )

    rows = [
        [InlineKeyboardButton("➕ Добавить домен", callback_data=f"srv_adddomain_{srv_idx}")],
        [InlineKeyboardButton("✏️ Переименовать", callback_data=f"srv_rename_{srv_idx}")],
    ]

    if not is_pri:
        loop = asyncio.get_event_loop()
        slave_peers = await loop.run_in_executor(None, _ssh_get_slave_peer_count, srv)
        try:
            with open(AWG_CONF) as f:
                primary_peers = _count_peers_in_conf(f.read())
        except Exception:
            primary_peers = 0

        if slave_peers is None:
            sync_line = "⚠️ Нет связи со slave"
            sync_icon = "🔄"
        elif slave_peers == primary_peers:
            sync_line = f"✅ Синхронизирован ({slave_peers} клиентов)"
            sync_icon = "🔄"
        else:
            sync_line = f"❗ Рассинхрон: primary {primary_peers}, slave {slave_peers}"
            sync_icon = "🔄 Синхронизировать"

        text += f"\n\n*Синхронизация:* {sync_line}"
        rows.append([InlineKeyboardButton(sync_icon, callback_data=f"srv_sync_{srv_idx}")])
        rows.append([InlineKeyboardButton("🗑 Удалить сервер", callback_data=f"srv_del_{srv_idx}")])

    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="servers")])
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )

async def _sync_peer_to_all_slaves(name: str, pub: str, psk: str, ip: str) -> list:
    """Синхронизирует нового клиента со всеми slave-серверами. Возвращает список ошибок."""
    if not _PARAMIKO_AVAILABLE:
        return []
    slaves = [s for s in load_servers() if not s.get("is_primary")]
    if not slaves:
        return []
    errors = []
    loop = asyncio.get_event_loop()
    for srv in slaves:
        sname = f"{srv.get('emoji', '')} {srv.get('name', 'Сервер')}".strip()
        try:
            await loop.run_in_executor(None, _ssh_sync_peer_to_slave, srv, name, pub, psk, ip)
        except Exception as e:
            errors.append(f"{sname}: {e}")
    return errors


async def _dns_check_all_servers() -> tuple[list, bool]:
    """Проверяет DNS всех domain-эндпоинтов, обновляет флаг verified. Возвращает (results, changed)."""
    import socket as _s
    loop = asyncio.get_event_loop()

    def _resolve(domain):
        try:
            return _s.gethostbyname(domain)
        except Exception:
            return None

    servers = load_servers()
    results = []
    changed = False
    for srv in servers:
        srv_ip = srv.get("ssh", {}).get("ip", "")
        for ep in srv.get("endpoints", []):
            if ep.get("type") != "domain":
                continue
            resolved = await loop.run_in_executor(None, _resolve, ep["value"])
            now_ok   = bool(resolved and srv_ip and resolved == srv_ip)
            was_ok   = ep.get("verified", False)
            if was_ok != now_ok:
                ep["verified"] = now_ok
                changed = True
            results.append({
                "srv_label": f"{srv.get('emoji','🖥')} {srv.get('name','Сервер')}".strip(),
                "domain":    ep["value"],
                "resolved":  resolved,
                "expected":  srv_ip,
                "now_ok":    now_ok,
                "was_ok":    was_ok,
            })
    if changed:
        save_servers(servers)
    return results, changed


# Последнее известное состояние синка по каждому слейву: {srv_id: "ok" | "bad"}
_slave_sync_state: dict = {}


async def _check_slaves_sync(context):
    """Фоновая сверка числа пиров primary ↔ каждый slave.

    Расхождение означает, что часть устройств через этот слейв не работает.
    Раньше увидеть его можно было, только зайдя в Настройки → Серверы, — теперь
    бот сам пишет админу при появлении расхождения и при его устранении.
    """
    if not _PARAMIKO_AVAILABLE:
        return
    try:
        with open(AWG_CONF) as f:
            primary_peers = _count_peers_in_conf(f.read())
    except Exception:
        return
    if primary_peers == 0:
        return

    loop = asyncio.get_event_loop()
    for idx, srv in enumerate(load_servers()):
        if srv.get("is_primary"):
            continue
        sid   = srv.get("id") or srv.get("name", "")
        label = f"{srv.get('emoji', '')} {srv.get('name', 'Слейв')}".strip()
        try:
            cnt = await asyncio.wait_for(
                loop.run_in_executor(None, _ssh_get_slave_peer_count, srv),
                timeout=15.0,
            )
        except Exception:
            cnt = None
        # Нет связи — не спамим: это видно в карточке сервера и в статусе
        if cnt is None:
            continue

        state = "ok" if cnt == primary_peers else "bad"
        prev  = _slave_sync_state.get(sid)
        _slave_sync_state[sid] = state
        if state == prev:
            continue

        if state == "bad":
            diff = abs(primary_peers - cnt)
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Рассинхрон со слейвом*\n\n"
                f"*{_md(label)}*\n"
                f"На основном: {primary_peers} устройств\n"
                f"На слейве:   {cnt}\n\n"
                f"Расхождение в {diff} — эти устройства через данный сервер "
                f"работать не будут.\n"
                f"Синхронизация перезальёт конфиг с основного сервера.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Синхронизировать", callback_data=f"srv_sync_{idx}")],
                    [InlineKeyboardButton("🖥 Серверы", callback_data="servers")],
                ]),
            )
        elif prev == "bad":
            await context.bot.send_message(
                ADMIN_ID,
                f"✅ *{_md(label)}* снова синхронизирован — {cnt} устройств.",
                parse_mode="Markdown",
            )


async def _check_endpoint_dns(context):
    """Фоновая проверка DNS — уведомляет админа если домен перестал указывать на нужный IP."""
    results, _ = await _dns_check_all_servers()
    for r in results:
        if r["was_ok"] and not r["now_ok"]:
            await context.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Домен отвязался от сервера*\n"
                f"Сервер: *{r['srv_label']}*\n"
                f"Домен: `{r['domain']}`\n"
                f"DNS → `{r['resolved']}`, ожидался `{r['expected']}`",
                parse_mode="Markdown"
            )


async def srv_checkdns(query):
    """Ручная проверка DNS всех domain-эндпоинтов с отчётом по каждому."""
    await query.edit_message_text("🔍 Проверяю DNS…")
    results, _ = await _dns_check_all_servers()

    if not results:
        await query.edit_message_text(
            "Нет domain-эндпоинтов для проверки.",
            reply_markup=back_kb("servers")
        )
        return

    by_srv = {}
    for r in results:
        by_srv.setdefault(r["srv_label"], []).append(r)

    lines = ["🔍 *DNS эндпоинты*\n"]
    for srv_label, items in by_srv.items():
        lines.append(f"*{srv_label}*")
        for r in items:
            if r["now_ok"]:
                lines.append(f"  ✅ `{r['domain']}` → `{r['resolved']}`")
            elif r["resolved"]:
                lines.append(f"  ❌ `{r['domain']}` → `{r['resolved']}` (ожидался `{r['expected']}`)")
            else:
                lines.append(f"  ❌ `{r['domain']}` — не разрешился")
        lines.append("")

    await query.edit_message_text(
        "\n".join(lines).rstrip(),
        reply_markup=back_kb("servers"),
        parse_mode="Markdown"
    )


async def srv_del_confirm(query, srv_idx: int):
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers[srv_idx]
    if srv.get("is_primary"):
        await query.answer("Нельзя удалить PRIMARY сервер.", show_alert=True)
        return
    emoji = srv.get("emoji", "🖥")
    name  = srv.get("name", f"Сервер {srv_idx+1}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"srv_del_ok_{srv_idx}")],
        [InlineKeyboardButton(BTN_BACK,        callback_data=f"srv_card_{srv_idx}")],
    ])
    await query.edit_message_text(
        f"Удалить сервер *{emoji} {name}*?",
        reply_markup=kb, parse_mode="Markdown"
    )

async def srv_del_ok(query, srv_idx: int):
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers[srv_idx]
    if srv.get("is_primary"):
        await query.answer("Нельзя удалить PRIMARY сервер.", show_alert=True)
        return
    name  = srv.get("name", "Сервер")
    emoji = srv.get("emoji", "🖥")

    stop_note = ""
    if _PARAMIKO_AVAILABLE:
        ssh = srv.get("ssh", {})
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _ssh_stop_slave_awg, ssh
            )
            stop_note = "\n✅ AWG на slave остановлен."
        except Exception as e:
            stop_note = f"\n⚠️ Не удалось остановить AWG на slave: {e}"

    # Переносим domain-эндпоинты удалённого slave на PRIMARY (не IP — они slave-специфичны)
    moved_note = ""
    domain_eps = [ep for ep in srv.get("endpoints", []) if ep.get("type") == "domain"]
    if domain_eps:
        primary = next((s for s in servers if s.get("is_primary")), None)
        if primary:
            existing = {e["value"] for e in primary.get("endpoints", [])}
            moved = []
            for ep in domain_eps:
                if ep["value"] not in existing:
                    primary.setdefault("endpoints", []).append(ep)
                    moved.append(ep["value"])
            if moved:
                moved_note = "\n📌 Домены перенесены на PRIMARY: " + ", ".join(f"`{d}`" for d in moved)

    servers.pop(srv_idx)
    save_servers(servers)
    await query.edit_message_text(
        f"✅ Сервер *{emoji} {name}* удалён.{stop_note}{moved_note}",
        reply_markup=back_kb("servers"),
        parse_mode="Markdown"
    )


async def srv_sync_now(query, srv_idx: int):
    """Принудительная синхронизация primary → slave."""
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers[srv_idx]
    if srv.get("is_primary"):
        await query.answer("Это PRIMARY сервер.", show_alert=True)
        return
    name  = srv.get("name", "Сервер")
    emoji = srv.get("emoji", "🖥")

    await query.edit_message_text(
        f"🔄 Синхронизирую *{emoji} {name}*...",
        parse_mode="Markdown"
    )
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _ssh_clone_awg_to_slave, srv)
        await query.edit_message_text(
            f"✅ *{emoji} {name}* синхронизирован с primary.\n\nВсе клиенты скопированы, AWG перезапущен.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
            ]])
        )
    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка синхронизации *{emoji} {name}*:\n`{e}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
            ]])
        )


# ── Переименование сервера ────────────────────────────────────────────────────

async def srv_rename_start(update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога переименования сервера (entry point ConversationHandler)."""
    query = update.callback_query
    await query.answer()
    srv_idx = int(query.data.split("_")[-1])
    context.user_data["srv_rename"] = {"srv_idx": srv_idx}
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.edit_message_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]
    await query.edit_message_text(
        f"✏️ *Переименование сервера*\n\n"
        f"Текущее название: *{srv.get('emoji', '')} {srv.get('name', '')}*\n\n"
        f"Введите новое название (например: NLD, FIN, RUS)\nили /cancel для отмены:",
        parse_mode="Markdown"
    )
    return WAITING_SRV_EDIT_NAME


async def srv_rename_name(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["srv_rename"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите эмодзи/флаг (например: 🇳🇱), или отправьте пробел чтобы оставить текущий:"
    )
    return WAITING_SRV_EDIT_EMOJI


async def srv_rename_emoji(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["srv_rename"]["emoji"] = update.message.text.strip()
    srv_idx = context.user_data["srv_rename"]["srv_idx"]
    servers = load_servers()
    current_country = servers[srv_idx].get("country", "") if srv_idx < len(servers) else ""
    hint = f" (текущее: {current_country})" if current_country else " (не задано)"
    await update.message.reply_text(
        f"Введите название страны на русском (например: Голландия, Финляндия),\n"
        f"или пробел чтобы оставить текущее{hint}:"
    )
    return WAITING_SRV_COUNTRY


async def srv_rename_country(update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.pop("srv_rename", {})
    srv_idx     = d.get("srv_idx", 0)
    new_name    = d.get("name", "").strip()
    new_emoji   = d.get("emoji", "").strip()
    new_country = update.message.text.strip()

    servers = load_servers()
    if srv_idx >= len(servers):
        await update.message.reply_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]
    if new_name:    srv["name"]    = new_name
    if new_emoji:   srv["emoji"]   = new_emoji
    if new_country: srv["country"] = new_country
    servers[srv_idx] = srv
    save_servers(servers)
    await update.message.reply_text(
        f"✅ Сервер обновлён: *{srv['emoji']} {srv['name']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
        ]])
    )
    return ConversationHandler.END


# ── Добавление домена к серверу ───────────────────────────────────────────────

async def srv_adddomain_start(update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога добавления домена к серверу."""
    query = update.callback_query
    await query.answer()
    srv_idx = int(query.data.split("_")[-1])
    context.user_data["srv_domain"] = {"srv_idx": srv_idx}

    servers = load_servers()
    all_eps = []
    for si, srv in enumerate(servers):
        for ep in srv.get("endpoints", []):
            val = ep["value"]
            # Не предлагаем то, что уже есть на этом сервере
            if srv_idx < len(servers) and any(
                e["value"] == val for e in servers[srv_idx].get("endpoints", [])
            ):
                continue
            emoji = srv.get("emoji", "🖥")
            sname = srv.get("name", f"Сервер {si+1}")
            all_eps.append((si, val, emoji, sname))

    context.user_data["srv_domain"]["all_eps"] = [(v, e, s) for _, v, e, s in all_eps]

    rows = []
    for i, (val, emoji, sname) in enumerate(context.user_data["srv_domain"]["all_eps"]):
        rows.append([InlineKeyboardButton(
            f"{emoji} {val}  ({sname})",
            callback_data=f"srv_ep_pick_{i}"
        )])

    prompt = "🌐 *Добавить эндпоинт*\n\nВведите домен или IP-адрес:"
    if rows:
        prompt = "🌐 *Добавить эндпоинт*\n\nВыберите из существующих или введите новый домен/IP:"

    await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(rows) if rows else None, parse_mode="Markdown")
    return WAITING_SRV_DOMAIN


async def srv_adddomain_pick(update, context: ContextTypes.DEFAULT_TYPE):
    """Быстрый выбор существующего эндпоинта для назначения серверу."""
    import socket as _sock
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_")[-1])
    d = context.user_data.get("srv_domain", {})
    srv_idx = d.get("srv_idx", 0)
    all_eps = d.get("all_eps", [])

    if idx >= len(all_eps):
        await query.edit_message_text("❌ Эндпоинт не найден.")
        return ConversationHandler.END

    domain, _, _ = all_eps[idx]
    context.user_data.pop("srv_domain", None)

    servers = load_servers()
    if srv_idx >= len(servers):
        await query.edit_message_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]
    srv_ip = srv.get("ssh", {}).get("ip", "")

    verified = False
    dns_ip = None
    try:
        dns_ip = _sock.gethostbyname(domain)
        verified = (dns_ip == srv_ip) if srv_ip else False
    except Exception:
        pass

    # Снимаем с других серверов если там уже есть
    transferred_from = None
    for other_idx, other_srv in enumerate(servers):
        if other_idx == srv_idx:
            continue
        old_eps = other_srv.get("endpoints", [])
        new_eps = [e for e in old_eps if e["value"] != domain]
        if len(new_eps) < len(old_eps):
            transferred_from = f"{other_srv.get('emoji', '')} {other_srv.get('name', '')}".strip()
            other_srv["endpoints"] = new_eps
            servers[other_idx] = other_srv

    eps = srv.get("endpoints", [])
    if not any(e["value"] == domain for e in eps):
        eps.append({"value": domain, "type": "domain" if "." in domain and not domain.replace(".", "").isdigit() else "ip", "verified": verified})
        srv["endpoints"] = eps
        servers[srv_idx] = srv
        save_servers(servers)

    transfer_note = f"\n↩️ Снят с сервера *{transferred_from}*" if transferred_from else ""
    dns_note = f"✅ DNS → `{dns_ip}`" if verified else (f"⚠️ DNS → `{dns_ip}`" if dns_ip else "⚠️ DNS не разрешился")
    await query.edit_message_text(
        f"✅ `{domain}` привязан к *{srv.get('name', '')}*\n{dns_note}{transfer_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
        ]])
    )
    return ConversationHandler.END

async def srv_adddomain_receive(update, context: ContextTypes.DEFAULT_TYPE):
    """Получает домен, проверяет DNS, добавляет к серверу."""
    import socket as _sock
    # ВАЖНО: не .lstrip("https://") — lstrip срезает НАБОР символов, а не префикс,
    # и съедал первые буквы домена: test.ru → est.ru, shop.example.com → op.example.com
    domain = re.sub(r"^https?://", "", update.message.text.strip().lower()).split("/")[0]
    d = context.user_data.pop("srv_domain", {})
    srv_idx = d.get("srv_idx", 0)

    servers = load_servers()
    if srv_idx >= len(servers):
        await update.message.reply_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]

    # Попытка DNS-резолва — проверяем только против IP текущего сервера
    verified = False
    dns_ip   = None
    srv_ip   = srv.get("ssh", {}).get("ip", "")
    try:
        dns_ip = _sock.gethostbyname(domain)
        verified = (dns_ip == srv_ip) if srv_ip else False
    except Exception:
        pass

    eps = srv.get("endpoints", [])
    # Уже есть у этого сервера — ничего не делаем
    if any(e["value"] == domain for e in eps):
        await update.message.reply_text(
            f"⚠️ Домен `{domain}` уже есть у этого сервера.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
            ]])
        )
        return ConversationHandler.END

    # Если домен числится за другим сервером — снимаем его оттуда (перепривязка)
    transferred_from = None
    for other_idx, other_srv in enumerate(servers):
        if other_idx == srv_idx:
            continue
        old_eps = other_srv.get("endpoints", [])
        new_eps = [e for e in old_eps if e["value"] != domain]
        if len(new_eps) < len(old_eps):
            transferred_from = f"{other_srv.get('emoji', '')} {other_srv.get('name', f'Сервер {other_idx+1}')}".strip()
            other_srv["endpoints"] = new_eps
            servers[other_idx] = other_srv

    eps.append({"value": domain, "type": "domain", "verified": verified})
    srv["endpoints"] = eps
    servers[srv_idx] = srv
    save_servers(servers)

    if verified:
        dns_note = f"✅ DNS → `{dns_ip}` (совпадает с IP сервера)"
    elif dns_ip and srv_ip and dns_ip != srv_ip:
        dns_note = f"⚠️ DNS → `{dns_ip}`, ожидается `{srv_ip}` — обновите A-запись"
    elif dns_ip:
        dns_note = f"⚠️ DNS → `{dns_ip}` (IP сервера не задан)"
    else:
        dns_note = "⚠️ DNS не разрешился — проверьте правильность домена"

    transfer_note = f"\n↩️ Снят с сервера *{transferred_from}*" if transferred_from else ""
    await update.message.reply_text(
        f"✅ Домен `{domain}` привязан к *{srv.get('name', '')}*\n{dns_note}{transfer_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")
        ]])
    )
    return ConversationHandler.END
