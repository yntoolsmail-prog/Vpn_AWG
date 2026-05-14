#!/usr/bin/env python3
# bot.py — Telegram-бот AmneziaWG
# Version: 3.0
# Вся бизнес-логика — в awg_core.py и sites_data.py
import os, subprocess, logging, json, time, tempfile, shutil, re, asyncio, threading, ipaddress
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler, PicklePersistence
)
from awg_core import (
    ADMIN_ID, AWG_CONF, AWG_IFACE, BOT_SERVICE, BOT_TOKEN, BW_LOG_FILE,
    CLIENTS_DIR, CONFIG_FILE, ENV_FILE, EXCL_EXT, QRENCODE_BIN,
    RESTART_FLAG_FILE, SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP,
    SERVER_IP, SERVER_PORT, SERVER_PUBLIC, TMA_URL, TZ,
    can_access_device, create_backup, create_client, device_short_name,
    fmt_bytes, fmt_handshake, fmt_histogram,
    gen_obfs, get_all_clients, get_allowed_ips_for_client,
    get_awg_dump, get_bw_histogram, get_bw_histogram_day,
    get_bw_top, get_client_keys, get_client_pub,
    get_host_iface, get_kernel_version, get_log_days,
    get_maintenance, get_real_server_ip, get_system_stats,
    get_ubuntu_version, get_user_clients, get_user_display,
    get_user_name, get_vnstat_monthly,
    is_approved, load_bw_peak, load_client_excl, load_users,
    log_maintenance_done, make_conf_for_client, make_conf_for_client_ep,
    make_vpn_link, read_iface_bytes, remove_client_from_awg, resolve_endpoint,
    save_bw_peak, save_client_excl, save_users,
    load_servers, save_servers, invalidate_servers_cache,
    process_domain, run_subnet_daemon,
    PARAMIKO_AVAILABLE as _PARAMIKO_AVAILABLE,
    ssh_read_slave_env      as _ssh_read_slave_env,
    ssh_clone_awg_to_slave  as _ssh_clone_awg_to_slave,
    ssh_sync_peer_to_slave  as _ssh_sync_peer_to_slave,
    ssh_sync_all_clients_to_slave as _ssh_sync_all_clients_to_slave,
    ssh_stop_slave_awg      as _ssh_stop_slave_awg,
    ssh_get_slave_peer_count as _ssh_get_slave_peer_count,
    get_admin_pubkey, get_ssh_password_auth_local,
    ssh_toggle_password_auth_all, ssh_regen_admin_key,
    ADMIN_KEY_PATH,
)
from sites_data import (
    SITES, CATEGORIES, DEFAULT_SELECTED, ALL_SELECTABLE,
)
from module_loader import load_modules
from strings import (
    BTN_BACK, BTN_BACK_MENU, BTN_BACK_CARD, BTN_BACK_MAINT,
    BTN_CANCEL, BTN_DONE, BTN_REFRESH, BTN_MY_DEVICES,
    HELP_MAIN, HELP_DNS,
)
from handlers.common import (
    WAITING_REGISTER_NAME, WAITING_DEVICE_NAME, WAITING_RESTORE_FILE,
    WAITING_TZ_INPUT, WAITING_SITES_DOMAIN, WAITING_SRV_DOMAIN,
    WAITING_SRV_EDIT_NAME, WAITING_SRV_EDIT_EMOJI,
    IMG_BASE, back_kb, _tma_button, sites_keyboard, _md,
)
from handlers.bandwidth import (
    bw_monitor_job, show_bandwidth, show_bw_days,
    show_bw_reset_ask, do_bw_reset, do_bw_reset_all, do_backup,
)
from handlers.help import show_help, show_help_dns
from handlers.users import show_manage_users, do_kick_user, confirm_kick_user
from handlers.sites import (
    show_sites_menu, toggle_site_handler, toggle_category_handler,
    apply_sites, sites_add_custom_start, sites_add_custom_receive,
)
from handlers.clients import (
    show_my_devices, show_device, show_all_clients,
    show_conf_ep_select, show_qr_ep_select, show_share_ep_select, show_server_eps,
    do_send_conf, do_send_qr, do_send_share,
    do_send_conf_direct, do_send_qr_direct, do_send_share_direct,
    do_delete, confirm_delete,
    add_device_entry, receive_device_name, cancel_add_device,
)
from handlers.servers import (
    show_servers_list, srv_deldomain_list, srv_deldomain_confirm, srv_deldomain_ok,
    show_server_card, _sync_peer_to_all_slaves, _check_endpoint_dns,
    srv_checkdns, srv_del_confirm, srv_del_ok, srv_sync_now,
    srv_rename_start, srv_rename_name, srv_rename_emoji,
    srv_adddomain_start, srv_adddomain_pick, srv_adddomain_receive,
)
from handlers.maintenance import (
    show_status, do_diagnostics,
    start_restore, cancel_restore, receive_restore_file, confirm_restore,
    show_ssh_admin, do_ssh_getkey, do_ssh_toggle_pass, do_ssh_regen_ask, do_ssh_regen,
    show_maintenance, show_maint_tz, ask_tz_manual, receive_tz_manual,
    do_set_tz, do_maint_upgrade, do_maint_ptb, do_maint_done,
    do_refresh_subnets, maintenance_reminder,
)
from handlers.updates import (
    check_repo_updates, do_repo_update, send_start_hello,
    check_ip_on_start, do_update_ip,
    _write_last_commit,
)
_modules = load_modules()

