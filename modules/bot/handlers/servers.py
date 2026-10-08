import asyncio, ipaddress, logging, os, re, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from awg_core import (
    ADMIN_ID, AWG_IFACE, AWG_CONF, SERVER_IP, SERVER_PORT, SERVER_PUBLIC,
    SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP,
    PARAMIKO_AVAILABLE as _PARAMIKO_AVAILABLE,
    get_all_clients, get_client_pub, load_servers, save_servers,
    invalidate_servers_cache, resolve_endpoint,
    get_real_server_ip, sync_server_ip, restart_after_restore, BOT_SERVICE,
    ssh_check_server, ssh_push_admin_key,
    read_iface_bytes, get_system_stats, load_bw_peak, get_combined_awg_dump, fmt_bytes,
    ssh_read_slave_env      as _ssh_read_slave_env,
    ssh_clone_awg_to_slave  as _ssh_clone_awg_to_slave,
    ssh_sync_peer_to_slave  as _ssh_sync_peer_to_slave,
    ssh_sync_all_clients_to_slave as _ssh_sync_all_clients_to_slave,
    ssh_stop_slave_awg      as _ssh_stop_slave_awg,
    ssh_get_slave_peer_count as _ssh_get_slave_peer_count,
    ssh_get_slave_sys_stats as _ssh_get_slave_sys_stats,
)
from .common import _md, back_kb, WAITING_SRV_DOMAIN, WAITING_SRV_EDIT_VALUE, WAITING_SRV_EDIT_FORCE, BTN_BACK, BTN_BACK_MENU, BTN_CANCEL, BTN_BACK_CARD


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

    # Раньше строка была зашита «🟢 работает» и не менялась, даже если AWG лёг
    awg_ok = os.path.isdir(f"/sys/class/net/{AWG_IFACE}")
    lines = [
        f"{'🟢' if awg_ok else '🔴'} AWG: {'работает' if awg_ok else 'не работает'}",
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
        [InlineKeyboardButton("✏️ Редактировать", callback_data=f"srv_edit_{srv_idx}")],
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
            sync_line = ("⚠️ Нет связи со slave. Сменился IP или не подошёл "
                         "SSH-ключ — «✏️ Редактировать»")
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
# Сколько проверок подряд слейв не отвечает: {srv_id: int}
_slave_unreachable: dict = {}
# Порог, после которого сообщаем о потере связи (3 × 30 мин ≈ 1.5 часа) —
# короткие сетевые моргания так не превращаются в поток сообщений
_UNREACHABLE_ALERT_AFTER = 3


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
        # Нет связи. Одиночные пропуски игнорируем, но затяжная потеря — это
        # и есть симптом «ключ не подошёл» (например, после восстановления из
        # бэкапа, снятого до пересоздания ключа). Молчать про такое нельзя.
        if cnt is None:
            miss = _slave_unreachable.get(sid, 0) + 1
            _slave_unreachable[sid] = miss
            if miss == _UNREACHABLE_ALERT_AFTER:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🔌 *Нет связи со слейвом*\n\n"
                    f"*{_md(label)}* не отвечает по SSH уже {miss} проверки подряд (~1,5 часа).\n\n"
                    f"Если сервер включён, VPN на нём работает, но бот до него не "
                    f"достаёт: новые устройства на нём не подключатся, а удалённые "
                    f"продолжают на нём работать.\n\n"
                    f"*Причины:*\n"
                    f"• хостер сменил IP — карточка → ✏️ Редактировать → IP;\n"
                    f"• не подошёл SSH-ключ (после переезда или пересоздания ключа) — "
                    f"там же → Пароль SSH: бот войдёт по паролю и заново поставит ключ;\n"
                    f"• сервер выключен.\n\n"
                    f"После починки — «🔄 Синхронизировать» в карточке.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🖥 Карточка сервера", callback_data=f"srv_card_{idx}")
                    ]]),
                )
            continue

        if _slave_unreachable.get(sid, 0) >= _UNREACHABLE_ALERT_AFTER:
            await context.bot.send_message(
                ADMIN_ID, f"🔌 Связь со слейвом *{_md(label)}* восстановлена.",
                parse_mode="Markdown",
            )
        _slave_unreachable[sid] = 0

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


# Последний отчёт о неудачной правке IP: job ходит раз в час, и одна и та же
# ошибка записи не должна приходить админу каждый час
_ip_sync_last_report = ""


