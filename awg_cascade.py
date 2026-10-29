#!/usr/bin/env python3
# awg_cascade.py — каскад «российский вход → зарубежные выходы» в боте и веб-панели:
# роли серверов, пункты выдачи, связка серверов (cascade.sh по SSH).
# Устройство каскада — docs/CASCADE.md.
#
# Поля сервера в servers.json:
#   role: "entry", entry_no: E   вход — российский слейв, к нему подключаются устройства
#   issue: {direct, via, auto}   выдача: напрямую (как раньше) / через вход отдельной
#                                страной / участвует в «Авто». Без поля — только напрямую
#   slot: K                      номер выхода: через вход устройство с адресом
#                                A.B.(C+K).N идёт в него. Остаётся за сервером и после
#                                выключения — включил снова, и конфиги его слота живы
#   cascade_retired: {K: подпись}   у входов: слоты удалённых серверов. Их адреса вход
#                                держит (конфиги уходят в «Авто»), новым серверам номер
#                                не выдаётся — только явно, «занять слот»
import hashlib, logging, os, re, subprocess

from awg_core import SERVER_IP, load_servers, save_servers
from awg_clients import slot_ip  # noqa: F401  (реэкспорт для обработчиков)
from awg_ssh import PARAMIKO_AVAILABLE, _ssh_connect

logger = logging.getLogger(__name__)

CASCADE_SH = "/root/cascade.sh"
MAX_SLOT   = 250
_TOKEN_RE  = re.compile(r"AWGX1:[A-Za-z0-9+/=]+")
_ANSI_RE   = re.compile(r"\x1b\[[0-9;]*m")

# Подмена исполнителя команд — для проверки в песочнице: fn(srv, args, stdin, timeout) -> (rc, out)
RUNNER = None


# ── Роли и выдача ─────────────────────────────────────────────────────────────

def is_entry(srv: dict) -> bool:
    return srv.get("role") == "entry"


def issue_of(srv: dict) -> dict:
    """Настройки выдачи с умолчаниями: обычный сервер — только напрямую, как до
    каскада; вход — никак: он дверь, а не страна."""
    i = srv.get("issue") or {}
    entry = is_entry(srv)
    return {"direct": bool(i.get("direct", not entry)),
            "via":    bool(i.get("via", False)) and not entry,
            "auto":   bool(i.get("auto", False)) and not entry}


def is_exit(srv: dict) -> bool:
    i = issue_of(srv)
    return bool(srv.get("slot")) and (i["via"] or i["auto"])


def entries(servers: list) -> list:
    return sorted((s for s in servers if is_entry(s)), key=lambda s: s.get("entry_no") or 99)


def exits(servers: list) -> list:
    return sorted((s for s in servers if is_exit(s)), key=lambda s: int(s["slot"]))


def cascade_active(servers: list) -> bool:
    """Есть вход с адресами и хотя бы один выход — выдача через вход включена."""
    return any(e.get("endpoints") for e in entries(servers)) and bool(exits(servers))


def srv_label(srv: dict) -> str:
    """«🇳🇱 NLD Голландия» — как на кнопках выбора сервера."""
    return " ".join(p for p in (srv.get("emoji", "🖥"), srv.get("name", ""), srv.get("country", "")) if p)


def status_label(srv: dict) -> str:
    """Статус для админа: вход задаётся явно, остальное следует из выдачи."""
    if is_entry(srv):
        return "🔀 Вход"
    i = issue_of(srv)
    if is_exit(srv):
        s = f"🚪 Выход {srv['slot']}"
        if i["direct"]:
            s += " + 🔗 прямой"
        if not i["via"]:
            s += " (только «Авто»)"
        return s
    return "🔗 Прямой" if i["direct"] else "⏸ Не выдаётся"


def auto_endpoint(srv: dict) -> str | None:
    """Адрес сервера по умолчанию: первый домен, без доменов — первый адрес."""
    eps = srv.get("endpoints", [])
    ep = next((e for e in eps if e.get("type") == "domain"), eps[0] if eps else None)
    return ep["value"] if ep else None


