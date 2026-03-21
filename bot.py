#!/usr/bin/env python3
# Version: 2.2
import os, subprocess, logging, json, zlib, base64, struct, time, tarfile, tempfile, shutil, socket, ipaddress, re, asyncio
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
TZ            = srv.get("TIMEZONE", "UTC")
# Эндпоинты: если домен не задан — используем IP
SERVER_ENDPOINT        = srv.get("SERVER_ENDPOINT", "") or SERVER_IP
SERVER_ENDPOINT_BACKUP = srv.get("SERVER_ENDPOINT_BACKUP", "")

# Применяем часовой пояс для всего процесса
os.environ["TZ"] = TZ
try:
    time.tzset()
except AttributeError:
    pass  # Windows не поддерживает tzset, но на сервере Ubuntu всегда есть

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Глобальный lock — один клиент за раз, никаких гонок данных
_client_lock = asyncio.Lock()

# Состояния ConversationHandler
WAITING_REGISTER_NAME  = 10
WAITING_DEVICE_NAME    = 11
WAITING_RESTORE_FILE   = 12
WAITING_EXCL_ALLOWED   = 13   # ждём строку AllowedIPs от пользователя
WAITING_EXCL_DOMAIN    = 14   # ждём домен для исключения
WAITING_TZ_INPUT       = 15   # ждём ручной ввод часового пояса

IMG_BASE = "https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/.images"

# ── Split tunneling: база сайтов ───────────────────────────────────────────────
SITES = {
    "local": {
        "name": "Локальная сеть (роутер, NAS...)", "emoji": "🏠",
        "domains": [], "subnets": ["192.168.0.0/16"],
    },
    "sber": {
        "name": "Сбербанк", "emoji": "💚",
        "domains": ["sber.ru", "sberbank.ru", "online.sberbank.ru", "sberbank.com", "sbrf.ru"],
    },
    "tbank": {
        "name": "Т-Банк (Тинькофф)", "emoji": "🟡",
        "domains": ["tbank.ru", "tinkoff.ru", "acdn.tinkoff.ru"],
    },
    "alfa": {
        "name": "Альфа-Банк", "emoji": "🔴",
        "domains": ["alfabank.ru", "click.alfabank.ru"],
    },
    "vtb": {
        "name": "ВТБ", "emoji": "🔵",
        "domains": ["vtb.ru", "online.vtb.ru", "mb.vtb.ru"],
    },
    "raiffeisen": {
        "name": "Райффайзен", "emoji": "🟠",
        "domains": ["raiffeisen.ru", "ecom.raiffeisen.ru"],
    },
    "sbp": {
        "name": "СБП / НСПК", "emoji": "⚡",
        "domains": ["sbp.nspk.ru", "nspk.ru", "qr.nspk.ru"],
    },
    "ozon": {
        "name": "Ozon", "emoji": "🔵",
        "domains": ["ozon.ru", "static.ozon.ru", "cdn1.ozone.ru"],
    },
    "wildberries": {
        "name": "Wildberries", "emoji": "🟣",
        "domains": ["wildberries.ru", "wbstatic.net", "wbbasket.ru", "wbx-static.com"],
    },
    "avito": {
        "name": "Авито", "emoji": "🟢",
        "domains": ["avito.ru", "cdn.avito.ru", "m.avito.ru"],
    },
    "yandex": {
        "name": "Яндекс (все сервисы)", "emoji": "🔴",
        "domains": [
            "yandex.ru", "ya.ru", "yandex.com", "yandex.net",
            "maps.yandex.ru", "taxi.yandex.ru", "go.yandex.ru",
            "music.yandex.ru", "storage.mds.yandex.net",
            "market.yandex.ru", "yastatic.net", "avatars.mds.yandex.net",
        ],
    },
    "kinopoisk": {
        "name": "Кинопоиск", "emoji": "🎬",
        "domains": ["kinopoisk.ru", "www.kinopoisk.ru"],
    },
    "rzd": {
        "name": "РЖД", "emoji": "🚂",
        "domains": ["rzd.ru", "www.rzd.ru", "pass.rzd.ru", "ticket.rzd.ru"],
    },
    "pochta": {
        "name": "Почта России", "emoji": "📬",
        "domains": ["pochta.ru", "www.pochta.ru", "tracking.pochta.ru"],
    },
    "delivery": {
        "name": "Яндекс Еда / Самокат", "emoji": "🍔",
        "domains": ["eda.yandex.ru", "eats.yandex.ru", "samokat.ru", "delivery-club.ru"],
    },
    "zhkh": {
        "name": "ЖКХ / Энергосбыт", "emoji": "🏘",
        "domains": ["mosenergosbyt.ru", "lkk.mosenergosbyt.ru", "eirc-mo.ru", "dom.gosuslugi.ru"],
    },
    "vk": {
        "name": "ВКонтакте", "emoji": "💙",
        "domains": ["vk.com", "vk.me", "userapi.com", "vkuseraudio.net", "vk-cdn.net"],
    },
    "ok": {
        "name": "Одноклассники", "emoji": "🟠",
        "domains": ["ok.ru", "www.ok.ru", "udn.odnoklassniki.ru"],
    },
    "hh": {
        "name": "HeadHunter (hh.ru)", "emoji": "💼",
        "domains": ["hh.ru", "api.hh.ru", "hhcdn.ru"],
    },
    "2gis": {
        "name": "2ГИС", "emoji": "🗺",
        "domains": ["2gis.ru", "2gis.com"],
    },
}

CATEGORIES = {
    "🏠 Локальная сеть":       ["local"],
    "🏦 Банки и платежи":      ["sber", "tbank", "alfa", "vtb", "raiffeisen", "sbp"],
    "🛒 Маркетплейсы":         ["ozon", "wildberries", "avito"],
    "🔴 Яндекс":               ["yandex", "kinopoisk"],
    "🚂 Транспорт и доставка": ["rzd", "pochta", "delivery"],
    "🏘 ЖКХ":                  ["zhkh"],
    "💬 Соцсети":              ["vk", "ok"],
    "📦 Прочее":               ["hh", "2gis"],
}

# Всегда включены — пользователь снять не может
DEFAULT_SELECTED = {"local"}


def build_allowed_ips(selected_keys) -> str:
    """Резолвит домены, вычитает IP из 0.0.0.0/0, возвращает строку AllowedIPs."""
    excluded: set[str] = set()
    for key in selected_keys:
        site = SITES.get(key, {})
        for subnet in site.get("subnets", []):
            excluded.add(subnet)
        for domain in site.get("domains", []):
            try:
                results = socket.getaddrinfo(domain, None, socket.AF_INET)
                for r in results:
                    excluded.add(f"{r[4][0]}/32")
            except Exception:
                pass

    if not excluded:
        return "0.0.0.0/0"

    allowed = [ipaddress.ip_network("0.0.0.0/0")]
    for net_str in excluded:
        target = ipaddress.ip_network(net_str, strict=False)
        new_allowed = []
        for net in allowed:
            if target.overlaps(net):
                new_allowed.extend(net.address_exclude(target))
            else:
                new_allowed.append(net)
        allowed = new_allowed

    return ", ".join(
        str(n) for n in sorted(allowed, key=lambda n: (n.network_address, n.prefixlen))
    )


def sites_keyboard(selected: set, device_name: str) -> InlineKeyboardMarkup:
    rows = []
    for cat_name, keys in CATEGORIES.items():
        rows.append([InlineKeyboardButton(f"── {cat_name} ──", callback_data="noop")])
        for key in keys:
            site = SITES[key]
            locked = key in DEFAULT_SELECTED
            is_on  = key in selected
            mark   = "✅" if locked else ("☑" if is_on else "☐")
            label  = f"{mark} {site['emoji']} {site['name']}"
            cb     = "noop" if locked else f"ts_{key}"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([
        InlineKeyboardButton("✅ Готово",  callback_data="sites_done"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"device_{device_name}"),
    ])
    return InlineKeyboardMarkup(rows)

