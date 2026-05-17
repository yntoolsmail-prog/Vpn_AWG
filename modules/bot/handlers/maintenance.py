import os, subprocess, logging, json, time, tarfile, shutil, threading, asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from awg_core import (
    ADMIN_ID, AWG_CONF, AWG_IFACE, BOT_SERVICE, CLIENTS_DIR, ENV_FILE, EXCL_EXT,
    SERVER_IP, SERVER_PORT,
    ADMIN_KEY_PATH,
    create_backup, device_short_name, fmt_bytes, fmt_handshake,
    get_all_clients, get_awg_dump, get_combined_awg_dump, get_client_pub, get_kernel_version,
    get_maintenance, get_system_stats, get_ubuntu_version,
    get_user_clients, is_approved, load_users, log_maintenance_done,
    remove_client_from_awg, save_users,
    get_admin_pubkey, get_ssh_password_auth_local,
    ssh_toggle_password_auth_all, ssh_regen_admin_key,
    process_domain, run_subnet_daemon,
    load_servers,
)
from .common import back_kb, WAITING_RESTORE_FILE, BTN_BACK, BTN_BACK_MENU, BTN_BACK_MAINT, BTN_CANCEL

logger = logging.getLogger(__name__)


async def show_status(query):
    peers  = get_combined_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)
    sys    = get_system_stats()
    total_dl  = sum(p.get("tx", 0) for p in peers.values())  # tx сервера = клиенты скачали (↓)
    total_ul  = sum(p.get("rx", 0) for p in peers.values())  # rx сервера = клиенты отдали (↑)
    users     = load_users()

    load = sys["load"]
    text = (
        f"📊 Статус сервера\n\n"
        f"🟢 AWG: работает\n"
        f"🖥 IP: {SERVER_IP}:{SERVER_PORT}\n"
        f"⏱ Uptime: {sys['uptime']}\n\n"
        f"📈 Load: {load[0]} {load[1]} {load[2]}\n"
        f"💾 RAM: {sys['ram_used']}/{sys['ram_total']} MB\n"
        f"💿 Диск: {sys['disk_used']}/{sys['disk_total']} ({sys['disk_pct']}%)\n\n"
        f"👤 Клиентов: {len(get_all_clients())}\n"
        f"👥 Пользователей: {len(users['approved'])}\n"
        f"🟢 Онлайн: {online}\n"
        f"📶 Трафик (с перезагрузки): ↓{fmt_bytes(total_dl)} ↑{fmt_bytes(total_ul)}"
    )

    is_admin = (query.from_user.id == ADMIN_ID)
    kb_rows = [[InlineKeyboardButton("🔄 Перезапустить бота", callback_data="restart_bot")]]
    if is_admin:
        kb_rows.append([InlineKeyboardButton("⚡ Перезапустить AWG",  callback_data="restart_awg")])
        kb_rows.append([InlineKeyboardButton("📈 Трафик / пики",      callback_data="bandwidth")])
    kb_rows.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))

async def do_diagnostics(query):
    """Диагностика конфигов: orphan peers, orphan files, orphan excl.json."""
    await query.edit_message_text("⏳ Проверяю конфиги...")

    peers      = get_awg_dump()
    clients    = get_all_clients()
    known_pubs = {get_client_pub(n) for n in clients} - {None}
    report     = []
    removed_peers = 0
    removed_excl  = 0

    # 1. Пиры в AWG без файлов в CLIENTS_DIR
    orphan_peers = [pub for pub in peers if pub not in known_pubs]
    if orphan_peers:
        for pub in orphan_peers:
            if subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"]).returncode == 0:
                removed_peers += 1
        report.append(f"🔴 Пиров без конфига: {len(orphan_peers)} → удалено {removed_peers}")
    else:
        report.append("✅ Пиры в AWG — всё в порядке")

    # 2. Файлы .conf без пира в AWG
    if os.path.isdir(CLIENTS_DIR):
        conf_files = [f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")]
        orphan_files = [n for n in conf_files if get_client_pub(n) not in peers]
        if orphan_files:
            report.append(f"⚠️ Конфиги без пира в AWG ({len(orphan_files)}):\n" +
                          "\n".join(f"  • {n}" for n in orphan_files))
        else:
            report.append("✅ Конфиги — всё в порядке")

        # 3. Orphan .excl.json без соответствующего .conf
        excl_files = [f[:-len(EXCL_EXT)] for f in os.listdir(CLIENTS_DIR) if f.endswith(EXCL_EXT)]
        orphan_excl = [n for n in excl_files if not os.path.exists(f"{CLIENTS_DIR}/{n}.conf")]
        if orphan_excl:
            for n in orphan_excl:
                try:
                    os.remove(f"{CLIENTS_DIR}/{n}{EXCL_EXT}")
                    removed_excl += 1
                except Exception:
                    pass
            report.append(f"🗑 Мусорных excl.json: {len(orphan_excl)} → удалено {removed_excl}")
        else:
            report.append("✅ Файлы исключений — всё в порядке")

    text = "🔍 Диагностика конфигов\n\n" + "\n".join(report)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_BACK_MAINT, callback_data="maintenance")]
    ]))

