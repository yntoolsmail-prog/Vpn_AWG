#!/usr/bin/env python3
# awg_core.py — общее ядро: конфиг, утилиты, работа с клиентами, статистика
# Импортируется из bot.py и tma_server.py.
# Не содержит ничего Telegram-специфичного и ничего HTTP-специфичного.
# Version: 1.0

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

logger = logging.getLogger(__name__)

# Глобальный lock для create_client — защита от гонки при одновременном создании.
# threading.Lock работает и в sync (Flask/threaded) и в async (bot.py) контексте.
_AWG_LOCK = threading.Lock()

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

# Применяем часовой пояс
os.environ["TZ"] = TZ
try:
    time.tzset()
except AttributeError:
    pass

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
        "H1":   srv.get("H1",   "1"),
        "H2":   srv.get("H2",   "2"),
        "H3":   srv.get("H3",   "3"),
        "H4":   srv.get("H4",   "4"),
    }

def make_wg_conf(priv, ip, psk, obfs, endpoint: str = None,
                 allowed_ips: str = "0.0.0.0/0") -> str:
    ep = endpoint or SERVER_ENDPOINT
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {priv}", f"Address = {ip}/32",
        f"DNS = {PRIMARY_DNS}, {SECONDARY_DNS}",
        f"Jc = {obfs['Jc']}", f"Jmin = {obfs['Jmin']}", f"Jmax = {obfs['Jmax']}",
        f"S1 = {obfs['S1']}", f"S2 = {obfs['S2']}",
        f"H1 = {obfs['H1']}", f"H2 = {obfs['H2']}", f"H3 = {obfs['H3']}", f"H4 = {obfs['H4']}",
        "", "[Peer]", f"PublicKey = {SERVER_PUBLIC}", f"PresharedKey = {psk}",
        f"Endpoint = {ep}:{SERVER_PORT}", f"AllowedIPs = {allowed_ips}",
        "PersistentKeepalive = 25",
    ]) + "\n"

def make_vpn_link(priv, pub, ip, psk, obfs, name, endpoint: str = None) -> str:
    ep = endpoint or SERVER_ENDPOINT
    wg = (
        f"[Interface]\nAddress = {ip}/32\nDNS = {PRIMARY_DNS}, {SECONDARY_DNS}\n"
        f"PrivateKey = {priv}\nJc = {obfs['Jc']}\nJmin = {obfs['Jmin']}\nJmax = {obfs['Jmax']}\n"
        f"S1 = {obfs['S1']}\nS2 = {obfs['S2']}\nH1 = {obfs['H1']}\nH2 = {obfs['H2']}\n"
        f"H3 = {obfs['H3']}\nH4 = {obfs['H4']}\n\n"
        f"[Peer]\nPublicKey = {SERVER_PUBLIC}\nPresharedKey = {psk}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {ep}:{SERVER_PORT}\n"
        f"PersistentKeepalive = 25\n"
    )
    lc = {**obfs, "allowed_ips": ["0.0.0.0/0", "::/0"], "clientId": pub,
          "client_ip": ip, "client_priv_key": priv, "client_pub_key": pub,
          "config": wg, "hostName": ep, "mtu": "1420",
          "persistent_keep_alive": "25", "port": int(SERVER_PORT),
          "psk_key": psk, "server_pub_key": SERVER_PUBLIC}
    c = {"containers": [{"awg": {**obfs, "last_config": json.dumps(lc, indent=4),
         "port": str(SERVER_PORT),
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
    for ext in [".conf", ".pub", ".vpn", ".vpnlink"]:
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

# ── Split tunneling ─────────────────────────────────────────────────────────────
def build_allowed_ips(selected_keys, extra_domains=None) -> str:
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
        for line in open("/proc/net/dev").readlines()[2:]:
            iface = line.split(":")[0].strip()
            if iface and iface != "lo" and not iface.startswith("awg"):
                return iface
    except Exception:
        pass
    return "eth0"

def read_iface_bytes(iface: str) -> tuple[int, int]:
    try:
        rx = int(open(f"/sys/class/net/{iface}/statistics/rx_bytes").read())
        tx = int(open(f"/sys/class/net/{iface}/statistics/tx_bytes").read())
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
        ram_used, ram_total = int(mem[2]), int(mem[1])
    except Exception:
        ram_used = ram_total = 0

    try:
        disk = subprocess.check_output(["df", "-h", "/"], text=True).split("\n")[1].split()
        disk_used, disk_total = disk[2], disk[1]
        disk_pct = int(disk[4].replace("%", ""))
    except Exception:
        disk_used = disk_total = "—"
        disk_pct  = 0

    try:    load = open("/proc/loadavg").read().split()[:3]
    except: load = ["0", "0", "0"]

    return {
        "uptime":     uptime,
        "ram_used":   ram_used,
        "ram_total":  ram_total,
        "disk_used":  disk_used,
        "disk_total": disk_total,
        "disk_pct":   disk_pct,
        "load":       load,
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

        "bw_top":  get_bw_top(5),
        "monthly": get_vnstat_monthly(),
        "peers":   peers_list,
    }

def collect_stats_basic() -> dict:
    """Урезанная статистика — для обычных пользователей"""
    peers  = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values()
                 if p.get("handshake") and now - p["handshake"] < 180)
    uptime = get_system_stats()["uptime"]
    return {
        "awg_status":      "running",
        "server_endpoint": SERVER_ENDPOINT,
        "uptime":          uptime,
        "peers_total":     len(get_all_clients()),
        "peers_online":    online,
    }

# ── Бэкап ──────────────────────────────────────────────────────────────────────
def create_backup() -> str:
    """Создаёт tar.gz бэкап, возвращает путь к файлу"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts          = time.strftime("%Y%m%d_%H%M%S")
    backup_path = f"{BACKUP_DIR}/awg_backup_{ts}.tar.gz"
    with tarfile.open(backup_path, "w:gz") as tar:
        tar.add(AWG_CONF,    arcname=f"{AWG_IFACE}.conf")
        tar.add(ENV_FILE,    arcname="server.env")
        tar.add(CLIENTS_DIR, arcname="clients")
        if os.path.exists(USERS_FILE):
            tar.add(USERS_FILE, arcname="users.json")
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