# ── Пользователи ───────────────────────────────────────────────────────────────
_users_cache: dict | None = None
_users_cache_ts: float = 0.0
_USERS_CACHE_TTL: float = 5.0  # секунд

def load_users() -> dict:
    global _users_cache, _users_cache_ts
    now = time.monotonic()
    if _users_cache is not None and (now - _users_cache_ts) < _USERS_CACHE_TTL:
        return _users_cache
    try:
        with open(USERS_FILE) as f:
            _users_cache = json.load(f)
    except:
        _users_cache = {"approved": {}, "pending": {}}
    _users_cache_ts = now
    return _users_cache

def save_users(data: dict):
    global _users_cache, _users_cache_ts
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _users_cache = data
    _users_cache_ts = time.monotonic()

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
    """Ищет свободный IP в ОБОИХ источниках: awg0.conf и файлах в /clients/.
    Это защищает от рассинхрона между конфигом и реальным состоянием."""
    used: set[str] = set()

    # Источник 1: awg0.conf
    try:
        with open(AWG_CONF) as f:
            for line in f:
                if "AllowedIPs" in line:
                    for part in line.split():
                        if part.startswith(VPN_SUBNET + "."):
                            used.add(part.split("/")[0])
    except FileNotFoundError:
        pass

    # Источник 2: клиентские конфиги в /clients/
    if os.path.isdir(CLIENTS_DIR):
        for fname in os.listdir(CLIENTS_DIR):
            if not fname.endswith(".conf"):
                continue
            try:
                with open(f"{CLIENTS_DIR}/{fname}") as f:
                    for line in f:
                        if line.startswith("Address"):
                            ip = line.split("=", 1)[1].strip().split("/")[0]
                            used.add(ip)
            except Exception:
                pass

    i = 2
    while f"{VPN_SUBNET}.{i}" in used:
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

def get_client_keys(name: str) -> dict | None:
    """Читает все ключи и параметры из клиентского .conf файла.
    Возвращает dict с priv, pub, ip, psk, obfs — или None если файл не найден/битый."""
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        return None
    try:
        data: dict = {}
        obfs: dict = {}
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "PrivateKey":
                    data["priv"] = v
                elif k == "Address":
                    data["ip"] = v.split("/")[0]
                elif k == "PresharedKey":
                    data["psk"] = v
                elif k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
                    obfs[k] = v
        # Публичный ключ — из .pub файла или вычисляем
        pub = get_client_pub(name)
        if not pub:
            return None
        data["pub"] = pub
        if obfs:
            data["obfs"] = obfs
        # Обязательные поля
        if not all(k in data for k in ("priv", "pub", "ip", "psk")):
            return None
        return data
    except Exception:
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

def make_wg_conf(priv, ip, psk, obfs, endpoint: str = None, allowed_ips: str = "0.0.0.0/0") -> str:
    ep = endpoint or SERVER_ENDPOINT
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {priv}", f"Address = {ip}/32",
        f"DNS = {PRIMARY_DNS}, {SECONDARY_DNS}",
        f"Jc = {obfs['Jc']}", f"Jmin = {obfs['Jmin']}", f"Jmax = {obfs['Jmax']}",
        f"S1 = {obfs['S1']}", f"S2 = {obfs['S2']}",
        f"H1 = {obfs['H1']}", f"H2 = {obfs['H2']}", f"H3 = {obfs['H3']}", f"H4 = {obfs['H4']}",
        "", "[Peer]", f"PublicKey = {SERVER_PUBLIC}", f"PresharedKey = {psk}",
        f"Endpoint = {ep}:{SERVER_PORT}", f"AllowedIPs = {allowed_ips}", "PersistentKeepalive = 25",
    ]) + "\n"

def make_vpn_link(priv, pub, ip, psk, obfs, name, endpoint: str = None) -> str:
    """Генерирует vpn:// ссылку для AmneziaVPN.
    endpoint — хост без порта; если не указан, используется SERVER_ENDPOINT."""
    ep = endpoint or SERVER_ENDPOINT
    wg = (
        f"[Interface]\nAddress = {ip}/32\nDNS = {PRIMARY_DNS}, {SECONDARY_DNS}\n"
        f"PrivateKey = {priv}\nJc = {obfs['Jc']}\nJmin = {obfs['Jmin']}\nJmax = {obfs['Jmax']}\n"
        f"S1 = {obfs['S1']}\nS2 = {obfs['S2']}\nH1 = {obfs['H1']}\nH2 = {obfs['H2']}\n"
        f"H3 = {obfs['H3']}\nH4 = {obfs['H4']}\n\n"
        f"[Peer]\nPublicKey = {SERVER_PUBLIC}\nPresharedKey = {psk}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {ep}:{SERVER_PORT}\nPersistentKeepalive = 25\n"
    )
    lc = {**obfs, "allowed_ips": ["0.0.0.0/0", "::/0"], "clientId": pub,
          "client_ip": ip, "client_priv_key": priv, "client_pub_key": pub,
          "config": wg, "hostName": ep, "mtu": "1420",
          "persistent_keep_alive": "25", "port": int(SERVER_PORT),
          "psk_key": psk, "server_pub_key": SERVER_PUBLIC}
    c = {"containers": [{"awg": {**obfs, "last_config": json.dumps(lc, indent=4),
         "port": str(SERVER_PORT), "subnet_address": ".".join(ip.split(".")[:3]) + ".0",
         "transport_proto": "udp"}, "container": "amnezia-awg"}],
         "defaultContainer": "amnezia-awg", "description": name,
         "dns1": PRIMARY_DNS, "dns2": SECONDARY_DNS,
         "hostName": ep, "nameOverriddenByUser": True}
    b = json.dumps(c, ensure_ascii=False).encode()
    p = struct.pack(">I", len(b)) + zlib.compress(b)
    return "vpn://" + base64.urlsafe_b64encode(p).decode().rstrip("=")