# ══════════════════════════════════════════════════════════════════════════════
# ВОССТАНОВЛЕНИЕ ИЗ БЭКАПА
# ══════════════════════════════════════════════════════════════════════════════

async def start_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — предупреждение и запрос файла"""
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_CANCEL, callback_data="restore_cancel")],
    ])
    await query.edit_message_text(
        "📥 Восстановление из бэкапа\n\n"
        "⚠️ *Внимание* — текущие конфиги будут перезаписаны!\n"
        "Используйте только при переезде на новый сервер.\n\n"
        "Перед восстановлением будет автоматически создан бэкап текущего состояния.\n\n"
        "Отправьте файл бэкапа (`awg_backup_*.tar.gz`) в этот чат.\n\n"
        "Для отмены нажмите кнопку ниже или напишите /cancel",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return WAITING_RESTORE_FILE


async def cancel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена восстановления через кнопку"""
    query = update.callback_query
    await query.answer()
    tmp_path = context.user_data.pop("restore_path", None)
    if tmp_path and os.path.exists(tmp_path):
        os.remove(tmp_path)
    await query.edit_message_text("❌ Восстановление отменено.", reply_markup=back_kb())
    return ConversationHandler.END

async def receive_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — получаем файл, показываем что внутри и просим подтверждение"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".tar.gz"):
        await update.message.reply_text(
            "❌ Ожидается файл `.tar.gz`\n\nОтправьте файл бэкапа или напишите /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_RESTORE_FILE

    await update.message.reply_text("⏳ Проверяю бэкап...")

    tmp_path = f"/tmp/restore_{int(time.time())}.tar.gz"
    tg_file  = await doc.get_file()
    await tg_file.download_to_drive(tmp_path)

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            names = tar.getnames()
    except Exception as e:
        os.remove(tmp_path)
        await update.message.reply_text(f"❌ Не удалось открыть архив: {e}")
        return ConversationHandler.END

    has_conf    = any(n.endswith(".conf") and "awg" in n for n in names)
    has_env     = "server.env" in names
    has_clients = any(n.startswith("clients/") for n in names)
    clients_count = len([n for n in names if n.startswith("clients/") and n.endswith(".conf")])
    has_users   = "users.json" in names

    if not has_conf or not has_env:
        os.remove(tmp_path)
        await update.message.reply_text(
            "❌ Файл не похож на бэкап AmneziaWG.\n"
            "Не найдены обязательные файлы (конфиг интерфейса, server.env)."
        )
        return ConversationHandler.END

    context.user_data["restore_path"] = tmp_path

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить восстановление", callback_data="restore_confirm")],
        [InlineKeyboardButton(BTN_CANCEL,                     callback_data="restore_cancel")],
    ])
    await update.message.reply_text(
        f"📦 Содержимое бэкапа:\n\n"
        f"{'✅' if has_conf else '❌'} Конфиг интерфейса\n"
        f"{'✅' if has_env else '❌'} server.env\n"
        f"{'✅' if has_clients else '❌'} Клиенты: {clients_count} шт.\n"
        f"{'✅' if has_users else '⚠️'} users.json {'(найден)' if has_users else '(не найден — пользователи бота не восстановятся)'}\n\n"
        f"⚠️ Текущие конфиги будут перезаписаны.\n"
        f"Сначала будет создан автобэкап текущего состояния.",
        reply_markup=kb
    )
    return WAITING_RESTORE_FILE