def issue_options(servers: list = None) -> list:
    """Пункты экрана выбора сервера — общие для бота и веб-панели.
    Каскад включён: «🌍 Авто» и выходы отдельными странами (адрес входа, слот K),
    затем прямые серверы с пометкой «· напрямую». Без каскада — прежний список
    серверов. Пункт: key (k0 — «Авто», kK — слот K, dI — прямой сервер с индексом
    I), kind, slot, srv (чей адрес и ключ в конфиге), label (кнопка), tag (имя
    сервера в имени файла), emoji (в названии в AmneziaVPN)."""
    servers = load_servers() if servers is None else servers
    opts = []
    active = cascade_active(servers)
    if active:
        door = next(e for e in entries(servers) if e.get("endpoints"))
        exs = exits(servers)
        if any(issue_of(x)["auto"] for x in exs):
            opts.append({"key": "k0", "kind": "auto", "slot": 0, "srv": door,
                         "label": "🌍 Авто", "tag": "Auto", "emoji": "🌍",
                         "name": "Авто", "country": ""})
        for x in exs:
            if issue_of(x)["via"]:
                k = int(x["slot"])
                opts.append({"key": f"k{k}", "kind": "via", "slot": k, "srv": door, "exit": x,
                             "label": srv_label(x), "tag": x.get("name", "") or f"Slot{k}",
                             "emoji": x.get("emoji", ""), "name": x.get("name", ""),
                             "country": x.get("country", "")})
    for si, s in enumerate(servers):
        if is_entry(s) or not s.get("endpoints") or not issue_of(s)["direct"]:
            continue
        opts.append({"key": f"d{si}", "kind": "direct", "slot": 0, "srv": s, "si": si,
                     "label": srv_label(s) + (" · напрямую" if active else ""),
                     "tag": s.get("name", "") + ("-direct" if active else ""),
                     "emoji": s.get("emoji", ""), "name": s.get("name", ""),
                     "country": s.get("country", "") + (" · напрямую" if active else "")})
    return opts


def find_option(key: str, servers: list = None) -> dict | None:
    return next((o for o in issue_options(servers) if o["key"] == key), None)


def describe(srv: dict, slot: int = 0, servers: list = None) -> tuple:
    """(tag, emoji, подпись) для конфига на сервер srv со слотом slot — когда
    сервер выбран не пунктом списка, а адресом в «Расширенной настройке»."""
    servers = load_servers() if servers is None else servers
    if slot:
        x = next((s for s in servers if s.get("slot") == slot), None)
        if x:
            return x.get("name", "") or f"Slot{slot}", x.get("emoji", ""), srv_label(x)
        return f"Slot{slot}", "🚪", f"Выход {slot}"
    if is_entry(srv):
        return "Auto", "🌍", "🌍 Авто"
    if cascade_active(servers):
        return srv.get("name", "") + "-direct", srv.get("emoji", ""), srv_label(srv) + " · напрямую"
    return srv.get("name", ""), srv.get("emoji", ""), srv_label(srv)


# ── Слоты ─────────────────────────────────────────────────────────────────────

def retired_slots(servers: list) -> dict:
    """{K: подпись} — слоты удалённых серверов (cascade_retired у входов)."""
    out = {}
    for e in entries(servers):
        for k, label in (e.get("cascade_retired") or {}).items():
            if str(k).isdigit():
                out[int(k)] = label
    taken = {int(s["slot"]) for s in servers if s.get("slot")}
    return {k: v for k, v in sorted(out.items()) if k not in taken}


def _retire(servers: list, slot: int, label: str):
    for e in entries(servers):
        e.setdefault("cascade_retired", {})[str(slot)] = label


def _unretire(servers: list, slot: int):
    for e in entries(servers):
        (e.get("cascade_retired") or {}).pop(str(slot), None)


def _new_slot(servers: list) -> int:
    used = {int(s["slot"]) for s in servers if s.get("slot")}
    used |= set(retired_slots(servers))
    k = max(used | {0}) + 1
    if k > MAX_SLOT:
        raise RuntimeError("свободных номеров выходов не осталось")
    return k


# ── Выполнение cascade.sh на серверах ─────────────────────────────────────────

def _host_of(srv: dict) -> str:
    return SERVER_IP if srv.get("is_primary") else srv.get("ssh", {}).get("ip", "")


def _tail(out: str, n: int = 2) -> str:
    lines = [ln.strip() for ln in _ANSI_RE.sub("", out).splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) or "нет вывода"