# ── Первый запуск: создаём bot.env если не существует ─────────────────────────
def setup():
    R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'; B='\033[1m'; NC='\033[0m'
    print(f"\n{C}{B}{'='*50}{NC}")
    print(f"{C}{B}   AmneziaWG — Настройка Telegram бота{NC}")
    print(f"\n{C}{B}{'='*50}{NC}\n")
    while True:
        token = input("  Вставьте токен бота: ").strip()
        if ":" in token and len(token) > 20: break
        print(f"  {R}Неверный формат токена{NC}")
    while True:
        admin_id = input("  Вставьте ваш Telegram ID: ").strip()
        if admin_id.isdigit(): break
        print(f"  {R}ID должен быть числом{NC}")
    os.makedirs("/etc/amnezia/amneziawg", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        f.write(f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\n")
    os.chmod(CONFIG_FILE, 0o600)
    print(f"\n{G}✓ Готово!{NC}\n")

if not os.path.exists(CONFIG_FILE):
    setup()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

_SUBNET_REFRESH_RUNNING = threading.Event()


async def show_start_screen(msg, user_id: int, edit: bool = False):
    """Стартовый экран:
    - Обычный пользователь → главное меню.
    - Администратор с TMA → статус-экран с кнопкой «Открыть VPN» + «Режим бота».
    - Администратор без TMA → сразу главное меню (промежуточный экран бессмысленен).
    """
    is_admin = (user_id == ADMIN_ID)

    if not is_admin:
        await main_menu(msg, user_id, edit=edit)
        return

    # Если TMA не настроен — показываем сразу полное меню администратора
    tma_btn = _tma_button()
    if not tma_btn:
        await main_menu(msg, user_id, edit=edit)
        return

    peers   = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)
    total  = len(get_all_clients())
    sys_s  = get_system_stats()
    bw     = load_bw_peak().get("last", {})
    ram_pct = round(sys_s["ram_used"] / sys_s["ram_total"] * 100) if sys_s.get("ram_total") else 0

    users         = load_users()
    pending_count = len(users["pending"])
    pending_note  = f"  🔴 Ожидают: {pending_count}" if pending_count else ""

    text = (
        f"🔐 AmneziaWG\n"
        f"━━━━━━━━━━━━━━\n"
        f"🟢 Онлайн: {online} из {total}\n"
        f"💾 RAM: {ram_pct}%  💿 Диск: {sys_s['disk_pct']}%\n"
        f"⬇️ {bw.get('awg_down', 0)} / ⬆️ {bw.get('awg_up', 0)} Mbit/s\n\n"
        f"🖥 IP: {SERVER_IP}\n"
        f"📱 Клиентов: {total} | 👥 Польз.: {len(users['approved'])}{pending_note}"
    )

    markup = InlineKeyboardMarkup([
        [tma_btn],
        [InlineKeyboardButton("📱 Режим бота", callback_data="back")],
    ])
    if edit:
        await msg.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await msg.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_approved(user_id):
        await show_start_screen(update.message, user_id)
        return ConversationHandler.END

    users = load_users()
    if str(user_id) in users["pending"]:
        await update.message.reply_text(
            "⏳ Ваш запрос уже отправлен администратору.\n"
            "Ожидайте подтверждения."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Добро пожаловать в семейный VPN!\n\n"
        "Введите ваше имя *латиницей* (только буквы, без пробелов).\n"
        "Именно оно будет использоваться для ваших устройств.\n\n"
        "Например: `Ivan`, `Lev`, `Artem`, `Marina`",
        parse_mode="Markdown"
    )
    return WAITING_REGISTER_NAME

async def receive_register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    tg_name  = update.effective_user.first_name or "Unknown"
    raw      = update.message.text.strip()

    latin_name = "".join(c for c in raw if c.isascii() and (c.isalpha() or c.isdigit()))
    latin_name = latin_name.capitalize()

    if not latin_name:
        await update.message.reply_text(
            "❌ Пожалуйста, введите имя *латиницей*. Например: `Ivan`",
            parse_mode="Markdown"
        )
        return WAITING_REGISTER_NAME

    users = load_users()
    taken = [u["name"].lower() for u in users["approved"].values()] + \
            [u["name"].lower() for u in users["pending"].values()]
    if latin_name.lower() in taken:
        await update.message.reply_text(
            f"❌ Имя *{latin_name}* уже занято. Попробуйте другое.",
            parse_mode="Markdown"
        )
        return WAITING_REGISTER_NAME

    users["pending"][str(user_id)] = {
        "name":         latin_name,
        "display":      tg_name,
        "requested_at": int(time.time())
    }
    save_users(users)

    await update.message.reply_text(
        f"✅ Запрос отправлен!\n\n"
        f"Ваше имя в системе: *{latin_name}*\n"
        f"Ожидайте подтверждения администратора.",
        parse_mode="Markdown"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Разрешить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(f"❌ Отклонить", callback_data=f"reject_{user_id}"),
        ]
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🔔 Новый запрос на доступ к VPN\n\n"
            f"👤 Telegram: {tg_name} (@{update.effective_user.username or '—'})\n"
            f"🆔 ID: `{user_id}`\n"
            f"📝 Имя в системе: *{latin_name}*"
        ),
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ
# ══════════════════════════════════════════════════════════════════════════════

async def main_menu(msg, user_id: int, edit=False):
    is_admin = (user_id == ADMIN_ID)

    if is_admin:
        peers_a  = get_awg_dump()
        now_a    = int(time.time())
        online_a = sum(1 for p in peers_a.values() if p.get("handshake") and now_a - p["handshake"] < 180)
        total_a  = len(get_all_clients())
        sys_a    = get_system_stats()
        bw_a     = load_bw_peak().get("last", {})
        ram_a    = round(sys_a["ram_used"] / sys_a["ram_total"] * 100) if sys_a.get("ram_total") else 0
        users         = load_users()
        pending_count = len(users["pending"])
        pending_note  = f"  🔴 Ожидают: {pending_count}" if pending_count else ""
        pending_label = f"👥 Пользователи" + (f" 🔴{pending_count}" if pending_count else "")
        text = (
            f"🔐 AmneziaWG\n"
            f"━━━━━━━━━━━━━━\n"
            f"🟢 Онлайн: {online_a} из {total_a}\n"
            f"💾 RAM: {ram_a}%  💿 Диск: {sys_a['disk_pct']}%\n"
            f"⬇️ {bw_a.get('awg_down', 0)} / ⬆️ {bw_a.get('awg_up', 0)} Mbit/s\n\n"
            f"🖥 IP: {SERVER_IP}\n"
            f"📱 Клиентов: {total_a} | 👥 Польз.: {len(users['approved'])}{pending_note}"
        )
        servers = load_servers()
        srv_label = f"🖥 Серверы ({len(servers)})"
        kb = [
            [InlineKeyboardButton("🧲 Добавить устройство",  callback_data="add")],
            [InlineKeyboardButton(BTN_MY_DEVICES,       callback_data="my_devices")],
            [InlineKeyboardButton("🌍 Все клиенты",          callback_data="all_clients")],
            [InlineKeyboardButton(pending_label,             callback_data="manage_users")],
            [InlineKeyboardButton(srv_label,                 callback_data="servers")],
            [InlineKeyboardButton("📊 Статус сервера",       callback_data="status")],
            [InlineKeyboardButton("🔧 Техобслуживание",      callback_data="maintenance")],
            [InlineKeyboardButton("📖 Инструкция",           callback_data="help")],
        ]
        kb.extend(_modules.get_user_menu_buttons(user_id))
        kb.extend(_modules.get_admin_menu_buttons())
    else:
        my_clients   = get_user_clients(user_id)
        display_name = get_user_display(user_id)
        n = len(my_clients)
        word = "устройство" if n == 1 else ("устройства" if 2 <= n <= 4 else "устройств")

        peers   = get_awg_dump()
        now_ts  = int(time.time())
        online  = sum(1 for p in peers.values() if p.get("handshake") and now_ts - p["handshake"] < 180)
        total   = len(get_all_clients())
        sys_s   = get_system_stats()
        bw      = load_bw_peak().get("last", {})
        ram_pct = round(sys_s["ram_used"] / sys_s["ram_total"] * 100) if sys_s.get("ram_total") else 0

        text = (
            f"🔐 Семейный VPN\n"
            f"━━━━━━━━━━━━━━\n"
            f"🟢 Онлайн: {online} из {total}\n"
            f"💾 RAM: {ram_pct}%  💿 Диск: {sys_s['disk_pct']}%\n"
            f"⬇️ {bw.get('awg_down', 0)} / ⬆️ {bw.get('awg_up', 0)} Mbit/s\n\n"
            f"👋 Привет, {_md(display_name)}!\n"
            f"📱 Ваших устройств: {n} {word}"
        )

        tma_btn = _tma_button()
        kb = []
        if tma_btn:
            kb.append([tma_btn])
        kb.append([InlineKeyboardButton(BTN_MY_DEVICES,      callback_data="my_devices")])
        kb.append([InlineKeyboardButton("🧲 Добавить устройство",  callback_data="add")])
        kb.append([InlineKeyboardButton("📊 Статус сервера",       callback_data="status")])
        kb.append([InlineKeyboardButton("📖 Инструкция",           callback_data="help")])
        kb.extend(_modules.get_user_menu_buttons(user_id))

    if edit:
        await msg.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КНОПОК
# ══════════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    data = query.data

    # Одобрение/отклонение — только для админа
    if data.startswith("approve_") or data.startswith("reject_"):
        if user_id != ADMIN_ID:
            await query.answer("⛔ Только для администратора", show_alert=True)
            return
        target_id = int(data.split("_", 1)[1])
        users = load_users()
        info  = users["pending"].get(str(target_id))
        if not info:
            await query.edit_message_text("⚠️ Запрос уже обработан.")
            return
        if data.startswith("approve_"):
            users["approved"][str(target_id)] = info
            del users["pending"][str(target_id)]
            save_users(users)
            await query.edit_message_text(
                f"✅ Пользователь *{info['name']}* ({info['display']}) одобрен.",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 Доступ к VPN открыт!\n\n"
                    f"Ваше имя в системе: *{info['name']}*\n\n"
                    f"Нажмите /start чтобы начать."
                ),
                parse_mode="Markdown"
            )
        else:
            del users["pending"][str(target_id)]
            save_users(users)
            await query.edit_message_text(
                f"❌ Пользователь *{info['name']}* ({info['display']}) отклонён.",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=target_id,
                text="❌ Ваш запрос на доступ к VPN отклонён администратором."
            )
        return

    # Все остальные кнопки — только для одобренных
    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    is_admin = (user_id == ADMIN_ID)

    if data == "back":
        await main_menu(query, user_id, edit=True)
    elif data == "my_devices":
        await show_my_devices(query, user_id)
    elif data == "all_clients" and is_admin:
        await show_all_clients(query)
    elif data == "manage_users" and is_admin:
        await show_manage_users(query)
    elif data == "servers" and is_admin:
        await show_servers_list(query)
    elif data.startswith("srv_card_") and is_admin:
        await show_server_card(query, int(data[9:]))
    elif data.startswith("srv_del_ok_") and is_admin:
        await srv_del_ok(query, int(data[11:]))
    elif data.startswith("srv_del_") and not data.startswith("srv_del_ok_") and is_admin:
        await srv_del_confirm(query, int(data[8:]))
    elif data.startswith("srv_sync_") and is_admin:
        await srv_sync_now(query, int(data[9:]))
    elif data == "srv_checkdns" and is_admin:
        await srv_checkdns(query)
    elif data == "srv_deldomain_list" and is_admin:
        await srv_deldomain_list(query)
    elif data.startswith("srv_deldomain_confirm_") and is_admin:
        await srv_deldomain_confirm(query, data[len("srv_deldomain_confirm_"):])
    elif data.startswith("srv_deldomain_ok_") and is_admin:
        await srv_deldomain_ok(query, data[len("srv_deldomain_ok_"):])
    # srv_rename_ handled by ConversationHandler below
    elif data == "status":
        await show_status(query)
    elif data == "restart_bot":
        await query.edit_message_text("🔄 Перезапускаю бота...\n\nЧерез несколько секунд появится кнопка меню.")
        # Пишем флаг с chat_id — при старте бот пришлёт меню именно этому пользователю
        try:
            with open(RESTART_FLAG_FILE, "w") as _rf:
                _rf.write(str(query.from_user.id))
        except Exception:
            pass
        subprocess.Popen(["systemctl", "restart", BOT_SERVICE])
    elif data == "restart_awg" and is_admin:
        await query.edit_message_text("⚡ Перезапускаю AWG...\n\nVPN будет недоступен ~5 секунд.")
        async def _awg_restart_check(ctx: ContextTypes.DEFAULT_TYPE):
            result = subprocess.run(
                ["systemctl", "is-active", f"awg-quick@{AWG_IFACE}"],
                capture_output=True, text=True
            )
            st = result.stdout.strip()
            if st == "active":
                status_text = "🟢 AWG работает"
            else:
                # Интерфейс может быть жив даже если сервис failed
                iface_up = subprocess.run(
                    ["ip", "link", "show", AWG_IFACE],
                    capture_output=True
                ).returncode == 0
                status_text = "🟢 AWG работает (интерфейс активен)" if iface_up else "🔴 AWG не запустился — проверьте логи"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 В меню", callback_data="back")],
                [InlineKeyboardButton("📊 Статус", callback_data="status")],
            ])
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚡ Перезапуск AWG завершён\n{status_text}",
                reply_markup=kb,
            )
        context.application.job_queue.run_once(_awg_restart_check, when=8)
        subprocess.Popen(["awg-quick", "down", AWG_IFACE], stderr=subprocess.DEVNULL)
        subprocess.Popen(["systemctl", "restart", f"awg-quick@{AWG_IFACE}"])
    elif data == "bandwidth" and is_admin:
        await show_bandwidth(query)
    elif data.startswith("bw_period_") and is_admin:
        await show_bandwidth(query, int(data.split("_")[2]))
    elif data.startswith("bw_days_") and is_admin:
        await show_bw_days(query, int(data.split("_")[2]))
    elif data == "bw_reset_ask" and is_admin:
        await show_bw_reset_ask(query)
    elif data == "bw_reset_confirm" and is_admin:
        await do_bw_reset(query)
    elif data == "bw_reset_all_confirm" and is_admin:
        await do_bw_reset_all(query)
    elif data == "noop":
        await query.answer()
    elif data == "diagnostics" and is_admin:
        await do_diagnostics(query)
    elif data == "backup" and is_admin:
        await do_backup(query)
    elif data == "maintenance" and is_admin:
        await show_maintenance(query)
    elif data == "ssh_admin" and is_admin:
        await show_ssh_admin(query)
    elif data == "ssh_getkey" and is_admin:
        await do_ssh_getkey(query)
    elif data == "ssh_toggle_pass" and is_admin:
        await do_ssh_toggle_pass(query)
    elif data == "ssh_regen_ask" and is_admin:
        await do_ssh_regen_ask(query)
    elif data == "ssh_regen_do" and is_admin:
        await do_ssh_regen(query)
    elif data == "maint_upgrade" and is_admin:
        await do_maint_upgrade(query)
    elif data == "maint_ptb" and is_admin:
        await do_maint_ptb(query)
    elif data == "maint_tz" and is_admin:
        await show_maint_tz(query)
    elif data == "set_tz_manual" and is_admin:
        await ask_tz_manual(update, context)
    elif data.startswith("set_tz_") and is_admin:
        await do_set_tz(query, data[7:])
    elif data == "maint_done" and is_admin:
        await do_maint_done(query)
    elif data == "refresh_subnets" and is_admin:
        await do_refresh_subnets(query, context)
    elif data == "maint_update_ip" and is_admin:
        await query.edit_message_text("⏳ Определяю текущий IP сервера...")
        real_ip = get_real_server_ip()
        if not real_ip:
            await query.edit_message_text(
                "❌ Не удалось определить внешний IP.\nПроверьте интернет-соединение сервера.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="maintenance")]])
            )
            return
        if real_ip == SERVER_IP:
            await query.edit_message_text(
                f"✅ IP актуален: `{SERVER_IP}`\nОбновление не требуется.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="maintenance")]]),
                parse_mode="Markdown"
            )
            return
        ep_note = ""
        if SERVER_ENDPOINT == SERVER_IP:
            ep_note = f"\n⚠️ SERVER_ENDPOINT тоже будет обновлён на `{real_ip}`."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Обновить", callback_data=f"update_ip_{real_ip}")],
            [InlineKeyboardButton(BTN_CANCEL,   callback_data="maintenance")],
        ])
        await query.edit_message_text(
            f"🔄 Обновление IP сервера\n\n"
            f"В настройках: `{SERVER_IP}`\n"
            f"Реальный IP:  `{real_ip}`{ep_note}\n\n"
            f"Подтвердить обновление?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif data.startswith("update_ip_") and is_admin:
        new_ip = data[10:]
        if new_ip == "skip":
            await query.edit_message_text("Пропущено. IP не изменён.", reply_markup=back_kb())
        else:
            await do_update_ip(query, new_ip)
    elif data.startswith("repo_update_") and is_admin:
        sha = data[12:]
        await do_repo_update(query, sha)
    elif data.startswith("repo_skip_") and is_admin:
        sha = data[10:]
        _write_last_commit(sha)
        await query.edit_message_text(
            f"⏭ Пропущено. Коммит `{sha[:7]}` отмечен как известный.",
            parse_mode="Markdown",
        )
    elif data == "help":
        await show_help(query)
    elif data == "help_dns":
        await show_help_dns(query)
    elif data == "add_cancel":
        await main_menu(query, user_id, edit=True)
    elif data == "my_devices_back":
        await show_my_devices(query, user_id)
    elif data.startswith("device_"):
        await show_device(query, data[7:], user_id)
    elif data.startswith("srv_header_"):
        await query.answer()  # заголовок сервера — нажатие игнорируем
    # ── .conf: выбор сервера/эндпоинта ──
    elif data.startswith("conf_"):
        rest = data[5:]
        if rest.startswith("s") and "_e" in rest:
            # conf_s{si}_e{ei}_{name} → отправить с конкретным эндпоинтом
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        srv = servers[si]
                        ep = eps[ei]["value"]
                        from handlers.clients import _do_send_action
                        await _do_send_action(query, n, "conf", ep, srv)
        elif rest.startswith("ep_"):
            # Обратная совместимость: conf_ep_<epkey>_<name>
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_conf(query, parts[1], parts[0])
        else:
            await show_conf_ep_select(query, rest, user_id)
    # ── QR: выбор сервера/эндпоинта ──
    elif data.startswith("qr_"):
        rest = data[3:]
        if rest.startswith("s") and "_e" in rest:
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        from handlers.clients import _do_send_action
                        await _do_send_action(query, n, "qr", eps[ei]["value"], servers[si])
        elif rest.startswith("ep_"):
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_qr(query, parts[1], parts[0])
        else:
            await show_qr_ep_select(query, rest, user_id)
    # ── Поделиться: выбор сервера/эндпоинта ──
    elif data.startswith("share_"):
        rest = data[6:]
        if rest.startswith("s") and "_e" in rest:
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        from handlers.clients import _do_send_action
                        await _do_send_action(query, n, "share", eps[ei]["value"], servers[si])
        elif rest.startswith("ep_"):
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_share(query, parts[1], parts[0])
        else:
            await show_share_ep_select(query, rest, user_id)
    elif data.startswith("del_"):
        await do_delete(query, data[4:], user_id)
    elif data.startswith("confirm_del_"):
        await confirm_delete(query, data[12:], user_id)
    elif data.startswith("kick_user_") and is_admin:
        await do_kick_user(query, int(data[10:]))
    elif data.startswith("confirm_kick_") and is_admin:
        await confirm_kick_user(query, int(data[13:]))
    elif data == "sites_done":
        await apply_sites(query, user_id, context)
    elif data.startswith("ts_"):
        await toggle_site_handler(query, data[3:], user_id, context)
    elif data.startswith("cat_toggle_"):
        await toggle_category_handler(query, data[11:], context)
    elif data.startswith("sites_"):
        # sites_<name> → меню исключений; sites_add_custom перехватывается ConversationHandler раньше
        await show_sites_menu(query, data[6:], user_id, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ /panel и /bot
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/panel — открыть панель управления (TMA)."""
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return

    tma_btn = _tma_button()
    if tma_btn:
        # Минимальный текст — сразу кнопка, нажать один раз
        await update.message.reply_text("🖥", reply_markup=InlineKeyboardMarkup([[tma_btn]]))
    else:
        await update.message.reply_text(
            "⚠️ Панель не настроена. Используйте /bot"
        )


async def cmd_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bot — открыть главное меню бота."""
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await main_menu(update.message, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application) -> None:
    """Регистрирует команды бота в Telegram (кнопка «Меню»)."""
    commands = [
        BotCommand("start",  "🏠 Главная"),
        BotCommand("cancel", BTN_CANCEL),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Команды бота зарегистрированы: /start /cancel")


def main():
    os.makedirs("/etc/awg-bot", exist_ok=True)
    persistence = PicklePersistence(filepath="/etc/awg-bot/bot_persistence.pkl")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).persistence(persistence).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_register_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
    )

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_device_entry, pattern="^add$")],
        states={
            WAITING_DEVICE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_device_name),
                CallbackQueryHandler(cancel_add_device, pattern="^add_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    restore_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_restore, pattern="^restore$")],
        states={
            WAITING_RESTORE_FILE: [
                MessageHandler(filters.Document.ALL, receive_restore_file),
                CallbackQueryHandler(confirm_restore, pattern="^restore_confirm$"),
                CallbackQueryHandler(cancel_restore, pattern="^restore_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_restore, pattern="^restore_cancel$"),
        ],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    tz_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_tz_manual, pattern="^set_tz_manual$")],
        states={
            WAITING_TZ_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tz_manual),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # sites_add_custom — регистрируем ДО общего button_handler,
    # чтобы колбэк "sites_add_custom" перехватывался сюда,
    # а не уходил в button_handler как sites_<name>
    sites_custom_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sites_add_custom_start, pattern="^sites_add_custom$")],
        states={
            WAITING_SITES_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sites_add_custom_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    srv_domain_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(srv_adddomain_start, pattern="^srv_adddomain_\\d+$")],
        states={
            WAITING_SRV_DOMAIN: [
                CallbackQueryHandler(srv_adddomain_pick, pattern="^srv_ep_pick_\\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, srv_adddomain_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    srv_rename_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(srv_rename_start, pattern="^srv_rename_\\d+$")],
        states={
            WAITING_SRV_EDIT_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_rename_name)],
            WAITING_SRV_EDIT_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_rename_emoji)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(reg_conv)
    app.add_handler(add_conv)
    app.add_handler(restore_conv)
    app.add_handler(tz_conv)
    app.add_handler(sites_custom_conv)  # до общего button_handler
    app.add_handler(srv_domain_conv)
    app.add_handler(srv_rename_conv)
    _modules.register_bot_handlers(app)  # модули регистрируются до общего button_handler
    app.add_handler(CommandHandler("panel",  cmd_panel))
    app.add_handler(CommandHandler("bot",    cmd_bot))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Проверка напоминания о техобслуживании — раз в сутки
    app.job_queue.run_repeating(maintenance_reminder, interval=86400, first=60)
    # Мониторинг трафика — каждые 5 секунд (пики), в лог раз в минуту
    app.job_queue.run_repeating(bw_monitor_job, interval=5, first=10)
    # Проверка IP сервера — один раз через 15 секунд после старта
    app.job_queue.run_once(check_ip_on_start, when=15)
    # Уведомление о старте — через 5 секунд после запуска
    app.job_queue.run_once(send_start_hello, when=5)
    # Мониторинг обновлений репозитория — раз в сутки, первая проверка через 20 секунд после старта
    app.job_queue.run_repeating(check_repo_updates, interval=86400, first=20)
    # Проверка DNS эндпоинтов — каждые 12 часов, первая через 5 минут после старта
    app.job_queue.run_repeating(_check_endpoint_dns, interval=43200, first=300)

    logger.info(f"Бот запущен. Admin ID: {ADMIN_ID}")
    print(f"\n\033[0;32m✓ Бот запущен! Admin ID: {ADMIN_ID}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
