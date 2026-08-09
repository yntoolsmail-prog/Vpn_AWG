#!/usr/bin/env python3
# awg_clients.py — управление клиентами AWG: ключи, конфиги, CRUD
import os, subprocess, logging, json, zlib, base64, struct, threading

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
