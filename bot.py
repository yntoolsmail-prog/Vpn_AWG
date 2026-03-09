#!/usr/bin/env python3
import os, subprocess, logging, json, zlib, base64, struct, time, tarfile, tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

CONFIG_FILE = "/etc/amnezia/amneziawg/bot.env"
ENV_FILE    = "/etc/amnezia/amneziawg/server.env"
USERS_FILE  = "/etc/amnezia/amneziawg/users.json"
CLIENTS_DIR = "/etc/amnezia/amneziawg/clients"
AWG_CONF    = "/etc/amnezia/amneziawg/awg0.conf"
BACKUP_DIR  = "/etc/amnezia/amneziawg/backups"

# ── Конфиг ─────────────────────────────────────────────────────────────────────
def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

def setup():
    R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'; B='\033[1m'; NC='\033[0m'
    print(f"\n{C}{B}{'='*50}{NC}")
    print(f"{C}{B}   AmneziaWG — Настройка Telegram бота{NC}")
    print(f"{C}{B}{'='*50}{NC}\n")
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

cfg           = load_env(CONFIG_FILE)
BOT_TOKEN     = cfg["BOT_TOKEN"]
ADMIN_ID      = int(cfg["ADMIN_ID"])
srv           = load_env(ENV_FILE)
SERVER_IP     = srv["SERVER_IP"]
SERVER_PORT   = srv["SERVER_PORT"]
SERVER_PUBLIC = srv["SERVER_PUBLIC"]
VPN_SUBNET    = srv["VPN_SUBNET"]
AWG_IFACE     = srv["VPN_IFACE"]
PRIMARY_DNS   = srv.get("PRIMARY_DNS", "1.1.1.1")
SECONDARY_DNS = srv.get("SECONDARY_DNS", "1.0.0.1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
WAITING_REGISTER_NAME = 10
WAITING_DEVICE_NAME   = 11

# ── Пользователи ───────────────────────────────────────────────────────────────
def load_users() -> dict:
    try:
        return json.load(open(USERS_FILE))
    except:
        return {"approved": {}, "pending": {}}

def save_users(data: dict):
    json.dump(data, open(USERS_FILE, "w"), indent=2, ensure_ascii=False)

def is_approved(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    users = load_users()
    return str(user_id) in users["approved"]

def get_user_name(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "Admin"
    users = load_users()
    info = users["approved"].get(str(user_id), {})
    return info.get("name", "User")

def get_user_display(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "Admin"
    users = load_users()
    info = users["approved"].get(str(user_id), {})
    return info.get("display", info.get("name", "User"))

# ── AWG хелперы ────────────────────────────────────────────────────────────────
def get_awg_dump() -> dict:
    try:
        out = subprocess.check_output(["awg", "show", AWG_IFACE, "dump"], text=True)
    except:
        return {}
    peers = {}
    for line in out.strip().split("\n")[1:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        pub       = parts[0]
        endpoint  = parts[2] if parts[2] != "(none)" else ""
        allowed   = parts[3] if parts[3] != "(none)" else ""
        handshake = int(parts[4]) if parts[4] not in ("0", "(none)") else 0
        rx        = int(parts[5])
        tx        = int(parts[6])
        peers[pub] = {"rx": rx, "tx": tx, "endpoint": endpoint,
                      "allowed": allowed, "handshake": handshake}
    return peers

def next_ip() -> int:
    with open(AWG_CONF) as f:
        content = f.read()
    i = 2
    while f"{VPN_SUBNET}.{i}/32" in content:
        i += 1
    return i

def get_all_clients() -> list:
    if not os.path.exists(CLIENTS_DIR):
        return []
    return sorted([f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")])

def get_user_clients(user_id: int) -> list:
    prefix = get_user_name(user_id) + "."
    return [c for c in get_all_clients() if c.startswith(prefix)]

def get_client_pub(name: str) -> str | None:
    """Получить публичный ключ клиента — сначала из .pub файла, иначе вычислить и сохранить"""
    pub_path = f"{CLIENTS_DIR}/{name}.pub"

    # Быстрый путь: .pub файл уже есть
    if os.path.exists(pub_path):
        with open(pub_path) as f:
            return f.read().strip()

    # Медленный путь: вычисляем из PrivateKey и сохраняем на будущее
    try:
        with open(f"{CLIENTS_DIR}/{name}.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PrivateKey"):
                    priv = line.split("=", 1)[1].strip()
                    pub = subprocess.check_output(
                        ["awg", "pubkey"], input=priv, text=True
                    ).strip()
                    # Сохраняем чтобы больше не вычислять
                    with open(pub_path, "w") as pf:
                        pf.write(pub)
                    return pub
    except:
        pass
    return None

def remove_client_from_awg(name: str):
    """Удалить клиента из AWG и конфига"""
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        return

    pub = get_client_pub(name)
    if pub:
        subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"])

    # Удаляем блок из awg0.conf
    with open(AWG_CONF, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")
    new_lines, skip = [], False
    for line in lines:
        if line.strip() == f"# Client: {name}":
            skip = True
        elif skip and line.strip().startswith("[") and line.strip() != "[Peer]":
            skip = False
            new_lines.append(line)
        elif not skip:
            new_lines.append(line)
    with open(AWG_CONF, "w") as f:
        f.write("\n".join(new_lines))

    # Удаляем все файлы клиента
    for ext in [".conf", ".pub", ".vpn", ".vpnlink"]:
        p = f"{CLIENTS_DIR}/{name}{ext}"
        if os.path.exists(p):
            os.remove(p)

# ── Обфускация и генерация конфига ─────────────────────────────────────────────
def gen_obfs() -> dict:
    return {
        "Jc":   srv.get("JC",   "4"),
        "Jmin": srv.get("JMIN", "40"),
        "Jmax": srv.get("JMAX", "70"),
        "S1":   srv.get("S1",   "0"),
        "S2":   srv.get("S2",   "0"),
        "H1":   srv.get("H1",   "1"),
        "H2":   srv.get("H2",   "2"),
        "H3":   srv.get("H3",   "3"),
        "H4":   srv.get("H4",   "4"),
    }

def make_wg_conf(priv, ip, psk, obfs) -> str:
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {priv}", f"Address = {ip}/32",
        f"DNS = {PRIMARY_DNS}, {SECONDARY_DNS}",
        f"Jc = {obfs['Jc']}", f"Jmin = {obfs['Jmin']}", f"Jmax = {obfs['Jmax']}",
        f"S1 = {obfs['S1']}", f"S2 = {obfs['S2']}",
        f"H1 = {obfs['H1']}", f"H2 = {obfs['H2']}", f"H3 = {obfs['H3']}", f"H4 = {obfs['H4']}",
        "", "[Peer]", f"PublicKey = {SERVER_PUBLIC}", f"PresharedKey = {psk}",
        f"Endpoint = {SERVER_IP}:{SERVER_PORT}", "AllowedIPs = 0.0.0.0/0", "PersistentKeepalive = 25",
    ]) + "\n"

def make_vpn_link(priv, pub, ip, psk, obfs, name) -> str:
    wg = (
        f"[Interface]\nAddress = {ip}/32\nDNS = {PRIMARY_DNS}, {SECONDARY_DNS}\n"
        f"PrivateKey = {priv}\nJc = {obfs['Jc']}\nJmin = {obfs['Jmin']}\nJmax = {obfs['Jmax']}\n"
        f"S1 = {obfs['S1']}\nS2 = {obfs['S2']}\nH1 = {obfs['H1']}\nH2 = {obfs['H2']}\n"
        f"H3 = {obfs['H3']}\nH4 = {obfs['H4']}\n\n"
        f"[Peer]\nPublicKey = {SERVER_PUBLIC}\nPresharedKey = {psk}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {SERVER_IP}:{SERVER_PORT}\nPersistentKeepalive = 25\n"
    )
    lc = {**obfs, "allowed_ips": ["0.0.0.0/0", "::/0"], "clientId": pub,
          "client_ip": ip, "client_priv_key": priv, "client_pub_key": pub,
          "config": wg, "hostName": SERVER_IP, "mtu": "1376",
          "persistent_keep_alive": "25", "port": int(SERVER_PORT),
          "psk_key": psk, "server_pub_key": SERVER_PUBLIC}
    c = {"containers": [{"awg": {**obfs, "last_config": json.dumps(lc, indent=4),
         "port": str(SERVER_PORT), "subnet_address": ".".join(ip.split(".")[:3]) + ".0",
         "transport_proto": "udp"}, "container": "amnezia-awg"}],
         "defaultContainer": "amnezia-awg", "description": name,
         "dns1": PRIMARY_DNS, "dns2": SECONDARY_DNS,
         "hostName": SERVER_IP, "nameOverriddenByUser": True}
    b = json.dumps(c, ensure_ascii=False).encode()
    p = struct.pack(">I", len(b)) + zlib.compress(b)
    return "vpn://" + base64.urlsafe_b64encode(p).decode().rstrip("=")

async def create_client(name: str, app, notify_chat_id: int = None):
    """Создаёт клиента AWG и отправляет файлы в чат"""
    priv = subprocess.check_output(["awg", "genkey"], text=True).strip()
    pub  = subprocess.check_output(["awg", "pubkey"], input=priv, text=True).strip()
    psk  = subprocess.check_output(["awg", "genpsk"], text=True).strip()
    ip   = f"{VPN_SUBNET}.{next_ip()}"
    obfs = gen_obfs()

    with open(AWG_CONF, "a") as f:
        f.write(f"\n# Client: {name}\n[Peer]\nPublicKey = {pub}\nPresharedKey = {psk}\nAllowedIPs = {ip}/32\n")
    subprocess.run(["awg", "set", AWG_IFACE, "peer", pub,
                    "preshared-key", "/dev/stdin", "allowed-ips", f"{ip}/32"],
                   input=psk, text=True)

    os.makedirs(CLIENTS_DIR, exist_ok=True)
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    pub_path  = f"{CLIENTS_DIR}/{name}.pub"
    vpn_file  = f"{CLIENTS_DIR}/{name}.vpn"

    with open(conf_path, "w") as f:
        f.write(make_wg_conf(priv, ip, psk, obfs))
    # Сохраняем pubkey сразу — get_client_pub() больше не будет запускать subprocess
    with open(pub_path, "w") as f:
        f.write(pub)
    with open(vpn_file, "w") as f:
        f.write(make_vpn_link(priv, pub, ip, psk, obfs, name))

    if notify_chat_id:
        await app.bot.send_document(
            chat_id=notify_chat_id,
            document=open(conf_path, "rb"),
            filename=f"{name}.conf",
            caption=f"✅ Устройство *{name}* добавлено\n🌐 IP: `{ip}`",
            parse_mode="Markdown"
        )
        qr_path = f"/tmp/{name}_qr.png"
        try:
            subprocess.run(["qrencode", "-o", qr_path, "-r", conf_path], check=True)
            await app.bot.send_photo(
                chat_id=notify_chat_id,
                photo=open(qr_path, "rb"),
                caption=f"📱 QR для AmneziaWG — {name}"
            )
        finally:
            if os.path.exists(qr_path):
                os.remove(qr_path)

# ── Бэкап ──────────────────────────────────────────────────────────────────────
async def do_backup(query):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts          = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{BACKUP_DIR}/awg_backup_{ts}.tar.gz"

    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(AWG_CONF,    arcname="awg0.conf")
            tar.add(ENV_FILE,    arcname="server.env")
            tar.add(CLIENTS_DIR, arcname="clients")
            if os.path.exists(USERS_FILE):
                tar.add(USERS_FILE, arcname="users.json")

        await query.message.reply_document(
            document=open(backup_path, "rb"),
            filename=f"awg_backup_{ts}.tar.gz",
            caption=f"💾 Бэкап от {time.strftime('%d.%m.%Y %H:%M:%S')}\n"
                    f"Клиентов: {len(get_all_clients())}"
        )
        await query.edit_message_text(
            f"✅ Бэкап создан и отправлен.\n\nФайл также сохранён на сервере:\n`{backup_path}`",
            reply_markup=back_kb(), parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка при создании бэкапа: {e}", reply_markup=back_kb())

# ── Форматирование ─────────────────────────────────────────────────────────────
def fmt_bytes(b: int) -> str:
    if b < 1024:        return f"{b} B"
    elif b < 1024**2:   return f"{b/1024:.1f} KB"
    elif b < 1024**3:   return f"{b/1024**2:.1f} MB"
    else:               return f"{b/1024**3:.2f} GB"

def fmt_handshake(ts: int) -> str:
    if not ts: return "никогда"
    diff = int(time.time()) - ts
    if diff < 60:      return f"{diff} сек назад 🟢"
    elif diff < 180:   return f"{diff//60} мин назад 🟢"
    elif diff < 3600:  return f"{diff//60} мин назад"
    elif diff < 86400: return f"{diff//3600} ч назад"
    else:              return f"{diff//86400} д назад"

def back_kb(target="back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data=target)]])

# ══════════════════════════════════════════════════════════════════════════════
# РЕГИСТРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_approved(user_id):
        await main_menu(update.message, user_id)
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
        clients_count = len(get_all_clients())
        users         = load_users()
        pending_count = len(users["pending"])
        pending_label = f"👥 Пользователи" + (f" 🔴{pending_count}" if pending_count else "")
        kb = [
            [InlineKeyboardButton("➕ Добавить устройство",  callback_data="add")],
            [InlineKeyboardButton("📋 Мои устройства",       callback_data="my_devices")],
            [InlineKeyboardButton("🌍 Все клиенты",          callback_data="all_clients")],
            [InlineKeyboardButton(pending_label,             callback_data="manage_users")],
            [InlineKeyboardButton("📊 Статус сервера",       callback_data="status")],
            [InlineKeyboardButton("🧹 Очистить мусор",       callback_data="cleanup")],
            [InlineKeyboardButton("💾 Бэкап",                callback_data="backup")],
            [InlineKeyboardButton("📖 Инструкция",           callback_data="help")],
        ]
        text = (
            f"🔐 AmneziaWG — Панель администратора\n\n"
            f"🖥 Сервер: {SERVER_IP}:{SERVER_PORT}\n"
            f"📱 Всего клиентов: {clients_count}\n"
            f"👥 Пользователей: {len(users['approved'])}"
            + (f"\n🔴 Ожидают одобрения: {pending_count}" if pending_count else "")
        )
    else:
        my_clients   = get_user_clients(user_id)
        display_name = get_user_display(user_id)
        kb = [
            [InlineKeyboardButton("➕ Добавить устройство",  callback_data="add")],
            [InlineKeyboardButton("📋 Мои устройства",       callback_data="my_devices")],
            [InlineKeyboardButton("📊 Статус сервера",       callback_data="status")],
            [InlineKeyboardButton("📖 Инструкция",           callback_data="help")],
        ]
        text = (
            f"🔐 Семейный VPN\n\n"
            f"👋 Привет, {display_name}!\n"
            f"📱 Ваших устройств: {len(my_clients)}"
        )

    if edit:
        await msg.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

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
    elif data == "status":
        await show_status(query)
    elif data == "cleanup" and is_admin:
        await do_cleanup(query)
    elif data == "backup" and is_admin:
        await do_backup(query)
    elif data == "help":
        await show_help(query)
    elif data.startswith("device_"):
        await show_device(query, data[7:], user_id)
    elif data.startswith("conf_"):
        await send_conf(query, data[5:])
    elif data.startswith("qr_"):
        await send_qr(query, data[3:])
    elif data.startswith("share_"):
        await send_share(query, data[6:])
    elif data.startswith("del_"):
        await do_delete(query, data[4:], user_id)
    elif data.startswith("confirm_del_"):
        await confirm_delete(query, data[12:], user_id)
    elif data.startswith("kick_user_") and is_admin:
        await do_kick_user(query, int(data[10:]))
    elif data.startswith("confirm_kick_") and is_admin:
        await confirm_kick_user(query, int(data[13:]))

# ══════════════════════════════════════════════════════════════════════════════
# МОИ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════

async def show_my_devices(query, user_id: int):
    clients = get_user_clients(user_id)
    peers   = get_awg_dump()

    if not clients:
        kb = [
            [InlineKeyboardButton("➕ Добавить первое устройство", callback_data="add")],
            [InlineKeyboardButton("◀️ В меню", callback_data="back")],
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
        rx    = fmt_bytes(stats.get("rx", 0))
        tx    = fmt_bytes(stats.get("tx", 0))
        short = name.split(".", 1)[1] if "." in name else name
        lines.append(f"• {short} | {hs} | ↓{rx} ↑{tx}")

    kb = [[InlineKeyboardButton(f"📋 {name.split('.', 1)[1] if '.' in name else name}",
           callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def show_device(query, name: str, user_id: int):
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    peers = get_awg_dump()
    pub   = get_client_pub(name)
    stats = peers.get(pub, {}) if pub else {}

    short = name.split(".", 1)[1] if "." in name else name
    hs    = fmt_handshake(stats.get("handshake", 0))
    rx    = fmt_bytes(stats.get("rx", 0))
    tx    = fmt_bytes(stats.get("tx", 0))
    ep    = stats.get("endpoint", "—")

    info = (
        f"📱 Устройство: *{short}*\n"
        f"👤 Пользователь: {name.split('.')[0]}\n\n"
        f"🕐 Хендшейк: {hs}\n"
        f"📍 Endpoint: {ep}\n"
        f"📶 Трафик: ↓{rx} ↑{tx}"
    )
    back_target = "my_devices" if user_id != ADMIN_ID else "all_clients"
    kb = [
        [InlineKeyboardButton("📄 Скачать .conf",    callback_data=f"conf_{name}")],
        [InlineKeyboardButton("📱 QR-код",            callback_data=f"qr_{name}")],
        [InlineKeyboardButton("📤 Поделиться кодом", callback_data=f"share_{name}")],
        [InlineKeyboardButton("🗑 Удалить",           callback_data=f"del_{name}")],
        [InlineKeyboardButton("◀️ Назад",             callback_data=back_target)],
    ]
    await query.edit_message_text(info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: ВСЕ КЛИЕНТЫ
# ══════════════════════════════════════════════════════════════════════════════

async def show_all_clients(query):
    clients = get_all_clients()
    peers   = get_awg_dump()

    if not clients:
        await query.edit_message_text("👥 Клиентов нет.", reply_markup=back_kb())
        return

    lines = [f"🌍 Все клиенты ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        rx    = fmt_bytes(stats.get("rx", 0))
        tx    = fmt_bytes(stats.get("tx", 0))
        lines.append(f"• {name} | {hs} | ↓{rx} ↑{tx}")

    kb = [[InlineKeyboardButton(f"📋 {name}", callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ══════════════════════════════════════════════════════════════════════════════

async def show_manage_users(query):
    users = load_users()
    lines = ["👥 Пользователи:\n"]

    if users["pending"]:
        lines.append("⏳ Ожидают одобрения:")
        for uid, info in users["pending"].items():
            lines.append(f"  • {info['name']} ({info['display']}) — ID: {uid}")
        lines.append("")

    if users["approved"]:
        lines.append("✅ Одобренные:")
        for uid, info in users["approved"].items():
            count = len(get_user_clients(int(uid)))
            lines.append(f"  • {info['name']} ({info['display']}) — {count} уст.")
    else:
        lines.append("✅ Одобренных пользователей пока нет.")

    kb = []
    for uid, info in users["pending"].items():
        kb.append([
            InlineKeyboardButton(f"✅ {info['name']}", callback_data=f"approve_{uid}"),
            InlineKeyboardButton(f"❌ {info['name']}", callback_data=f"reject_{uid}"),
        ])
    for uid, info in users["approved"].items():
        kb.append([InlineKeyboardButton(f"🚫 Удалить {info['name']}", callback_data=f"kick_user_{uid}")])

    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def do_kick_user(query, target_id: int):
    users = load_users()
    info  = users["approved"].get(str(target_id))
    if not info:
        await query.edit_message_text("⚠️ Пользователь не найден.", reply_markup=back_kb())
        return
    count = len(get_user_clients(target_id))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить всё", callback_data=f"confirm_kick_{target_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="manage_users")],
    ])
    await query.edit_message_text(
        f"🚫 Удаление пользователя *{info['name']}*\n\n"
        f"Будут удалены все его устройства: {count} шт.\n"
        f"Действие необратимо!",
        reply_markup=kb, parse_mode="Markdown"
    )

async def confirm_kick_user(query, target_id: int):
    users = load_users()
    info  = users["approved"].get(str(target_id))
    if not info:
        await query.edit_message_text("⚠️ Пользователь не найден.", reply_markup=back_kb())
        return

    for name in get_user_clients(target_id):
        remove_client_from_awg(name)

    del users["approved"][str(target_id)]
    save_users(users)

    try:
        await query.bot.send_message(
            chat_id=target_id,
            text="⛔ Ваш доступ к VPN был отозван администратором."
        )
    except:
        pass

    await query.edit_message_text(
        f"✅ Пользователь *{info['name']}* удалён со всеми устройствами.",
        reply_markup=back_kb("manage_users"), parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ОТПРАВКА ФАЙЛОВ
# ══════════════════════════════════════════════════════════════════════════════

async def send_conf(query, name: str):
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    short = name.split(".", 1)[1] if "." in name else name
    await query.message.reply_document(
        document=open(conf_path, "rb"),
        filename=f"{name}.conf",
        caption=f"📄 Конфиг устройства *{short}*",
        parse_mode="Markdown"
    )

async def send_qr(query, name: str):
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    qr_path   = f"/tmp/{name}_qr.png"
    short = name.split(".", 1)[1] if "." in name else name
    try:
        subprocess.run(["qrencode", "-o", qr_path, "-r", conf_path], check=True)
        await query.message.reply_photo(
            photo=open(qr_path, "rb"),
            caption=f"📱 QR для AmneziaWG — *{short}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    finally:
        if os.path.exists(qr_path):
            os.remove(qr_path)

async def send_share(query, name: str):
    vpn_path = f"{CLIENTS_DIR}/{name}.vpn"
    if not os.path.exists(vpn_path):
        await query.message.reply_text(f"❌ vpn-файл не найден для {name}")
        return
    short = name.split(".", 1)[1] if "." in name else name
    await query.message.reply_document(
        document=open(vpn_path, "rb"),
        filename=f"{name}.vpn",
        caption=f"📤 Файл для AmneziaVPN — *{short}*\n\nВставьте в приложении: + → Открыть файл",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# УДАЛЕНИЕ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════

async def do_delete(query, name: str, user_id: int):
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        await query.edit_message_text("❌ Устройство не найдено.", reply_markup=back_kb())
        return

    short = name.split(".", 1)[1] if "." in name else name
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del_{name}")],
        [InlineKeyboardButton("❌ Отмена",      callback_data=f"device_{name}")],
    ])
    await query.edit_message_text(
        f"🗑 Удалить устройство *{short}*?\n\nЭто действие необратимо.",
        reply_markup=kb, parse_mode="Markdown"
    )

async def confirm_delete(query, name: str, user_id: int):
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    remove_client_from_awg(name)
    short = name.split(".", 1)[1] if "." in name else name
    await query.edit_message_text(
        f"✅ Устройство *{short}* удалено.",
        reply_markup=back_kb("my_devices"), parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# СТАТУС, ОЧИСТКА, ИНСТРУКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def show_status(query):
    peers  = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)

    try:    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    except: uptime = "—"

    mem       = subprocess.check_output(["free", "-m"], text=True).split("\n")[1].split()
    ram_used  = int(mem[2]); ram_total = int(mem[1])
    disk      = subprocess.check_output(["df", "-h", "/"], text=True).split("\n")[1].split()
    load      = open("/proc/loadavg").read().split()[:3]
    total_rx  = sum(p.get("rx", 0) for p in peers.values())
    total_tx  = sum(p.get("tx", 0) for p in peers.values())
    users     = load_users()

    text = (
        f"📊 Статус сервера\n\n"
        f"🟢 AWG: работает\n"
        f"🖥 IP: {SERVER_IP}:{SERVER_PORT}\n"
        f"⏱ Uptime: {uptime}\n\n"
        f"📈 Load: {load[0]} {load[1]} {load[2]}\n"
        f"💾 RAM: {ram_used}/{ram_total} MB\n"
        f"💿 Диск: {disk[2]}/{disk[1]} ({disk[4]})\n\n"
        f"👤 Клиентов: {len(get_all_clients())}\n"
        f"👥 Пользователей: {len(users['approved'])}\n"
        f"🟢 Онлайн: {online}\n"
        f"📶 Трафик (с перезагрузки): ↓{fmt_bytes(total_rx)} ↑{fmt_bytes(total_tx)}"
    )
    await query.edit_message_text(text, reply_markup=back_kb())

async def do_cleanup(query):
    peers      = get_awg_dump()
    known_pubs = {get_client_pub(n) for n in get_all_clients()} - {None}
    trash      = [pub for pub in peers if pub not in known_pubs]

    if not trash:
        await query.edit_message_text("✅ Мусора нет — всё чисто!", reply_markup=back_kb())
        return

    removed = sum(
        1 for pub in trash
        if subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"]).returncode == 0
    )
    await query.edit_message_text(
        f"🧹 Очистка завершена\n\nУдалено мусорных пиров: {removed}",
        reply_markup=back_kb()
    )

async def show_help(query):
    text = (
        "📖 Инструкция\n\n"
        "➕ *Добавить устройство* — создать VPN-профиль для телефона, ноутбука, ПК и т.д.\n\n"
        "📋 *Мои устройства* — список ваших профилей. Нажмите на устройство чтобы:\n"
        "• скачать конфиг или QR-код\n"
        "• удалить устройство 🗑\n\n"
        "📊 *Статус сервера* — проверить работает ли VPN.\n\n"
        "⚠️ *Важно — на каждое устройство свой профиль!*\n"
        "Если использовать один конфиг на двух устройствах одновременно — "
        "оба будут глючить и отваливаться. Создайте отдельный профиль для каждого.\n\n"
        "📲 *Как подключиться:*\n"
        "1. Нажмите «Добавить устройство», введите название (`Phone`, `PC`, `iPad`)\n"
        "2. Получите .conf файл и QR-код\n"
        "3. Установите AmneziaWG → импортируйте конфиг или отсканируйте QR\n\n"
        "📱 *Приложения:*\n"
        "• AmneziaWG — простое подключение (рекомендуется)\n"
        "• AmneziaVPN — если нужно раздельное туннелирование"
    )
    await query.edit_message_text(text, reply_markup=back_kb(), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНИЕ УСТРОЙСТВА (ConversationHandler)
# ══════════════════════════════════════════════════════════════════════════════

async def add_device_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return ConversationHandler.END

    await query.edit_message_text(
        f"➕ Добавление устройства\n\n"
        f"Введите название устройства *латиницей*:\n"
        f"`Phone`, `PC`, `Nout`, `iPad`, `TV`",
        parse_mode="Markdown"
    )
    context.user_data["adding_user_id"] = user_id
    return WAITING_DEVICE_NAME

async def receive_device_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_approved(user_id):
        return ConversationHandler.END

    raw        = update.message.text.strip()
    device_raw = "".join(c for c in raw if c.isascii() and (c.isalnum() or c in "_-"))
    device_raw = device_raw.capitalize()

    if not device_raw:
        await update.message.reply_text(
            "❌ Введите название *латиницей*. Например: `Phone`",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    user_name = get_user_name(user_id)
    full_name = f"{user_name}.{device_raw}"

    if os.path.exists(f"{CLIENTS_DIR}/{full_name}.conf"):
        await update.message.reply_text(
            f"❌ Устройство *{full_name}* уже существует. Введите другое название.",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    await update.message.reply_text(f"⏳ Создаю профиль *{full_name}*...", parse_mode="Markdown")
    await create_client(full_name, context.application, notify_chat_id=update.effective_chat.id)
    await main_menu(update.message, user_id)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()

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
            WAITING_DEVICE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_device_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
    )

    app.add_handler(reg_conv)
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info(f"Бот запущен. Admin ID: {ADMIN_ID}")
    print(f"\n\033[0;32m✓ Бот запущен! Admin ID: {ADMIN_ID}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