async def confirm_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — подтверждение через callback кнопку"""
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if user_id != ADMIN_ID:
        return ConversationHandler.END

    tmp_path = context.user_data.get("restore_path")
    if not tmp_path or not os.path.exists(tmp_path):
        await query.edit_message_text("❌ Файл бэкапа не найден. Начните заново.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Создаю бэкап текущего состояния...")

    try:
        auto_backup = create_backup(prefix="pre_restore")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Не удалось создать автобэкап: {e}\nВосстановление отменено.")
        os.remove(tmp_path)
        return ConversationHandler.END

    await query.message.reply_text("⏳ Восстанавливаю конфиги...")

    # 1. Останавливаем AWG — PostDown почистит iptables по живому конфигу
    subprocess.run(["systemctl", "stop", f"awg-quick@{AWG_IFACE}"])

    # 2. Чистим clients/ чтобы не осталось мусора от старых клиентов
    if os.path.exists(CLIENTS_DIR):
        shutil.rmtree(CLIENTS_DIR)
    os.makedirs(CLIENTS_DIR)

    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall("/etc/amnezia/amneziawg/")
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка при распаковке: {e}")
        os.remove(tmp_path)
        return ConversationHandler.END

    # 3b. Если в бэкапе есть proxy_bot.env — переносим в /etc/proxy-bot/
    _extracted_mtp = f"/etc/amnezia/amneziawg/proxy_bot.env"
    if os.path.exists(_extracted_mtp):
        import shutil as _sh
        os.makedirs("/etc/proxy-bot", exist_ok=True)
        _sh.move(_extracted_mtp, "/etc/proxy-bot/proxy_bot.env")
        os.chmod("/etc/proxy-bot/proxy_bot.env", 0o600)

    os.remove(tmp_path)
    context.user_data.pop("restore_path", None)

    await query.message.reply_text(
        f"✅ Конфиги восстановлены!\n\n"
        f"Автобэкап сохранён: `{auto_backup}`\n\n"
        f"⏳ Перезапускаю AWG и бота...",
        parse_mode="Markdown"
    )

    # 4. Поднимаем AWG с новым конфигом, перезапускаем бота
    subprocess.Popen(
        ["bash", "-c",
         f"sleep 2 && systemctl start awg-quick@{AWG_IFACE} && systemctl restart {BOT_SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ТЕХОБСЛУЖИВАНИЕ
# ══════════════════════════════════════════════════════════════════════════════

def get_ptb_version() -> str:
    try:
        import telegram
        return telegram.__version__
    except:
        return "неизвестно"

async def show_ssh_admin(query):
    pubkey = get_admin_pubkey()
    pw_on  = get_ssh_password_auth_local()

    pw_icon  = "🔓" if pw_on  else "🔒"
    pw_label = "ВКЛ (небезопасно)" if pw_on else "ВЫКЛ"
    pw_btn   = "Выключить пароль SSH" if pw_on else "Включить пароль SSH"

    key_line = f"`{pubkey[:50]}…`" if pubkey else "_ключ не найден_"
    text = (
        f"🔑 *SSH-доступ*\n\n"
        f"Публичный ключ:\n{key_line}\n\n"
        f"{pw_icon} Вход по паролю: *{pw_label}*\n\n"
        f"Скачайте приватный ключ на устройство и подключайтесь по нему.\n"
        f"Один ключ работает на мейне и всех slave-серверах."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Скачать приватный ключ", callback_data="ssh_getkey")],
        [InlineKeyboardButton(f"{pw_icon} {pw_btn}",       callback_data="ssh_toggle_pass")],
        [InlineKeyboardButton("🔄 Пересоздать ключ",       callback_data="ssh_regen_ask")],
        [InlineKeyboardButton(BTN_BACK_MAINT,        callback_data="settings_menu")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def do_ssh_getkey(query):
    key_path = ADMIN_KEY_PATH
    if not os.path.exists(key_path):
        await query.answer("Ключ не найден — запустите setup.sh ещё раз.", show_alert=True)
        return
    await query.answer()

    server_ip = SERVER_IP or "СЕРВЕР"
    ssh_port_raw = subprocess.run(
        ["bash", "-c", "grep '^Port ' /etc/ssh/sshd_config | awk '{print $2}'"],
        capture_output=True, text=True
    ).stdout.strip() or "22"

    with open(key_path, "rb") as f:
        await query.message.reply_document(
            document=f,
            filename="awg_admin_key",
            caption="Приватный SSH-ключ администратора"
        )

    w_dir = f"\"$env:USERPROFILE\\.ssh\""
    w_key = f"\"$env:USERPROFILE\\.ssh\\awg_admin_key\""
    instructions = (
        "Файл выше — ваш пропуск на сервер. Кто имеет его — имеет root-доступ. Не пересылайте.\n\n"

        "*Куда положить файл*\n"
        "SSH ищет ключи в папке `.ssh` в вашем домашнем каталоге:\n"
        "Windows: `C:\\Users\\ВашеИмя\\.ssh\\awg_admin_key`\n"
        "macOS/Linux: `~/.ssh/awg_admin_key`\n\n"
        "Файл из Telegram — переложите его туда через Проводник или Finder.\n"
        "Важно: файл должен называться точно `awg_admin_key` (без расширения).\n"
        "На Windows папку `.ssh` нужно создать сначала — она скрытая и не создаётся сама:\n"
        f"`New-Item -ItemType Directory -Force {w_dir}`\n\n"

        "*Скачать напрямую с сервера (SCP) — надёжнее*\n"
        "Нужен пароль от сервера — убедитесь что вход по паролю включён.\n\n"
        f"Windows PowerShell:\n"
        f"`New-Item -ItemType Directory -Force {w_dir}`\n"
        f"`scp -P {ssh_port_raw} root@{server_ip}:/root/.ssh/awg_admin_key {w_key}`\n\n"
        f"Linux / macOS / Termux:\n"
        f"`mkdir -p ~/.ssh && scp -P {ssh_port_raw} root@{server_ip}:/root/.ssh/awg_admin_key ~/.ssh/awg_admin_key && chmod 600 ~/.ssh/awg_admin_key`\n\n"

        "*Как заходить на сервер*\n"
        f"Первый раз (проверить что ключ работает):\n"
        f"Windows: `ssh -p {ssh_port_raw} -i {w_key} root@{server_ip}`\n"
        f"Linux/macOS: `ssh -p {ssh_port_raw} -i ~/.ssh/awg_admin_key root@{server_ip}`\n\n"
        "Чтобы не вводить длинную команду каждый раз — создайте файл `~/.ssh/config`\n"
        "(Windows: `C:\\Users\\ВашеИмя\\.ssh\\config`) с таким содержимым:\n"
        f"`Host server`\n"
        f"`    HostName {server_ip}`\n"
        f"`    User root`\n"
        f"`    Port {ssh_port_raw}`\n"
        f"`    IdentityFile ~/.ssh/awg_admin_key`\n\n"
        "После этого просто: `ssh server`\n\n"

        "*После успешного входа*\n"
        "Вернитесь в меню SSH и отключите вход по паролю.\n"
        "Второе устройство: сначала включите пароль, скачайте ключ на него, затем снова отключите."
    )
    await query.message.reply_text(instructions, parse_mode="Markdown")


async def do_ssh_toggle_pass(query):
    pw_on = get_ssh_password_auth_local()
    enable = not pw_on
    await query.answer("Применяю…")
    results = await asyncio.get_event_loop().run_in_executor(
        None, ssh_toggle_password_auth_all, enable
    )
    state = "включён" if enable else "выключен"
    lines = [f"{'✅' if results['primary'] else '❌'} Primary: пароль {state}"]
    for name, ok in results.get("slaves", {}).items():
        lines.append(f"{'✅' if ok else '❌'} {name}: пароль {state}")
    await query.message.reply_text("\n".join(lines))
    await show_ssh_admin(query)


async def do_ssh_regen_ask(query):
    await query.edit_message_text(
        "🔄 *Пересоздать SSH-ключ?*\n\n"
        "Будет создан новый ключ. Старый ключ на всех устройствах перестанет работать.\n"
        "Новый ключ нужно будет скачать заново.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, пересоздать", callback_data="ssh_regen_do")],
            [InlineKeyboardButton(BTN_CANCEL,          callback_data="ssh_admin")],
        ])
    )


async def do_ssh_regen(query):
    await query.answer("Создаю новый ключ…")
    ok, slave_errors = await asyncio.get_event_loop().run_in_executor(None, ssh_regen_admin_key)
    if ok:
        msg = "✅ Ключ пересоздан и обновлён на всех серверах.\nСкачайте новый ключ через меню SSH-доступа."
        if slave_errors:
            msg = (
                "✅ Ключ пересоздан, но не удалось обновить некоторые slave:\n"
                + "\n".join(f"• {e}" for e in slave_errors)
                + "\n\nЭти slave нужно обновить вручную через консоль VPS провайдера."
            )
    else:
        msg = "❌ Ошибка при пересоздании ключа — проверьте логи бота."
    await query.message.reply_text(msg)
    await show_ssh_admin(query)


async def show_maintenance(query):
    m         = get_maintenance()
    last_date = m.get("last_date") or "никогда"
    ptb_ver   = get_ptb_version()
    ubuntu    = get_ubuntu_version()
    kernel    = get_kernel_version()

    text = (
        f"🔧 Техобслуживание\n\n"
        f"📅 Последнее: {last_date}\n\n"
        f"🖥 Система: {ubuntu}\n"
        f"⚙️ Ядро: {kernel}\n"
        f"🐍 python-telegram-bot: {ptb_ver}\n\n"
        f"Рекомендуется проводить раз в 6 месяцев."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Создать бэкап",               callback_data="backup")],
        [InlineKeyboardButton("📥 Восстановить из бэкапа",      callback_data="restore")],
        [InlineKeyboardButton("🔍 Диагностика конфигов",        callback_data="diagnostics")],
        [InlineKeyboardButton("💿 Бэкап + apt upgrade",         callback_data="maint_upgrade")],
        [InlineKeyboardButton("📦 Проверить версию библиотеки", callback_data="maint_ptb")],
        [InlineKeyboardButton("♻️ Обновить IP исключений",        callback_data="refresh_subnets")],
        [InlineKeyboardButton("✅ Отмечено — всё ок",            callback_data="maint_done")],
        [InlineKeyboardButton(BTN_BACK_MENU,                       callback_data="settings_menu")],
    ])
    await query.edit_message_text(text, reply_markup=kb)

async def do_maint_upgrade(query):
    """Бэкап + apt upgrade + перезапуск бота"""
    from .bandwidth import do_backup
    await do_backup(query)
    await query.message.reply_text(
        "⏳ Запускаю apt upgrade...\n\nЭто займёт пару минут. Бот перезапустится автоматически."
    )
    subprocess.Popen(
        ["bash", "-c", f"apt-get update -qq && apt-get upgrade -y -qq && systemctl restart {BOT_SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

async def do_maint_ptb(query):
    ptb_ver = get_ptb_version()
    text = (
        f"📦 python-telegram-bot\n\n"
        f"Установлена: *{ptb_ver}*\n\n"
        f"Список релизов и Breaking Changes:\n"
        f"https://github.com/python-telegram-bot/python-telegram-bot/releases\n\n"
        f"Если мажорная версия не изменилась (например всё ещё 20.x) — "
        f"достаточно нажать «Бэкап + apt upgrade».\n\n"
        f"Если мажорная версия выросла (20.x → 21.x) — загляните в Breaking Changes. "
        f"Скорее всего потребуется небольшая правка bot.py."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(BTN_BACK, callback_data="maintenance")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

async def do_maint_done(query):
    log_maintenance_done()
    now = time.strftime("%d.%m.%Y")
    await query.edit_message_text(
        f"✅ Техобслуживание отмечено\n\nДата: {now}\nСледующее напоминание через 6 месяцев.",
        reply_markup=back_kb()
    )

async def do_refresh_subnets(query, context):
    """Запускает полное обновление кэша подсетей в фоне."""
    try:
        from bot import _SUBNET_REFRESH_RUNNING
    except ImportError:
        import threading
        _SUBNET_REFRESH_RUNNING = threading.Event()

    if _SUBNET_REFRESH_RUNNING.is_set():
        await query.answer()
        return

    _SUBNET_REFRESH_RUNNING.set()
    await query.edit_message_text(
        "🌐 Обновление кэша подсетей запущено в фоне.\n\n"
        "Опрашиваются все домены из базы и исключений пользователей.\n"
        "Обычно занимает 15–60 секунд.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_BACK_MAINT, callback_data="maintenance")
        ]])
    )
    chat_id = query.message.chat_id
    msg_id  = query.message.message_id
    loop    = asyncio.get_running_loop()
    bot     = context.bot

    def _run():
        try:
            run_subnet_daemon()
            text = "✅ Кэш подсетей обновлён."
        except Exception as e:
            text = f"❌ Ошибка обновления: {e}"
        finally:
            _SUBNET_REFRESH_RUNNING.clear()
        asyncio.run_coroutine_threadsafe(
            bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BTN_BACK_MAINT, callback_data="maintenance")
                ]])
            ),
            loop
        )

    threading.Thread(target=_run, daemon=True).start()


async def maintenance_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание раз в 6 месяцев — запускается через job_queue"""
    m       = get_maintenance()
    last_ts = m.get("last_ts", 0)
    now_ts  = int(time.time())
    # 6 месяцев = 183 дня
    if now_ts - last_ts < 183 * 86400:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 Перейти к обслуживанию", callback_data="maintenance")],
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "🔔 Напоминание о техобслуживании\n\n"
            "Прошло 6 месяцев с последнего обслуживания.\n"
            "Рекомендуется сделать бэкап и обновить систему."
        ),
        reply_markup=kb
    )