async def create_client(name: str, app=None, notify_chat_id: int = None):
    """Создаёт клиента AWG.
    Защита от гонок: asyncio.Lock() — один клиент за раз.
    Верификация: после всех шагов проверяем что пир реально появился в awg show.
    Откат: если верификация провалилась — удаляем всё что успели создать."""
    async with _client_lock:
        priv = subprocess.check_output(["awg", "genkey"], text=True).strip()
        pub  = subprocess.check_output(["awg", "pubkey"], input=priv, text=True).strip()
        psk  = subprocess.check_output(["awg", "genpsk"], text=True).strip()
        ip   = f"{VPN_SUBNET}.{next_ip()}"
        obfs = gen_obfs()

        os.makedirs(CLIENTS_DIR, exist_ok=True)
        conf_path = f"{CLIENTS_DIR}/{name}.conf"
        pub_path  = f"{CLIENTS_DIR}/{name}.pub"

        # Шаг 1: записываем в awg0.conf
        with open(AWG_CONF, "a") as f:
            f.write(f"\n# Client: {name}\n[Peer]\nPublicKey = {pub}\nPresharedKey = {psk}\nAllowedIPs = {ip}/32\n")

        # Шаг 2: применяем в живой интерфейс
        subprocess.run(["awg", "set", AWG_IFACE, "peer", pub,
                        "preshared-key", "/dev/stdin", "allowed-ips", f"{ip}/32"],
                       input=psk, text=True)

        # Шаг 3: создаём файлы клиента
        with open(conf_path, "w") as f:
            f.write(make_wg_conf(priv, ip, psk, obfs))
        with open(pub_path, "w") as f:
            f.write(pub)

        # Шаг 4: верификация — проверяем что пир реально зарегистрирован в awg
        dump = get_awg_dump()
        peer_ok = pub in dump and dump[pub].get("allowed", "").startswith(ip)

        # Шаг 4b: проверяем что запись есть в awg0.conf
        try:
            with open(AWG_CONF) as f:
                conf_content = f.read()
            conf_ok = pub in conf_content and f"{ip}/32" in conf_content
        except Exception:
            conf_ok = False

        # Шаг 4c: проверяем что файлы клиента созданы
        files_ok = os.path.exists(conf_path) and os.path.exists(pub_path)

        if not peer_ok or not conf_ok or not files_ok:
            # ОТКАТ: удаляем всё что успели создать
            logger.error(f"create_client({name}): верификация провалилась "
                         f"(peer_ok={peer_ok}, conf_ok={conf_ok}, files_ok={files_ok}), откат")
            try:
                subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"])
            except Exception:
                pass
            # Удаляем запись из awg0.conf
            try:
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
            except Exception:
                pass
            # Удаляем файлы
            for ext in [".conf", ".pub"]:
                p = f"{CLIENTS_DIR}/{name}{ext}"
                if os.path.exists(p):
                    os.remove(p)
            raise RuntimeError(
                f"Не удалось создать клиента '{name}': верификация провалилась. "
                f"Попробуйте снова."
            )

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
    """Job: каждые 5 секунд замеряет скорость, обновляет пики.
    В лог пишет раз в минуту чтобы не раздувать файл."""
    iface = _get_host_iface()
    now   = int(time.time())

    prev = context.bot_data.get("bw_prev")
    r2, t2 = _read_iface_bytes(iface)

    if prev:
        dt = now - prev["ts"]
        if dt > 0:
            rx_mbit = round((r2 - prev["rx"]) * 8 / 1_000_000 / dt, 2)
            tx_mbit = round((t2 - prev["tx"]) * 8 / 1_000_000 / dt, 2)
            # Метрика нагрузки на канал — максимум из двух направлений
            load    = round(max(rx_mbit, tx_mbit), 2)

            # Защита от фантомных всплесков после перезапуска
            if r2 < prev["rx"] or t2 < prev["tx"] or load > 10_000:
                context.bot_data["bw_prev"] = {"rx": r2, "tx": t2, "ts": now}
                return

            # Пики по max(RX, TX)
            peak    = load_bw_peak()
            today   = time.strftime("%Y-%m-%d")
            day_peak = peak.get("day", {})
            if day_peak.get("date") != today:
                day_peak = {"date": today, "load": 0, "rx": 0, "tx": 0}
            if load > day_peak.get("load", 0):
                day_peak = {"date": today, "load": load, "rx": rx_mbit, "tx": tx_mbit}

            all_peak = peak.get("all", {"load": 0, "rx": 0, "tx": 0})
            if load > all_peak.get("load", 0):
                all_peak = {"load": load, "rx": rx_mbit, "tx": tx_mbit}

            save_bw_peak({"day": day_peak, "all": all_peak,
                          "last": {"rx": rx_mbit, "tx": tx_mbit, "ts": now}})

            # В лог пишем раз в минуту — максимальный load за окно
            minute_max = context.bot_data.get("bw_minute_max", {"rx": 0, "tx": 0, "load": 0, "ts": now})
            if load > minute_max.get("load", 0):
                minute_max = {"rx": rx_mbit, "tx": tx_mbit, "load": load, "ts": minute_max["ts"]}
            context.bot_data["bw_minute_max"] = minute_max

            if now - minute_max["ts"] >= 60:
                try:
                    with open(BW_LOG_FILE, "a") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} "
                                f"RX={minute_max['rx']} TX={minute_max['tx']}\n")
                    lines = open(BW_LOG_FILE).readlines()
                    if len(lines) > 10080:
                        with open(BW_LOG_FILE, "w") as f:
                            f.writelines(lines[-10080:])
                except:
                    pass
                context.bot_data["bw_minute_max"] = {"rx": 0, "tx": 0, "load": 0, "ts": now}

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

def get_bw_histogram() -> dict | None:
    """Читает лог и строит распределение по диапазонам скорости (RX+TX суммарно)"""
    buckets = [
        (0,   50,  "0–50"),
        (50,  100, "50–100"),
        (100, 150, "100–150"),
        (150, 200, "150–200"),
        (200, 300, "200–300"),
        (300, 400, "300–400"),
        (400, 500, "400–500"),
        (500, None,"500+"),
    ]
    counts = [0] * len(buckets)
    total  = 0
    try:
        for line in open(BW_LOG_FILE).readlines():
            parts = line.strip().split()
            if len(parts) == 4:
                try:
                    rx = float(parts[2].split("=")[1])
                    tx = float(parts[3].split("=")[1])
                    val = rx + tx
                    total += 1
                    for i, (lo, hi, _) in enumerate(buckets):
                        if hi is None or val < hi:
                            counts[i] += 1
                            break
                except:
                    pass
    except:
        return None
    if total == 0:
        return None
    return {"buckets": buckets, "counts": counts, "total": total}

def fmt_histogram(hist: dict) -> list[str]:
    """Форматирует гистограмму для вывода в Telegram"""
    lines = ["\n📊 Распределение нагрузки (мин/сут, RX+TX):"]
    buckets = hist["buckets"]
    counts  = hist["counts"]
    total   = hist["total"]
    bar_max = 12  # максимальная длина полоски

    for i, (lo, hi, label) in enumerate(buckets):
        cnt  = counts[i]
        if cnt == 0:
            continue
        pct  = cnt / total * 100
        mins_per_day = cnt / max(total / 1440, 1)  # приводим к минутам в сутки
        bar_len = max(1, round(pct / 100 * bar_max)) if pct > 0 else 0
        bar  = "█" * bar_len

        # Цветовой маркер по диапазону
        if lo >= 500:   icon = "🔴"
        elif lo >= 300: icon = "🟠"
        elif lo >= 200: icon = "🟡"
        elif lo >= 100: icon = "🟢"
        else:           icon = "⚪"

        lines.append(
            f"{icon} {label:>8} Mbit/s  {bar:<{bar_max}}  "
            f"{pct:4.1f}%  ~{mins_per_day:.0f} мин/сут"
        )
    lines.append(f"   Всего замеров: {total} (~{total//1440} сут данных)")
    return lines

def get_bw_histogram_for(lines_data: list[str]) -> dict | None:
    """Строит гистограмму из переданных строк лога"""
    buckets = [
        (0,   50,  "0–50  "),
        (50,  100, "50–100"),
        (100, 150, "100–150"),
        (150, 200, "150–200"),
        (200, 300, "200–300"),
        (300, 400, "300–400"),
        (400, 500, "400–500"),
        (500, None,"500+  "),
    ]
    counts = [0] * len(buckets)
    total  = 0
    for line in lines_data:
        parts = line.strip().split()
        if len(parts) == 4:
            try:
                rx  = float(parts[2].split("=")[1])
                tx  = float(parts[3].split("=")[1])
                val = max(rx, tx)  # нагрузка на канал = максимум из двух направлений
                total += 1
                for i, (lo, hi, _) in enumerate(buckets):
                    if hi is None or val < hi:
                        counts[i] += 1
                        break
            except:
                pass
    if total == 0:
        return None
    return {"buckets": buckets, "counts": counts, "total": total}

def get_bw_histogram(period_days: int = 0) -> dict | None:
    """period_days=0 — всё время, иначе последние N дней"""
    try:
        all_lines = open(BW_LOG_FILE).readlines()
    except:
        return None
    if period_days > 0:
        cutoff = time.strftime(
            "%Y-%m-%d",
            time.localtime(time.time() - period_days * 86400)
        )
        all_lines = [l for l in all_lines if l[:10] >= cutoff]
    return get_bw_histogram_for(all_lines)

