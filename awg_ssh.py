#!/usr/bin/env python3
# awg_ssh.py — SSH-управление slave-серверами: AWG, MTProxy, SOCKS5
import os, logging

logger = logging.getLogger(__name__)

ADMIN_KEY_PATH = "/root/.ssh/awg_admin_key"

try:
    import paramiko as _paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    _paramiko = None
    PARAMIKO_AVAILABLE = False

from awg_core import (
    AWG_CONF, AWG_IFACE, SERVER_PUBLIC, SERVER_PORT, SERVER_ENDPOINT, SERVER_IP,
    load_servers,
)


def _ssh_connect(ssh: dict, timeout: int = 10):
    """Подключение к SSH: сначала пробует ключ awg_admin_key, при неудаче — пароль."""
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    key_only = ssh.get("auth") == "key"
    if os.path.exists(ADMIN_KEY_PATH):
        try:
            client.connect(
                ssh.get("ip", ""), port=ssh.get("port", 22),
                username=ssh.get("login", "root"),
                key_filename=ADMIN_KEY_PATH,
                timeout=timeout, banner_timeout=timeout + 5,
                look_for_keys=False, allow_agent=False,
            )
            return client
        except Exception:
            if key_only:
                raise
    client.connect(
        ssh.get("ip", ""), port=ssh.get("port", 22),
        username=ssh.get("login", "root"),
        password=ssh.get("password", ""),
        timeout=timeout, banner_timeout=timeout + 5,
        look_for_keys=False, allow_agent=False,
    )
    return client


def get_admin_pubkey() -> str:
    """Возвращает содержимое awg_admin_key.pub или пустую строку."""
    try:
        with open(ADMIN_KEY_PATH + ".pub") as f:
            return f.read().strip()
    except Exception:
        return ""


