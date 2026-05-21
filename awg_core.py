#!/usr/bin/env python3
# awg_core.py — ядро: конфиг, пользователи, серверы, subnet-кэш, бэкап
# Импортируется из bot.py, tma_server.py и sub-модулей.
# Не содержит ничего Telegram-специфичного и ничего HTTP-специфичного.
# Version: 1.2

import os, subprocess, logging, json, socket, ipaddress, threading, time
import tarfile

from sites_data import SITES, CATEGORIES, DEFAULT_SELECTED, ALL_SELECTABLE

# ── Пути ───────────────────────────────────────────────────────────────────────
CONFIG_FILE      = "/etc/amnezia/amneziawg/bot.env"
ENV_FILE         = "/etc/amnezia/amneziawg/server.env"
USERS_FILE       = "/etc/amnezia/amneziawg/users.json"
CLIENTS_DIR      = "/etc/amnezia/amneziawg/clients"
BACKUP_DIR       = "/etc/amnezia/amneziawg/backups"
MAINTENANCE_FILE = "/etc/amnezia/amneziawg/maintenance.json"
BW_LOG_FILE      = "/var/log/awg-bw.log"
BW_PEAK_FILE     = "/etc/amnezia/amneziawg/bw_peak.json"
EXCL_EXT         = ".excl.json"
SERVERS_FILE     = "/etc/amnezia/amneziawg/servers.json"
SUBNET_CACHE_FILE = "/etc/amnezia/amneziawg/subnet_cache.json"
ADMIN_KEY_PATH   = "/root/.ssh/awg_admin_key"

logger = logging.getLogger(__name__)

# Lock для атомарного чтения/записи subnet_cache.json
_CACHE_LOCK = threading.Lock()

# ── Загрузка конфигов ──────────────────────────────────────────────────────────
def load_env(path: str) -> dict:
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

cfg = load_env(CONFIG_FILE)
srv = load_env(ENV_FILE)

BOT_TOKEN              = cfg.get("BOT_TOKEN", "")
ADMIN_ID               = int(cfg.get("ADMIN_ID", "0"))
SERVER_IP              = srv.get("SERVER_IP", "")
SERVER_PORT            = srv.get("SERVER_PORT", "")
SERVER_PUBLIC          = srv.get("SERVER_PUBLIC", "")
VPN_SUBNET             = srv.get("VPN_SUBNET", "")
AWG_IFACE              = srv.get("VPN_IFACE", "awg0")
AWG_CONF               = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
PRIMARY_DNS            = srv.get("PRIMARY_DNS", "1.1.1.1")
SECONDARY_DNS          = srv.get("SECONDARY_DNS", "1.0.0.1")
TZ                     = srv.get("TIMEZONE", "UTC")
SERVER_ENDPOINT        = srv.get("SERVER_ENDPOINT", "") or SERVER_IP
SERVER_ENDPOINT_BACKUP = srv.get("SERVER_ENDPOINT_BACKUP", "")
TMA_URL                = srv.get("TMA_URL", "")

# ── Системные константы (сервисы, бинарники, флаги) ───────────────────────────
BOT_SERVICE  = srv.get("BOT_SERVICE",  "awg-bot")
AWG_SERVICE  = f"awg-quick@{AWG_IFACE}"

QRENCODE_BIN = srv.get("QRENCODE_BIN", "qrencode")

IP_CHECK_URLS = [
    srv.get("IP_CHECK_URL_1", "https://ifconfig.me"),
    srv.get("IP_CHECK_URL_2", "https://api.ipify.org"),
    srv.get("IP_CHECK_URL_3", "https://ifconfig.co"),
]

RESTART_FLAG_FILE = srv.get("RESTART_FLAG_FILE", "/tmp/awg_bot_restart_flag")

os.environ["TZ"] = TZ
try:
    time.tzset()
except AttributeError:
    pass

# ── Серверы (мультисервер) ─────────────────────────────────────────────────────
_servers_cache: list | None = None
_servers_cache_ts: float = 0.0
_SERVERS_CACHE_TTL: float = 10.0