def get_log_days() -> list[str]:
    """Возвращает отсортированный список уникальных дат в логе"""
    days = set()
    try:
        for line in open(BW_LOG_FILE).readlines():
            parts = line.strip().split()
            if len(parts) == 4:
                days.add(parts[0])
    except:
        pass
    return sorted(days)

def get_bw_histogram_day(date_str: str) -> dict | None:
    """Гистограмма за конкретный день (YYYY-MM-DD)"""
    try:
        lines = [l for l in open(BW_LOG_FILE).readlines()
                 if l.startswith(date_str)]
    except:
        return None
    return get_bw_histogram_for(lines)

def fmt_histogram(hist: dict, period_label: str = "") -> list[str]:
    """Форматирует гистограмму для вывода в Telegram"""
    header = f"\n📊 Распределение нагрузки на канал"
    if period_label:
        header += f" ({period_label})"
    header += "\n   (по max из RX/TX, мин/сут):"
    lines  = [header]
    bar_max = 10
    for i, (lo, hi, label) in enumerate(hist["buckets"]):
        cnt = hist["counts"][i]
        if cnt == 0:
            continue
        pct  = cnt / hist["total"] * 100
        mins = cnt  # каждая запись = 1 минута
        bar  = "█" * max(1, round(pct / 100 * bar_max))
        if lo >= 500:   icon = "🔴"
        elif lo >= 300: icon = "🟠"
        elif lo >= 200: icon = "🟡"
        elif lo >= 100: icon = "🟢"
        else:           icon = "⚪"
        lines.append(f"{icon} {label} {bar:<{bar_max}}  {pct:4.1f}%  {mins} мин")
    lines.append(f"   Записей: {hist['total']}")
    return lines