def get_ssh_password_auth_local() -> bool:
    """True если на локальном sshd включён вход по паролю.
    Использует sshd -T для получения итоговой конфигурации с учётом всех
    Include и sshd_config.d/*.conf (Ubuntu/Debian cloud-init может переопределять)."""
    import subprocess as _sp
    try:
        out = _sp.run(["sshd", "-T"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.lower().startswith("passwordauthentication "):
                return line.split()[1].lower() == "yes"
    except Exception:
        pass
    # Fallback: читаем файлы в порядке приоритета (drop-ins первыми, как делает sshd)
    import glob as _glob
    paths = sorted(_glob.glob("/etc/ssh/sshd_config.d/*.conf")) + ["/etc/ssh/sshd_config"]
    for path in paths:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("PasswordAuthentication "):
                        return line.split()[1].lower() == "yes"
        except Exception:
            pass
    return True


def ssh_push_admin_key(server: dict) -> bool:
    """Копирует awg_admin_key.pub в authorized_keys на slave-сервере."""
    if not PARAMIKO_AVAILABLE:
        return False
    pubkey = get_admin_pubkey()
    if not pubkey:
        return False
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh)
        try:
            cmd = (
                "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                f"{{ grep -qF '{pubkey}' ~/.ssh/authorized_keys 2>/dev/null || "
                f"echo '{pubkey}' >> ~/.ssh/authorized_keys; }} && "
                "chmod 600 ~/.ssh/authorized_keys"
            )
            _, stdout, stderr = client.exec_command(cmd, timeout=10)
            stdout.read(); stderr.read()
            if stdout.channel.recv_exit_status() != 0:
                return False
        finally:
            client.close()
        return True
    except Exception:
        return False


def ssh_toggle_password_auth_all(enable: bool) -> dict:
    """Включает/выключает PasswordAuthentication на primary и всех slave.
    Возвращает {"primary": bool, "slaves": {name: bool}}."""
    import subprocess as _sp
    val = "yes" if enable else "no"
    results: dict = {"primary": False, "slaves": {}}

    try:
        _sp.run(
            ["bash", "-c",
             f"grep -q '^PasswordAuthentication' /etc/ssh/sshd_config "
             f"&& sed -i 's/^PasswordAuthentication.*/PasswordAuthentication {val}/' /etc/ssh/sshd_config "
             f"|| echo 'PasswordAuthentication {val}' >> /etc/ssh/sshd_config; "
             f"for f in /etc/ssh/sshd_config.d/*.conf; do "
             f"  [ -f \"$f\" ] && grep -q '^PasswordAuthentication' \"$f\" "
             f"  && sed -i 's/^PasswordAuthentication.*/PasswordAuthentication {val}/' \"$f\"; "
             f"done"],
            check=True, capture_output=True,
        )
        _sp.run(
            ["bash", "-c", "systemctl restart sshd 2>/dev/null || systemctl restart ssh"],
            check=True, capture_output=True,
        )
        results["primary"] = True
    except Exception:
        pass

    if not PARAMIKO_AVAILABLE:
        return results
    for srv in load_servers():
        if srv.get("is_primary"):
            continue
        name = srv.get("name", srv.get("ssh", {}).get("ip", "?"))
        try:
            client = _ssh_connect(srv.get("ssh", {}))
            try:
                cmd = (
                    f"grep -q '^PasswordAuthentication' /etc/ssh/sshd_config "
                    f"&& sed -i 's/^PasswordAuthentication.*/PasswordAuthentication {val}/' /etc/ssh/sshd_config "
                    f"|| echo 'PasswordAuthentication {val}' >> /etc/ssh/sshd_config; "
                    f"for f in /etc/ssh/sshd_config.d/*.conf; do "
                    f"  [ -f \"$f\" ] && grep -q '^PasswordAuthentication' \"$f\" "
                    f"  && sed -i 's/^PasswordAuthentication.*/PasswordAuthentication {val}/' \"$f\"; "
                    f"done; "
                    f"systemctl restart sshd 2>/dev/null || systemctl restart ssh"
                )
                client.exec_command(cmd, timeout=15)
            finally:
                client.close()
            results["slaves"][name] = True
        except Exception:
            results["slaves"][name] = False
    return results


def ssh_regen_admin_key() -> bool:
    """Перегенерирует awg_admin_key: создаёт новую пару, обновляет authorized_keys
    локально и на всех slave-серверах (используя старый ключ пока он ещё работает)."""
    import subprocess as _sp
    temp = ADMIN_KEY_PATH + ".new"
    try:
        _sp.run(
            ["ssh-keygen", "-t", "ed25519", "-f", temp, "-N", "", "-C", "awg-admin", "-q"],
            check=True, capture_output=True,
        )
        with open(temp + ".pub") as f:
            new_pub = f.read().strip()

        # Обновляем slave-серверы, пока ещё работает старый ключ
        slave_errors: list[str] = []
        if PARAMIKO_AVAILABLE:
            for srv in load_servers():
                if srv.get("is_primary"):
                    continue
                name = srv.get("name", srv.get("ssh", {}).get("ip", "?"))
                try:
                    client = _ssh_connect(srv.get("ssh", {}))
                    try:
                        _, stdout, stderr = client.exec_command(
                            f"sed -i '/awg-admin/d' ~/.ssh/authorized_keys 2>/dev/null; "
                            f"echo '{new_pub}' >> ~/.ssh/authorized_keys",
                            timeout=10,
                        )
                        stdout.read(); stderr.read()
                    finally:
                        client.close()
                except Exception as _e:
                    slave_errors.append(f"{name}: {_e}")

        auth = "/root/.ssh/authorized_keys"
        if os.path.exists(auth):
            with open(auth) as f:
                lines = [l for l in f if "awg-admin" not in l]
            with open(auth, "w") as f:
                f.writelines(lines)
        with open(auth, "a") as f:
            f.write(new_pub + "\n")

        os.replace(temp, ADMIN_KEY_PATH)
        os.replace(temp + ".pub", ADMIN_KEY_PATH + ".pub")
        return True, slave_errors
    except Exception:
        for p in [temp, temp + ".pub"]:
            try:
                os.unlink(p)
            except Exception:
                pass
        return False, []


def ssh_get_slave_peer_count(server: dict):
    """SSH на slave, считает кол-во [Peer] в его awg0.conf. None при ошибке."""
    if not PARAMIKO_AVAILABLE:
        return None
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=8)
        try:
            _, stdout, _ = client.exec_command(
                "grep -F '[Peer]' /etc/amnezia/amneziawg/awg0.conf 2>/dev/null | wc -l",
                timeout=5
            )
            return int(stdout.read().decode().strip())
        finally:
            client.close()
    except Exception:
        return None


def ssh_get_slave_awg_dump(server: dict) -> dict:
    """SSH на slave: запускает awg show dump, возвращает dict {pub: {rx,tx,endpoint,allowed,handshake}}.
    Возвращает {} при ошибке или если paramiko недоступен."""
    if not PARAMIKO_AVAILABLE:
        return {}
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=8)
        try:
            _, stdout, _ = client.exec_command(
                f"awg show {AWG_IFACE} dump 2>/dev/null",
                timeout=10
            )
            out = stdout.read().decode()
        finally:
            client.close()
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


def ssh_read_slave_awg_bytes(server: dict) -> tuple[int, int]:
    """SSH на slave: читает счётчики rx/tx интерфейса awg0. Возвращает (rx, tx) или (0, 0) при ошибке."""
    if not PARAMIKO_AVAILABLE:
        return 0, 0
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=8)
        try:
            _, stdout, _ = client.exec_command(
                f"cat /sys/class/net/{AWG_IFACE}/statistics/rx_bytes "
                f"/sys/class/net/{AWG_IFACE}/statistics/tx_bytes 2>/dev/null",
                timeout=5
            )
            lines = stdout.read().decode().strip().split("\n")
            if len(lines) >= 2:
                return int(lines[0]), int(lines[1])
        finally:
            client.close()
    except Exception:
        pass
    return 0, 0


