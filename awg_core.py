#!/usr/bin/env python3
# awg_core.py — общее ядро: конфиг, утилиты, работа с клиентами, статистика
# Импортируется из bot.py и tma_server.py.
# Не содержит ничего Telegram-специфичного и ничего HTTP-специфичного.
# Version: 1.1

import os, subprocess, logging, json, zlib, base64, struct, time
import tarfile, socket, ipaddress, threading

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

logger = logging.getLogger(__name__)

# Глобальный lock для create_client — защита от гонки при одновременном создании.
# threading.Lock работает и в sync (Flask/threaded) и в async (bot.py) контексте.
_AWG_LOCK = threading.Lock()

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
# Имена systemd-сервисов — можно переопределить через server.env
BOT_SERVICE  = srv.get("BOT_SERVICE",  "awg-bot")
AWG_SERVICE  = f"awg-quick@{AWG_IFACE}"

# Внешние бинарники
QRENCODE_BIN = srv.get("QRENCODE_BIN", "qrencode")

# URL для определения внешнего IP сервера
IP_CHECK_URLS = [
    srv.get("IP_CHECK_URL_1", "https://ifconfig.me"),
    srv.get("IP_CHECK_URL_2", "https://api.ipify.org"),
    srv.get("IP_CHECK_URL_3", "https://ifconfig.co"),
]

# Флаг-файл для передачи chat_id при перезапуске бота
RESTART_FLAG_FILE = srv.get("RESTART_FLAG_FILE", "/tmp/awg_bot_restart_flag")

# Применяем часовой пояс
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
    # Инициализация из server.env (первый запуск)
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

# ── AWG: работа с интерфейсом ──────────────────────────────────────────────────
def get_awg_dump() -> dict:
    """Читает awg show dump, возвращает dict {pub_key: {...}}"""
    try:
        out = subprocess.check_output(["awg", "show", AWG_IFACE, "dump"], text=True)
    except Exception:
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