async def show_bandwidth(query, period_days: int = 0):
    """Экран статистики трафика для админа"""
    iface = _get_host_iface()
    peak  = load_bw_peak()
    last  = peak.get("last", {})
    day   = peak.get("day", {})
    allp  = peak.get("all", {})
    top   = get_bw_top_fixed(5)

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

    monthly = get_vnstat_monthly()
    if monthly:
        lines.append(f"\n📦 Трафик по месяцам (↓вх + ↑исх = итого):")
        for m in monthly:
            lines.append(f"   {m['label']}  ↓{m['rx_gb']} + ↑{m['tx_gb']} = {m['total_gb']} GB")
        cur = monthly[-1]
        if cur["label"] == time.strftime("%Y-%m"):
            total = cur["total_gb"]
            warn = ""
            if total >= 4000:   warn = "  🔴 >4 TB!"
            elif total >= 3000: warn = "  🟠 >3 TB"
            elif total >= 2000: warn = "  🟡 >2 TB"
            elif total >= 1000: warn = "  🟢 >1 TB"
            lines.append(f"\n📅 Текущий месяц: {total} GB{warn}")
    else:
        lines.append("\n📦 Месячный трафик: vnstat ещё собирает данные.")

    if day:
        load_day = day.get("load", max(day.get("rx", 0), day.get("tx", 0)))
        lines.append(f"\n📅 Пик сегодня ({day.get('date', '—')}):")
        lines.append(f"   {load_day} Mbit/s  (↓{day.get('rx', 0)} ↑{day.get('tx', 0)})")
    if allp:
        load_all = allp.get("load", max(allp.get("rx", 0), allp.get("tx", 0)))
        lines.append(f"\n🏆 Абс. пик скорости:")
        lines.append(f"   {load_all} Mbit/s  (↓{allp.get('rx', 0)} ↑{allp.get('tx', 0)})")
    if top:
        lines.append(f"\n🔝 Топ-5 минут по нагрузке:")
        for dt, rx, tx in top:
            lines.append(f"   {dt}  ↓{rx} ↑{tx}")

    period_label = {0: "всё время", 7: "7 дней", 30: "30 дней"}.get(period_days, f"{period_days} дней")
    hist = get_bw_histogram(period_days)
    if hist:
        lines += fmt_histogram(hist, period_label)
    else:
        lines.append("\n📊 Гистограмма: данных пока нет, накапливается...")

    # Кнопки периода — подсвечиваем активный
    def p(label, days):
        mark = "✅ " if days == period_days else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"bw_period_{days}")

    kb = InlineKeyboardMarkup([
        [p("Всё время", 0), p("30 дней", 30), p("7 дней", 7)],
        [InlineKeyboardButton("📅 По дням",      callback_data="bw_days_0")],
        [InlineKeyboardButton("🗑 Сбросить пики", callback_data="bw_reset_ask")],
        [InlineKeyboardButton("🔄 Обновить",      callback_data=f"bw_period_{period_days}")],
        [InlineKeyboardButton("◀️ Статус",        callback_data="status")],
        [InlineKeyboardButton("◀️ В меню",        callback_data="back")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_days(query, page: int = 0):
    """Гистограмма по конкретному дню с листалкой"""
    days = get_log_days()
    if not days:
        await query.edit_message_text(
            "📊 Данных по дням пока нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="bandwidth")
            ]])
        )
        return

    # page=0 — последний день, листаем назад
    total_days = len(days)
    idx = total_days - 1 - page
    idx = max(0, min(idx, total_days - 1))
    date_str = days[idx]

    hist = get_bw_histogram_day(date_str)
    lines = [f"📅 Статистика за {date_str}"]
    if hist:
        lines += fmt_histogram(hist)
    else:
        lines.append("Нет данных за этот день.")

    has_prev = idx > 0            # есть более ранние дни
    has_next = idx < total_days - 1  # есть более поздние дни

    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️ Раньше", callback_data=f"bw_days_{page + 1}"))
    nav.append(InlineKeyboardButton(f"{idx + 1}/{total_days}", callback_data="noop"))
    if has_next:
        nav.append(InlineKeyboardButton("Позже ▶️", callback_data=f"bw_days_{page - 1}"))

    kb = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("◀️ Назад", callback_data="bandwidth")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_reset_ask(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Сбросить только пики",    callback_data="bw_reset_confirm")],
        [InlineKeyboardButton("💣 Сбросить всё (с нуля)",  callback_data="bw_reset_all_confirm")],
        [InlineKeyboardButton("❌ Отмена",                  callback_data="bandwidth")],
    ])
    await query.edit_message_text(
        "🗑 Сброс данных трафика\n\n"
        "🗑 *Только пики* — обнуляет абсолютный пик и пик дня.\n"
        "Лог и статистика vnstat сохраняются.\n\n"
        "💣 *Всё с нуля* — удаляет пики И лог замеров.\n"
        "Гистограмма и топ-5 начнут собираться заново.\n"
        "Статистика vnstat не затрагивается — её хранит система.",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def do_bw_reset(query):
    """Сброс только пиков"""
    peak = load_bw_peak()
    peak["all"] = {"total": 0, "rx": 0, "tx": 0}
    peak["day"]  = {}
    save_bw_peak(peak)
    await query.answer("✅ Пики сброшены", show_alert=False)
    await show_bandwidth(query)

async def do_bw_reset_all(query):
    """Полный сброс — пики + лог"""
    peak = {"all": {"total": 0, "rx": 0, "tx": 0}, "day": {}, "last": {}}
    save_bw_peak(peak)
    try:
        open(BW_LOG_FILE, "w").close()
    except:
        pass
    await query.answer("✅ Все данные сброшены", show_alert=False)
    await show_bandwidth(query)



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
        endpoint_line = f"🌐 {SERVER_ENDPOINT}:{SERVER_PORT}"
        if SERVER_ENDPOINT_BACKUP:
            endpoint_line += f"\n🔄 Резерв: {SERVER_ENDPOINT_BACKUP}:{SERVER_PORT}"
        text = (
            f"🔐 AmneziaWG — Панель администратора\n\n"
            f"{endpoint_line}\n"
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
    elif data == "cleanup" and is_admin:
        await do_cleanup(query)
    elif data == "backup" and is_admin:
        await do_backup(query)
    elif data == "maintenance" and is_admin:
        await show_maintenance(query)
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
    elif data == "maint_update_ip" and is_admin:
        await query.edit_message_text("⏳ Определяю текущий IP сервера...")
        real_ip = get_real_server_ip()
        if not real_ip:
            await query.edit_message_text(
                "❌ Не удалось определить внешний IP.\nПроверьте интернет-соединение сервера.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="maintenance")]])
            )
            return
        if real_ip == SERVER_IP:
            await query.edit_message_text(
                f"✅ IP актуален: `{SERVER_IP}`\nОбновление не требуется.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="maintenance")]]),
                parse_mode="Markdown"
            )
            return
        ep_note = ""
        if SERVER_ENDPOINT == SERVER_IP:
            ep_note = f"\n⚠️ SERVER_ENDPOINT тоже будет обновлён на `{real_ip}`."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Обновить", callback_data=f"update_ip_{real_ip}")],
            [InlineKeyboardButton("❌ Отмена",   callback_data="maintenance")],
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
    elif data == "help":
        await show_help(query)
    elif data == "add_cancel":
        await main_menu(query, user_id, edit=True)
    elif data == "excl_calc_cancel":
        context.user_data.pop("excl_allowed_ips", None)
        await query.edit_message_caption(caption="❌ Отменено.", reply_markup=None)
    elif data == "my_devices_back":
        await show_my_devices(query, user_id)
    elif data.startswith("device_"):
        await show_device(query, data[7:], user_id)
    # ── .conf: выбор эндпоинта ──
    elif data.startswith("conf_"):
        rest = data[5:]  # всё после "conf_"
        if rest.startswith("ep_"):
            # conf_ep_<epkey>_<name>  → показываем выбор исключений
            parts = rest[3:].split("_", 1)   # parts[0]=epkey, parts[1]=name
            if len(parts) == 2:
                await show_conf_excl_select(query, parts[1], user_id, parts[0])
        elif rest.startswith("excl_"):
            # conf_excl_<epkey>_<name> → открываем меню исключений
            parts = rest[5:].split("_", 1)
            if len(parts) == 2:
                await show_sites_menu(query, parts[1], user_id, context, ep_key=parts[0])
        elif rest.startswith("send_"):
            # conf_send_<epkey>_noexcl_<name> → отправляем без исключений
            parts = rest[5:].split("_noexcl_", 1)
            if len(parts) == 2:
                await do_send_conf(query, parts[1], parts[0])
        else:
            # conf_<name> — старый формат или прямой вызов → выбор эндпоинта
            await show_conf_ep_select(query, rest, user_id)
    # ── QR: выбор эндпоинта ──
    elif data.startswith("qr_"):
        rest = data[3:]
        if rest.startswith("ep_"):
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_qr(query, parts[1], parts[0])
        else:
            await show_qr_ep_select(query, rest, user_id)
    # ── Поделиться: выбор эндпоинта ──
    elif data.startswith("share_"):
        rest = data[6:]
        if rest.startswith("ep_"):
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
    elif data.startswith("sites_"):
        await show_sites_menu(query, data[6:], user_id, context)

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
    kb.append([InlineKeyboardButton("➕ Добавить сайт в исключения", callback_data="excl_calc")])
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
        [InlineKeyboardButton("📄 Скачать .conf (AmneziaWG)", callback_data=f"conf_{name}")],
        [InlineKeyboardButton("📱 QR-код (AmneziaWG)",        callback_data=f"qr_{name}")],
        [InlineKeyboardButton("📤 Поделиться кодом (AmneziaVPN)", callback_data=f"share_{name}")],
        [InlineKeyboardButton("🗑 Удалить",                    callback_data=f"del_{name}")],
        [InlineKeyboardButton("◀️ Назад",                      callback_data=back_target)],
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
# ВЫБОР ЭНДПОИНТА ДЛЯ .CONF
# ══════════════════════════════════════════════════════════════════════════════

def _endpoint_kb(name: str, action: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора эндпоинта. action = 'conf' | 'qr' | 'share'"""
    rows = []
    # Основной — всегда есть
    rows.append([InlineKeyboardButton(
        f"🌐 Основной ({SERVER_ENDPOINT})",
        callback_data=f"{action}_ep_main_{name}"
    )])
    # Резервный — только если задан
    if SERVER_ENDPOINT_BACKUP:
        rows.append([InlineKeyboardButton(
            f"🔄 Резервный ({SERVER_ENDPOINT_BACKUP})",
            callback_data=f"{action}_ep_backup_{name}"
        )])
    # По IP — только если эндпоинт не совпадает с IP
    if SERVER_ENDPOINT != SERVER_IP:
        rows.append([InlineKeyboardButton(
            f"🔢 По IP ({SERVER_IP})",
            callback_data=f"{action}_ep_ip_{name}"
        )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"device_{name}")])
    return InlineKeyboardMarkup(rows)

def _resolve_endpoint(ep_key: str) -> str:
    """ep_key: 'main' | 'backup' | 'ip'  →  строка эндпоинта"""
    if ep_key == "backup":
        return SERVER_ENDPOINT_BACKUP
    if ep_key == "ip":
        return SERVER_IP
    return SERVER_ENDPOINT

def _conf_for_endpoint(name: str, ep_key: str, allowed_ips: str = "0.0.0.0/0") -> bytes:
    """Читает базовый .conf, подставляет нужный эндпоинт и AllowedIPs, возвращает bytes."""
    ep = _resolve_endpoint(ep_key)
    with open(f"{CLIENTS_DIR}/{name}.conf") as f:
        base = f.read()
    result = re.sub(r"^Endpoint = .+$", f"Endpoint = {ep}:{SERVER_PORT}", base, flags=re.MULTILINE)
    result = re.sub(r"^AllowedIPs = .+$", f"AllowedIPs = {allowed_ips}", result, flags=re.MULTILINE)
    return result.encode()

async def show_conf_ep_select(query, name: str, user_id: int):
    """Экран выбора эндпоинта для .conf"""
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    # Если только один эндпоинт (основной == IP, резервного нет) — пропускаем выбор
    has_backup = bool(SERVER_ENDPOINT_BACKUP)
    has_ip     = SERVER_ENDPOINT != SERVER_IP
    if not has_backup and not has_ip:
        # Сразу показываем выбор исключений для основного
        await show_conf_excl_select(query, name, user_id, "main")
        return
    short = name.split(".", 1)[1] if "." in name else name
    await query.edit_message_text(
        f"📄 Скачать .conf для *{short}*\n\nВыберите канал подключения:",
        reply_markup=_endpoint_kb(name, "conf"),
        parse_mode="Markdown"
    )

async def show_qr_ep_select(query, name: str, user_id: int):
    """Экран выбора эндпоинта для QR"""
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    has_backup = bool(SERVER_ENDPOINT_BACKUP)
    has_ip     = SERVER_ENDPOINT != SERVER_IP
    if not has_backup and not has_ip:
        await do_send_qr(query, name, "main")
        return
    short = name.split(".", 1)[1] if "." in name else name
    await query.edit_message_text(
        f"📱 QR-код для *{short}*\n\nВыберите канал подключения:",
        reply_markup=_endpoint_kb(name, "qr"),
        parse_mode="Markdown"
    )

async def show_share_ep_select(query, name: str, user_id: int):
    """Экран выбора эндпоинта для vpn:// ссылки"""
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    has_backup = bool(SERVER_ENDPOINT_BACKUP)
    has_ip     = SERVER_ENDPOINT != SERVER_IP
    if not has_backup and not has_ip:
        await do_send_share(query, name, "main")
        return
    short = name.split(".", 1)[1] if "." in name else name
    await query.edit_message_text(
        f"📤 Поделиться кодом для *{short}*\n\nВыберите канал подключения:",
        reply_markup=_endpoint_kb(name, "share"),
        parse_mode="Markdown"
    )

# ── Экран выбора: пропустить исключения / настроить исключения ────────────────

async def show_conf_excl_select(query, name: str, user_id: int, ep_key: str):
    """После выбора эндпоинта предлагаем: пропустить или настроить исключения."""
    short = name.split(".", 1)[1] if "." in name else name
    ep    = _resolve_endpoint(ep_key)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Пропустить (весь трафик через VPN)",
                              callback_data=f"conf_send_{ep_key}_noexcl_{name}")],
        [InlineKeyboardButton("🌐 Настроить исключения сайтов",
                              callback_data=f"conf_excl_{ep_key}_{name}")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"device_{name}")],
    ])
    await query.edit_message_text(
        f"📄 .conf для *{short}*\n🌐 Эндпоинт: `{ep}`\n\n"
        f"Выберите режим трафика:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# ── Финальная отправка .conf ──────────────────────────────────────────────────

async def do_send_conf(query, name: str, ep_key: str, allowed_ips: str = "0.0.0.0/0"):
    """Генерирует .conf в памяти и отправляет в чат. Без дефисов в имени файла."""
    short    = name.split(".", 1)[1] if "." in name else name
    ep       = _resolve_endpoint(ep_key)
    content  = _conf_for_endpoint(name, ep_key, allowed_ips)
    filename = f"{short}.conf"  # без дефисов — AmneziaWG их не любит
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
    """Генерирует .conf в памяти → QR → отправляет, без файлов на диске."""
    short   = name.split(".", 1)[1] if "." in name else name
    ep      = _resolve_endpoint(ep_key)
    content = _conf_for_endpoint(name, ep_key)
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)

    # Пишем во временный файл только для qrencode — сразу удаляем
    tmp_conf = f"/tmp/qr_{name}_{ep_key}.conf"
    qr_path  = f"/tmp/qr_{name}_{ep_key}.png"
    try:
        with open(tmp_conf, "wb") as f:
            f.write(content)
        subprocess.run(["qrencode", "-o", qr_path, "-r", tmp_conf], check=True)
        await query.message.reply_photo(
            photo=open(qr_path, "rb"),
            caption=(
                f"📱 QR для AmneziaWG — *{short}* ({ep_label})\n"
                f"🌐 Endpoint: `{ep}:{SERVER_PORT}`"
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
    short    = name.split(".", 1)[1] if "." in name else name
    ep       = _resolve_endpoint(ep_key)
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
        filename=f"{short}.vpn",
        caption=(
            f"📤 Файл для AmneziaVPN — *{short}* ({ep_label})\n"
            f"Вставьте в приложении: + → Открыть файл"
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ══════════════════════════════════════════════════════════════════════════════
# УДАЛЕНИЕ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════
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

async def start_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — предупреждение и запрос файла"""
    query = update.callback_query
    await query.answer()
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

    # 1. Останавливаем AWG — PostDown почистит iptables по живому конфигу
    subprocess.run(["systemctl", "stop", f"awg-quick@{AWG_IFACE}"])

    # 2. Чистим clients/ чтобы не осталось мусора от старых клиентов
    if os.path.exists(CLIENTS_DIR):
        shutil.rmtree(CLIENTS_DIR)
    os.makedirs(CLIENTS_DIR)

    # 3. Распаковываем бэкап
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

    # 4. Поднимаем AWG с новым конфигом, перезапускаем бота
    subprocess.Popen(
        ["bash", "-c",
         f"sleep 2 && systemctl start awg-quick@{AWG_IFACE} && systemctl restart awg-bot"],
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

    # Текущий часовой пояс
    try:
        tz_sys = subprocess.check_output(["cat", "/etc/timezone"], text=True).strip()
    except:
        tz_sys = TZ

    text = (
        f"🔧 Техобслуживание\n\n"
        f"📅 Последнее: {last_date}\n\n"
        f"🖥 Система: {ubuntu}\n"
        f"⚙️ Ядро: {kernel}\n"
        f"🐍 python-telegram-bot: {ptb_ver}\n"
        f"🕐 Часовой пояс: {tz_sys} (бот: {TZ})\n\n"
        f"Рекомендуется проводить раз в 6 месяцев."
    )
    # Проверяем актуальность IP для отображения в меню
    real_ip = get_real_server_ip()
    ip_status = ""
    if real_ip and real_ip != SERVER_IP:
        ip_status = f"\n\n⚠️ IP расходится!\nВ настройках: {SERVER_IP}\nРеальный: {real_ip}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Бэкап + apt upgrade",         callback_data="maint_upgrade")],
        [InlineKeyboardButton("📦 Проверить версию библиотеки", callback_data="maint_ptb")],
        [InlineKeyboardButton("🕐 Сменить часовой пояс",        callback_data="maint_tz")],
        [InlineKeyboardButton("🔄 Обновить IP сервера",         callback_data="maint_update_ip")],
        [InlineKeyboardButton("✅ Отмечено — всё ок",            callback_data="maint_done")],
        [InlineKeyboardButton("◀️ В меню",                       callback_data="back")],
    ])
    await query.edit_message_text(text + ip_status, reply_markup=kb)

async def show_maint_tz(query):
    """Экран выбора часового пояса"""
    popular_tz = [
        ("🇷🇺 Москва",       "Europe/Moscow"),
        ("🇷🇺 Екатеринбург", "Asia/Yekaterinburg"),
        ("🇷🇺 Новосибирск",  "Asia/Novosibirsk"),
        ("🇷🇺 Владивосток",  "Asia/Vladivostok"),
        ("🇺🇦 Киев",         "Europe/Kiev"),
        ("🇰🇿 Алматы",       "Asia/Almaty"),
        ("🇩🇪 Берлин",       "Europe/Berlin"),
        ("🌍 UTC",           "UTC"),
    ]
    try:
        sys_tz = subprocess.check_output(["cat", "/etc/timezone"], text=True).strip()
    except:
        sys_tz = "неизвестно"

    now_local = time.strftime("%H:%M %d.%m.%Y")
    kb_rows = []
    for label, tz in popular_tz:
        mark = "✅ " if tz == TZ else ""
        kb_rows.append([InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"set_tz_{tz}"
        )])
    mark_sys = "✅ " if sys_tz == TZ else ""
    kb_rows.append([InlineKeyboardButton(
        f"{mark_sys}🖥 Как у сервера ({sys_tz})", callback_data=f"set_tz_{sys_tz}"
    )])
    kb_rows.append([InlineKeyboardButton("⌨️ Ввести вручную", callback_data="set_tz_manual")])
    kb_rows.append([InlineKeyboardButton("◀️ Назад", callback_data="maintenance")])
    await query.edit_message_text(
        f"🕐 Часовой пояс\n\n"
        f"Текущий бота: *{TZ}*\n"
        f"Системный:    *{sys_tz}*\n"
        f"Время бота:   {now_local}\n\n"
        f"Выберите из списка, укажите как у сервера, или введите вручную.\n"
        f"Бот перезапустится автоматически.",
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode="Markdown"
    )

async def ask_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просим ввести пояс вручную"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⌨️ Введите часовой пояс вручную\n\n"
        "Примеры: `Europe/Moscow`, `Asia/Tokyo`, `America/New_York`, `UTC`\n\n"
        "Полный список: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones\n\n"
        "Напишите пояс в ответном сообщении или /cancel для отмены.",
        parse_mode="Markdown"
    )
    return WAITING_TZ_INPUT

async def receive_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем ручной ввод пояса и применяем"""
    tz = update.message.text.strip()
    try:
        all_tz = subprocess.check_output(["timedatectl", "list-timezones"], text=True).splitlines()
    except:
        all_tz = []
    if all_tz and tz not in all_tz:
        await update.message.reply_text(
            f"❌ Часовой пояс `{tz}` не найден.\n\n"
            f"Проверьте написание и попробуйте снова, или /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_TZ_INPUT
    # Применяем
    try:
        env_lines = open(ENV_FILE).readlines()
        new_lines = [l for l in env_lines if not l.startswith("TIMEZONE=")]
        new_lines.append(f"TIMEZONE={tz}\n")
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        subprocess.run(["timedatectl", "set-timezone", tz], check=True)
        await update.message.reply_text(
            f"✅ Часовой пояс изменён на *{tz}*\n\nБот перезапускается...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", "sleep 2 && systemctl restart awg-bot"])
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    return ConversationHandler.END


async def do_set_tz(query, tz: str):
    """Применяет новый часовой пояс — пишет в server.env и перезапускает бота"""
    try:
        # Проверяем что пояс существует
        subprocess.check_output(["timedatectl", "list-timezones"], text=True)
        # Пишем в server.env
        env_lines = open(ENV_FILE).readlines()
        new_lines = [l for l in env_lines if not l.startswith("TIMEZONE=")]
        new_lines.append(f"TIMEZONE={tz}\n")
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        # Устанавливаем системный часовой пояс тоже
        subprocess.run(["timedatectl", "set-timezone", tz], check=True)
        await query.edit_message_text(
            f"✅ Часовой пояс изменён на *{tz}*\n\nБот перезапустится через 3 секунды...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", "sleep 3 && systemctl restart awg-bot"])
    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка: {e}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="maintenance")
            ]])
        )

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
# ИСКЛЮЧЕНИЯ САЙТОВ (split tunneling)
# ══════════════════════════════════════════════════════════════════════════════