def ssh_get_slave_sys_stats(server: dict) -> dict:
    """SSH на slave: системные метрики одним подключением.
    Возвращает {awg_ok, uptime, ram_pct, disk_pct, rx_bytes, tx_bytes}
    или {} при ошибке."""
    if not PARAMIKO_AVAILABLE:
        return {}
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=8)
        try:
            cmd = (
                f"systemctl is-active awg-quick@{AWG_IFACE} 2>/dev/null; echo ---SEP---;"
                "uptime -p 2>/dev/null; echo ---SEP---;"
                "free -m 2>/dev/null | awk 'NR==2{print $2, $7}'; echo ---SEP---;"
                "df / 2>/dev/null | awk 'NR==2{print $5}'; echo ---SEP---;"
                f"cat /sys/class/net/{AWG_IFACE}/statistics/rx_bytes "
                f"/sys/class/net/{AWG_IFACE}/statistics/tx_bytes 2>/dev/null"
            )
            _, stdout, _ = client.exec_command(cmd, timeout=10)
            out = stdout.read().decode()
        finally:
            client.close()
    except Exception:
        return {}

    secs = out.split("---SEP---")
    if len(secs) < 5:
        return {}

    awg_ok    = secs[0].strip() == "active"
    uptime    = secs[1].strip()
    mem_parts = secs[2].strip().split()
    disk_str  = secs[3].strip()
    byte_lines = [l.strip() for l in secs[4].strip().split("\n") if l.strip()]

    ram_pct = 0
    if len(mem_parts) >= 2:
        try:
            total = int(mem_parts[0])
            avail = int(mem_parts[1])
            ram_pct = round((total - avail) / max(total, 1) * 100)
        except (ValueError, ZeroDivisionError):
            pass

    disk_pct = 0
    try:
        disk_pct = int(disk_str.replace("%", ""))
    except ValueError:
        pass

    rx_bytes = tx_bytes = 0
    if len(byte_lines) >= 2:
        try:
            rx_bytes = int(byte_lines[0])
            tx_bytes = int(byte_lines[1])
        except ValueError:
            pass

    return {
        "awg_ok":   awg_ok,
        "uptime":   uptime,
        "ram_pct":  ram_pct,
        "disk_pct": disk_pct,
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
    }


def ssh_read_slave_env(ip: str, port: int, login: str, password: str) -> None:
    """Подключается к slave по SSH, проверяет соединение и наличие AWG. Бросает исключение при ошибке."""
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен: pip3 install paramiko")
    client = _paramiko.SSHClient()
    client.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
    client.connect(ip, port=port, username=login, password=password, timeout=10, banner_timeout=15)
    try:
        _, stdout, _ = client.exec_command(
            "test -f /etc/amnezia/amneziawg/awg0.conf && echo OK || echo MISSING",
            timeout=10
        )
        result = stdout.read().decode().strip()
    finally:
        client.close()
    if result != "OK":
        raise ValueError("AmneziaWG не установлен — /etc/amnezia/amneziawg/awg0.conf не найден")