# ── Клиенты ────────────────────────────────────────────────────────────────────
def get_all_clients() -> list:
    if not os.path.exists(CLIENTS_DIR):
        return []
    return sorted([f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")])

def get_user_clients(user_id: int) -> list:
    prefix = get_user_name(user_id) + "."
    return [c for c in get_all_clients() if c.startswith(prefix)]

def get_client_pub(name: str) -> str | None:
    pub_path = f"{CLIENTS_DIR}/{name}.pub"
    if os.path.exists(pub_path):
        with open(pub_path) as f:
            return f.read().strip()
    try:
        with open(f"{CLIENTS_DIR}/{name}.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PrivateKey"):
                    priv = line.split("=", 1)[1].strip()
                    pub = subprocess.check_output(
                        ["awg", "pubkey"], input=priv, text=True
                    ).strip()
                    with open(pub_path, "w") as pf:
                        pf.write(pub)
                    return pub
    except Exception:
        pass
    return None

def get_client_keys(name: str) -> dict | None:
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
                if k == "PrivateKey":     data["priv"] = v
                elif k == "Address":      data["ip"]   = v.split("/")[0]
                elif k == "PresharedKey": data["psk"]  = v
                elif k in ("Jc","Jmin","Jmax","S1","S2","H1","H2","H3","H4"):
                    obfs[k] = v
        pub = get_client_pub(name)
        if not pub:
            return None
        data["pub"] = pub
        if obfs:
            data["obfs"] = obfs
        if not all(k in data for k in ("priv", "pub", "ip", "psk")):
            return None
        return data
    except Exception:
        return None

def next_ip() -> int:
    used: set[str] = set()
    try:
        with open(AWG_CONF) as f:
            for line in f:
                if "AllowedIPs" in line:
                    for part in line.split():
                        if part.startswith(VPN_SUBNET + "."):
                            used.add(part.split("/")[0])
    except FileNotFoundError:
        pass
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

def gen_obfs() -> dict:
    return {
        "Jc":   srv.get("JC",   "4"),
        "Jmin": srv.get("JMIN", "40"),
        "Jmax": srv.get("JMAX", "70"),
        "S1":   srv.get("S1",   "0"),
        "S2":   srv.get("S2",   "0"),
        "H1":   srv.get("H1",   "1..4"),
        "H2":   srv.get("H2",   "1..4"),
        "H3":   srv.get("H3",   "1..4"),
        "H4":   srv.get("H4",   "1..4"),
        "i1":   srv.get("I1",   ""),
    }

def make_wg_conf(priv, ip, psk, obfs, endpoint: str = None,
                 allowed_ips: str = "0.0.0.0/0",
                 server_public: str = None, server_port: str = None) -> str:
    ep  = endpoint or SERVER_ENDPOINT
    pub = server_public or SERVER_PUBLIC
    prt = server_port or SERVER_PORT
    parts = [
        "[Interface]",
        f"PrivateKey = {priv}", f"Address = {ip}/32",
        f"DNS = {PRIMARY_DNS}, {SECONDARY_DNS}",
        f"Jc = {obfs['Jc']}", f"Jmin = {obfs['Jmin']}", f"Jmax = {obfs['Jmax']}",
        f"S1 = {obfs['S1']}", f"S2 = {obfs['S2']}",
        f"H1 = {obfs['H1']}", f"H2 = {obfs['H2']}", f"H3 = {obfs['H3']}", f"H4 = {obfs['H4']}",
    ]
    if obfs.get("i1"):
        parts.append(f"i1 = {obfs['i1']}")
    parts += ["", "[Peer]", f"PublicKey = {pub}", f"PresharedKey = {psk}",
              f"Endpoint = {ep}:{prt}", f"AllowedIPs = {allowed_ips}",
              "PersistentKeepalive = 25"]
    return "\n".join(parts) + "\n"

def make_vpn_link(priv, pub, ip, psk, obfs, name, endpoint: str = None,
                  server_public: str = None, server_port: str = None) -> str:
    ep  = endpoint or SERVER_ENDPOINT
    spub = server_public or SERVER_PUBLIC
    prt  = server_port or SERVER_PORT
    i1_line = f"i1 = {obfs['i1']}\n" if obfs.get("i1") else ""
    wg = (
        f"[Interface]\nAddress = {ip}/32\nDNS = {PRIMARY_DNS}, {SECONDARY_DNS}\n"
        f"PrivateKey = {priv}\nJc = {obfs['Jc']}\nJmin = {obfs['Jmin']}\nJmax = {obfs['Jmax']}\n"
        f"S1 = {obfs['S1']}\nS2 = {obfs['S2']}\nH1 = {obfs['H1']}\nH2 = {obfs['H2']}\n"
        f"H3 = {obfs['H3']}\nH4 = {obfs['H4']}\n{i1_line}"
        f"\n[Peer]\nPublicKey = {spub}\nPresharedKey = {psk}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {ep}:{prt}\n"
        f"PersistentKeepalive = 25\n"
    )
    lc = {**obfs, "allowed_ips": ["0.0.0.0/0", "::/0"], "clientId": pub,
          "client_ip": ip, "client_priv_key": priv, "client_pub_key": pub,
          "config": wg, "hostName": ep, "mtu": "1420",
          "persistent_keep_alive": "25", "port": int(prt),
          "psk_key": psk, "server_pub_key": spub}
    c = {"containers": [{"awg": {**obfs, "last_config": json.dumps(lc, indent=4),
         "port": str(prt),
         "subnet_address": ".".join(ip.split(".")[:3]) + ".0",
         "transport_proto": "udp"}, "container": "amnezia-awg"}],
         "defaultContainer": "amnezia-awg", "description": name,
         "dns1": PRIMARY_DNS, "dns2": SECONDARY_DNS,
         "hostName": ep, "nameOverriddenByUser": True}
    b = json.dumps(c, ensure_ascii=False).encode()
    p = struct.pack(">I", len(b)) + zlib.compress(b)
    return "vpn://" + base64.urlsafe_b64encode(p).decode().rstrip("=")

def _remove_peer_from_conf(name: str):
    """Удаляет блок # Client: name … [Peer] … из awg0.conf.
    Останавливает скип на следующем '# Client:' или на секции не-[Peer]."""
    try:
        with open(AWG_CONF, encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        return
    new_lines, skip = [], False
    for line in lines:
        stripped = line.strip()
        if stripped == f"# Client: {name}":
            skip = True
        elif skip and (
            stripped.startswith("# Client:")
            or (stripped.startswith("[") and stripped != "[Peer]")
        ):
            skip = False
            new_lines.append(line)
        elif not skip:
            new_lines.append(line)
    with open(AWG_CONF, "w") as f:
        f.write("\n".join(new_lines))


def remove_client_from_awg(name: str):
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        return
    pub = get_client_pub(name)
    if pub:
        subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"])
    _remove_peer_from_conf(name)
    for ext in [".conf", ".pub", ".vpn", ".vpnlink", EXCL_EXT]:
        p = f"{CLIENTS_DIR}/{name}{ext}"
        if os.path.exists(p):
            os.remove(p)

async def create_client(name: str) -> dict:
    """Создаёт клиента AWG с верификацией и откатом.
    Использует глобальный _AWG_LOCK для защиты от гонки."""
    with _AWG_LOCK:
        priv = subprocess.check_output(["awg", "genkey"], text=True).strip()
        pub  = subprocess.check_output(["awg", "pubkey"], input=priv, text=True).strip()
        psk  = subprocess.check_output(["awg", "genpsk"], text=True).strip()
        ip   = f"{VPN_SUBNET}.{next_ip()}"
        obfs = gen_obfs()

        os.makedirs(CLIENTS_DIR, exist_ok=True)
        conf_path = f"{CLIENTS_DIR}/{name}.conf"
        pub_path  = f"{CLIENTS_DIR}/{name}.pub"

        with open(AWG_CONF, "a") as f:
            f.write(f"\n# Client: {name}\n[Peer]\nPublicKey = {pub}\n"
                    f"PresharedKey = {psk}\nAllowedIPs = {ip}/32\n")

        subprocess.run(["awg", "set", AWG_IFACE, "peer", pub,
                        "preshared-key", "/dev/stdin", "allowed-ips", f"{ip}/32"],
                       input=psk, text=True)

        with open(conf_path, "w") as f:
            f.write(make_wg_conf(priv, ip, psk, obfs))
        with open(pub_path, "w") as f:
            f.write(pub)

        dump    = get_awg_dump()
        peer_ok = pub in dump and dump[pub].get("allowed", "").startswith(ip)
        try:
            with open(AWG_CONF) as f:
                cc = f.read()
            conf_ok = pub in cc and f"{ip}/32" in cc
        except Exception:
            conf_ok = False
        files_ok = os.path.exists(conf_path) and os.path.exists(pub_path)

        if not peer_ok or not conf_ok or not files_ok:
            logger.error(f"create_client({name}): провалилась верификация "
                         f"(peer={peer_ok} conf={conf_ok} files={files_ok}), откат")
            try:
                subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"])
            except Exception:
                pass
            try:
                _remove_peer_from_conf(name)
            except Exception:
                pass
            for ext in [".conf", ".pub"]:
                p = f"{CLIENTS_DIR}/{name}{ext}"
                if os.path.exists(p):
                    os.remove(p)
            raise RuntimeError(
                f"Не удалось создать клиента '{name}': верификация провалилась."
            )

        return {"priv": priv, "pub": pub, "ip": ip, "psk": psk, "obfs": obfs}

# ── Subnet cache (демон) ────────────────────────────────────────────────────────

_DNS_SERVERS = ["127.0.0.53", "1.1.1.1", "8.8.8.8", "77.88.8.8", "9.9.9.9", "208.67.222.222"]
_DNS_ROUNDS  = 3

def load_subnet_cache() -> dict:
    try:
        with open(SUBNET_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _collect_ips(domain: str) -> list:
    """3 раунда × 6 DNS-серверов → уникальные IPv4 для домена."""
    ips: set = set()
    for _ in range(_DNS_ROUNDS):
        for ns in _DNS_SERVERS:
            try:
                out = subprocess.check_output(
                    ["dig", "+short", "A", domain, f"@{ns}"],
                    text=True, timeout=5
                )
                for line in out.splitlines():
                    line = line.strip()
                    try:
                        ipaddress.IPv4Address(line)
                        ips.add(line)
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
    logger.info("subnet_daemon: завершён")

# ── Split tunneling ─────────────────────────────────────────────────────────────
def build_allowed_ips(selected_keys, extra_domains=None) -> str:
    excluded: set = set()
    cache = load_subnet_cache()

    for key in selected_keys:
        site = SITES.get(key, {})
        for subnet in site.get("subnets", []):
            excluded.add(subnet)
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

    # Кастомные домены / IP-адреса
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

# ── Форматирование ──────────────────────────────────────────────────────────────
def fmt_bytes(b: int) -> str:
    if b < 1024:       return f"{b} B"
    elif b < 1024**2:  return f"{b/1024:.1f} KB"
    elif b < 1024**3:  return f"{b/1024**2:.1f} MB"
    else:              return f"{b/1024**3:.2f} GB"

def fmt_handshake(ts: int) -> str:
    if not ts: return "никогда"
    diff = int(time.time()) - ts
    if diff < 60:      return f"{diff} сек назад 🟢"
    elif diff < 180:   return f"{diff//60} мин назад 🟢"
    elif diff < 3600:  return f"{diff//60} мин назад"
    elif diff < 86400: return f"{diff//3600} ч назад"
    else:              return f"{diff//86400} д назад"

# ── Сетевые утилиты ─────────────────────────────────────────────────────────────
def get_real_server_ip() -> str | None:
    """Определяет реальный внешний IPv4 сервера через публичные сервисы."""
    for url in IP_CHECK_URLS:
        if not url:
            continue
        try:
            out = subprocess.check_output(
                ["curl", "-4", "-s", "--max-time", "5", url], text=True
            ).strip()
            if out and "." in out and ":" not in out:
                return out
        except Exception:
            pass
    return None

def get_host_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"], text=True)
        parts = out.split()
        for i, p in enumerate(parts):
            if p == "dev" and i + 1 < len(parts):
                return parts[i + 1]
    except Exception:
        pass
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                iface = line.split(":")[0].strip()
                if iface and iface != "lo" and not iface.startswith("awg"):
                    return iface
    except Exception:
        pass
    return "eth0"

def read_iface_bytes(iface: str) -> tuple[int, int]:
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
            rx = int(f.read())
        with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
            tx = int(f.read())
        return rx, tx
    except Exception:
        return 0, 0

# ── Bandwidth ──────────────────────────────────────────────────────────────────
# Формат строки лога (одна запись = одна минута):
#   2024-01-15 14:32 AWG_DOWN=12.34 AWG_UP=5.67 ETH_DOWN=13.10 ETH_UP=6.20
#
# AWG = трафик клиентов:
#   AWG_DOWN — клиенты скачивают (TX awg0, сервер отдаёт клиентам)
#   AWG_UP   — клиенты отдают    (RX awg0, сервер получает от клиентов)
# ETH = трафик сервера (для сравнения и контроля лимита):
#   ETH_DOWN — сервер получает от провайдера
#   ETH_UP   — сервер отдаёт провайдеру

def load_bw_peak() -> dict:
    try:
        with open(BW_PEAK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_bw_peak(data: dict):
    try:
        with open(BW_PEAK_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def load_client_excl(name: str) -> dict | None:
    """Возвращает dict исключений клиента или None если файл не существует."""
    path = f"{CLIENTS_DIR}/{name}{EXCL_EXT}"
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning(f"load_client_excl({name}): broken file, ignoring")
        return None

def save_client_excl(name: str, data: dict):
    """Сохраняет исключения клиента в файл .excl.json."""
    path = f"{CLIENTS_DIR}/{name}{EXCL_EXT}"
    with open(path, "w") as f:
        json.dump(data, f)

def _parse_log_line(line: str) -> dict | None:
    """Парсит строку лога. Возвращает dict с полями dt, awg_down, awg_up, eth_down, eth_up."""
    parts = line.strip().split()
    if len(parts) < 6:
        return None
    try:
        fields = {p.split("=")[0]: float(p.split("=")[1]) for p in parts[2:] if "=" in p}
        return {
            "dt":       parts[0] + " " + parts[1],
            "awg_down": fields.get("AWG_DOWN", 0.0),
            "awg_up":   fields.get("AWG_UP",   0.0),
            "eth_down": fields.get("ETH_DOWN",  0.0),
            "eth_up":   fields.get("ETH_UP",    0.0),
        }
    except Exception:
        return None

def get_bw_top(n: int = 5) -> list[dict]:
    """Топ-N минут по суммарной клиентской нагрузке (awg_down + awg_up)."""
    try:
        rows = []
        for line in open(BW_LOG_FILE).readlines():
            rec = _parse_log_line(line)
            if rec:
                rows.append(rec)
        rows.sort(key=lambda x: x["awg_down"] + x["awg_up"], reverse=True)
        return rows[:n]
    except Exception:
        return []

def get_bw_histogram_for(lines_data: list[str]) -> dict | None:
    """Гистограмма клиентской нагрузки (по max из awg_down/awg_up)."""
    buckets = [
        (0,   50,  "0–50  "), (50,  100, "50–100"),
        (100, 150, "100–150"), (150, 200, "150–200"),
        (200, 300, "200–300"), (300, 400, "300–400"),
        (400, 500, "400–500"), (500, None, "500+  "),
    ]
    counts = [0] * len(buckets)
    total  = 0
    for line in lines_data:
        rec = _parse_log_line(line)
        if rec:
            val = max(rec["awg_down"], rec["awg_up"])
            total += 1
            for i, (lo, hi, _) in enumerate(buckets):
                if hi is None or val < hi:
                    counts[i] += 1
                    break
    if total == 0:
        return None
    return {"buckets": buckets, "counts": counts, "total": total}

def get_bw_histogram(period_days: int = 0) -> dict | None:
    """period_days=0 — всё время, иначе последние N дней."""
    try:
        all_lines = open(BW_LOG_FILE).readlines()
    except Exception:
        return None
    if period_days > 0:
        cutoff = time.strftime("%Y-%m-%d",
                               time.localtime(time.time() - period_days * 86400))
        all_lines = [l for l in all_lines if l[:10] >= cutoff]
    return get_bw_histogram_for(all_lines)

def get_bw_histogram_day(date_str: str) -> dict | None:
    """Гистограмма за конкретный день (YYYY-MM-DD)."""
    try:
        lines = [l for l in open(BW_LOG_FILE).readlines() if l.startswith(date_str)]
    except Exception:
        return None
    return get_bw_histogram_for(lines)

def get_log_days() -> list[str]:
    """Уникальные даты в логе."""
    days = set()
    try:
        for line in open(BW_LOG_FILE).readlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                days.add(parts[0])
    except Exception:
        pass
    return sorted(days)

def fmt_histogram(hist: dict, period_label: str = "") -> list[str]:
    """Форматирует гистограмму клиентской нагрузки для вывода в Telegram."""
    header = "\n📊 Клиентская нагрузка (max ↓↑ клиентов, мин/сут)"
    if period_label:
        header += f" — {period_label}"
    lines  = [header]
    bar_max = 10
    for i, (lo, hi, label) in enumerate(hist["buckets"]):
        cnt = hist["counts"][i]
        if cnt == 0:
            continue
        pct = cnt / hist["total"] * 100
        bar = "█" * max(1, round(pct / 100 * bar_max))
        if lo >= 500:   icon = "🔴"
        elif lo >= 300: icon = "🟠"
        elif lo >= 200: icon = "🟡"
        elif lo >= 100: icon = "🟢"
        else:           icon = "⚪"
        lines.append(f"{icon} {label} Mbit/s  {bar:<{bar_max}}  {pct:4.1f}%  {cnt} мин")
    lines.append(f"   Записей: {hist['total']}")
    return lines

# ── vnstat ──────────────────────────────────────────────────────────────────────
def get_vnstat_monthly() -> list[dict]:
    iface = get_host_iface()
    try:
        out = subprocess.check_output(
            ["vnstat", "-i", iface, "--months", "--json"],
            text=True, stderr=subprocess.DEVNULL
        )
        data   = json.loads(out)
        months = data["interfaces"][0]["traffic"]["month"]
        result = []
        current_label = time.strftime("%Y-%m")
        for m in months[-6:]:
            rx_gb = round(m["rx"] / 1024**3, 2)
            tx_gb = round(m["tx"] / 1024**3, 2)
            label = f"{m['date']['year']}-{m['date']['month']:02d}"
            result.append({
                "label": label, "rx_gb": rx_gb, "tx_gb": tx_gb,
                "total_gb": round(rx_gb + tx_gb, 2),
                "current":  label == current_label,
            })
        return result
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["vnstat", "-i", iface, "--months"],
            text=True, stderr=subprocess.DEVNULL
        )
        result = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4 and "-" in parts[0] and len(parts[0].strip()) == 7:
                label = parts[0].strip()
                def parse_gb(s):
                    s = s.strip()
                    try:
                        val, unit = s.split()
                        val = float(val)
                        u = unit.lower()
                        if "gib" in u or "gb" in u: return round(val, 2)
                        if "mib" in u or "mb" in u: return round(val / 1024, 2)
                        if "kib" in u or "kb" in u: return round(val / 1024**2, 2)
                    except Exception:
                        pass
                    return 0.0
                rx = parse_gb(parts[1])
                tx = parse_gb(parts[2])
                result.append({"label": label, "rx_gb": rx, "tx_gb": tx,
                                "total_gb": round(rx + tx, 2), "current": False})
        return result[-6:]
    except Exception:
        return []

# ── Статистика ─────────────────────────────────────────────────────────────────
def get_system_stats() -> dict:
    """Системные метрики: uptime, RAM, диск, load average.
    Единая точка сбора — используется bot.py, tma_server.py и будущими блоками."""
    try:    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    except: uptime = "—"

    try:
        mem = subprocess.check_output(["free", "-m"], text=True).split("\n")[1].split()
        ram_total = int(mem[1])
        ram_used  = ram_total - int(mem[6])   # total - available: совпадает с htop
    except Exception:
        ram_used = ram_total = 0

    try:
        disk = subprocess.check_output(["df", "-h", "/"], text=True).split("\n")[1].split()
        disk_used, disk_total = disk[2], disk[1]
        disk_pct = int(disk[4].replace("%", ""))
    except Exception:
        disk_used = disk_total = "—"
        disk_pct  = 0

    try:
        with open("/proc/loadavg") as f:
            load = f.read().split()[:3]
    except Exception:
        load = ["0", "0", "0"]

    cpu_count = os.cpu_count() or 1

    try:
        def _rs():
            with open("/proc/stat") as f:
                v = list(map(int, f.readline().split()[1:]))
            return v[3] + v[4], sum(v)   # idle, total
        idle1, tot1 = _rs()
        time.sleep(0.5)
        idle2, tot2 = _rs()
        cpu_pct = round(100 * (1 - (idle2 - idle1) / max(tot2 - tot1, 1)), 1)
    except Exception:
        cpu_pct = 0.0

    return {
        "uptime":     uptime,
        "ram_used":   ram_used,
        "ram_total":  ram_total,
        "disk_used":  disk_used,
        "disk_total": disk_total,
        "disk_pct":   disk_pct,
        "load":       load,
        "cpu_count":  cpu_count,
        "cpu_pct":    cpu_pct,
    }


def collect_stats_full() -> dict:
    """Полная статистика — только для ADMIN_ID"""
    peers = get_awg_dump()
    now   = int(time.time())
    iface = get_host_iface()
    peak  = load_bw_peak()
    sys   = get_system_stats()

    online = sum(1 for p in peers.values()
                 if p.get("handshake") and now - p["handshake"] < 180)

    uptime     = sys["uptime"]
    ram_used   = sys["ram_used"];  ram_total  = sys["ram_total"]
    disk_used  = sys["disk_used"]; disk_total = sys["disk_total"]
    disk_pct   = sys["disk_pct"]
    load       = sys["load"]
    cpu_count  = sys["cpu_count"]
    cpu_pct    = sys["cpu_pct"]

    # Текущая скорость из последней записи пиков
    last_bw = peak.get("last", {})

    # Суммарный трафик клиентов из awg dump (накопительно с перезагрузки):
    # RX awg0 = клиенты отдают (upload), TX awg0 = клиенты скачивают (download)
    total_awg_up   = sum(p.get("rx", 0) for p in peers.values())
    total_awg_down = sum(p.get("tx", 0) for p in peers.values())

    day  = peak.get("day",  {})
    allp = peak.get("all",  {})

    clients = get_all_clients()
    peers_list = []
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = stats.get("handshake", 0)
        peers_list.append({
            "name":      name,
            "handshake": hs,
            "online":    bool(hs and now - hs < 180),
            # RX awg0 = клиент отдал, TX awg0 = клиент скачал
            "client_upload":   fmt_bytes(stats.get("rx", 0)),
            "client_download": fmt_bytes(stats.get("tx", 0)),
        })

    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
        users_count = len(users.get("approved", {}))
    except Exception:
        users_count = 0

    return {
        "awg_status":      "running",
        "server_ip":       SERVER_IP,
        "server_port":     SERVER_PORT,
        "server_endpoint": SERVER_ENDPOINT,
        "eth_iface":       iface,
        "uptime":          uptime,
        "load":            load,
        "cpu_count":       cpu_count,
        "cpu_pct":         cpu_pct,
        "ram_used_mb":     ram_used,
        "ram_total_mb":    ram_total,
        "disk_used":       disk_used,
        "disk_total":      disk_total,
        "disk_pct":        disk_pct,
        "peers_total":     len(clients),
        "peers_online":    online,
        "users_count":     users_count,

        # Суммарный трафик клиентов (awg0, накопительно с перезагрузки)
        "clients_total_download": fmt_bytes(total_awg_down),
        "clients_total_upload":   fmt_bytes(total_awg_up),

        # Скорость прямо сейчас
        "awg_current_down": last_bw.get("awg_down", 0),  # клиенты скачивают
        "awg_current_up":   last_bw.get("awg_up",   0),  # клиенты отдают
        "eth_current_down": last_bw.get("eth_down",  0),  # сервер получает
        "eth_current_up":   last_bw.get("eth_up",    0),  # сервер отдаёт

        # Пики клиентов (awg0)
        "awg_peak_day": {
            "down": day.get("awg_down", 0),
            "up":   day.get("awg_up",   0),
            "date": day.get("date", "—"),
        },
        "awg_peak_all": {
            "down": allp.get("awg_down", 0),
            "up":   allp.get("awg_up",   0),
        },
        # Пики сервера (eth0) — для сравнения
        "eth_peak_day": {
            "down": day.get("eth_down", 0),
            "up":   day.get("eth_up",   0),
            "date": day.get("date", "—"),
        },
        "eth_peak_all": {
            "down": allp.get("eth_down", 0),
            "up":   allp.get("eth_up",   0),
        },

        "bw_top":      get_bw_top(5),
        "bw_histogram": _histogram_for_tma(get_bw_histogram(7)),
        "monthly":     get_vnstat_monthly(),
        "peers":       peers_list,
    }

def _histogram_for_tma(hist: dict | None) -> dict | None:
    """Конвертирует гистограмму для передачи в TMA (JSON-совместимый формат)."""
    if not hist:
        return None
    return {
        "buckets": [[lo, hi, label.strip()] for lo, hi, label in hist["buckets"]],
        "counts":  hist["counts"],
        "total":   hist["total"],
    }


def collect_stats_basic() -> dict:
    """Урезанная статистика — для обычных пользователей"""
    peers  = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values()
                 if p.get("handshake") and now - p["handshake"] < 180)
    sys_s  = get_system_stats()
    peak    = load_bw_peak()
    day     = peak.get("day",  {})
    allp    = peak.get("all",  {})
    last_bw = peak.get("last", {})
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
        users_count = len(users.get("approved", {}))
    except Exception:
        users_count = 0
    total_awg_up   = sum(p.get("rx", 0) for p in peers.values())
    total_awg_down = sum(p.get("tx", 0) for p in peers.values())
    return {
        "awg_status":      "running",
        "server_endpoint": SERVER_ENDPOINT,
        "uptime":          sys_s["uptime"],
        "load":            sys_s["load"],
        "cpu_count":       sys_s["cpu_count"],
        "cpu_pct":         sys_s["cpu_pct"],
        "ram_used_mb":     sys_s["ram_used"],
        "ram_total_mb":    sys_s["ram_total"],
        "disk_used":       sys_s["disk_used"],
        "disk_total":      sys_s["disk_total"],
        "disk_pct":        sys_s["disk_pct"],
        "peers_total":     len(get_all_clients()),
        "peers_online":    online,
        "users_count":     users_count,
        "clients_total_download": fmt_bytes(total_awg_down),
        "clients_total_upload":   fmt_bytes(total_awg_up),
        "awg_current_down": last_bw.get("awg_down", 0),
        "awg_current_up":   last_bw.get("awg_up",   0),
        "awg_peak_day": {
            "down": day.get("awg_down", 0),
            "up":   day.get("awg_up",   0),
            "date": day.get("date", "—"),
        },
        "awg_peak_all": {
            "down": allp.get("awg_down", 0),
            "up":   allp.get("awg_up",   0),
        },
        "bw_histogram": _histogram_for_tma(get_bw_histogram(7)),
    }

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

# ── Общие хелперы (используются и в bot.py и в tma_server.py) ─────────────────

def can_access_device(user_id: int, name: str) -> bool:
    """True если user_id == ADMIN_ID или устройство принадлежит пользователю."""
    if user_id == ADMIN_ID:
        return True
    return name.startswith(get_user_name(user_id) + ".")

def device_short_name(name: str) -> str:
    """Возвращает имя устройства без префикса пользователя: 'Ivan.Phone' → 'Phone'."""
    return name.split(".", 1)[1] if "." in name else name

def get_allowed_ips_for_client(name: str) -> str:
    """Возвращает строку AllowedIPs для клиента с учётом сохранённых исключений.
    Если исключений нет — возвращает '0.0.0.0/0' (полный туннель)."""
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

# ── Эндпоинты и генерация конфигов ────────────────────────────────────────────

def resolve_endpoint(ep_key: str) -> str:
    """Преобразует символьный ключ эндпоинта в строку адреса.
    ep_key: 'main' | 'backup' | 'ip'"""
    if ep_key == "backup":
        return SERVER_ENDPOINT_BACKUP
    if ep_key == "ip":
        return SERVER_IP
    return SERVER_ENDPOINT

def make_conf_for_client(name: str, endpoint: str,
                         allowed_ips: str = "0.0.0.0/0") -> str | None:
    """Генерирует .conf для клиента с заданным эндпоинтом и AllowedIPs.
    Возвращает строку конфига или None если ключи не найдены."""
    keys = get_client_keys(name)
    if not keys:
        return None
    return make_wg_conf(
        keys["priv"], keys["ip"], keys["psk"], keys["obfs"],
        endpoint=endpoint, allowed_ips=allowed_ips,
    )

def make_conf_for_client_ep(name: str, endpoint: str,
                             server_public: str = None, server_port: str = None,
                             allowed_ips: str = "0.0.0.0/0") -> str | None:
    """Генерирует .conf для клиента с конкретным сервером (ключ/порт) и эндпоинтом.
    Используется при мультисерверной конфигурации."""
    keys = get_client_keys(name)
    if not keys:
        return None
    return make_wg_conf(
        keys["priv"], keys["ip"], keys["psk"], keys["obfs"],
        endpoint=endpoint, allowed_ips=allowed_ips,
        server_public=server_public, server_port=server_port,
    )

# ── Техобслуживание ───────────────────────────────────────────────────────────

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