async def show_sites_menu(query, name: str, user_id: int, context, ep_key: str = "main"):
    """Меню исключений. ep_key передаётся из conf_excl_<ep_key>_<name>."""
    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    context.user_data["sites_device"]   = name
    context.user_data["sites_selected"] = set(DEFAULT_SELECTED)
    context.user_data["sites_ep_key"]   = ep_key

    short = name.split(".", 1)[1] if "." in name else name
    ep    = _resolve_endpoint(ep_key)
    await query.edit_message_text(
        f"🌐 Исключения сайтов для *{short}*\n"
        f"🌐 Эндпоинт: `{ep}`\n\n"
        f"Отмеченные сайты будут работать *без VPN*.\n"
        f"🏠 Локальная сеть включена всегда.",
        reply_markup=sites_keyboard(context.user_data["sites_selected"], name),
        parse_mode="Markdown",
    )


async def toggle_site_handler(query, key: str, user_id: int, context):
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return

    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    if key in DEFAULT_SELECTED:
        await query.answer()
        return

    selected = context.user_data.get("sites_selected", set(DEFAULT_SELECTED))
    if key in selected:
        selected.discard(key)
    else:
        selected.add(key)
    context.user_data["sites_selected"] = selected

    short = name.split(".", 1)[1] if "." in name else name
    await query.edit_message_text(
        f"🌐 Исключения сайтов для *{short}*\n\n"
        f"Отмеченные сайты будут работать *без VPN*.\n"
        f"🏠 Локальная сеть включена всегда.",
        reply_markup=sites_keyboard(selected, name),
        parse_mode="Markdown",
    )