def ssh_clone_awg_to_slave(server: dict) -> None:
    """Клонирует AWG-конфиг с primary на slave: одинаковые ключи, обфускация, все клиенты.
    Slave становится точной копией primary — клиентские конфиги совместимы с обоими серверами."""
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен: pip3 install paramiko")
    try:
        with open(AWG_CONF) as f:
            primary_conf = f.read()
    except Exception as e:
        raise ValueError(f"Не удалось прочитать конфиг primary: {e}")

    ssh = server.get("ssh", {})
    client = _ssh_connect(ssh)
    try:
        # Читаем PostUp/PostDown со slave (у него свой сетевой интерфейс)
        _, stdout, _ = client.exec_command(
            "grep -E '^Post(Up|Down)' /etc/amnezia/amneziawg/awg0.conf 2>/dev/null",
            timeout=5
        )
        slave_post_lines = stdout.read().decode().strip().splitlines()

        # Заменяем PostUp/PostDown в конфиге primary на slave-версию
        new_conf_lines = []
        for line in primary_conf.splitlines():
            if line.strip().startswith("PostUp") or line.strip().startswith("PostDown"):
                continue
            new_conf_lines.append(line)
            if line.strip().startswith("ListenPort") and slave_post_lines:
                new_conf_lines.extend(slave_post_lines)

        new_conf = "\n".join(new_conf_lines) + "\n"

        transport = client.get_transport()
        chan = transport.open_session()
        chan.exec_command("cat > /etc/amnezia/amneziawg/awg0.conf")
        chan.sendall(new_conf.encode())
        chan.shutdown_write()
        chan.recv_exit_status()
        chan.close()

        _, stdout, stderr = client.exec_command(
            f"sed -i 's|^SERVER_PUBLIC=.*|SERVER_PUBLIC={SERVER_PUBLIC}|' "
            f"/etc/amnezia/amneziawg/server.env",
            timeout=5
        )
        stdout.read(); stderr.read()

        _, stdout, stderr = client.exec_command(
            "awg-quick down awg0 2>/dev/null; awg-quick up /etc/amnezia/amneziawg/awg0.conf",
            timeout=20
        )
        stdout.read(); stderr.read()
    finally:
        client.close()


def ssh_sync_peer_to_slave(server: dict, name: str, pub: str, psk: str, ip: str) -> None:
    """Регистрирует peer клиента на slave-сервере через SSH (идемпотентно).
    ip — без /32, функция сама добавляет суффикс."""
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен: pip3 install paramiko")
    ssh = server.get("ssh", {})
    client = _ssh_connect(ssh)
    try:
        conf_line = (
            f"\\n# Client: {name}\\n[Peer]\\n"
            f"PublicKey = {pub}\\nPresharedKey = {psk}\\nAllowedIPs = {ip}/32\\n"
        )
        _, stdout, stderr = client.exec_command(
            f"grep -qF '{pub}' /etc/amnezia/amneziawg/awg0.conf || "
            f"printf '{conf_line}' >> /etc/amnezia/amneziawg/awg0.conf",
            timeout=10
        )
        stdout.read()
        err = stderr.read().decode().strip()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"запись в awg0.conf не удалась: {err or 'ошибка записи'}")

        transport = client.get_transport()
        chan = transport.open_session()
        chan.exec_command(
            f"awg set awg0 peer {pub} preshared-key /dev/stdin allowed-ips {ip}/32"
        )
        chan.sendall(psk.encode())
        chan.shutdown_write()
        rc = chan.recv_exit_status()
        chan.close()
        if rc != 0:
            raise RuntimeError(
                f"awg set вернул код {rc} — проверьте, что AWG на слейве запущен"
            )

        # Контроль результата. Без него функция сообщала об успехе всегда, когда
        # прошло само SSH-подключение: коды возврата отбрасывались, и устройство
        # молча не доезжало до слейва.
        _, stdout, _ = client.exec_command(
            f"grep -cF '{pub}' /etc/amnezia/amneziawg/awg0.conf; "
            f"awg show awg0 2>/dev/null | grep -cF '{pub}'",
            timeout=10
        )
        nums = stdout.read().decode().split()
        in_conf  = nums[0] if len(nums) > 0 else "0"
        on_iface = nums[1] if len(nums) > 1 else "0"
        if in_conf == "0" or on_iface == "0":
            raise RuntimeError(
                f"проверка не прошла: в конфиге={in_conf}, на интерфейсе={on_iface}"
            )
    finally:
        client.close()