async def _check_server_ip(context):
    """Job: хостер сменил IP сервера — сам или по просьбе, так уходят от блокировки
    адреса. Вручную server.env и servers.json после этого никто не правит, и бот,
    TMA и ссылки MTProxy показывали бы прежний адрес. sync_server_ip() переписывает
    их; админу — отчёт с проверкой доменов основного, затем перезапуск бота и TMA:
    оба держат SERVER_IP в памяти с импорта. AWG перезапускать не нужно — он
    слушает на всех адресах."""
    global _ip_sync_last_report
    loop = asyncio.get_running_loop()
    real_ip = await loop.run_in_executor(None, get_real_server_ip)
    if not real_ip:
        return
    changed, lines = await loop.run_in_executor(None, sync_server_ip, real_ip)
    if not lines:
        return
    text = "\n".join(lines)
    if not changed and text == _ip_sync_last_report:
        return
    _ip_sync_last_report = text
    try:
        if changed:
            results, _ = await _dns_check_all_servers()
            doms = [r for r in results if r["expected"] == real_ip]
            if doms:
                lines.append("\nДомены основного:")
                for r in doms:
                    if r["now_ok"]:
                        lines.append(f"✅ {r['domain']} → {r['resolved']}")
                    else:
                        lines.append(f"❌ {r['domain']} → {r['resolved'] or 'не разрешился'}"
                                     f" — нужна A-запись на {real_ip}")
                if not all(r["now_ok"] for r in doms):
                    lines.append("Если A-запись уже поменяли — DNS обновляется до нескольких часов.")
            lines.append("\nУстройства, скачанные с IP вместо домена в адресе сервера, "
                         "нужно скачать заново.")
            lines.append("Бот и веб-панель перезапускаются.")
        # Без parse_mode: в доменах и текстах ошибок бывает «_»
        await context.bot.send_message(ADMIN_ID, "\n".join(lines))
    except Exception as e:
        logging.getLogger(__name__).warning(f"_check_server_ip: {e}")
    finally:
        if changed:
            restart_after_restore(BOT_SERVICE)


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
        [InlineKeyboardButton("🗑 Удалить и остановить AWG", callback_data=f"srv_del_ok_{srv_idx}")],
        [InlineKeyboardButton("📤 Только убрать из бота",    callback_data=f"srv_detach_{srv_idx}")],
        [InlineKeyboardButton(BTN_BACK,                      callback_data=f"srv_card_{srv_idx}")],
    ])
    await query.edit_message_text(
        f"Сервер *{emoji} {name}*:\n\n"
        "🗑 *Удалить* — бот остановит AWG на нём и уберёт из списка: клиенты "
        "к нему больше не подключатся.\n\n"
        "📤 *Только убрать из бота* — сервер продолжит работать как есть, со "
        "своими клиентами и параметрами, но бот перестанет им управлять: новые "
        "устройства и удаления до него не дойдут. Вернуть — «➕ Добавить сервер», "
        "конфиг основного скопируется на него заново.",
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


async def srv_detach_ok(query, srv_idx: int):
    """Убирает slave из servers.json, не заходя на него. Нужен, когда сервер
    должен доработать на своих параметрах — например, слейвы на AWG 2.0 после
    перевода основного на 3.1: «Удалить» остановил бы на них AWG, а оставленные
    в списке они попали бы под перевод и «Синхронизировать»."""
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers[srv_idx]
    if srv.get("is_primary"):
        await query.answer("Нельзя убрать PRIMARY сервер.", show_alert=True)
        return
    name  = srv.get("name", "Сервер")
    emoji = srv.get("emoji", "🖥")
    servers.pop(srv_idx)
    save_servers(servers)
    await query.edit_message_text(
        f"📤 Сервер *{emoji} {name}* убран из бота.\n\n"
        "AWG на нём не тронут: клиенты с уже скачанными конфигами подключаются "
        "как раньше. Новые устройства и удаления до него больше не доходят.",
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

# ── Редактирование сервера ────────────────────────────────────────────────────
# Всё, что вносится при добавлении сервера. SSH-поля — только у слейва: на
# основной бот по SSH не ходит, а его IP при смене у хостера sync_server_ip()
# обновляет сам (_check_server_ip).
_SRV_EDIT_FIELDS = {
    # поле: (кнопка, только у слейва, подсказка)
    "name":     ("Название", False,
                 "Новое название — латиницей, оно входит в имя файлов конфигов "
                 "(например: NLD, FIN):"),
    "emoji":    ("Флаг", False, "Новый флаг или эмодзи (например: 🇳🇱):"),
    "country":  ("Страна", False, "Название страны на русском (например: Голландия):"),
    "ip":       ("IP", True,
                 "Новый IP сервера. Бот зайдёт на него по SSH и проверит, что это "
                 "тот же сервер:"),
    "port":     ("SSH-порт", True, "Новый SSH-порт:"),
    "login":    ("Логин SSH", True, "Новый логин SSH:"),
    "password": ("Пароль SSH", True,
                 "Пароль SSH. Бот войдёт с ним и заново поставит свой ключ — так "
                 "чинится «не подошёл SSH-ключ». Сообщение с паролем бот удалит:"),
}
# Значение, которое не прошло проверку SSH, — до ответа «Сохранить всё равно».
# Не в user_data: та пишется на диск (PicklePersistence), а здесь бывает пароль.
_PENDING_SRV_EDIT: dict = {}


def _srv_find(servers: list, srv_id: str):
    """(индекс, сервер) по id — индекс мог сдвинуться, пока админ вводил значение."""
    for i, s in enumerate(servers):
        if s.get("id") == srv_id:
            return i, s
    return None, None


def _srv_edit_view(srv_idx: int, note: str = ""):
    """Текст и кнопки экрана «✏️ Редактирование» или None, если сервера нет."""
    servers = load_servers()
    if srv_idx >= len(servers):
        return None
    srv    = servers[srv_idx]
    is_pri = srv.get("is_primary", False)
    ssh    = srv.get("ssh", {})
    lines  = [note, ""] if note else []
    lines += [
        f"✏️ *Редактирование:* {srv.get('emoji', '')} {_md(srv.get('name', ''))}"
        + (" _(Основной)_" if is_pri else " _(Слейв)_"),
        "",
        f"Название: {_md(srv.get('name', '') or '—')}",
        f"Флаг: {srv.get('emoji', '') or '—'}",
        f"Страна: {_md(srv.get('country', '') or '—')}",
    ]
    if is_pri:
        lines.append(f"IP: `{ssh.get('ip', '—')}` — обновляется сам при смене у хостера")
    else:
        lines += [
            f"IP: `{ssh.get('ip', '—')}`",
            f"SSH-порт: `{ssh.get('port', 22)}`",
            f"Логин SSH: `{ssh.get('login', 'root')}`",
            "Вход: " + ("по ключу" if ssh.get("auth") == "key" else "по паролю"),
        ]
    rows = [[InlineKeyboardButton(label, callback_data=f"srv_editf_{field}_{srv_idx}")]
            for field, (label, slave_only, _) in _SRV_EDIT_FIELDS.items()
            if not (slave_only and is_pri)]
    rows.append([InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def show_srv_edit(query, srv_idx: int):
    """Экран «✏️ Редактировать» из карточки сервера: текущие данные и кнопка на каждое поле."""
    view = _srv_edit_view(srv_idx)
    if not view:
        await query.answer("Сервер не найден.", show_alert=True)
        return
    await query.edit_message_text(view[0], reply_markup=view[1], parse_mode="Markdown")


def _srv_edit_check(field: str, value: str, servers: list, srv: dict) -> str:
    """Проверка введённого значения; пустая строка — годится, иначе что не так."""
    if field == "name" and not re.fullmatch(r"[A-Za-z0-9-]{1,16}", value):
        return "Название — латиница, цифры и дефис, до 16 символов."
    if field == "emoji" and not 0 < len(value) <= 10:
        return "Флаг — один эмодзи."
    if field == "country" and not 0 < len(value) <= 30:
        return "Страна — до 30 символов."
    if field == "ip":
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            return "Это не IPv4-адрес, пример: 87.58.204.107"
        if any(s is not srv and s.get("ssh", {}).get("ip") == value for s in servers):
            return "Этот IP уже записан у другого сервера."
    if field == "port" and not (value.isdigit() and 0 < int(value) < 65536):
        return "Порт — число от 1 до 65535."
    if field == "login" and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
        return "Логин — латиница в нижнем регистре, например root."
    if field == "password" and not value:
        return "Пароль пустой."
    return ""


def _srv_edit_apply(srv_id: str, field: str, value: str, checked: bool) -> list:
    """Записывает SSH-поле слейва в servers.json; возвращает строки отчёта.
    Синхронная — для run_in_executor: при новом пароле ставит ключ по SSH."""
    servers = load_servers()
    _, srv = _srv_find(servers, srv_id)
    if not srv:
        return ["❌ Сервер не найден."]
    ssh = srv.setdefault("ssh", {})
    out = []
    if field == "ip":
        old = ssh.get("ip", "")
        ssh["ip"] = value
        for ep in srv.get("endpoints", []):
            if ep.get("value") == old:
                ep["value"] = value
        out.append(f"✅ IP изменён: {old} → {value}")
    elif field == "port":
        ssh["port"] = int(value)
        out.append(f"✅ SSH-порт: {value}")
    elif field == "login":
        ssh["login"] = value
        out.append(f"✅ Логин SSH: {value}")
    elif field == "password":
        # Пароль задают, когда ключ не подошёл: без «auth: key» _ssh_connect
        # после неудачи с ключом пробует пароль
        ssh["password"] = value
        ssh.pop("auth", None)
        out.append("✅ Пароль SSH сохранён")
    save_servers(servers)

    if field == "password" and checked:
        if ssh_push_admin_key(srv):
            servers = load_servers()
            _, srv = _srv_find(servers, srv_id)
            if srv:
                srv["ssh"]["auth"] = "key"
                srv["ssh"]["password"] = ""
                save_servers(servers)
            out.append("🔑 Ключ бота заново установлен, вход переключён на ключ, "
                       "пароль больше не хранится")
        else:
            out.append("⚠️ Ключ поставить не удалось — бот будет входить по паролю")
    return out


async def _srv_domains_report(srv_id: str) -> list:
    """После смены IP слейва: куда указывают его домены и что делать с конфигами."""
    import socket as _s
    _, srv = _srv_find(load_servers(), srv_id)
    if not srv:
        return []
    ip   = srv.get("ssh", {}).get("ip", "")
    loop = asyncio.get_running_loop()
    out  = []
    for ep in srv.get("endpoints", []):
        if ep.get("type") != "domain":
            continue
        try:
            got = await loop.run_in_executor(None, _s.gethostbyname, ep["value"])
        except Exception:
            got = None
        if got == ip:
            out.append(f"✅ {ep['value']} → {got}")
        else:
            out.append(f"❌ {ep['value']} → {got or 'не разрешился'} — нужна A-запись на {ip}")
    out.append("Устройства, скачанные с прежним IP вместо домена, нужно скачать заново.")
    out.append("В карточке — синхронизация: устройства, добавленные или удалённые, "
               "пока не было связи, доедут после «Синхронизировать».")
    return out


def _srv_edit_done_kb(srv_idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Редактировать ещё", callback_data=f"srv_edit_{srv_idx}")],
        [InlineKeyboardButton(BTN_BACK_CARD, callback_data=f"srv_card_{srv_idx}")],
    ])


async def srv_edit_field_start(update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка поля на экране редактирования (entry point ConversationHandler)."""
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    _, _, field, idx = query.data.split("_", 3)
    srv_idx = int(idx)
    _PENDING_SRV_EDIT.pop(query.message.chat_id, None)
    servers = load_servers()
    if srv_idx >= len(servers) or field not in _SRV_EDIT_FIELDS:
        await query.edit_message_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]
    label, slave_only, prompt = _SRV_EDIT_FIELDS[field]
    if slave_only and srv.get("is_primary"):
        return ConversationHandler.END
    context.user_data["srv_edit"] = {"srv_id": srv.get("id"), "field": field}
    ssh = srv.get("ssh", {})
    cur = {"name": srv.get("name"), "emoji": srv.get("emoji"), "country": srv.get("country"),
           "ip": ssh.get("ip"), "port": ssh.get("port", 22), "login": ssh.get("login", "root")}
    now = "" if field == "password" else f"Сейчас: {cur.get(field) or '—'}\n\n"
    await query.edit_message_text(
        f"✏️ {label}\n\n{now}{prompt}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_CANCEL, callback_data="srv_edit_cancel")
        ]]),
    )
    return WAITING_SRV_EDIT_VALUE


async def srv_edit_receive(update, context: ContextTypes.DEFAULT_TYPE):
    """Новое значение поля. Название/флаг/страна — сразу в servers.json; SSH-поля —
    после проверки подключения и ключа AWG (ssh_check_server)."""
    d     = context.user_data.get("srv_edit") or {}
    field = d.get("field", "")
    value = (update.message.text or "").strip()
    chat  = update.effective_chat
    if field == "password":
        try:
            await update.message.delete()
        except Exception:
            pass

    servers = load_servers()
    srv_idx, srv = _srv_find(servers, d.get("srv_id"))
    if not srv:
        context.user_data.pop("srv_edit", None)
        await chat.send_message("❌ Сервер не найден.")
        return ConversationHandler.END
    if field == "port":
        value = value.replace(" ", "")
    err = _srv_edit_check(field, value, servers, srv)
    if err:
        await chat.send_message(
            f"⚠️ {err}\n\nВведите ещё раз:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_CANCEL, callback_data="srv_edit_cancel")
            ]]),
        )
        return WAITING_SRV_EDIT_VALUE

    if field in ("name", "emoji", "country"):
        srv[field] = value
        save_servers(servers)
        context.user_data.pop("srv_edit", None)
        text, kb = _srv_edit_view(srv_idx, "✅ Сохранено")
        await chat.send_message(text, reply_markup=kb, parse_mode="Markdown")
        return ConversationHandler.END

    cand = {**srv, "ssh": dict(srv.get("ssh", {}))}
    if field == "port":
        cand["ssh"]["port"] = int(value)
    elif field == "password":
        cand["ssh"]["password"] = value
        cand["ssh"].pop("auth", None)
    else:
        cand["ssh"][field] = value
    ssh = cand["ssh"]
    status = await chat.send_message(f"🔌 Проверяю {ssh.get('ip')}:{ssh.get('port', 22)}…")
    loop = asyncio.get_running_loop()
    err = await loop.run_in_executor(None, ssh_check_server, cand)
    if err:
        _PENDING_SRV_EDIT[chat.id] = (d.get("srv_id"), field, value)
        # Без parse_mode: в тексте ошибки paramiko бывают «_» и «`»
        await status.edit_text(
            f"❌ {err}\n\nСохранить всё равно? Например, если сервер сейчас выключен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Сохранить всё равно", callback_data="srv_edit_force")],
                [InlineKeyboardButton("✏️ Ввести заново", callback_data=f"srv_editf_{field}_{srv_idx}")],
                [InlineKeyboardButton(BTN_CANCEL, callback_data="srv_edit_cancel")],
            ]),
        )
        return WAITING_SRV_EDIT_FORCE

    context.user_data.pop("srv_edit", None)
    lines = ["✅ Подключение есть, это тот же сервер."]
    lines += await loop.run_in_executor(None, _srv_edit_apply, d.get("srv_id"), field, value, True)
    if field == "ip":
        lines += await _srv_domains_report(d.get("srv_id"))
    await status.edit_text("\n".join(lines), reply_markup=_srv_edit_done_kb(srv_idx))
    return ConversationHandler.END


async def srv_edit_force(update, context: ContextTypes.DEFAULT_TYPE):
    """«💾 Сохранить всё равно» после неудачной проверки SSH."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("srv_edit", None)
    pending = _PENDING_SRV_EDIT.pop(query.message.chat_id, None)
    if not pending:
        await query.edit_message_text("Нечего сохранять — начните заново из карточки сервера.")
        return ConversationHandler.END
    srv_id, field, value = pending
    loop  = asyncio.get_running_loop()
    lines = await loop.run_in_executor(None, _srv_edit_apply, srv_id, field, value, False)
    if field == "ip":
        lines += await _srv_domains_report(srv_id)
    srv_idx, _ = _srv_find(load_servers(), srv_id)
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=_srv_edit_done_kb(srv_idx) if srv_idx is not None else back_kb("servers"),
    )
    return ConversationHandler.END


async def srv_edit_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена ввода — назад на экран редактирования."""
    query = update.callback_query
    await query.answer()
    d = context.user_data.pop("srv_edit", None) or {}
    _PENDING_SRV_EDIT.pop(query.message.chat_id, None)
    srv_idx, _ = _srv_find(load_servers(), d.get("srv_id"))
    view = _srv_edit_view(srv_idx) if srv_idx is not None else None
    if view:
        await query.edit_message_text(view[0], reply_markup=view[1], parse_mode="Markdown")
    else:
        await query.edit_message_text("Отмена.", reply_markup=back_kb("servers"))
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