async def apply_sites(query, user_id: int, context):
    """Применяет исключения сайтов — генерирует .conf в памяти с нужным эндпоинтом."""
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return

    user_prefix = get_user_name(user_id) + "."
    if user_id != ADMIN_ID and not name.startswith(user_prefix):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    selected  = context.user_data.get("sites_selected", set(DEFAULT_SELECTED))
    ep_key    = context.user_data.get("sites_ep_key", "main")
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        await query.edit_message_text("❌ Файл конфига не найден.", reply_markup=back_kb())
        return

    await query.edit_message_text("⏳ Резолвлю IP-адреса, подождите...")

    allowed_ips = build_allowed_ips(selected)
    excl_count  = len(selected - DEFAULT_SELECTED)

    context.user_data.pop("sites_device", None)
    context.user_data.pop("sites_selected", None)
    context.user_data.pop("sites_ep_key", None)

    # Отправляем конфиг с исключениями через общий механизм
    await do_send_conf(query, name, ep_key, allowed_ips)



# ══════════════════════════════════════════════════════════════════════════════
# КАЛЬКУЛЯТОР ИСКЛЮЧЕНИЙ САЙТОВ
# ══════════════════════════════════════════════════════════════════════════════

async def excl_calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — инструкция с картинками, просим вставить AllowedIPs"""
    query = update.callback_query
    await query.answer()

    # Шаг 1 — выбрать туннель
    await query.message.reply_photo(
        photo=f"{IMG_BASE}/111.jpg",
        caption=(
            "➕ *Добавить сайт в исключения*\n\n"
            "*Шаг 1.* Откройте AmneziaWG на своём устройстве и выберите туннель, "
            "в который хотите внести изменения."
        ),
        parse_mode="Markdown"
    )
    # Шаг 2 — нажать редактировать
    await query.message.reply_photo(
        photo=f"{IMG_BASE}/222.jpg",
        caption="*Шаг 2.* Нажмите кнопку *Редактировать* (значок карандаша).",
        parse_mode="Markdown"
    )
    # Шаг 3 — скопировать AllowedIPs
    await query.message.reply_photo(
        photo=f"{IMG_BASE}/333.jpg",
        caption=(
            "*Шаг 3.* Найдите поле *AllowedIPs*, выделите и скопируйте всё его содержимое.\n\n"
            "📋 Вставьте скопированную строку в следующем сообщении."
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="excl_calc_cancel")]
        ])
    )
    return WAITING_EXCL_ALLOWED


async def excl_calc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена через кнопку"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("excl_allowed_ips", None)
    await query.edit_message_caption(
        caption="❌ Отменено.",
        reply_markup=None
    )
    return ConversationHandler.END


async def excl_receive_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — получаем строку AllowedIPs, просим домен"""
    text = update.message.text.strip()

    # Базовая валидация — должны быть цифры, точки, слэши, запятые
    if not any(c.isdigit() for c in text) or "/" not in text:
        await update.message.reply_text(
            "❌ Это не похоже на строку AllowedIPs.\n\n"
            "Она должна содержать IP-адреса вида `10.0.0.0/8, 172.16.0.0/12`\n\n"
            "Попробуйте скопировать ещё раз или нажмите /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_EXCL_ALLOWED

    context.user_data["excl_allowed_ips"] = text

    await update.message.reply_text(
        "✅ Строка получена!\n\n"
        "*Шаг 4.* Теперь введите домен сайта который хотите исключить из VPN трафика.\n\n"
        "Примеры:\n"
        "`sberbank.ru`\n"
        "`gosuslugi.ru`\n"
        "`tbank.ru`\n\n"
        "Просто введите домен без `http://` и без `www`.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data="my_devices_back")]
        ])
    )
    return WAITING_EXCL_DOMAIN