def ssh_remove_peer_from_slave(server: dict, name: str, pub: str) -> None:
    """Удаляет peer клиента со slave-сервера: с живого интерфейса и из awg0.conf.

    Зеркало ssh_sync_peer_to_slave. Без неё удаление устройства не отзывает доступ:
    slave — полная копия primary, и старый конфиг продолжает на нём работать.
    Бросает исключение, если после чистки ключ всё ещё остался в конфиге."""
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен: pip3 install paramiko")
    # Блок клиента всегда пишется этим же проектом в фиксированном виде:
    #   # Client: <name>  /  [Peer]  /  PublicKey  /  PresharedKey  /  AllowedIPs
    # поэтому диапазон «от комментария до AllowedIPs» вырезает ровно его.
    name_re = name.replace("\\", "\\\\").replace(".", "\\.").replace("/", "\\/")
    ssh = server.get("ssh", {})
    client = _ssh_connect(ssh, timeout=8)
    try:
        if pub:
            _, stdout, stderr = client.exec_command(
                f"awg set awg0 peer '{pub}' remove", timeout=10
            )
            stdout.read(); stderr.read()

        _, stdout, stderr = client.exec_command(
            f"sed -i '/^# Client: {name_re}$/,/^AllowedIPs/d' "
            f"/etc/amnezia/amneziawg/awg0.conf",
            timeout=10
        )
        stdout.read(); stderr.read()

        if pub:
            _, stdout, _ = client.exec_command(
                f"grep -cF '{pub}' /etc/amnezia/amneziawg/awg0.conf || true",
                timeout=10
            )
            left = stdout.read().decode().strip()
            if left and left != "0":
                raise RuntimeError(
                    "ключ остался в awg0.conf — нужна кнопка «Синхронизировать»"
                )
    finally:
        client.close()


def ssh_sync_all_clients_to_slave(server: dict) -> None:
    """Синхронизирует всех существующих клиентов primary → slave через SSH."""
    import re as _re
    try:
        with open(AWG_CONF) as f:
            conf_text = f.read()
    except Exception:
        return
    for block in _re.split(r'\n(?=# Client:)', conf_text):
        name_m = _re.search(r'# Client: (.+)', block)
        pub_m  = _re.search(r'PublicKey = (.+)', block)
        psk_m  = _re.search(r'PresharedKey = (.+)', block)
        ip_m   = _re.search(r'AllowedIPs = (\S+?)(?:/32)?$', block, _re.MULTILINE)
        if not (name_m and pub_m and psk_m and ip_m):
            continue
        try:
            ssh_sync_peer_to_slave(
                server,
                name_m.group(1).strip(),
                pub_m.group(1).strip(),
                psk_m.group(1).strip(),
                ip_m.group(1).strip(),
            )
        except Exception:
            pass


def ssh_stop_slave_awg(ssh: dict) -> None:
    """SSH к slave и останавливает awg-quick@awg0."""
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен: pip3 install paramiko")
    client = _ssh_connect(ssh, timeout=8)
    try:
        _, stdout, stderr = client.exec_command(
            "systemctl stop awg-quick@awg0", timeout=15
        )
        stdout.read(); stderr.read()
    finally:
        client.close()