def _run(srv: dict, args: str, stdin: str = "", timeout: int = 600) -> tuple:
    """`bash /root/cascade.sh ARGS` на сервере: основной — локально, слейв — по
    SSH. Возвращает (код, вывод вместе с ошибками)."""
    if RUNNER:
        return RUNNER(srv, args, stdin, timeout)
    cmd = f"bash {CASCADE_SH} {args}"
    if srv.get("is_primary"):
        r = subprocess.run(["bash", "-c", cmd], input=stdin, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    if not PARAMIKO_AVAILABLE:
        raise RuntimeError("paramiko не установлен")
    client = _ssh_connect(srv.get("ssh", {}), timeout=10)
    try:
        chan = client.get_transport().open_session()
        chan.settimeout(timeout)
        chan.set_combine_stderr(True)
        chan.exec_command(cmd)
        if stdin:
            chan.sendall(stdin.encode())
        chan.shutdown_write()
        out = b""
        while True:
            data = chan.recv(65536)
            if not data:
                break
            out += data
        return chan.recv_exit_status(), out.decode(errors="replace")
    finally:
        client.close()


def ensure_script(srv: dict):
    """Та же версия cascade.sh, что на основном: бот заливает её сам, обновлять
    каждый сервер вручную не нужно. Основной — источник, ему заливать нечего."""
    if RUNNER or srv.get("is_primary"):
        return
    try:
        with open(CASCADE_SH, "rb") as f:
            data = f.read()
    except OSError:
        raise RuntimeError("на основном нет /root/cascade.sh — обновите проект: "
                           "bash /root/setup.sh --update")
    want = hashlib.md5(data).hexdigest()
    client = _ssh_connect(srv.get("ssh", {}), timeout=10)
    try:
        _, so, _ = client.exec_command(f"md5sum {CASCADE_SH} 2>/dev/null | cut -c1-32", timeout=10)
        if so.read().decode().strip() == want:
            return
        chan = client.get_transport().open_session()
        chan.exec_command(f"cat > {CASCADE_SH}.new && chmod 755 {CASCADE_SH}.new "
                          f"&& mv {CASCADE_SH}.new {CASCADE_SH}")
        chan.sendall(data)
        chan.shutdown_write()
        if chan.recv_exit_status() != 0:
            raise RuntimeError("не удалось записать /root/cascade.sh")
    finally:
        client.close()


def info(srv: dict) -> dict:
    """`cascade.sh info` — состояние каскада на сервере строками KEY=VALUE."""
    ensure_script(srv)
    rc, out = _run(srv, "info", timeout=30)
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def health(srv: dict) -> dict:
    """Состояние входа: {daemon, ru_count, ru_age, exits: {K: {state, since, rtt, hs, auto}}}."""
    d = info(srv)
    exs = {}
    for k, v in d.items():
        if not k.startswith("HEALTH_") or not k[7:].isdigit():
            continue
        p = v.split() + ["", "", "", "", ""]
        exs[int(k[7:])] = {"state": p[0], "since": p[1], "rtt": p[2], "hs": p[3], "auto": p[4]}
    num = lambda x, dflt: int(x) if str(x).lstrip("-").isdigit() else dflt
    return {"daemon": d.get("DAEMON") == "1", "ru_count": num(d.get("RU_COUNT"), 0),
            "ru_age": num(d.get("RU_AGE"), -1), "exits": exs,
            "slots": [int(x) for x in d.get("ENTRY_SLOTS", "").split() if x.isdigit()]}


# ── Связка серверов ───────────────────────────────────────────────────────────

def _link(entry: dict, ex: dict):
    """exit-up на выходе → entry-add на входе → exit-peer на выходе. Повторять
    безопасно: ключи сохраняются, блоки заменяются. Токены — через stdin."""
    ensure_script(ex)
    ensure_script(entry)
    k = int(ex["slot"])
    rc, out = _run(ex, f"exit-up --slot {k} --host {_host_of(ex)}")
    tok = _TOKEN_RE.findall(out)
    if rc or not tok:
        raise RuntimeError(f"{srv_label(ex)}: {_tail(out)}")
    auto = k if issue_of(ex)["auto"] else 0
    rc, out = _run(entry, f"entry-add - --entry {int(entry['entry_no'])} --auto {auto} "
                          f"--ip {_host_of(entry)}", stdin=tok[-1] + "\n")
    tok = _TOKEN_RE.findall(out)
    if rc or not tok:
        raise RuntimeError(f"{srv_label(entry)}: {_tail(out)}")
    rc, out = _run(ex, "exit-peer -", stdin=tok[-1] + "\n")
    if rc:
        raise RuntimeError(f"{srv_label(ex)}: {_tail(out)}")


def _unlink(entry: dict, ex: dict, forget: bool = False) -> list:
    """Снимает выход со входа (конфиги его слота уходят в «Авто», с forget — перестают
    работать) и вход с выхода. Ошибки — строками отчёта, а не исключением."""
    notes = []
    k = int(ex["slot"])
    try:
        rc, out = _run(entry, f"entry-del {k}" + (" --forget" if forget else ""))
        if rc and "нет" not in out:
            notes.append(f"⚠️ {srv_label(entry)}: {_tail(out)}")
    except Exception as e:
        notes.append(f"⚠️ {srv_label(entry)}: {e}")
    try:
        rc, out = _run(ex, f"exit-unpeer {int(entry['entry_no'])}")
        if rc and "не поднят" not in out:
            notes.append(f"⚠️ {srv_label(ex)}: {_tail(out)}")
    except Exception as e:
        notes.append(f"⚠️ {srv_label(ex)}: {e}")
    return notes


def _down(srv: dict) -> list:
    try:
        rc, out = _run(srv, "down")
        return [] if rc == 0 else [f"⚠️ {srv_label(srv)}: {_tail(out)}"]
    except Exception as e:
        return [f"⚠️ {srv_label(srv)}: {e}"]


def _find(servers: list, srv_id: str):
    return next((s for s in servers if s.get("id") == srv_id), None)


def _link_all(servers: list, ex: dict) -> list:
    out = []
    for e in entries(servers):
        try:
            _link(e, ex)
            out.append(f"✅ {srv_label(ex)} ↔ {srv_label(e)}")
        except Exception as err:
            out.append(f"❌ {err}")
    return out


def set_issue(srv_id: str, field: str, value: bool, take_slot: int = None) -> list:
    """Меняет выдачу сервера (direct / via / auto) и приводит каскад в соответствие:
    стал выходом — связать со всеми входами (номер: свой прежний, «занятый» take_slot,
    поднятый на нём вручную или новый); перестал — снять со входов (конфиги его
    слота уходят в «Авто») и погасить; поменялось «Авто» — сказать входам.
    Синхронная, с SSH — из run_in_executor. Возвращает строки отчёта."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or is_entry(srv) or field not in ("direct", "via", "auto"):
        return ["❌ Сервер не найден."]
    was = is_exit(srv)
    iss = issue_of(srv)
    iss[field] = bool(value)
    srv["issue"] = iss
    lines = []
    if not was and (iss["via"] or iss["auto"]):
        if take_slot:
            srv["slot"] = int(take_slot)
            _unretire(servers, int(take_slot))
        elif not srv.get("slot") or any(s is not srv and s.get("slot") == srv.get("slot") for s in servers):
            srv["slot"] = None
            try:
                k = info(srv).get("EXIT_SLOT", "")
            except Exception:
                k = ""
            if k.isdigit() and not any(s.get("slot") == int(k) for s in servers):
                srv["slot"] = int(k)   # выход уже поднимали вручную — берём его номер
                _unretire(servers, int(k))
            else:
                srv["slot"] = _new_slot(servers)
    save_servers(servers)
    now = is_exit(srv)
    if not was and now:
        lines += _link_all(servers, srv)
        if not entries(servers):
            lines.append("Входа пока нет — сделайте входом российский сервер.")
    elif was and not now:
        for e in entries(servers):
            lines += _unlink(e, srv)
        lines += _down(srv)
        lines.append(f"Выход {srv['slot']} снят. Конфиги его слота работают через «Авто».")
    elif now and field == "auto":
        for e in entries(servers):
            try:
                rc, out = _run(e, f"auto {int(srv['slot'])} {int(srv['slot']) if value else 0}")
                if rc:
                    lines.append(f"⚠️ {srv_label(e)}: {_tail(out)}")
            except Exception as err:
                lines.append(f"⚠️ {srv_label(e)}: {err}")
    return lines


def make_entry(srv_id: str) -> list:
    """Слейв становится входом: роль, номер входа (поднятый вручную или свободный),
    выдача напрямую выключается (вход — не страна), связь со всеми выходами."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or srv.get("is_primary"):
        return ["❌ Входом может быть только слейв."]
    lines = []
    if is_exit(srv):
        for e in entries(servers):
            lines += _unlink(e, srv)
        lines += _down(srv)
    used = {e.get("entry_no") for e in entries(servers)}
    try:
        n = info(srv).get("ENTRY_NO", "")
    except Exception as err:
        return [f"❌ {srv_label(srv)}: {err}"]
    no = int(n) if n.isdigit() and int(n) not in used else next(i for i in range(1, 201) if i not in used)
    retired = {}
    for e in entries(servers):
        retired.update(e.get("cascade_retired") or {})
    srv.update(role="entry", entry_no=no, issue={"direct": False, "via": False, "auto": False},
               cascade_retired=retired)
    srv.pop("slot", None)
    save_servers(servers)
    exs = exits(servers)
    for x in exs:
        try:
            _link(srv, x)
            lines.append(f"✅ {srv_label(x)} ↔ {srv_label(srv)}")
        except Exception as err:
            lines.append(f"❌ {err}")
    if not exs:
        lines.append("Выходов пока нет: включите «Через вход» или «В Авто» у зарубежных серверов.")
    return lines


def unmake_entry(srv_id: str) -> list:
    """Вход становится обычным слейвом: выходы снимают его, сам каскад гасится.
    Выдача напрямую остаётся выключенной — включить можно в карточке."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or not is_entry(srv):
        return ["❌ Это не вход."]
    lines = []
    for x in exits(servers):
        try:
            rc, out = _run(x, f"exit-unpeer {int(srv['entry_no'])}")
            if rc:
                lines.append(f"⚠️ {srv_label(x)}: {_tail(out)}")
        except Exception as err:
            lines.append(f"⚠️ {srv_label(x)}: {err}")
    lines += _down(srv)
    for k in ("role", "entry_no", "cascade_retired"):
        srv.pop(k, None)
    srv["issue"] = {"direct": False}
    save_servers(servers)
    lines.append(f"{srv_label(srv)} больше не вход.")
    return lines


def relink(srv_id: str) -> list:
    """Пересвязать вход со всеми выходами: после переустановки, восстановления или
    ручных правок. Выходы, которых больше нет, вход снимает (их слоты — в «Авто»)."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or not is_entry(srv):
        return ["❌ Это не вход."]
    lines = []
    exs = exits(servers)
    for x in exs:
        try:
            _link(srv, x)
            lines.append(f"✅ {srv_label(x)}")
        except Exception as err:
            lines.append(f"❌ {err}")
    try:
        have = info(srv).get("ENTRY_EXITS", "").split()
    except Exception:
        have = []
    keep = {str(x["slot"]) for x in exs}
    for k in have:
        if k not in keep:
            rc, out = _run(srv, f"entry-del {k}")
            lines.append(f"🧹 Выход {k} снят со входа" if rc == 0 else f"⚠️ выход {k}: {_tail(out)}")
    if not exs:
        lines.append("Выходов нет: включите «Через вход» или «В Авто» у зарубежных серверов.")
    return lines


def forget(servers: list, srv: dict):
    """«Только убрать из бота»: на серверах ничего не трогаем, но номер его выхода
    запоминается у входов — новому серверу он не выдаётся (без SSH)."""
    if srv.get("slot") and not is_entry(srv):
        _retire(servers, int(srv["slot"]), srv_label(srv))


def on_delete(servers: list, srv: dict) -> list:
    """Перед удалением сервера из бота (srv_del_ok): вход — гасится и снимается с
    выходов; выход — снимается со входов, его слот запоминается у входов (конфиги
    этого слота уходят в «Авто», номер можно занять другим сервером). Меняет
    servers на месте — сохранить их должен вызывающий."""
    lines = []
    if is_entry(srv):
        for x in exits(servers):
            try:
                _run(x, f"exit-unpeer {int(srv['entry_no'])}")
            except Exception as err:
                lines.append(f"⚠️ {srv_label(x)}: {err}")
        lines += _down(srv)
    elif srv.get("slot"):
        if is_exit(srv):
            for e in entries(servers):
                lines += _unlink(e, srv)
            lines += _down(srv)
        _retire(servers, int(srv["slot"]), srv_label(srv))
        lines.append(f"Слот {srv['slot']} свободен: конфиги этого выхода работают через «Авто», "
                     f"номер можно отдать другому серверу.")
    return lines