async def excl_receive_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — получаем домен, резолвим, вычисляем новую строку"""
    domain = update.message.text.strip().lower()
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    if "." not in domain or len(domain) < 4:
        await update.message.reply_text(
            "❌ Неверный формат домена. Введите например: `sberbank.ru`",
            parse_mode="Markdown"
        )
        return WAITING_EXCL_DOMAIN

    allowed_str = context.user_data.get("excl_allowed_ips", "")
    if not allowed_str:
        await update.message.reply_text("❌ Сессия устарела. Начните заново.")
        return ConversationHandler.END

    await update.message.reply_text(f"⏳ Резолвлю `{domain}`...", parse_mode="Markdown")

    # Резолвим домен в IP
    try:
        results = socket.getaddrinfo(domain, None, socket.AF_INET)
        domain_ips = list({r[4][0] for r in results})
    except Exception:
        await update.message.reply_text(
            f"❌ Не удалось определить IP для домена `{domain}`.\n\n"
            f"Проверьте правильность написания домена.",
            parse_mode="Markdown"
        )
        return WAITING_EXCL_DOMAIN

    # Парсим текущие AllowedIPs
    try:
        allowed_nets = []
        for part in allowed_str.split(","):
            part = part.strip()
            if part:
                allowed_nets.append(ipaddress.ip_network(part, strict=False))
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Ошибка парсинга строки AllowedIPs: {e}\n\nПопробуйте скопировать строку заново.",
        )
        return WAITING_EXCL_ALLOWED

    # Вычитаем IP домена из AllowedIPs
    for ip_str in domain_ips:
        target = ipaddress.ip_network(f"{ip_str}/32", strict=False)
        new_nets = []
        for net in allowed_nets:
            if target.overlaps(net):
                new_nets.extend(net.address_exclude(target))
            else:
                new_nets.append(net)
        allowed_nets = new_nets

    new_allowed = ", ".join(
        str(n) for n in sorted(allowed_nets, key=lambda n: (n.network_address, n.prefixlen))
    )

    ip_list = ", ".join(domain_ips)
    context.user_data.pop("excl_allowed_ips", None)

    await update.message.reply_text(
        f"✅ Готово!\n\n"
        f"🌐 Домен `{domain}` → IP: `{ip_list}`\n\n"
        f"*Скопируйте строку ниже и вставьте её в поле AllowedIPs вместо старого содержимого, "
        f"затем нажмите Сохранить:*",
        parse_mode="Markdown"
    )
    # Отправляем результат отдельным сообщением — удобнее копировать
    await update.message.reply_text(
        f"`{new_allowed}`",
        parse_mode="Markdown"
    )

    context.user_data.pop("excl_allowed_ips", None)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНИЕ УСТРОЙСТВА (ConversationHandler)
# ══════════════════════════════════════════════════════════════════════════════

async def cancel_add_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена добавления устройства через кнопку Отмена"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("adding_user_id", None)
    await main_menu(query, query.from_user.id, edit=True)
    return ConversationHandler.END

async def add_device_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="add_cancel")]
    ])
    await query.edit_message_text(
        f"➕ Добавление устройства\n\n"
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
    # Убираем дефисы — AmneziaWG их не любит в имени файла
    device_raw = "".join(c for c in raw if c.isascii() and (c.isalnum() or c == "_"))
    device_raw = device_raw.capitalize()

    if not device_raw:
        await update.message.reply_text(
            "❌ Введите название *латиницей*, только буквы и цифры. Например: `Phone`",
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
        await create_client(full_name)
    except Exception as e:
        logger.error(f"receive_device_name: create_client failed: {e}")
        await update.message.reply_text(
            f"❌ Не удалось создать устройство *{device_raw}*.\n\n"
            f"`{e}`\n\n"
            f"Попробуйте ещё раз или обратитесь к администратору.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Перейти к устройству", callback_data=f"device_{full_name}")],
        [InlineKeyboardButton("◀️ В меню", callback_data="back")],
    ])
    await update.message.reply_text(
        f"✅ Устройство *{device_raw}* создано!\n\n"
        f"Теперь перейдите в карточку устройства и выберите способ подключения:\n"
        f"• 📄 *.conf* файл — для AmneziaWG (рекомендуется)\n"
        f"• 📱 *QR-код* — для AmneziaWG на телефоне\n"
        f"• 📤 *Поделиться кодом* — для AmneziaVPN (раздельное туннелирование)\n\n"
        f"Для каждого варианта можно выбрать канал подключения и настроить исключения сайтов.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ПРОВЕРКА И ОБНОВЛЕНИЕ IP СЕРВЕРА
# ══════════════════════════════════════════════════════════════════════════════

def get_real_server_ip() -> str | None:
    """Получает реальный внешний IPv4 сервера."""
    for url in ["https://ifconfig.me", "https://api.ipify.org", "https://ifconfig.co"]:
        try:
            out = subprocess.check_output(
                ["curl", "-4", "-s", "--max-time", "5", url],
                text=True
            ).strip()
            if out and "." in out and ":" not in out:
                return out
        except:
            pass
    return None

async def check_ip_on_start(context: ContextTypes.DEFAULT_TYPE):
    """Job: запускается один раз через 15 секунд после старта.
    Сравнивает реальный IP с SERVER_IP в server.env.
    Если расходятся — уведомляет админа."""
    real_ip = get_real_server_ip()
    if not real_ip:
        logger.warning("check_ip_on_start: не удалось получить внешний IP")
        return
    if real_ip == SERVER_IP:
        logger.info(f"check_ip_on_start: IP актуален ({SERVER_IP})")
        return
    # IP расходится — уведомляем админа
    ep_note = ""
    if SERVER_ENDPOINT == SERVER_IP:
        ep_note = f"\n⚠️ SERVER_ENDPOINT тоже указывает на старый IP и будет обновлён."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить IP", callback_data=f"update_ip_{real_ip}")],
        [InlineKeyboardButton("❌ Пропустить",  callback_data="update_ip_skip")],
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"⚠️ IP сервера изменился!\n\n"
            f"В настройках: `{SERVER_IP}`\n"
            f"Реальный IP:  `{real_ip}`{ep_note}\n\n"
            f"Обновить server.env?"
        ),
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def do_update_ip(query, new_ip: str):
    """Обновляет SERVER_IP (и SERVER_ENDPOINT если он был равен старому IP) в server.env."""
    global SERVER_IP, SERVER_ENDPOINT
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()

        updated = []
        ep_updated = False
        for line in lines:
            if line.startswith("SERVER_IP="):
                updated.append(f"SERVER_IP={new_ip}\n")
            elif line.startswith("SERVER_ENDPOINT=") and SERVER_ENDPOINT == SERVER_IP:
                updated.append(f"SERVER_ENDPOINT={new_ip}\n")
                ep_updated = True
            else:
                updated.append(line)

        with open(ENV_FILE, "w") as f:
            f.writelines(updated)

        ep_note = f"\n✅ SERVER_ENDPOINT обновлён: `{new_ip}`" if ep_updated else ""
        old_ip = SERVER_IP
        await query.edit_message_text(
            f"✅ IP обновлён!\n\n"
            f"Старый: `{old_ip}`\n"
            f"Новый:  `{new_ip}`{ep_note}\n\n"
            f"⏳ Бот перезапускается...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", "sleep 2 && systemctl restart awg-bot"])
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка обновления IP: {e}")

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

    excl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(excl_calc_start, pattern="^excl_calc$")],
        states={
            WAITING_EXCL_ALLOWED: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, excl_receive_allowed),
                CallbackQueryHandler(excl_calc_cancel, pattern="^excl_calc_cancel$"),
            ],
            WAITING_EXCL_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, excl_receive_domain),
                CallbackQueryHandler(lambda u, c: (show_my_devices(u.callback_query, u.callback_query.from_user.id), ConversationHandler.END)[1], pattern="^my_devices_back$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
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

    app.add_handler(reg_conv)
    app.add_handler(add_conv)
    app.add_handler(restore_conv)
    app.add_handler(excl_conv)
    app.add_handler(tz_conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    # Проверка напоминания о техобслуживании — раз в сутки
    app.job_queue.run_repeating(maintenance_reminder, interval=86400, first=60)
    # Мониторинг трафика — каждые 5 секунд (пики), в лог раз в минуту
    app.job_queue.run_repeating(bw_monitor_job, interval=5, first=10)
    # Проверка IP сервера — один раз через 15 секунд после старта
    app.job_queue.run_once(check_ip_on_start, when=15)

    logger.info(f"Бот запущен. Admin ID: {ADMIN_ID}")
    print(f"\n\033[0;32m✓ Бот запущен! Admin ID: {ADMIN_ID}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()