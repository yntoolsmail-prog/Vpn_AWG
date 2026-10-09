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
    # Тип контейнера AmneziaVPN, а не версия протокола: «amnezia-awg» приложение
    # подписывает «AmneziaWG Legacy» — так у него называется старая раскладка
    # собственной установки сервера. На подключение тип не влияет, оба идут
    # одним кодом протокола. «amnezia-awg2» — только для 3.x: его знают
    # приложения с поддержкой 3.1 (AmneziaVPN 5.0.1.5+), а ссылки сервера на 2.0
    # могут открывать и старые
    container = "amnezia-awg"
    if is_awg3(obfs):
        awg["protocol_version"] = "3.1"
        container = "amnezia-awg2"
    c = {"containers": [{"awg": awg, "container": container}],
         "defaultContainer": container, "description": name,
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
# чтобы период рекея и keepalive не был ровным. setup.sh генерирует то же самое —
# меняя здесь, поменяйте и там.
AWG31_DEFAULTS = {
    "REKEY_AFTER_TIME":       "100-120",
    "REKEY_TIMEOUT":          "3-7",
    "REJECT_AFTER_TIME":      "150-180",
    "KEEPALIVE_TIMEOUT":      "5-15",
    "MAX_HANDSHAKE_ATTEMPTS": "15-20",
    "RANDOM_TRAILERS":        "on",
    "DISABLE_COOKIES":        "on",
    # Набивка пакетов данных: случайные 10–100 байт вместо хвоста RandomTrailers
    # «до размера самого большого пакета потока». Без ограничения каждое мелкое
    # подтверждение раздувалось в среднем на ~650 байт: при скачивании отправка
    # росла с ~6% до ~27% от объёма, и на мобильном интернете, где отправка узкая,
    # тормозило и скачивание. Размеры остаются случайными (не кратны 16), хвост
    # хендшейка — прежний. Набивка внутри шифра — параметр односторонний
    "CONTENT_PADDING_ADDITION": "10-100",
    "CLIENT_MTU":             "1376",
    "PERSISTENT_KEEPALIVE":   "25-35",
}

# I1 по умолчанию у AmneziaVPN: «DNS-ответ про icloud.com», у всех стандартных
# установок одинаковы 42 байта из 44 — готовая сигнатура первого пакета
# хендшейка. Нужен только чтобы узнать его в server.env и заменить
AMNEZIA_DEFAULT_I1 = ("<r 2><b 0x858000010001000000000669636c6f756403636f6d0000010001"
                      "c00c000100010000105a00044d583737>")

# Jc — сколько мусорных пакетов клиент шлёт перед каждым хендшейком, одной
# пачкой вместе с I1 и самим хендшейком. Параметр односторонний: сервер мусор
# молча выбрасывает и с клиентом его не сверяет. 4–6 — как у AmneziaVPN
# (awgInstaller.cpp). Больше вредно: найдены сети, которые пропускают от нового
# потока UDP только первые ~10 пакетов, пока его не распознают, — при Jc = 9
# хендшейк (11-й пакет) терялся, и клиент подключался со второй попытки,
# через 3–7 с, или не подключался вовсе. С Jc = 4–6 пачка — 6–8 пакетов
JC_RANGE = (4, 6)

# Порт: 51820 — стандартный порт WireGuard, его проверяют первым. AmneziaVPN
# своим серверам ставит случайный из этого диапазона (protocolUtils.cpp)
PORT_RANGE = (30000, 49999)

_UPGRADE_HINT = (
    "Обновите пакеты AmneziaWG (бот: Техобслуживание → «Бэкап + обновление всех",
    "серверов», или apt update && apt upgrade) и перезагрузите сервер, если",
    "обновился модуль ядра. Сборки 3.1 есть в PPA Amnezia для LTS-версий Ubuntu.",
)


def gen_i1(rnd=None) -> str:
    """I1 — DNS-запрос, который меняется при каждом хендшейке: <r 2> — случайный ID,
    <rc N> — случайные буквы имени (a–z, A–Z, как у резолверов с 0x20-рандомизацией).
    Постоянна только структура DNS-заголовка — общая для любого DNS-запроса.
    Для сервера выбираются длина имени 6–14, зона и тип записи, поэтому и размер
    пакета у серверов разный (39–47 байт). Добавлена запись EDNS (UDP 1232),
    как у современных резолверов. Нужен только клиенту: сервер его выбрасывает.
    setup.sh собирает тот же пакет — меняя здесь, поменяйте и там."""
    import random
    rnd = rnd or random.SystemRandom()
    n = rnd.randint(6, 14)
    zone = rnd.choice(("com", "net", "org"))
    qtype = rnd.choice(("0001", "001c"))                # A / AAAA
    head = "01000001000000000001"                      # RD; 1 вопрос, 1 доп. запись
    tail = (f"{len(zone):02x}{zone.encode().hex()}00"  # .zone и корень
            f"{qtype}0001"                             # тип, класс IN
            "00002904d0000000000000")                  # OPT: UDP 1232, без флагов
    return f"<r 2><b 0x{head}><b 0x{n:02x}><rc {n}><b 0x{tail}>"


def gen_awg31_env() -> dict:
    """Полный набор переменных server.env для AWG 3.1 — всё с нуля, ничего от
    прежнего сервера: переход на 3.1 и так ломает все выданные конфиги, а
    унаследованные значения 2.0 (у установок с апреля — H1–H4 = 1–4 и S1 = S2 = 0)
    тянули бы старые слабости. Порт сюда не входит — его меняет migrate_to_awg31.
    • Jc — из JC_RANGE; Jmin/Jmax — размер каждого мусорного пакета, случайный в
      этих пределах.
    • S1–S4 не меньше 12: первые 12 байт каждой набивки — нонс защиты заголовков.
      S1/S2 из 15–64: init (148+S1) и response (92+S2) не совпадут по размеру.
      S4 не больше 20: он удлиняет каждый пакет данных, с MTU интерфейса 1420
      пакет не выйдет за 1500.
    • H1–H4 — одиночные случайные числа. Под ключом защиты заголовок каждого
      пакета (тип, индекс, счётчик) шифруется ChaCha20 со случайным нонсом, и H на
      проводе не видны — AmneziaVPN ставит даже 1–4. Случайные бесплатны и
      страхуют, если ключ защиты когда-нибудь выключат. Диапазоны нельзя: с
      RandomTrailers тип опознаётся по «размер ≥ и H в диапазоне», и широкий
      диапазон изредка выдавал бы пакет данных за хендшейк.
    • I1 — gen_i1(), меняется при каждом хендшейке.
    • Набивка пакетов данных ограничена (CONTENT_PADDING_ADDITION из
      AWG31_DEFAULTS) — см. комментарий там."""
    import random
    rnd = random.SystemRandom()
    env = dict(AWG31_DEFAULTS)
    env.update(JC=str(rnd.randint(*JC_RANGE)),
               JMIN=str(rnd.randint(10, 50)), JMAX=str(rnd.randint(51, 100)),
               S1=str(rnd.randint(15, 64)), S2=str(rnd.randint(15, 64)),
               S3=str(rnd.randint(12, 32)), S4=str(rnd.randint(12, 20)))
    env.update({f"H{i}": str(h) for i, h in enumerate(rnd.sample(range(5, 2**32), 4), 1)})
    env["I1"] = gen_i1(rnd)
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


def _replace_endpoint_port(text: str, port) -> str:
    """Порт в строке Endpoint конфига клиента (адрес остаётся, [IPv6] тоже)."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if "=" in s and not s.startswith("#") and s.split("=", 1)[0].strip().lower() == "endpoint":
            host = s.split("=", 1)[1].strip().rsplit(":", 1)[0]
            line = f"Endpoint = {host}:{port}"
        out.append(line)
    return "\n".join(out)


def migrate_to_awg31(skip_unready_slaves: bool = False, regen: bool = False,
                     new_port: int = None) -> tuple:
    """Переводит основной сервер и все слейвы с AWG 2.0 и старше на 3.1, а с
    regen=True перевыпускает весь набор параметров 3.1 на сервере, который уже
    на 3.x (gen_awg31_env — всё с нуля). new_port — заодно сменить порт AWG:
    ListenPort, SERVER_PORT, Endpoint в файлах устройств и awg_port всех серверов
    в servers.json (слейвы получают порт основного при переклоне).
    Интерфейс 3.x клиентов 2.0 не пускает, а перевыпуск меняет S/H/ключ, поэтому
    это разовый перелом: старые конфиги перестают подключаться, всем
    устройствам нужен новый конфиг.
    Порядок: проверка версий здесь и на слейвах → бэкап → новые параметры в
    server.env, awg0.conf и конфиги клиентов → перезапуск AWG с проверкой, что
    параметры применились (иначе файлы возвращаются как были) → переклон слейвов.
    Возвращает (успех, строки отчёта, число слейвов, не готовых к 3.1).
    Бот и TMA держат server.env в памяти —
    после перевода их нужно перезапустить (vpn.sh делает это сам)."""
    from awg_core import (ENV_FILE, create_backup, invalidate_servers_cache, load_env,
                          load_servers, save_servers)
    from awg_ssh import (awg31_blockers, awg_versions_local,
                         ssh_clone_awg_to_slave, ssh_get_awg_versions)

    if os.path.exists("/etc/awg-slave"):
        return False, ["Это слейв: параметры AWG приходят с основного сервера.",
                       "Запустите перевод на основном — слейвы он переведёт сам."], 0
    current = load_env(ENV_FILE)
    if is_awg3(gen_obfs(current)) and not regen:
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
        backup = create_backup("pre_regen" if regen else "pre_awg31")
        new_env = gen_awg31_env()
        if new_port:
            new_env["SERVER_PORT"] = str(new_port)
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
            server_conf = _set_interface_params(saved[AWG_CONF], server_lines, ("ListenPort",))
            if new_port:
                server_conf = _replace_iface_param(server_conf, "ListenPort", str(new_port))
            _write_private(AWG_CONF, server_conf)
            for path in conf_paths:
                text = _set_interface_params(
                    saved[path], client_lines, ("Address", "DNS"),
                    drop_extra=("MTU",), keepalive=keepalive)
                if new_port:
                    text = _replace_endpoint_port(text, new_port)
                _write_private(path, text)
            _write_private(ENV_FILE, _env_with_updates(saved[ENV_FILE], new_env))
            err = _restart_awg_iface()
            if not err and (
                _iface_param("header-protection-key") != new_env["HEADER_PROTECTION_KEY"]
                or _iface_param("random-trailers") != "on"
            ):
                err = "интерфейс поднялся без параметров 3.1 — утилиты и модуль ядра разных версий?"
            if not err and new_port and _iface_param("listen-port") != str(new_port):
                err = f"интерфейс слушает не порт {new_port} — занят другим процессом?"
        except Exception as e:
            err = str(e)
        if err:
            for path, text in saved.items():
                _write_private(path, text)
            try:
                back = _restart_awg_iface()
            except Exception as e:
                back = str(e)
            return False, [f"{'Перевыпуск' if regen else 'Перевод'} не удался: {err}",
                           "Файлы возвращены как были, " + (
                               "AWG перезапущен на прежних параметрах." if not back else
                               f"но AWG не поднялся: {back} — systemctl restart awg-quick@{AWG_IFACE}"),
                           f"Бэкап до {'перевыпуска' if regen else 'перевода'}: {backup}"], 0

    # Этот процесс дальше выдаёт конфиги уже с новыми параметрами
    srv.update(new_env)
    report = [("✅ Параметры AWG 3.1 перевыпущены." if regen
               else "✅ Основной сервер работает на AWG 3.1."),
              f"   Конфигов клиентов переписано: {len(conf_paths)}",
              f"   Бэкап до {'перевыпуска' if regen else 'перевода'}: {backup}"]
    if new_port:
        # Бот берёт порт для конфигов из servers.json. Слейв получает ListenPort
        # основного при переклоне, и его awg_port пишет ssh_clone_awg_to_slave —
        # здесь только основной
        try:
            invalidate_servers_cache()
            servers = load_servers()
            for s in servers:
                if s.get("is_primary"):
                    s["awg_port"] = int(new_port)
            save_servers(servers)
            report.append(f"   Порт AWG: {new_port}/UDP")
        except Exception as e:
            report.append(f"⚠️ servers.json: порт не обновлён ({e}) — бот выдаст старый порт")
    for server, label in ready:
        try:
            ssh_clone_awg_to_slave(server)
            report.append(f"✅ {label}: {'обновлён' if regen else 'переведён'}")
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


def _replace_iface_param(text: str, key: str, value: str) -> str:
    """Меняет значение key в [Interface] (регистр ключа не важен). Строки нет —
    текст не меняется: параметр не задан, и вписывать его незачем."""
    out, section = [], ""
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s.lower()
        elif (section == "[interface]" and "=" in s and not s.startswith("#")
              and s.split("=", 1)[0].strip().lower() == key.lower()):
            line = f"{key} = {value}"
        out.append(line)
    return "\n".join(out)


def _upsert_iface_param(text: str, key: str, value: str,
                        after: str = "HeaderProtectionKey") -> str:
    """key = value в [Interface]: строка есть — меняется, нет — встаёт после
    строки after, а без неё — последней строкой секции."""
    lines = text.split("\n")
    section, start, end, anchor = "", None, None, None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if section == "[interface]" and end is None:
                end = i
            section = s.lower()
            if section == "[interface]":
                start = i
            continue
        if section == "[interface]" and "=" in s and not s.startswith("#"):
            k = s.split("=", 1)[0].strip().lower()
            if k == key.lower():
                lines[i] = f"{key} = {value}"
                return "\n".join(lines)
            if k == after.lower():
                anchor = i
    if start is None:
        return text
    if anchor is None:
        stop = end if end is not None else len(lines)
        anchor = max((i for i in range(start, stop) if lines[i].strip()), default=start)
    lines.insert(anchor + 1, f"{key} = {value}")
    return "\n".join(lines)


def set_junk_count(jc: int = None) -> tuple:
    """Новый Jc (по умолчанию случайный из JC_RANGE) на работающем сервере: JC в
    server.env — для новых устройств, Jc во всех clients/*.conf — бот собирает
    перекачиваемый конфиг из файла устройства, а не из server.env, — и в awg0.conf.
    К интерфейсу применяется через `awg set … jc` без перезапуска: Jc односторонний,
    выданные конфиги со старым Jc продолжают работать. Слейвам ничего не нужно —
    свой Jc сервер использует, только когда хендшейк начинает он сам, а
    «Синхронизировать» заберёт новое значение вместе с конфигом.
    Бот и TMA держат server.env в памяти — после вызова их перезапускают.
    Возвращает (ok, отчёт)."""
    import random
    from awg_core import ENV_FILE
    if jc is None:
        jc = random.SystemRandom().randint(*JC_RANGE)
    report = []
    try:
        with awg_file_lock():
            paths = [AWG_CONF] + [f"{CLIENTS_DIR}/{n}.conf" for n in get_all_clients()]
            changed = 0
            for path in paths:
                with open(path) as f:
                    text = f.read()
                new = _replace_iface_param(text, "Jc", str(jc))
                if new != text:
                    _write_private(path, new)
                    changed += 1
            with open(ENV_FILE) as f:
                env_text = f.read()
            _write_private(ENV_FILE, _env_with_updates(env_text, {"JC": str(jc)}))
    except Exception as e:
        return False, [f"Не удалось записать Jc = {jc}: {e}"]
    report.append(f"Jc = {jc}: server.env, awg0.conf и файлы устройств ({changed} шт.)")
    try:
        r = subprocess.run(["awg", "set", AWG_IFACE, "jc", str(jc)],
                           capture_output=True, text=True, timeout=10)
        err = (r.stderr or "").strip() if r.returncode else ""
    except Exception as e:
        err = str(e)
    if not err and _iface_param("jc") == str(jc):
        report.append("Интерфейс: применено без перезапуска")
    else:
        report.append("Интерфейс: применится при следующем перезапуске AWG "
                      f"({err or 'awg set не подтвердил'}). До него «Синхронизировать» "
                      "слейв покажет расхождение Jc — сначала перезапустите AWG")
    report.append("Новый Jc получат новые устройства и перекачанные конфиги; "
                  "уже выданные работают и со старым.")
    return True, report


def set_padding_addition(value: str = None) -> tuple:
    """Ограничение набивки пакетов данных (ContentPaddingAddition, по умолчанию из
    AWG31_DEFAULTS) на работающем сервере 3.x — по образцу set_junk_count:
    CONTENT_PADDING_ADDITION в server.env (новые устройства), строка во всех
    clients/*.conf (перекачиваемый конфиг собирается из файла устройства) и в
    awg0.conf, к интерфейсу — `awg set … content-padding-addition` без перезапуска.
    Набивка лежит внутри шифра, получатель её просто отбрасывает: параметр
    односторонний, выданные конфиги продолжают работать, а ускорение на устройстве
    наступает после перекачки его конфига — набивку того, что отправляет устройство,
    задаёт оно само. Слейвы получат по «Синхронизировать» вместе с awg0.conf.
    На сервере 2.0 отказывается: ключ 3.x старые утилиты не разберут, и интерфейс
    не поднимется. Бот и TMA держат server.env в памяти — после вызова их
    перезапускают. Возвращает (ok, отчёт)."""
    from awg_core import ENV_FILE
    value = value or AWG31_DEFAULTS["CONTENT_PADDING_ADDITION"]
    try:
        with open(AWG_CONF) as f:
            server_awg3 = is_awg3(conf_awg_params(f.read()))
    except Exception as e:
        return False, [f"Не удалось прочитать {AWG_CONF}: {e}"]
    if not server_awg3:
        return False, ["Сервер на AWG 2.0: ограничение набивки — параметр 3.x, сначала "
                       "перевод на 3.1 (он задаёт его сам)"]
    try:
        with awg_file_lock():
            changed = 0
            for path in [AWG_CONF] + [f"{CLIENTS_DIR}/{n}.conf" for n in get_all_clients()]:
                with open(path) as f:
                    text = f.read()
                # Файл устройства без параметров 3.x к этому серверу и так не
                # подключится, а с ключом 3.x стал бы конфигом «под 3.1»
                if path != AWG_CONF and not is_awg3(conf_awg_params(text)):
                    continue
                new = _upsert_iface_param(text, "ContentPaddingAddition", value)
                if new != text:
                    _write_private(path, new)
                    changed += 1
            with open(ENV_FILE) as f:
                env_text = f.read()
            _write_private(ENV_FILE, _env_with_updates(env_text,
                                                       {"CONTENT_PADDING_ADDITION": value}))
    except Exception as e:
        return False, [f"Не удалось записать ContentPaddingAddition = {value}: {e}"]
    report = [f"ContentPaddingAddition = {value}: server.env, awg0.conf и файлы "
              f"устройств ({changed} шт.)"]
    try:
        r = subprocess.run(["awg", "set", AWG_IFACE, "content-padding-addition", value],
                           capture_output=True, text=True, timeout=10)
        err = (r.stderr or "").strip() if r.returncode else ""
    except Exception as e:
        err = str(e)
    if not err and _iface_param("content-padding-addition") == value:
        report.append("Интерфейс: применено без перезапуска")
    else:
        report.append("Интерфейс: применится при следующем перезапуске AWG "
                      f"({err or 'awg set не подтвердил'})")
    report.append("Слейвы получат ограничение по «Синхронизировать» — до этого "
                  "подключения через них работают как раньше.")
    report.append("Ускорение на устройстве — после перекачки его конфига; уже "
                  "выданные конфиги работают и без ограничения.")
    return True, report