def ssh_sync_mtproxy_secret(server: dict, secret: str, port: str) -> tuple[bool, str]:
    """Обновляет секрет MTProxy на slave и перезапускает сервис.
    Возвращает (success, message)."""
    if not PARAMIKO_AVAILABLE:
        return False, "paramiko не установлен"
    ssh = server.get("ssh", {})
    client = None   # иначе finally уронит NameError и затрёт настоящую ошибку
    try:
        client = _ssh_connect(ssh)
        _, stdout, _ = client.exec_command(
            "test -f /opt/mtproxy/teleproxy && echo OK || echo NO",
            timeout=5
        )
        if stdout.read().decode().strip() != "OK":
            return False, "MTProxy не установлен"

        if secret.startswith("ee"):
            clean = secret[2:34]
            extra = " -D www.google.com"
        elif secret.startswith("dd"):
            clean = secret[2:]
            extra = " -R"
        else:
            clean = secret
            extra = ""

        _mtp_bin   = "/opt/mtproxy/teleproxy"
        exec_start = (
            f"{_mtp_bin} -u nobody -p 8888 -H {port} -S {clean}"
            f"{extra} --direct"
        )

        _, out_old, _ = client.exec_command(
            "grep '^MTP_PORT=' /etc/proxy-bot/proxy_bot.env 2>/dev/null | cut -d= -f2 || true",
            timeout=5
        )
        old_port = out_old.read().decode().strip()

        cmds = [
            "mkdir -p /etc/proxy-bot",
            f"grep -q '^MTP_SECRET=' /etc/proxy-bot/proxy_bot.env 2>/dev/null "
            f"&& sed -i 's|^MTP_SECRET=.*|MTP_SECRET={secret}|' /etc/proxy-bot/proxy_bot.env "
            f"|| echo 'MTP_SECRET={secret}' >> /etc/proxy-bot/proxy_bot.env",
            f"grep -q '^MTP_PORT=' /etc/proxy-bot/proxy_bot.env 2>/dev/null "
            f"&& sed -i 's|^MTP_PORT=.*|MTP_PORT={port}|' /etc/proxy-bot/proxy_bot.env "
            f"|| echo 'MTP_PORT={port}' >> /etc/proxy-bot/proxy_bot.env",
            f"sed -i 's|^ExecStart=.*|ExecStart={exec_start}|'"
            f" /etc/systemd/system/mtproxy.service",
            "systemctl daemon-reload",
            "systemctl restart mtproxy 2>/dev/null || true",
        ]
        if old_port and old_port != port:
            cmds += [
                f"command -v ufw &>/dev/null && ufw allow {port}/tcp comment 'MTProxy' 2>/dev/null || true",
                f"command -v ufw &>/dev/null && ufw delete allow {old_port}/tcp 2>/dev/null || true",
            ]
        elif not old_port:
            cmds.append(
                f"command -v ufw &>/dev/null && ufw allow {port}/tcp comment 'MTProxy' 2>/dev/null || true"
            )

        for cmd in cmds:
            _, stdout, stderr = client.exec_command(cmd, timeout=15)
            stdout.read(); stderr.read()

        _, out_ip, _ = client.exec_command(
            "curl -4 -sf --max-time 5 https://api.ipify.org 2>/dev/null "
            "|| curl -4 -sf --max-time 5 https://ifconfig.me 2>/dev/null",
            timeout=10
        )
        slave_pub_ip = out_ip.read().decode().strip()
        if slave_pub_ip:
            fix_server_cmd = (
                f"grep -q '^MTP_SERVER=' /etc/proxy-bot/proxy_bot.env 2>/dev/null "
                f"&& sed -i 's|^MTP_SERVER=.*|MTP_SERVER={slave_pub_ip}|' /etc/proxy-bot/proxy_bot.env "
                f"|| echo 'MTP_SERVER={slave_pub_ip}' >> /etc/proxy-bot/proxy_bot.env"
            )
            _, _, _ = client.exec_command(fix_server_cmd, timeout=10)

        return True, "✅ MTProxy синхронизирован"
    except Exception as e:
        return False, f"❌ {e}"
    finally:
        if client:
            client.close()


def ssh_check_mtproxy_installed(server: dict) -> bool:
    """Проверяет через SSH наличие MTProxy на slave. False при ошибке."""
    if not PARAMIKO_AVAILABLE:
        return False
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=8)
        try:
            _, stdout, _ = client.exec_command(
                "test -f /opt/mtproxy/teleproxy && echo OK || echo NO",
                timeout=5
            )
            return stdout.read().decode().strip() == "OK"
        finally:
            client.close()
    except Exception:
        return False


