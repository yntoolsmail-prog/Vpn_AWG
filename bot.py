#!/usr/bin/env python3
# Version: 1.7
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
BACKUP_DIR  = "/etc/amnezia/amneziawg/backups"
MAINTENANCE_FILE = "/etc/amnezia/amneziawg/maintenance.json"
BW_LOG_FILE      = "/var/log/awg-bw.log"
BW_PEAK_FILE     = "/etc/amnezia/amneziawg/bw_peak.json"
# AWG_CONF строится динамически после загрузки server.env — см. ниже

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
AWG_IFACE     = srv.get("VPN_IFACE", "awg0")   # фолбэк на awg0 для старых установок
AWG_CONF      = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
PRIMARY_DNS   = srv.get("PRIMARY_DNS", "1.1.1.1")
SECONDARY_DNS = srv.get("SECONDARY_DNS", "1.0.0.1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
WAITING_REGISTER_NAME = 10
WAITING_DEVICE_NAME   = 11
WAITING_RESTORE_FILE  = 12

# ── Пользователи ───────────────────────────────────────────────────────────────
def load_users() -> dict:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except:
        return {"approved": {}, "pending": {}}

def save_users(data: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

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

    # Удаляем блок из конфига интерфейса
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
          "config": wg, "hostName": SERVER_IP, "mtu": "1420",
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
        with open(conf_path, "rb") as fh:
            await app.bot.send_document(
                chat_id=notify_chat_id,
                document=fh,
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

# ── Мониторинг трафика ─────────────────────────────────────────────────────────
def _read_iface_bytes(iface: str) -> tuple[int, int]:
    """Читает rx/tx байты для сетевого интерфейса из /sys"""
    try:
        rx = int(open(f"/sys/class/net/{iface}/statistics/rx_bytes").read())
        tx = int(open(f"/sys/class/net/{iface}/statistics/tx_bytes").read())
        return rx, tx
    except:
        return 0, 0

def _get_host_iface() -> str:
    """Определяет основной сетевой интерфейс сервера (не awg)"""
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"], text=True)
        for part in out.split():
            if part not in ("via", "dev", "src", "uid", "8.8.8.8", "cache") and "/" not in part:
                prev = out.split()[out.split().index(part) - 1]
                if prev == "dev":
                    return part
    except:
        pass
    # фолбэк — первый не-loopback не-awg интерфейс
    try:
        for line in open("/proc/net/dev").readlines()[2:]:
            iface = line.split(":")[0].strip()
            if iface and iface != "lo" and not iface.startswith("awg"):
                return iface
    except:
        pass
    return "eth0"

def load_bw_peak() -> dict:
    try:
        with open(BW_PEAK_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_bw_peak(data: dict):
    try:
        with open(BW_PEAK_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

async def bw_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Job: раз в минуту замеряет скорость и пишет лог + обновляет пики"""
    iface = _get_host_iface()
    now   = int(time.time())

    # Читаем предыдущее состояние из context.bot_data
    prev = context.bot_data.get("bw_prev")
    r2, t2 = _read_iface_bytes(iface)

    if prev:
        dt = now - prev["ts"]
        if dt > 0:
            rx_mbit = round((r2 - prev["rx"]) * 8 / 1_000_000 / dt, 2)
            tx_mbit = round((t2 - prev["tx"]) * 8 / 1_000_000 / dt, 2)

            # Пишем в лог
            try:
                with open(BW_LOG_FILE, "a") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M')} RX={rx_mbit} TX={tx_mbit}\n")
                # Обрезаем лог — держим только последние 10080 строк (7 дней по минуте)
                lines = open(BW_LOG_FILE).readlines()
                if len(lines) > 10080:
                    with open(BW_LOG_FILE, "w") as f:
                        f.writelines(lines[-10080:])
            except:
                pass

            # Обновляем пики
            peak = load_bw_peak()
            today = time.strftime("%Y-%m-%d")
            day_peak = peak.get("day", {})
            if day_peak.get("date") != today:
                day_peak = {"date": today, "rx": 0, "tx": 0}
            if rx_mbit > day_peak["rx"]: day_peak["rx"] = rx_mbit
            if tx_mbit > day_peak["tx"]: day_peak["tx"] = tx_mbit

            all_peak = peak.get("all", {"rx": 0, "tx": 0})
            if rx_mbit > all_peak["rx"]: all_peak["rx"] = rx_mbit
            if tx_mbit > all_peak["tx"]: all_peak["tx"] = tx_mbit

            save_bw_peak({"day": day_peak, "all": all_peak,
                          "last": {"rx": rx_mbit, "tx": tx_mbit, "ts": now}})

    context.bot_data["bw_prev"] = {"rx": r2, "tx": t2, "ts": now}

def get_vnstat_monthly() -> list[dict]:
    """Парсит vnstat --months и возвращает последние месяцы с трафиком"""
    iface = _get_host_iface()
    try:
        out = subprocess.check_output(
            ["vnstat", "-i", iface, "--months", "--json"],
            text=True, stderr=subprocess.DEVNULL
        )
        data = json.loads(out)
        months = data["interfaces"][0]["traffic"]["month"]
        result = []
        for m in months[-6:]:  # последние 6 месяцев
            rx_gb = round(m["rx"] / 1024**3, 2)
            tx_gb = round(m["tx"] / 1024**3, 2)
            result.append({
                "label": f"{m['date']['year']}-{m['date']['month']:02d}",
                "rx_gb": rx_gb,
                "tx_gb": tx_gb,
                "total_gb": round(rx_gb + tx_gb, 2),
            })
        return result
    except Exception:
        pass

    # фолбэк — текстовый вывод если нет --json (старый vnstat)
    try:
        out = subprocess.check_output(
            ["vnstat", "-i", iface, "--months"],
            text=True, stderr=subprocess.DEVNULL
        )
        result = []
        for line in out.splitlines():
            # Формат: "  2024-01  |  1.23 GiB  |  4.56 GiB  |  5.79 GiB"
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4 and "-" in parts[0] and len(parts[0].strip()) == 7:
                label = parts[0].strip()
                def parse_gb(s):
                    s = s.strip()
                    try:
                        val, unit = s.split()
                        val = float(val)
                        unit = unit.lower()
                        if "gib" in unit or "gb" in unit: return round(val, 2)
                        if "mib" in unit or "mb" in unit: return round(val / 1024, 2)
                        if "kib" in unit or "kb" in unit: return round(val / 1024**2, 2)
                    except: pass
                    return 0.0
                rx = parse_gb(parts[1])
                tx = parse_gb(parts[2])
                result.append({"label": label, "rx_gb": rx, "tx_gb": tx,
                                "total_gb": round(rx + tx, 2)})
        return result[-6:]
    except Exception:
        return []

def get_bw_top(n: int = 5) -> list[tuple[str, float, float]]:
    """Топ-N минут по суммарному трафику из лога"""
    try:
        lines = open(BW_LOG_FILE).readlines()
        rows = []
        for line in lines:
            parts = line.strip().split()
            if len(parts) == 3:
                try:
                    dt   = parts[0] + " " + parts[1]
                    rx   = float(parts[2].split("=")[1])
                    tx   = float(parts[3].split("=")[1]) if len(parts) > 3 else 0.0
                    rows.append((dt, rx, tx))
                except:
                    pass
        rows.sort(key=lambda x: x[1] + x[2], reverse=True)
        return rows[:n]
    except:
        return []

def get_bw_top_fixed(n: int = 5) -> list[tuple[str, float, float]]:
    """Топ-N минут по суммарному трафику из лога (фиксированный парсер)"""
    try:
        rows = []
        for line in open(BW_LOG_FILE).readlines():
            # Формат: 2024-01-15 14:32 RX=12.34 TX=5.67
            parts = line.strip().split()
            if len(parts) == 4:
                try:
                    dt = parts[0] + " " + parts[1]
                    rx = float(parts[2].split("=")[1])
                    tx = float(parts[3].split("=")[1])
                    rows.append((dt, rx, tx))
                except:
                    pass
        rows.sort(key=lambda x: x[1] + x[2], reverse=True)
        return rows[:n]
    except:
        return []

async def show_bandwidth(query):
    """Экран статистики трафика для админа"""
    iface = _get_host_iface()
    peak  = load_bw_peak()
    last  = peak.get("last", {})
    day   = peak.get("day", {})
    allp  = peak.get("all", {})
    top   = get_bw_top_fixed(5)

    # Текущая скорость — быстрый замер за 1 сек
    r1, t1 = _read_iface_bytes(iface)
    time.sleep(1)
    r2, t2 = _read_iface_bytes(iface)
    cur_rx = round((r2 - r1) * 8 / 1_000_000, 2)
    cur_tx = round((t2 - t1) * 8 / 1_000_000, 2)

    lines = [
        f"📈 Статистика трафика\n",
        f"🌐 Интерфейс: {iface}",
        f"⚡ Сейчас: ↓{cur_rx} ↑{cur_tx} Mbit/s",
    ]

    if last:
        last_time = time.strftime("%H:%M", time.localtime(last.get("ts", 0)))
        lines.append(f"🕐 Замер {last_time}: ↓{last.get('rx', 0)} ↑{last.get('tx', 0)} Mbit/s")

    # ── Месячный трафик из vnstat ──────────────────────────────────────────────
    monthly = get_vnstat_monthly()
    if monthly:
        lines.append(f"\n📦 Трафик по месяцам (↓вх + ↑исх = итого):")
        for m in monthly:
            lines.append(f"   {m['label']}  ↓{m['rx_gb']} + ↑{m['tx_gb']} = {m['total_gb']} GB")
        # Текущий месяц отдельно с прогресс-баром
        cur = monthly[-1]
        cur_label = time.strftime("%Y-%m")
        if cur["label"] == cur_label:
            total = cur["total_gb"]
            # Типичные лимиты хостеров: подсказка если > 1 TB
            warn = ""
            if total >= 4000:   warn = "  🔴 >4 TB!"
            elif total >= 3000: warn = "  🟠 >3 TB"
            elif total >= 2000: warn = "  🟡 >2 TB"
            elif total >= 1000: warn = "  🟢 >1 TB"
            lines.append(f"\n📅 Текущий месяц: {total} GB{warn}")
    else:
        lines.append("\n📦 Месячный трафик: vnstat ещё собирает данные,\n   вернитесь через час.")

    # ── Пики скорости ──────────────────────────────────────────────────────────
    if day:
        lines.append(f"\n📅 Пик сегодня ({day.get('date', '—')}):")
        lines.append(f"   ↓{day.get('rx', 0)} ↑{day.get('tx', 0)} Mbit/s")

    if allp:
        lines.append(f"\n🏆 Абс. пик скорости:")
        lines.append(f"   ↓{allp.get('rx', 0)} ↑{allp.get('tx', 0)} Mbit/s")

    if top:
        lines.append(f"\n🔝 Топ-5 минут по нагрузке:")
        for dt, rx, tx in top:
            lines.append(f"   {dt}  ↓{rx} ↑{tx}")
    elif not peak:
        lines.append("\n⏳ Замеры скорости идут каждую минуту.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="bandwidth")],
        [InlineKeyboardButton("◀️ Статус",   callback_data="status")],
        [InlineKeyboardButton("◀️ В меню",   callback_data="back")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

# ── Бэкап ──────────────────────────────────────────────────────────────────────
async def do_backup(query):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts          = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{BACKUP_DIR}/awg_backup_{ts}.tar.gz"

    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(AWG_CONF,    arcname=f"{AWG_IFACE}.conf")
            tar.add(ENV_FILE,    arcname="server.env")
            tar.add(CLIENTS_DIR, arcname="clients")
            if os.path.exists(USERS_FILE):
                tar.add(USERS_FILE, arcname="users.json")

        with open(backup_path, "rb") as fh:
            await query.message.reply_document(
                document=fh,
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
            [InlineKeyboardButton("📥 Восстановить из бэкапа", callback_data="restore")],
            [InlineKeyboardButton("🔧 Техобслуживание",      callback_data="maintenance")],
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
    elif data == "restart_bot":
        await query.edit_message_text("🔄 Перезапускаю бота...")
        subprocess.Popen(["systemctl", "restart", "awg-bot"])
    elif data == "restart_awg" and is_admin:
        await query.edit_message_text("⚡ Перезапускаю AWG...\n\nVPN будет недоступен ~5 секунд.")
        subprocess.Popen(["systemctl", "restart", f"awg-quick@{AWG_IFACE}"])
    elif data == "bandwidth" and is_admin:
        await show_bandwidth(query)
    elif data == "cleanup" and is_admin:
        await do_cleanup(query)
    elif data == "backup" and is_admin:
        await do_backup(query)
    elif data == "restore" and is_admin:
        await start_restore(query)
    elif data == "restore_cancel" and is_admin:
        await query.edit_message_text("❌ Восстановление отменено.", reply_markup=back_kb())
    elif data == "maintenance" and is_admin:
        await show_maintenance(query)
    elif data == "maint_upgrade" and is_admin:
        await do_maint_upgrade(query)
    elif data == "maint_ptb" and is_admin:
        await do_maint_ptb(query)
    elif data == "maint_done" and is_admin:
        await do_maint_done(query)
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
    with open(conf_path, "rb") as fh:
        await query.message.reply_document(
            document=fh,
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
    with open(vpn_path) as f:
        vpn_link = f.read().strip()
    with open(vpn_path, "rb") as fh:
        await query.message.reply_document(
            document=fh,
            filename=f"{name}.vpn",
            caption=f"📤 Файл для AmneziaVPN — *{short}*\n\nВставьте в приложении: + → Открыть файл",
            parse_mode="Markdown"
        )
    # Отправляем ссылку текстом для копирования
    await query.message.reply_text(
        f"🔗 Ссылка для AmneziaVPN (нажмите чтобы скопировать):\n\n`{vpn_link}`",
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

    is_admin = (query.from_user.id == ADMIN_ID)
    kb_rows = [[InlineKeyboardButton("🔄 Перезапустить бота", callback_data="restart_bot")]]
    if is_admin:
        kb_rows.append([InlineKeyboardButton("⚡ Перезапустить AWG",  callback_data="restart_awg")])
        kb_rows.append([InlineKeyboardButton("📈 Трафик / пики",      callback_data="bandwidth")])
    kb_rows.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))

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

# ══════════════════════════════════════════════════════════════════════════════
# ВОССТАНОВЛЕНИЕ ИЗ БЭКАПА
# ══════════════════════════════════════════════════════════════════════════════

async def start_restore(query):
    """Шаг 1 — предупреждение и запрос файла"""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="restore_cancel")],
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

    # Скачиваем во временный файл
    tmp_path = f"/tmp/restore_{int(time.time())}.tar.gz"
    tg_file  = await doc.get_file()
    await tg_file.download_to_drive(tmp_path)

    # Проверяем содержимое архива
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            names = tar.getnames()
    except Exception as e:
        os.remove(tmp_path)
        await update.message.reply_text(f"❌ Не удалось открыть архив: {e}")
        return ConversationHandler.END

    # Ищем ключевые файлы
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

    # Сохраняем путь к файлу в user_data
    context.user_data["restore_path"] = tmp_path

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить восстановление", callback_data="restore_confirm")],
        [InlineKeyboardButton("❌ Отмена",                     callback_data="restore_cancel")],
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

    # Автобэкап перед восстановлением
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts          = time.strftime("%Y%m%d_%H%M%S")
    auto_backup = f"{BACKUP_DIR}/pre_restore_{ts}.tar.gz"
    try:
        with tarfile.open(auto_backup, "w:gz") as tar:
            tar.add(AWG_CONF,    arcname=f"{AWG_IFACE}.conf")
            tar.add(ENV_FILE,    arcname="server.env")
            tar.add(CLIENTS_DIR, arcname="clients")
            if os.path.exists(USERS_FILE):
                tar.add(USERS_FILE, arcname="users.json")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Не удалось создать автобэкап: {e}\nВосстановление отменено.")
        os.remove(tmp_path)
        return ConversationHandler.END

    await query.message.reply_text("⏳ Восстанавливаю конфиги...")

    # Распаковываем бэкап
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall("/etc/amnezia/amneziawg/")
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка при распаковке: {e}")
        os.remove(tmp_path)
        return ConversationHandler.END

    os.remove(tmp_path)
    context.user_data.pop("restore_path", None)

    await query.message.reply_text(
        f"✅ Конфиги восстановлены!\n\n"
        f"Автобэкап сохранён: `{auto_backup}`\n\n"
        f"⏳ Перезапускаю AWG и бота...",
        parse_mode="Markdown"
    )

    # Перезапускаем AWG и бота
    subprocess.Popen(
        ["bash", "-c",
         f"sleep 2 && systemctl restart awg-quick@{AWG_IFACE} && systemctl restart awg-bot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ТЕХОБСЛУЖИВАНИЕ
# ══════════════════════════════════════════════════════════════════════════════

def load_maintenance() -> dict:
    try:
        with open(MAINTENANCE_FILE) as f:
            return json.load(f)
    except:
        return {"last_date": None, "last_ts": 0}

def save_maintenance(data: dict):
    with open(MAINTENANCE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_ptb_version() -> str:
    try:
        import telegram
        return telegram.__version__
    except:
        return "неизвестно"

def get_ubuntu_version() -> str:
    try:
        return subprocess.check_output(["lsb_release", "-ds"], text=True).strip()
    except:
        return "неизвестно"

def get_kernel_version() -> str:
    try:
        return subprocess.check_output(["uname", "-r"], text=True).strip()
    except:
        return "неизвестно"

async def show_maintenance(query):
    m         = load_maintenance()
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
        [InlineKeyboardButton("💾 Бэкап + apt upgrade",         callback_data="maint_upgrade")],
        [InlineKeyboardButton("📦 Проверить версию библиотеки", callback_data="maint_ptb")],
        [InlineKeyboardButton("✅ Отмечено — всё ок",            callback_data="maint_done")],
        [InlineKeyboardButton("◀️ В меню",                       callback_data="back")],
    ])
    await query.edit_message_text(text, reply_markup=kb)

async def do_maint_upgrade(query):
    """Бэкап + apt upgrade + перезапуск бота"""
    await do_backup(query)
    await query.message.reply_text(
        "⏳ Запускаю apt upgrade...\n\nЭто займёт пару минут. Бот перезапустится автоматически."
    )
    subprocess.Popen(
        ["bash", "-c", "apt-get update -qq && apt-get upgrade -y -qq && systemctl restart awg-bot"],
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
        [InlineKeyboardButton("◀️ Назад", callback_data="maintenance")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

async def do_maint_done(query):
    now = time.strftime("%d.%m.%Y")
    save_maintenance({"last_date": now, "last_ts": int(time.time())})
    await query.edit_message_text(
        f"✅ Техобслуживание отмечено\n\nДата: {now}\nСледующее напоминание через 6 месяцев.",
        reply_markup=back_kb()
    )

async def maintenance_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание раз в 6 месяцев — запускается через job_queue"""
    m       = load_maintenance()
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
        per_message=False,
        allow_reentry=True,
    )

    restore_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_restore, pattern="^restore$")],
        states={
            WAITING_RESTORE_FILE: [
                MessageHandler(filters.Document.ALL, receive_restore_file),
                CallbackQueryHandler(confirm_restore, pattern="^restore_confirm$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(reg_conv)
    app.add_handler(add_conv)
    app.add_handler(restore_conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Проверка напоминания о техобслуживании — раз в сутки
    app.job_queue.run_repeating(maintenance_reminder, interval=86400, first=60)
    # Мониторинг трафика — раз в минуту
    app.job_queue.run_repeating(bw_monitor_job, interval=60, first=10)

    logger.info(f"Бот запущен. Admin ID: {ADMIN_ID}")
    print(f"\n\033[0;32m✓ Бот запущен! Admin ID: {ADMIN_ID}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()