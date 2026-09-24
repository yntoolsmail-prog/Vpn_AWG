#!/usr/bin/env python3
# awg_clients.py — управление клиентами AWG: ключи, конфиги, CRUD
import os, re, subprocess, logging, json, zlib, base64, struct, threading

logger = logging.getLogger(__name__)

from awg_core import (
    AWG_CONF, AWG_IFACE, CLIENTS_DIR, EXCL_EXT, VPN_SUBNET,
    SERVER_ENDPOINT, SERVER_PUBLIC, SERVER_PORT, PRIMARY_DNS, SECONDARY_DNS, srv,
    awg_file_lock,
)

# Оставлен для обратной совместимости; реальная защита — awg_file_lock(),
# т.к. бот и TMA работают в разных процессах и threading.Lock их не разводит.
_AWG_LOCK = threading.Lock()


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


def get_all_clients() -> list:
    if not os.path.exists(CLIENTS_DIR):
        return []
    return sorted([f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")])


def get_user_clients(user_id: int) -> list:
    from awg_core import get_user_name
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
                elif k.lower() in _AWG_KEY_BY_LOWER:
                    # Ключи AWG регистронезависимы: старые конфиги писали «i1»
                    key = _AWG_KEY_BY_LOWER[k.lower()]
                    if key in _IPACKETS and not _valid_ipacket(v):
                        continue
                    obfs[key] = v
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


# ── Параметры обфускации AWG ──────────────────────────────────────────────────
# (ключ в конфиге, имя в server.env) в порядке записи в конфиг. Ключи конфига
# совпадают с ключами vpn://-JSON клиента AmneziaVPN, поэтому obfs уходит туда как есть.
#   до 2.0 — Jc…H4, I1–I5;  2.0 — S3/S4, диапазоны H1–H4;
#   3.0 — HeaderProtectionKey, ContentPaddingAddition, тайминги;
#   3.1 — RandomTrailers, DisableCookies.
# Новый ключ пишется в конфиг, только если задан в server.env: сервер на 2.0
# продолжает выдавать ровно те конфиги, что и раньше.
AWG_PARAMS = (
    ("Jc", "JC"), ("Jmin", "JMIN"), ("Jmax", "JMAX"),
    ("S1", "S1"), ("S2", "S2"), ("S3", "S3"), ("S4", "S4"),
    ("H1", "H1"), ("H2", "H2"), ("H3", "H3"), ("H4", "H4"),
    ("I1", "I1"), ("I2", "I2"), ("I3", "I3"), ("I4", "I4"), ("I5", "I5"),
    ("HeaderProtectionKey",    "HEADER_PROTECTION_KEY"),
    ("ContentPaddingAddition", "CONTENT_PADDING_ADDITION"),
    ("RekeyAfterTime",         "REKEY_AFTER_TIME"),
    ("RekeyTimeout",           "REKEY_TIMEOUT"),
    ("RejectAfterTime",        "REJECT_AFTER_TIME"),
    ("KeepaliveTimeout",       "KEEPALIVE_TIMEOUT"),
    ("MaxHandshakeAttempts",   "MAX_HANDSHAKE_ATTEMPTS"),
    ("RandomTrailers",         "RANDOM_TRAILERS"),
    ("DisableCookies",         "DISABLE_COOKIES"),
)
_AWG_KEY_BY_LOWER = {k.lower(): k for k, _ in AWG_PARAMS}
# I1–I5 шлёт перед хендшейком его инициатор — клиент; в awg0.conf сервера их
# нет (так же делает сам Amnezia)
_IPACKETS = ("I1", "I2", "I3", "I4", "I5")
# Признаки конфига 3.x — те же, что hasAwg3Markers() в клиенте AmneziaVPN
_AWG3_VALUES  = ("HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime",
                 "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout", "MaxHandshakeAttempts")
_AWG3_TOGGLES = ("RandomTrailers", "DisableCookies")


def _is_on(value) -> bool:
    return str(value or "").strip().lower() not in ("", "off", "0")


def _valid_ipacket(value: str) -> bool:
    """I1–I5 — цепочка тегов <b 0x…><r N>…. Голое число (так писал setup.sh
    до AWG 3.1) ядро разбирает в пакет нулевой длины, и он не отправляется."""
    return "<" in value and ">" in value


def is_awg3(obfs: dict) -> bool:
    """Конфиг с параметрами AWG 3.x: приложения на 2.0 и старше его не примут."""
    return (any(str(obfs.get(k) or "").strip() for k in _AWG3_VALUES)
            or any(_is_on(obfs.get(k)) for k in _AWG3_TOGGLES))


def gen_obfs(env: dict = None) -> dict:
    """Параметры обфускации для конфигов клиентов — из server.env основного сервера
    (env — другой набор переменных того же вида, например новый при переводе на 3.1)."""
    src = srv if env is None else env
    obfs = {"Jc": "4", "Jmin": "40", "Jmax": "70", "S1": "0", "S2": "0",
            "H1": "1", "H2": "2", "H3": "3", "H4": "4"}
    for key, name in AWG_PARAMS:
        value = str(src.get(name, "")).strip()
        if not value or (key in _IPACKETS and not _valid_ipacket(value)):
            continue
        obfs[key] = value
    return obfs


def conf_awg_params(text: str) -> dict:
    """Параметры AWG из секции [Interface] текста конфига (ключи — как в AWG_PARAMS)."""
    params, section = {}, ""
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s.lower()
            continue
        if section != "[interface]" or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        key = _AWG_KEY_BY_LOWER.get(k.strip().lower())
        if key:
            params[key] = v.strip()
    return params


def _obfs_lines(obfs: dict, skip: tuple = ()) -> list:
    return [f"{k} = {obfs[k]}" for k, _ in AWG_PARAMS
            if k not in skip and str(obfs.get(k) or "").strip()]


def _client_tunnel_opts(obfs: dict, env: dict = None) -> tuple:
    """(MTU, PersistentKeepalive) для конфига клиента.
    3.x: нонс защиты заголовков (S4 ≥ 12) удлиняет каждый пакет данных, поэтому
    MTU 1376 — как у клиента AmneziaVPN; keepalive — диапазон, чтобы ровный
    интервал не был подписью. До 3.x — как раньше: MTU не задаём, keepalive 25
    (приложения 2.0 диапазон не разбирают)."""
    src = srv if env is None else env
    if is_awg3(obfs):
        return (str(src.get("CLIENT_MTU", "")).strip() or "1376",
                str(src.get("PERSISTENT_KEEPALIVE", "")).strip() or "25-35")
    return "", "25"


def make_wg_conf(priv, ip, psk, obfs, endpoint: str = None,
                 allowed_ips: str = "0.0.0.0/0",
                 server_public: str = None, server_port: str = None) -> str:
    ep  = endpoint or SERVER_ENDPOINT
    pub = server_public or SERVER_PUBLIC
    prt = server_port or SERVER_PORT
    mtu, keepalive = _client_tunnel_opts(obfs)
    parts = [
        "[Interface]",
        f"PrivateKey = {priv}", f"Address = {ip}/32",
        f"DNS = {PRIMARY_DNS}, {SECONDARY_DNS}",
    ]
    if mtu:
        parts.append(f"MTU = {mtu}")
    parts += _obfs_lines(obfs)
    parts += ["", "[Peer]", f"PublicKey = {pub}", f"PresharedKey = {psk}",
              f"Endpoint = {ep}:{prt}", f"AllowedIPs = {allowed_ips}",
              f"PersistentKeepalive = {keepalive}"]
    return "\n".join(parts) + "\n"


def make_vpn_link(priv, pub, ip, psk, obfs, name, endpoint: str = None,
                  server_public: str = None, server_port: str = None) -> str:
    ep  = endpoint or SERVER_ENDPOINT
    spub = server_public or SERVER_PUBLIC
    prt  = server_port or SERVER_PORT
    mtu, keepalive = _client_tunnel_opts(obfs)
    mtu_line = f"MTU = {mtu}\n" if mtu else ""
    obfs_block = "".join(f"{line}\n" for line in _obfs_lines(obfs))
    wg = (
        f"[Interface]\nAddress = {ip}/32\nDNS = {PRIMARY_DNS}, {SECONDARY_DNS}\n"
        f"PrivateKey = {priv}\n{mtu_line}{obfs_block}"
        f"\n[Peer]\nPublicKey = {spub}\nPresharedKey = {psk}\n"
        f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {ep}:{prt}\n"
        f"PersistentKeepalive = {keepalive}\n"
    )
    params = {k: obfs[k] for k, _ in AWG_PARAMS if str(obfs.get(k) or "").strip()}
    lc = {**params, "allowed_ips": ["0.0.0.0/0", "::/0"], "clientId": pub,
          "client_ip": ip, "client_priv_key": priv, "client_pub_key": pub,
          "config": wg, "hostName": ep, "mtu": mtu or "1420",
          "persistent_keep_alive": keepalive, "port": int(prt),
          "psk_key": psk, "server_pub_key": spub}
    awg = {**params, "last_config": json.dumps(lc, indent=4),
           "port": str(prt),
           "subnet_address": ".".join(ip.split(".")[:3]) + ".0",
           "transport_proto": "udp"}
    if is_awg3(obfs):
        awg["protocol_version"] = "3.1"
    c = {"containers": [{"awg": awg, "container": "amnezia-awg"}],
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


def _remove_peer_from_all_slaves(name: str, pub: str):
    """Снимает peer со всех slave-серверов в фоне.
    Slave — полная копия primary, поэтому без этого удалённый конфиг
    продолжает работать через slave-эндпоинт."""
    from awg_core import load_servers
    from awg_ssh import PARAMIKO_AVAILABLE, ssh_remove_peer_from_slave
    if not PARAMIKO_AVAILABLE:
        return
    for srv_item in [s for s in load_servers() if not s.get("is_primary")]:
        label = f"{srv_item.get('emoji', '')} {srv_item.get('name', 'slave')}".strip()

        def _run(server=srv_item, lbl=label):
            try:
                ssh_remove_peer_from_slave(server, name, pub)
                logger.info(f"remove_client({name}): снят со slave {lbl}")
            except Exception as e:
                logger.error(f"remove_client({name}): slave {lbl} — НЕ снят: {e}")

        threading.Thread(target=_run, daemon=True).start()


def remove_client_from_awg(name: str):
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        return
    pub = get_client_pub(name)
    with awg_file_lock():
        if pub:
            subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"])
        _remove_peer_from_conf(name)
        for ext in [".conf", ".pub", ".vpn", ".vpnlink", EXCL_EXT]:
            p = f"{CLIENTS_DIR}/{name}{ext}"
            if os.path.exists(p):
                os.remove(p)
    # Slave'ы — после освобождения лока: SSH долгий, держать блокировку незачем
    _remove_peer_from_all_slaves(name, pub or "")


async def create_client(name: str) -> dict:
    """Создаёт клиента AWG с верификацией и откатом.
    awg_file_lock() защищает от гонки между процессами бота и TMA."""
    with awg_file_lock():
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
        # В .conf лежит приватный ключ — закрываем от чтения кем угодно
        try:
            os.chmod(CLIENTS_DIR, 0o700)
            os.chmod(conf_path, 0o600)
        except Exception:
            pass

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


# ── Перевод сервера на AWG 3.1 ────────────────────────────────────────────────
# Значения — как у самого Amnezia (amnezia-client: protocolConstants.h,
# awgInstaller.cpp): тайминги разбросаны вокруг констант WireGuard (120/5/180/10 с),
# чтобы период рекея и keepalive не был ровным; I1 — пакет, похожий на DNS-ответ
# (2 случайных байта ID + ответ на icloud.com). setup.sh генерирует то же самое —
# меняя здесь, поменяйте и там.
AWG31_DEFAULTS = {
    "REKEY_AFTER_TIME":       "100-120",
    "REKEY_TIMEOUT":          "3-7",
    "REJECT_AFTER_TIME":      "150-180",
    "KEEPALIVE_TIMEOUT":      "5-15",
    "MAX_HANDSHAKE_ATTEMPTS": "15-20",
    "RANDOM_TRAILERS":        "on",
    "DISABLE_COOKIES":        "on",
    "CLIENT_MTU":             "1376",
    "PERSISTENT_KEEPALIVE":   "25-35",
    "I1": "<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001"
          "c00c000100010000105a00044d583737>",
}

_UPGRADE_HINT = (
    "Обновите пакеты AmneziaWG (бот: Техобслуживание → «Бэкап + обновление всех",
    "серверов», или apt update && apt upgrade) и перезагрузите сервер, если",
    "обновился модуль ядра. Сборки 3.1 есть в PPA Amnezia для LTS-версий Ubuntu.",
)


def gen_awg31_env(current: dict) -> dict:
    """Переменные server.env для AWG 3.1 поверх текущих (current — прочитанный server.env).
    Jc/Jmin/Jmax и одиночные H1–H4 остаются прежними. S1–S4 не меньше 12: первые
    12 байт каждой набивки — нонс защиты заголовков. S4 не больше 20: он удлиняет
    каждый пакет данных, и с MTU интерфейса сервера 1420 пакет не должен выйти за
    1500. H1–H4 — одиночные числа: под защитой заголовков они на проводе всё равно
    зашифрованы, а широкий диапазон вместе с RandomTrailers изредка выдавал бы
    пакет данных за хендшейк (ядро опознаёт тип по «размер ≥ и H в диапазоне»)."""
    import random
    rnd = random.SystemRandom()
    cur = {k: str(v).strip() for k, v in current.items()}

    def num(key):
        return int(cur[key]) if cur.get(key, "").isdigit() else None

    env = dict(AWG31_DEFAULTS)
    jc, jmin, jmax = num("JC"), num("JMIN"), num("JMAX")
    if None in (jc, jmin, jmax) or jmin > jmax:
        jc, jmin, jmax = rnd.randint(3, 10), rnd.randint(10, 50), rnd.randint(51, 100)
    env.update(JC=str(jc), JMIN=str(jmin), JMAX=str(jmax))

    s1, s2 = num("S1"), num("S2")
    if s1 is None or s2 is None or min(s1, s2) < 12 or s1 + 56 == s2:
        # Оба из 15–64: S1 + 56 ≥ 71, поэтому init (148+S1) и response (92+S2)
        # никогда не совпадут по размеру
        s1, s2 = rnd.randint(15, 64), rnd.randint(15, 64)
    s3, s4 = num("S3"), num("S4")
    if s3 is None or not 12 <= s3 <= 64:
        s3 = rnd.randint(12, 32)
    if s4 is None or not 12 <= s4 <= 20:
        s4 = rnd.randint(12, 20)
    env.update(S1=str(s1), S2=str(s2), S3=str(s3), S4=str(s4))

    hs = [cur.get(f"H{i}", "") for i in range(1, 5)]
    if not all(h.isdigit() and int(h) < 2**32 for h in hs) or len(set(hs)) < 4:
        hs = [str(h) for h in rnd.sample(range(5, 2**32), 4)]
    env.update({f"H{i}": h for i, h in enumerate(hs, 1)})

    if _valid_ipacket(cur.get("I1", "")):
        env["I1"] = cur["I1"]
    env["HEADER_PROTECTION_KEY"] = subprocess.check_output(["awg", "genkey"], text=True).strip()
    return env


def _env_quote(value: str) -> str:
    """server.env читают и Python (load_env), и bash (source в vpn.sh): значение
    с пробелами или < > (теги I1) без кавычек bash принял бы за перенаправление."""
    return value if re.fullmatch(r"[A-Za-z0-9_.,:/+=@%-]*", value) else f"'{value}'"


def _env_with_updates(text: str, updates: dict) -> str:
    out, done = [], set()
    for line in text.splitlines():
        s = line.strip()
        key = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else ""
        if key in updates:
            if key not in done:
                out.append(f"{key}={_env_quote(updates[key])}")
                done.add(key)
            continue
        out.append(line)
    out += [f"{k}={_env_quote(v)}" for k, v in updates.items() if k not in done]
    return "\n".join(out) + "\n"


def _set_interface_params(text: str, new_lines: list, anchors: tuple,
                          drop_extra: tuple = (), keepalive: str = "") -> str:
    """Меняет параметры AWG в [Interface]: прежние строки убираются, новые встают
    после последней строки-якоря (ListenPort у сервера, DNS у клиента).
    keepalive — новое значение PersistentKeepalive в [Peer] конфига клиента."""
    drop = set(_AWG_KEY_BY_LOWER) | {k.lower() for k in drop_extra}
    anchors = {a.lower() for a in anchors}
    out, section, pos = [], "", None
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s.lower()
            out.append(line)
            if section == "[interface]" and pos is None:
                pos = len(out)
            continue
        key = s.split("=", 1)[0].strip().lower() if "=" in s and not s.startswith("#") else ""
        if section == "[interface]" and key:
            if key in drop:
                continue
            if key in anchors:
                pos = len(out) + 1
        if section == "[peer]" and key == "persistentkeepalive" and keepalive:
            line = f"PersistentKeepalive = {keepalive}"
        out.append(line)
    if pos is None:
        raise ValueError("в конфиге нет секции [Interface]")
    out[pos:pos] = new_lines
    return "\n".join(out)


def _write_private(path: str, text: str):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _iface_param(param: str) -> str:
    try:
        r = subprocess.run(["awg", "show", AWG_IFACE, param],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _restart_awg_iface() -> str:
    """Перезапуск интерфейса тем же путём, что на слейве. '' — успех, иначе причина."""
    unit = f"awg-quick@{AWG_IFACE}"
    subprocess.run(["systemctl", "stop", unit], capture_output=True, timeout=60)
    subprocess.run(["awg-quick", "down", AWG_IFACE], capture_output=True, timeout=60)
    r = subprocess.run(["systemctl", "start", unit], capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        return ""
    try:
        log = subprocess.run(["journalctl", "-u", unit, "-n", "3", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        log = ""
    return log.replace("\n", " | ") or r.stderr.strip() or f"systemctl start: код {r.returncode}"


def migrate_to_awg31(skip_unready_slaves: bool = False) -> tuple:
    """Переводит основной сервер и все слейвы с AWG 2.0 и старше на 3.1.
    Интерфейс 3.x клиентов 2.0 не пускает, поэтому это разовый перелом: старые
    конфиги перестают подключаться, всем устройствам нужен новый конфиг.
    Порядок: проверка версий здесь и на слейвах → бэкап → новые параметры в
    server.env, awg0.conf и конфиги клиентов → перезапуск AWG с проверкой, что
    параметры применились (иначе файлы возвращаются как были) → переклон слейвов.
    Возвращает (успех, строки отчёта, число слейвов, не готовых к 3.1).
    Бот и TMA держат server.env в памяти —
    после перевода их нужно перезапустить (vpn.sh делает это сам)."""
    from awg_core import ENV_FILE, create_backup, load_env, load_servers
    from awg_ssh import (awg31_blockers, awg_versions_local,
                         ssh_clone_awg_to_slave, ssh_get_awg_versions)

    if os.path.exists("/etc/awg-slave"):
        return False, ["Это слейв: параметры AWG приходят с основного сервера.",
                       "Запустите перевод на основном — слейвы он переведёт сам."], 0
    current = load_env(ENV_FILE)
    if is_awg3(gen_obfs(current)):
        return False, ["Сервер уже работает на AWG 3.x — переводить нечего."], 0
    blockers = awg31_blockers(awg_versions_local())
    if blockers:
        return False, ["Основной сервер не готов к AWG 3.1:",
                       *[f"  • {b}" for b in blockers], *_UPGRADE_HINT], 0

    ready, unready = [], []
    for server in [s for s in load_servers() if not s.get("is_primary")]:
        label = f"{server.get('emoji', '')} {server.get('name', 'slave')}".strip()
        try:
            problems = awg31_blockers(ssh_get_awg_versions(server))
        except Exception as e:
            problems = [f"нет SSH-связи: {e}"]
        if problems:
            unready.append(f"  • {label}: {'; '.join(problems)}")
        else:
            ready.append((server, label))
    if unready and not skip_unready_slaves:
        return False, ["Слейвы не готовы к AWG 3.1 — после перевода они перестали бы",
                       "пускать клиентов:", *unready, *_UPGRADE_HINT], len(unready)

    try:
        backup = create_backup("pre_awg31")
        new_env = gen_awg31_env(current)
    except Exception as e:
        return False, [f"Перевод не начат — бэкап или генерация ключа не удались: {e}"], 0
    obfs = gen_obfs({**current, **new_env})
    mtu, keepalive = _client_tunnel_opts(obfs, new_env)
    client_lines = [f"MTU = {mtu}", *_obfs_lines(obfs)]
    server_lines = _obfs_lines(obfs, skip=_IPACKETS)

    with awg_file_lock():
        conf_paths = [f"{CLIENTS_DIR}/{n}.conf" for n in get_all_clients()]
        saved = {}
        for path in [AWG_CONF, ENV_FILE, *conf_paths]:
            with open(path) as f:
                saved[path] = f.read()
        try:
            _write_private(AWG_CONF, _set_interface_params(
                saved[AWG_CONF], server_lines, ("ListenPort",)))
            for path in conf_paths:
                _write_private(path, _set_interface_params(
                    saved[path], client_lines, ("Address", "DNS"),
                    drop_extra=("MTU",), keepalive=keepalive))
            _write_private(ENV_FILE, _env_with_updates(saved[ENV_FILE], new_env))
            err = _restart_awg_iface()
            if not err and (
                _iface_param("header-protection-key") != new_env["HEADER_PROTECTION_KEY"]
                or _iface_param("random-trailers") != "on"
            ):
                err = "интерфейс поднялся без параметров 3.1 — утилиты и модуль ядра разных версий?"
        except Exception as e:
            err = str(e)
        if err:
            for path, text in saved.items():
                _write_private(path, text)
            try:
                back = _restart_awg_iface()
            except Exception as e:
                back = str(e)
            return False, [f"Перевод не удался: {err}",
                           "Файлы возвращены как были, " + (
                               "AWG перезапущен на прежних параметрах." if not back else
                               f"но AWG не поднялся: {back} — systemctl restart awg-quick@{AWG_IFACE}"),
                           f"Бэкап до перевода: {backup}"], 0

    # Этот процесс дальше выдаёт конфиги уже с новыми параметрами
    srv.update(new_env)
    report = ["✅ Основной сервер работает на AWG 3.1.",
              f"   Конфигов клиентов переписано: {len(conf_paths)}",
              f"   Бэкап до перевода: {backup}"]
    for server, label in ready:
        try:
            ssh_clone_awg_to_slave(server)
            report.append(f"✅ {label}: переведён")
        except Exception as e:
            report.append(f"❌ {label}: {e}")
    if unready:
        report += ["⚠️ Остались на прежних параметрах (клиенты с новыми конфигами",
                   "   к ним не подключатся — после обновления нажмите",
                   "   «Синхронизировать» в карточке сервера в боте):", *unready]
    report += [
        "",
        "Старые конфиги больше не подключаются: каждому устройству нужен новый",
        "конфиг, QR или ссылка из бота. Приложения — AmneziaVPN 5.0.1.5+ или",
        "AmneziaWG 3.1+; роутеры и сторонние клиенты без поддержки 3.1 отвалятся.",
        f"Откат: восстановить бэкап {os.path.basename(backup)} и синхронизировать слейвы.",
    ]
    return True, report, len(unready)