def ssh_get_slave_mtp_stats(server: dict, port: str) -> dict:
    """Собирает статистику MTProxy со slave по SSH."""
    if not PARAMIKO_AVAILABLE:
        return {"conns": 0, "bytes_in": 0, "bytes_out": 0,
                "ext_ips": [], "awg_ips": [], "error": "paramiko недоступен"}
    ssh = server.get("ssh", {})
    try:
        client = _ssh_connect(ssh, timeout=10)
        try:
            cmd = (
                f"iptables -C INPUT -p tcp --dport {port} -m comment --comment mtp_count_in"
                f" 2>/dev/null || iptables -I INPUT 1 -p tcp --dport {port}"
                f" -m comment --comment mtp_count_in; "
                f"iptables -C OUTPUT -p tcp --sport {port} -m comment --comment mtp_count_out"
                f" 2>/dev/null || iptables -I OUTPUT 1 -p tcp --sport {port}"
                f" -m comment --comment mtp_count_out; "
                f"echo __IN__; "
                f"iptables -nvxL INPUT 2>/dev/null | awk '/mtp_count_in/{{print $2}}' || echo 0; "
                f"echo __OUT__; "
                f"iptables -nvxL OUTPUT 2>/dev/null | awk '/mtp_count_out/{{print $2}}' || echo 0; "
                f"echo __SS__; "
                f"ss -tn 2>/dev/null | grep ':{port} ' || true"
            )
            _, stdout, _ = client.exec_command(cmd, timeout=12)
            raw = stdout.read().decode()
        finally:
            client.close()

        bytes_in = bytes_out = 0
        ext_ips: list[str] = []
        awg_ips: list[str] = []
        section = "pre"
        for line in raw.splitlines():
            if line == "__IN__":
                section = "in"
            elif line == "__OUT__":
                section = "out"
            elif line == "__SS__":
                section = "ss"
            elif section == "in" and line.strip().isdigit():
                bytes_in = int(line.strip())
            elif section == "out" and line.strip().isdigit():
                bytes_out = int(line.strip())
            elif section == "ss":
                parts = line.split()
                if len(parts) >= 5:
                    ip = parts[4].rsplit(":", 1)[0].strip("[]")
                    if ip:
                        (awg_ips if ip.startswith("10.") else ext_ips).append(ip)

        return {
            "conns":     len(ext_ips) + len(awg_ips),
            "bytes_in":  bytes_in,
            "bytes_out": bytes_out,
            "ext_ips":   ext_ips,
            "awg_ips":   awg_ips,
            "error":     None,
        }
    except Exception as e:
        return {"conns": 0, "bytes_in": 0, "bytes_out": 0,
                "ext_ips": [], "awg_ips": [], "error": str(e)}


_SOCKS5_PRIVATE_NETS = [
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "224.0.0.0/4", "240.0.0.0/4",
    # Telegram datacenter ranges — mtproto-proxy must reach them directly
    "91.108.0.0/16", "149.154.0.0/16", "91.105.192.0/23", "149.154.160.0/20",
]


def ssh_apply_socks5_on_slave(
    server: dict,
    client_ip: str,
    socks5_host: str,
    socks5_port: int,
    socks5_user: str,
    socks5_pass: str,
) -> tuple[bool, str]:
    """Пишет redsocks2.conf и применяет iptables для клиента на slave-сервере.
    Возвращает (success, message)."""
    if not PARAMIKO_AVAILABLE:
        return False, "paramiko не установлен"
    ssh = server.get("ssh", {})
    client = None   # иначе finally уронит NameError и затрёт настоящую ошибку
    try:
        client = _ssh_connect(ssh)
        _, stdout, _ = client.exec_command(
            "test -f /usr/local/bin/redsocks2 && echo OK || echo NO", timeout=5
        )
        if stdout.read().decode().strip() != "OK":
            return False, "redsocks2 не установлен"

        auth_lines = ""
        if socks5_user:
            auth_lines = f'    login = "{socks5_user}";\n    password = "{socks5_pass}";\n'
        conf = (
            "base {\n"
            "    log_debug = off;\n"
            "    log_info = on;\n"
            "    log = \"syslog:daemon\";\n"
            "    daemon = off;\n"
            "    redirector = iptables;\n"
            "}\n\n"
            "redsocks {\n"
            f"    bind = \"0.0.0.0:12345\";\n"
            f"    relay = \"{socks5_host}:{socks5_port}\";\n"
            "    type = socks5;\n"
            "    autoproxy = 0;\n"
            "    timeout = 10;\n"
            f"{auth_lines}"
            "}\n"
        )
        chan = client.get_transport().open_session()
        chan.exec_command("mkdir -p /etc/redsocks2 && cat > /etc/redsocks2/redsocks2.conf")
        chan.sendall(conf.encode())
        chan.shutdown_write()
        chan.recv_exit_status()
        chan.close()

        chain = f"SOCKS5_{client_ip.replace('.', '_')}"

        cleanup = [
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p udp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5399 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p tcp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5399 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p udp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p tcp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -j {chain} 2>/dev/null || true",
            f"iptables -t nat -F {chain} 2>/dev/null || true",
            f"iptables -t nat -X {chain} 2>/dev/null || true",
            f"iptables -t filter -D FORWARD -s {client_ip}/32 -p udp ! --dport 53 -j REJECT 2>/dev/null || true",
        ]
        for cmd in cleanup:
            _, so, se = client.exec_command(cmd, timeout=5)
            so.read(); se.read()

        _, stdout, stderr = client.exec_command("systemctl restart redsocks2", timeout=15)
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            err = stderr.read().decode().strip()
            return False, f"❌ redsocks2 restart: {err}"

        _, stdout, _ = client.exec_command(
            f"getent hosts {socks5_host} 2>/dev/null | awk '{{print $1}}' | head -1 || echo {socks5_host}",
            timeout=5
        )
        socks5_ip = stdout.read().decode().strip() or socks5_host

        dns_cmds = [
            f"iptables -t nat -A PREROUTING -s {client_ip}/32 -p udp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300",
            f"iptables -t nat -A PREROUTING -s {client_ip}/32 -p tcp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300",
        ]
        for cmd in dns_cmds:
            _, so, se = client.exec_command(cmd, timeout=5)
            so.read(); se.read()

        apply_cmds = [
            f"iptables -t nat -N {chain}",
            f"iptables -t nat -A {chain} -d {socks5_ip} -j RETURN",
        ]
        for net in _SOCKS5_PRIVATE_NETS:
            apply_cmds.append(f"iptables -t nat -A {chain} -d {net} -j RETURN")
        for srv in load_servers():
            srv_ip = srv.get("ssh", {}).get("ip", "")
            if srv_ip:
                apply_cmds.append(f"iptables -t nat -A {chain} -d {srv_ip} -j RETURN")
        _, stdout_iface, _ = client.exec_command(
            "ip route get 8.8.8.8 | grep -o 'dev [^ ]*' | cut -d' ' -f2", timeout=5
        )
        slave_ext_iface = stdout_iface.read().decode().strip() or "eth0"

        apply_cmds.extend([
            f"iptables -t nat -A {chain} -p tcp -j REDIRECT --to-ports 12345",
            f"iptables -t nat -A PREROUTING -s {client_ip}/32 -j {chain}",
            f"iptables -t filter -A FORWARD -s {client_ip}/32 -p udp ! --dport 53 -j REJECT",
            f"iptables -t filter -A FORWARD -s {client_ip}/32 -p tcp --dport 853 -j REJECT",
            f"iptables -I INPUT -i {slave_ext_iface} -p tcp --dport 12345 -j DROP",
            "sysctl -w net.ipv4.conf.all.route_localnet=1",
            "sysctl -w net.ipv4.ip_forward=1",
            "mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4",
        ])
        for cmd in apply_cmds:
            _, so, se = client.exec_command(cmd, timeout=5)
            so.read(); se.read()

        return True, "✅ SOCKS5 применён"
    except Exception as e:
        return False, f"❌ {e}"
    finally:
        if client:
            client.close()


