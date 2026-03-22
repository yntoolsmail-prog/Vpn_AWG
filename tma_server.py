#!/usr/bin/env python3
# tma_server.py — HTTP-сервер статистики для TMA
# Version: 1.0
# Запускается отдельно от bot.py, читает те же файлы конфигурации.
# Не трогает и не импортирует bot.py.

import os, json, time, subprocess, hashlib, hmac
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

# ── Пути (те же что в bot.py) ──────────────────────────────────────────────────
CONFIG_FILE  = "/etc/amnezia/amneziawg/bot.env"
ENV_FILE     = "/etc/amnezia/amneziawg/server.env"
USERS_FILE   = "/etc/amnezia/amneziawg/users.json"
CLIENTS_DIR  = "/etc/amnezia/amneziawg/clients"
BW_PEAK_FILE = "/etc/amnezia/amneziawg/bw_peak.json"
BW_LOG_FILE  = "/var/log/awg-bw.log"
TMA_DIR      = "/etc/amnezia/amneziawg/tma"   # здесь лежит index.html

LISTEN_HOST  = "127.0.0.1"   # только локально — снаружи через nginx
LISTEN_PORT  = 8080

# ── Загрузка конфигов ──────────────────────────────────────────────────────────
def load_env(path):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except:
        pass
    return env

cfg       = load_env(CONFIG_FILE)
srv       = load_env(ENV_FILE)

BOT_TOKEN  = cfg.get("BOT_TOKEN", "")
ADMIN_ID   = int(cfg.get("ADMIN_ID", "0"))
SERVER_IP  = srv.get("SERVER_IP", "")
SERVER_PORT_AWG = srv.get("SERVER_PORT", "")
SERVER_ENDPOINT = srv.get("SERVER_ENDPOINT", "") or SERVER_IP
AWG_IFACE  = srv.get("VPN_IFACE", "awg0")
TZ         = srv.get("TIMEZONE", "UTC")

os.environ["TZ"] = TZ
try:
    time.tzset()
except AttributeError:
    pass