def _init_primary_server() -> dict:
    """Создаёт запись первичного сервера из server.env."""
    endpoints: list = []
    if SERVER_ENDPOINT and SERVER_ENDPOINT != SERVER_IP:
        endpoints.append({"value": SERVER_ENDPOINT, "type": "domain", "verified": False})
    if SERVER_IP:
        endpoints.append({"value": SERVER_IP, "type": "ip"})
    if SERVER_ENDPOINT_BACKUP:
        endpoints.append({"value": SERVER_ENDPOINT_BACKUP, "type": "domain", "verified": False})
    return {
        "id": "primary",
        "name": "Основной",
        "emoji": "🖥",
        "is_primary": True,
        "ssh": {"ip": SERVER_IP, "port": 22, "login": "root", "password": ""},
        "awg_public_key": SERVER_PUBLIC,
        "awg_port": int(SERVER_PORT) if SERVER_PORT else 51820,
        "endpoints": endpoints,
    }

def invalidate_servers_cache():
    global _servers_cache, _servers_cache_ts
    _servers_cache = None
    _servers_cache_ts = 0.0

def load_servers() -> list:
    """Загружает список серверов; при отсутствии файла создаёт из server.env."""
    global _servers_cache, _servers_cache_ts
    now = time.monotonic()
    if _servers_cache is not None and (now - _servers_cache_ts) < _SERVERS_CACHE_TTL:
        return _servers_cache
    if os.path.exists(SERVERS_FILE):
        try:
            with open(SERVERS_FILE) as f:
                data = json.load(f)
            _servers_cache = data.get("servers", [])
            _servers_cache_ts = now
            return _servers_cache
        except Exception:
            pass
    primary = _init_primary_server()
    save_servers([primary])
    _servers_cache = [primary]
    _servers_cache_ts = now
    return _servers_cache

def save_servers(servers: list):
    """Сохраняет список серверов и сбрасывает кэш."""
    global _servers_cache, _servers_cache_ts
    os.makedirs(os.path.dirname(SERVERS_FILE), exist_ok=True)
    with open(SERVERS_FILE, "w") as f:
        json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SERVERS_FILE, 0o600)
    except Exception:
        pass
    _servers_cache = servers
    _servers_cache_ts = time.monotonic()

# ── Пользователи ───────────────────────────────────────────────────────────────
_users_cache: dict | None = None
_users_cache_ts: float = 0.0
_USERS_CACHE_TTL: float = 5.0

def load_users() -> dict:
    global _users_cache, _users_cache_ts
    now = time.monotonic()
    if _users_cache is not None and (now - _users_cache_ts) < _USERS_CACHE_TTL:
        return _users_cache
    try:
        with open(USERS_FILE) as f:
            _users_cache = json.load(f)
    except Exception:
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
    return str(user_id) in users.get("approved", {})

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

# ── Subnet cache (демон) ────────────────────────────────────────────────────────

_DNS_SERVERS = ["127.0.0.53", "1.1.1.1", "8.8.8.8", "77.88.8.8", "9.9.9.9", "208.67.222.222"]
_DNS_ROUNDS  = 3