def ssh_remove_socks5_from_slave(server: dict, client_ip: str) -> tuple[bool, str]:
    """Снимает iptables-правила SOCKS5 для клиента с slave-сервера."""
    if not PARAMIKO_AVAILABLE:
        return False, "paramiko не установлен"
    ssh = server.get("ssh", {})
    client = None   # иначе finally уронит NameError и затрёт настоящую ошибку
    try:
        client = _ssh_connect(ssh)
        chain = f"SOCKS5_{client_ip.replace('.', '_')}"
        _, stdout_iface, _ = client.exec_command(
            "ip route get 8.8.8.8 | grep -o 'dev [^ ]*' | cut -d' ' -f2", timeout=5
        )
        slave_ext_iface = stdout_iface.read().decode().strip() or "eth0"
        cmds = [
            f"iptables -D INPUT -i {slave_ext_iface} -p tcp --dport 12345 -j DROP 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p udp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5399 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p tcp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5399 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p udp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -p tcp --dport 53"
            f" -j DNAT --to-destination 127.0.0.1:5300 2>/dev/null || true",
            f"iptables -t nat -D PREROUTING -s {client_ip}/32 -j {chain} 2>/dev/null || true",
            f"iptables -t nat -F {chain} 2>/dev/null || true",
            f"iptables -t nat -X {chain} 2>/dev/null || true",
            f"iptables -t filter -D FORWARD -s {client_ip}/32 -p udp ! --dport 53 -j REJECT 2>/dev/null || true",
            f"iptables -t filter -D FORWARD -s {client_ip}/32 -p tcp --dport 853 -j REJECT 2>/dev/null || true",
            "iptables-save > /etc/iptables/rules.v4 2>/dev/null || true",
        ]
        for cmd in cmds:
            _, so, se = client.exec_command(cmd, timeout=5)
            so.read(); se.read()
        return True, "✅ SOCKS5 снят"
    except Exception as e:
        return False, f"❌ {e}"
    finally:
        if client:
            client.close()