# ── Авторизация через Telegram initData ───────────────────────────────────────
def verify_telegram_init_data(init_data_raw: str) -> int | None:
    """
    Проверяет подпись initData от Telegram WebApp.
    Возвращает user_id если подпись верна, иначе None.
    Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data_raw or not BOT_TOKEN:
        return None

    try:
        # Разбираем строку вида: key=value&key=value&...
        params = {}
        for part in init_data_raw.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[unquote(k)] = unquote(v)

        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        # Строим data_check_string — отсортированные пары key=value через \n
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )

        # Секретный ключ = HMAC-SHA256("WebAppData", BOT_TOKEN)
        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        # Ожидаемый хэш
        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        # Проверяем что данные не устарели (не старше 1 часа)
        auth_date = int(params.get("auth_date", "0"))
        if time.time() - auth_date > 3600:
            return None

        # Достаём user_id из поля user (JSON-строка)
        user_json = params.get("user", "{}")
        user = json.loads(user_json)
        return int(user.get("id", 0)) or None

    except Exception:
        return None

# ── Сбор данных о сервере ──────────────────────────────────────────────────────
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
        handshake = int(parts[4]) if parts[4] not in ("0", "(none)") else 0
        rx        = int(parts[5])
        tx        = int(parts[6])
        peers[pub] = {"rx": rx, "tx": tx, "endpoint": endpoint, "handshake": handshake}
    return peers

def get_all_clients() -> list:
    if not os.path.exists(CLIENTS_DIR):
        return []
    return sorted([f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")])

def get_client_pub(name: str) -> str | None:
    pub_path = f"{CLIENTS_DIR}/{name}.pub"
    if os.path.exists(pub_path):
        with open(pub_path) as f:
            return f.read().strip()
    return None

def get_host_iface() -> str:
    try:
        out = subprocess.check_output(["ip", "route", "get", "8.8.8.8"], text=True)
        parts = out.split()
        for i, p in enumerate(parts):
            if p == "dev" and i + 1 < len(parts):
                return parts[i + 1]
    except:
        pass
    try:
        for line in open("/proc/net/dev").readlines()[2:]:
            iface = line.split(":")[0].strip()
            if iface and iface != "lo" and not iface.startswith("awg"):
                return iface
    except:
        pass
    return "eth0"

def read_iface_bytes(iface: str) -> tuple:
    try:
        rx = int(open(f"/sys/class/net/{iface}/statistics/rx_bytes").read())
        tx = int(open(f"/sys/class/net/{iface}/statistics/tx_bytes").read())
        return rx, tx
    except:
        return 0, 0

def fmt_bytes(b: int) -> str:
    if b < 1024:       return f"{b} B"
    elif b < 1024**2:  return f"{b/1024:.1f} KB"
    elif b < 1024**3:  return f"{b/1024**2:.1f} MB"
    else:              return f"{b/1024**3:.2f} GB"

def load_bw_peak() -> dict:
    try:
        with open(BW_PEAK_FILE) as f:
            return json.load(f)
    except:
        return {}

def get_vnstat_monthly() -> list:
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
                "label":    label,
                "rx_gb":    rx_gb,
                "tx_gb":    tx_gb,
                "total_gb": round(rx_gb + tx_gb, 2),
                "current":  label == current_label,
            })
        return result
    except:
        return []

def get_bw_top(n: int = 5) -> list:
    try:
        rows = []
        for line in open(BW_LOG_FILE).readlines():
            parts = line.strip().split()
            if len(parts) == 4:
                try:
                    dt = parts[0] + " " + parts[1]
                    rx = float(parts[2].split("=")[1])
                    tx = float(parts[3].split("=")[1])
                    rows.append({"dt": dt, "rx": rx, "tx": tx})
                except:
                    pass
        rows.sort(key=lambda x: x["rx"] + x["tx"], reverse=True)
        return rows[:n]
    except:
        return []

def collect_stats_full() -> dict:
    """Полная статистика — только для ADMIN_ID"""
    peers  = get_awg_dump()
    now    = int(time.time())
    iface  = get_host_iface()
    peak   = load_bw_peak()

    # Онлайн: хэндшейк < 3 минут
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)

    # Uptime
    try:    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    except: uptime = "—"

    # RAM
    try:
        mem = subprocess.check_output(["free", "-m"], text=True).split("\n")[1].split()
        ram_used  = int(mem[2])
        ram_total = int(mem[1])
    except:
        ram_used = ram_total = 0

    # Диск
    try:
        disk = subprocess.check_output(["df", "-h", "/"], text=True).split("\n")[1].split()
        disk_used  = disk[2]
        disk_total = disk[1]
        disk_pct   = int(disk[4].replace("%", ""))
    except:
        disk_used = disk_total = "—"
        disk_pct  = 0

    # Load average
    try:
        load = open("/proc/loadavg").read().split()[:3]
    except:
        load = ["0", "0", "0"]

    # Текущая скорость — берём из bw_peak.json (bot.py пишет туда каждые 5 сек)
    last_bw = peak.get("last", {})
    cur_rx  = last_bw.get("rx", 0)
    cur_tx  = last_bw.get("tx", 0)

    # Суммарный трафик AWG пиров (с момента перезагрузки)
    total_rx = sum(p.get("rx", 0) for p in peers.values())
    total_tx = sum(p.get("tx", 0) for p in peers.values())

    # Пики
    day  = peak.get("day", {})
    allp = peak.get("all", {})

    # Клиенты с деталями
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
            "rx":        fmt_bytes(stats.get("rx", 0)),
            "tx":        fmt_bytes(stats.get("tx", 0)),
        })

    # Пользователи
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
        users_count = len(users.get("approved", {}))
    except:
        users_count = 0

    return {
        "awg_status":      "running",
        "server_ip":       SERVER_IP,
        "server_port":     SERVER_PORT_AWG,
        "server_endpoint": SERVER_ENDPOINT,
        "iface":           iface,
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
        "traffic_total_rx": fmt_bytes(total_rx),
        "traffic_total_tx": fmt_bytes(total_tx),
        "bw_current_rx":   cur_rx,
        "bw_current_tx":   cur_tx,
        "bw_peak_day": {
            "rx":   day.get("rx", 0),
            "tx":   day.get("tx", 0),
            "load": day.get("load", 0),
            "date": day.get("date", "—"),
        },
        "bw_peak_all": {
            "rx":   allp.get("rx", 0),
            "tx":   allp.get("tx", 0),
            "load": allp.get("load", 0),
        },
        "bw_top":  get_bw_top(5),
        "monthly": get_vnstat_monthly(),
        "peers":   peers_list,
    }

def collect_stats_basic() -> dict:
    """Урезанная статистика — для обычных пользователей"""
    peers = get_awg_dump()
    now   = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)

    try:    uptime = subprocess.check_output(["uptime", "-p"], text=True).strip()
    except: uptime = "—"

    return {
        "awg_status":   "running",
        "server_endpoint": SERVER_ENDPOINT,
        "uptime":       uptime,
        "peers_total":  len(get_all_clients()),
        "peers_online": online,
    }

# ── HTTP-обработчик ────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Убираем стандартный лог — слишком шумный
        pass

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # CORS для Telegram WebApp
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
            ext = path.rsplit(".", 1)[-1].lower()
            mime = {
                "html": "text/html; charset=utf-8",
                "js":   "application/javascript",
                "css":  "text/css",
                "png":  "image/png",
                "ico":  "image/x-icon",
            }.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        # Preflight CORS
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Init-Data")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        # ── Статика (index.html и прочие файлы TMA) ──
        if path == "/" or path == "/index.html":
            self.send_file(os.path.join(TMA_DIR, "index.html"))
            return

        # ── API: статистика ──
        if path == "/api/stats":
            # Читаем initData из заголовка X-Init-Data
            init_data = self.headers.get("X-Init-Data", "")
            user_id   = verify_telegram_init_data(init_data)

            if not user_id:
                self.send_json({"error": "unauthorized"}, 401)
                return

            if user_id == ADMIN_ID:
                data = collect_stats_full()
                data["is_admin"] = True
            else:
                data = collect_stats_basic()
                data["is_admin"] = False

            self.send_json(data)
            return

        # ── API: healthcheck (без авторизации) ──
        if path == "/api/health":
            self.send_json({"ok": True, "ts": int(time.time())})
            return

        self.send_response(404)
        self.end_headers()


# ── Запуск ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n\033[0;32m✓ TMA сервер запущен: http://{LISTEN_HOST}:{LISTEN_PORT}\033[0m")
    print(f"  Admin ID: {ADMIN_ID}")
    print(f"  TMA dir:  {TMA_DIR}\n")

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлен.")