#!/usr/bin/env python3
# awg_stats.py — трафик, полоса, статистика системы
import os, subprocess, logging, json, time

logger = logging.getLogger(__name__)

from awg_core import (
    BW_LOG_FILE, BW_PEAK_FILE, USERS_FILE, SERVER_IP, SERVER_PORT,
    SERVER_ENDPOINT, IP_CHECK_URLS,
)
from awg_clients import get_awg_dump, get_all_clients, get_client_pub

# ── Агрегация дампов primary + slaves ──────────────────────────────────────────
_combined_dump_cache: dict = {}
_combined_dump_ts: float = 0.0
_COMBINED_DUMP_TTL: int = 30  # секунд


def get_combined_awg_dump() -> dict:
    """Объединённый awg dump: primary + все доступные slaves.
    Для каждого peer: handshake = max по серверам, rx/tx = sum, endpoint = с сервера с макс. handshake.
    Дополнительное поле 'server' — метка сервера с последним хендшейком (пусто = primary).
    Кэшируется на 30 секунд."""
    global _combined_dump_cache, _combined_dump_ts
    now = time.time()
    if now - _combined_dump_ts < _COMBINED_DUMP_TTL and _combined_dump_cache:
        return _combined_dump_cache

    from awg_ssh import ssh_get_slave_awg_dump, PARAMIKO_AVAILABLE
    from awg_core import load_servers

    merged: dict = {}

    def _merge(dump: dict, srv_label: str = ""):
        for pub, data in dump.items():
            if pub not in merged:
                merged[pub] = dict(data)
                merged[pub]["server"] = srv_label if data.get("handshake") else ""
            else:
                ex = merged[pub]
                if data.get("handshake", 0) > ex.get("handshake", 0):
                    ex["handshake"] = data["handshake"]
                    ex["endpoint"]  = data.get("endpoint", "")
                    ex["server"]    = srv_label
                ex["rx"] = ex.get("rx", 0) + data.get("rx", 0)
                ex["tx"] = ex.get("tx", 0) + data.get("tx", 0)

    _merge(get_awg_dump(), "")

    if PARAMIKO_AVAILABLE:
        for srv in load_servers():
            if not srv.get("is_primary"):
                slave_dump = ssh_get_slave_awg_dump(srv)
                if slave_dump:
                    label = f"{srv.get('emoji', '')} {srv.get('name', 'Slave')}".strip()
                    _merge(slave_dump, label)

    _combined_dump_cache = merged
    _combined_dump_ts = now
    return merged


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


# Формат строки лога (одна запись = одна минута):
#   2024-01-15 14:32 AWG_DOWN=12.34 AWG_UP=5.67 ETH_DOWN=13.10 ETH_UP=6.20
#
# AWG = трафик клиентов:
#   AWG_DOWN — клиенты скачивают (TX awg0, сервер отдаёт клиентам)
#   AWG_UP   — клиенты отдают    (RX awg0, сервер получает от клиентов)
# ETH = трафик сервера:
#   ETH_DOWN — сервер получает от провайдера
#   ETH_UP   — сервер отдаёт провайдеру

def _parse_log_line(line: str) -> dict | None:
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


def get_system_stats() -> dict:
    """Системные метрики: uptime, RAM, диск, load average."""
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
    peers = get_combined_awg_dump()
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

    last_bw = peak.get("last", {})

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
        "clients_total_download": fmt_bytes(total_awg_down),
        "clients_total_upload":   fmt_bytes(total_awg_up),
        "awg_current_down": last_bw.get("awg_down", 0),
        "awg_current_up":   last_bw.get("awg_up",   0),
        "eth_current_down": last_bw.get("eth_down",  0),
        "eth_current_up":   last_bw.get("eth_up",    0),
        "awg_peak_day": {
            "down": day.get("awg_down", 0),
            "up":   day.get("awg_up",   0),
            "date": day.get("date", "—"),
        },
        "awg_peak_all": {
            "down": allp.get("awg_down", 0),
            "up":   allp.get("awg_up",   0),
        },
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
    peers  = get_combined_awg_dump()
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