def load_subnet_cache() -> dict:
    try:
        with open(SUBNET_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _dns_query(domain: str, ns: str, timeout: float = 4.0) -> list:
    """A-запрос к конкретному DNS-серверу через raw UDP. Без внешних зависимостей."""
    import struct as _struct
    try:
        domain = '.'.join(lbl.encode('idna').decode('ascii') for lbl in domain.split('.'))
    except (UnicodeError, UnicodeDecodeError):
        pass
    tx_id  = os.urandom(2)
    header = tx_id + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
    qname  = b''.join(bytes([len(p)]) + p.encode() for p in domain.split('.'))
    packet = header + qname + b'\x00\x00\x01\x00\x01'
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(packet, (ns, 53))
        data, _ = sock.recvfrom(4096)
        sock.close()
    except Exception:
        return []
    ips = []
    try:
        ancount = _struct.unpack('>H', data[6:8])[0]
        pos = 12
        while data[pos]:
            pos += data[pos] + 1
        pos += 5
        for _ in range(ancount):
            if data[pos] & 0xC0 == 0xC0:
                pos += 2
            else:
                while data[pos]:
                    pos += data[pos] + 1
                pos += 1
            rtype, _, _, rdlen = _struct.unpack('>HHIH', data[pos:pos + 10])
            pos += 10
            if rtype == 1 and rdlen == 4:
                ips.append('.'.join(str(b) for b in data[pos:pos + 4]))
            pos += rdlen
    except Exception:
        pass
    return ips

def _collect_ips(domain: str) -> list:
    """3 раунда × 6 DNS-серверов → уникальные IPv4 для домена.
    Запросы выполняются параллельно: вместо ~72 с в худшем случае
    весь сбор занимает ~таймаут одного запроса (4 с).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ips: set = set()
    tasks = [(domain, ns) for _ in range(_DNS_ROUNDS) for ns in _DNS_SERVERS]
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = [ex.submit(_dns_query, d, n) for d, n in tasks]
        for fut in as_completed(futs):
            try:
                for ip in fut.result():
                    try:
                        ipaddress.IPv4Address(ip)
                        ips.add(ip)
                    except ValueError:
                        pass
            except Exception:
                pass
    return list(ips)

def _ips_to_result(all_ips: list) -> list:
    """Группирует IP по /24. ≥2 в одном /24 → исключаем /24, иначе → /32."""
    groups: dict = {}
    for ip in all_ips:
        prefix = ".".join(ip.split(".")[:3])
        groups.setdefault(prefix, set()).add(ip)
    result = []
    for prefix, ips in groups.items():
        if len(ips) >= 2:
            result.append(f"{prefix}.0/24")
        else:
            result.append(f"{next(iter(ips))}/32")
    return sorted(result)

def process_domain(domain: str):
    """DNS-зондирование одного домена, обновляет subnet_cache.json."""
    new_ips = _collect_ips(domain)
    with _CACHE_LOCK:
        cache = load_subnet_cache()
        entry   = cache.get(domain, {"records": []})
        records = entry.get("records", [])
        records.append({"ts": int(time.time()), "ips": new_ips})
        if len(records) > 7:
            records = records[-7:]
        all_ips: list = []
        for rec in records:
            all_ips.extend(rec.get("ips", []))
        cache[domain] = {
            "records":    records,
            "result":     _ips_to_result(list(set(all_ips))),
            "updated_at": int(time.time()),
        }
        with open(SUBNET_CACHE_FILE, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

def get_all_tracked_domains() -> set:
    """Все домены: статика из SITES + кастомные из всех .excl.json клиентов."""
    domains: set = set()
    for site in SITES.values():
        for d in site.get("domains", []):
            domains.add(d)
    if os.path.isdir(CLIENTS_DIR):
        for fname in os.listdir(CLIENTS_DIR):
            if fname.endswith(EXCL_EXT):
                name = fname[:-len(EXCL_EXT)]
                excl = load_client_excl(name)
                if excl:
                    for entry in excl.get("custom_domains", []):
                        try:
                            ipaddress.ip_network(entry, strict=False)
                        except ValueError:
                            domains.add(entry)
    return domains

def run_subnet_daemon():
    """Полный проход: обрабатывает все домены и обновляет кэш."""
    domains = get_all_tracked_domains()
    logger.info(f"subnet_daemon: обработка {len(domains)} доменов")
    for domain in sorted(domains):
        try:
            process_domain(domain)
            logger.info(f"subnet_daemon: {domain} — готово")
        except Exception as e:
            logger.warning(f"subnet_daemon: {domain} — ошибка: {e}")
    with _CACHE_LOCK:
        cache = load_subnet_cache()
        _compute_site_results(cache)
        orphan_keys = [k for k in cache if k != "_sites" and k not in domains]
        for k in orphan_keys:
            del cache[k]
        if orphan_keys:
            logger.info(f"subnet_daemon: удалено {len(orphan_keys)} устаревших записей: {orphan_keys}")
        with open(SUBNET_CACHE_FILE, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    logger.info("subnet_daemon: завершён")

def _compute_site_results(cache: dict):
    """Объединяет все IP всех доменов одного сайта → агрегированный result.
    Записывает в cache['_sites']. Вызывается внутри _CACHE_LOCK."""
    site_results = {}
    for key, site in SITES.items():
        all_ips: list = []
        for domain in site.get("domains", []):
            for rec in cache.get(domain, {}).get("records", []):
                all_ips.extend(rec.get("ips", []))
        if all_ips:
            site_results[key] = _ips_to_result(list(set(all_ips)))
    cache["_sites"] = site_results

# ── Split tunneling ─────────────────────────────────────────────────────────────
def build_allowed_ips(selected_keys, extra_domains=None) -> str:
    excluded: set = set()
    cache = load_subnet_cache()
    site_agg = cache.get("_sites", {})

    for key in selected_keys:
        site = SITES.get(key, {})
        for subnet in site.get("subnets", []):
            excluded.add(subnet)
        if site_agg.get(key):
            excluded.update(site_agg[key])
        else:
            for domain in site.get("domains", []):
                cached = cache.get(domain, {}).get("result")
                if cached:
                    excluded.update(cached)
                else:
                    try:
                        results = socket.getaddrinfo(domain, None, socket.AF_INET)
                        for r in results:
                            excluded.add(f"{r[4][0]}/32")
                    except Exception:
                        pass

    for entry in (extra_domains or []):
        entry = entry.strip()
        if not entry:
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
            excluded.add(entry)
            continue
        except ValueError:
            pass
        cached = cache.get(entry, {}).get("result")
        if cached:
            excluded.update(cached)
        else:
            try:
                results = socket.getaddrinfo(entry, None, socket.AF_INET)
                for r in results:
                    excluded.add(f"{r[4][0]}/32")
            except Exception:
                pass
    if not excluded:
        return "0.0.0.0/0"
    excl_nets = []
    for s in excluded:
        try:
            excl_nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            pass
    if not excl_nets:
        return "0.0.0.0/0"
    # Схлопываем пересекающиеся/смежные исключения, затем берём дополнение за один проход
    collapsed = list(ipaddress.collapse_addresses(excl_nets))
    allowed = []
    start = ipaddress.ip_address("0.0.0.0")
    end   = ipaddress.ip_address("255.255.255.255")
    for net in collapsed:
        if net.network_address > start:
            allowed.extend(ipaddress.summarize_address_range(start, net.network_address - 1))
        if net.broadcast_address >= end:
            start = None
            break
        start = net.broadcast_address + 1
    if start is not None and start <= end:
        allowed.extend(ipaddress.summarize_address_range(start, end))
    return ", ".join(str(n) for n in allowed) if allowed else "0.0.0.0/0"

# ── Бэкап ──────────────────────────────────────────────────────────────────────
def create_backup(prefix: str = "awg_backup") -> str:
    """Создаёт tar.gz бэкап, возвращает путь к файлу.
    prefix — префикс имени файла (по умолчанию 'awg_backup', для авто-бэкапов — 'pre_restore')."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts          = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{BACKUP_DIR}/{prefix}_{ts}.tar.gz"
    with tarfile.open(backup_path, "w:gz") as tar:
        tar.add(AWG_CONF,    arcname=f"{AWG_IFACE}.conf")
        tar.add(ENV_FILE,    arcname="server.env")
        tar.add(CLIENTS_DIR, arcname="clients")
        if os.path.exists(USERS_FILE):
            tar.add(USERS_FILE, arcname="users.json")
        if os.path.exists(SUBNET_CACHE_FILE):
            tar.add(SUBNET_CACHE_FILE, arcname="subnet_cache.json")
        if os.path.exists(SERVERS_FILE):
            tar.add(SERVERS_FILE, arcname="servers.json")
        # SSH admin keypair — критично для доступа к slave-серверам
        if os.path.exists(ADMIN_KEY_PATH):
            tar.add(ADMIN_KEY_PATH,           arcname="ssh/awg_admin_key")
        if os.path.exists(ADMIN_KEY_PATH + ".pub"):
            tar.add(ADMIN_KEY_PATH + ".pub",  arcname="ssh/awg_admin_key.pub")
        _bot_pkl = "/etc/awg-bot/bot_persistence.pkl"
        if os.path.exists(_bot_pkl):
            tar.add(_bot_pkl, arcname="bot_persistence.pkl")
        if os.path.exists("/root/modules.conf"):
            tar.add("/root/modules.conf", arcname="modules.conf")
        _mtp_conf = "/etc/proxy-bot/proxy_bot.env"
        if os.path.exists(_mtp_conf):
            tar.add(_mtp_conf, arcname="proxy_bot.env")
    return backup_path

# ── Техобслуживание ─────────────────────────────────────────────────────────────
def get_maintenance() -> dict:
    try:
        with open(MAINTENANCE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"enabled": False}

def set_maintenance(enabled: bool, message: str = "", end_time: int = 0) -> dict:
    data = {"enabled": enabled, "message": message, "end_time": end_time}
    with open(MAINTENANCE_FILE, "w") as f:
        json.dump(data, f)
    return data

# ── Общие хелперы ──────────────────────────────────────────────────────────────

def can_access_device(user_id: int, name: str) -> bool:
    """True если user_id == ADMIN_ID или устройство принадлежит пользователю."""
    if user_id == ADMIN_ID:
        return True
    return name.startswith(get_user_name(user_id) + ".")

def device_short_name(name: str) -> str:
    """Возвращает имя устройства без префикса пользователя: 'Ivan.Phone' → 'Phone'."""
    return name.split(".", 1)[1] if "." in name else name

def get_allowed_ips_for_client(name: str) -> str:
    """Возвращает строку AllowedIPs для клиента с учётом сохранённых исключений."""
    excl = load_client_excl(name)
    if not excl:
        return "0.0.0.0/0"
    sites  = set(excl.get("sites", [])) | set(DEFAULT_SELECTED)
    custom = excl.get("custom_domains", [])
    if not sites and not custom:
        return "0.0.0.0/0"
    return build_allowed_ips(sites, extra_domains=custom)

def get_sites_json() -> list:
    """Возвращает список категорий со списком сайтов для UI (TMA и любых других клиентов)."""
    result = []
    for cat_label, keys in CATEGORIES.items():
        sites = []
        for k in keys:
            s = SITES.get(k)
            if not s:
                continue
            sites.append({
                "key":    k,
                "name":   s["name"],
                "emoji":  s.get("emoji", ""),
                "locked": k in DEFAULT_SELECTED,
            })
        if sites:
            result.append({"category": cat_label, "sites": sites})
    return result

def resolve_endpoint(ep_key: str) -> str:
    """Преобразует символьный ключ эндпоинта в строку адреса.
    ep_key: 'main' | 'backup' | 'ip'"""
    if ep_key == "backup":
        return SERVER_ENDPOINT_BACKUP
    if ep_key == "ip":
        return SERVER_IP
    return SERVER_ENDPOINT

def log_maintenance_done():
    """Сохраняет дату последнего техобслуживания, не затирая поля enabled/message."""
    data = get_maintenance()
    data["last_date"] = time.strftime("%d.%m.%Y")
    data["last_ts"]   = int(time.time())
    with open(MAINTENANCE_FILE, "w") as f:
        json.dump(data, f)

# ── Версии системных компонентов ──────────────────────────────────────────────

def get_ubuntu_version() -> str:
    try:
        return subprocess.check_output(["lsb_release", "-ds"], text=True).strip()
    except Exception:
        return "неизвестно"

def get_kernel_version() -> str:
    try:
        return subprocess.check_output(["uname", "-r"], text=True).strip()
    except Exception:
        return "неизвестно"

# ── Re-экспорт из sub-модулей (обратная совместимость) ─────────────────────────
# Любой код, делающий `from awg_core import create_client` и т.п., продолжает работать.
from awg_clients import *  # noqa: F401,F403,E402
from awg_stats import *    # noqa: F401,F403,E402
from awg_ssh import *      # noqa: F401,F403,E402
