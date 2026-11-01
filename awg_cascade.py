#!/usr/bin/env python3
# awg_cascade.py — каскад «российский вход → зарубежные выходы» в боте и веб-панели:
# роли серверов, пункты выдачи, связка серверов (cascade.sh по SSH).
# Устройство каскада — docs/CASCADE.md.
#
# Поля сервера в servers.json:
#   role: "entry", entry_no: E   вход — российский сервер (любой, и основной), к нему
#                                подключаются устройства
#   issue: {direct, via, auto}   выдача: напрямую (как раньше) / через вход отдельной
#                                страной / участвует в «Авто». Без поля — только напрямую
#   slot: K                      номер выхода: через вход устройство с адресом
#                                A.B.(C+K).N идёт в него. Остаётся за сервером и после
#                                выключения — включил снова, и конфиги его слота живы
#   issue.direct у входа         пункт «🇷🇺 RUS Россия»: устройство подключается к
#                                входу и выходит в интернет с его IP, Telegram — через
#                                выход. Адрес A.B.(255−C).N (direct_slot), его разбирает
#                                cascade.sh (DSLOT)
#   cascade_retired: {K: подпись}   у основного: слоты удалённых серверов. Их адреса
#                                вход держит (конфиги уходят в «Авто»), новым серверам
#                                номер не выдаётся — только явно, «занять слот». У
#                                основного, а не у входов: он есть всегда, и номера не
#                                теряются, когда убран последний вход
import hashlib, logging, os, re, shlex, subprocess, time

from awg_core import SERVER_IP, VPN_SUBNET, load_servers, save_servers
from awg_clients import slot_ip  # noqa: F401  (реэкспорт для обработчиков)
from awg_ssh import PARAMIKO_AVAILABLE, _ssh_connect

logger = logging.getLogger(__name__)

CASCADE_SH = "/root/cascade.sh"
# Флаг «пересвязать все входы после запуска бота»: ставят восстановление бэкапа и
# смена IP основного — обе перезапускают бота, и фоновая задача до конца бы не дожила
RELINK_FLAG = "/etc/amnezia/amneziawg/cascade_relink.flag"
MAX_SLOT   = 250
# Ключи пунктов «напрямую через вход»: k{ENTRY_DIRECT_BASE + индекс сервера} — не
# пересекаются с номерами выходов, у каждого входа свой
ENTRY_DIRECT_BASE = 1000
_TOKEN_RE  = re.compile(r"AWGX1:[A-Za-z0-9+/=]+")
_ANSI_RE   = re.compile(r"\x1b\[[0-9;]*m")

# Подмена исполнителя команд — для проверки в песочнице: fn(srv, args, stdin, timeout) -> (rc, out)
RUNNER = None


# ── Роли и выдача ─────────────────────────────────────────────────────────────

def is_entry(srv: dict) -> bool:
    return srv.get("role") == "entry"


def direct_slot() -> int:
    """«Слот» пункта «напрямую» у входа: адрес устройства A.B.(255−C).N. Вход
    выпускает такие устройства со своего IP, Telegram и force_exit.list — через
    выход (cascade.sh, DSLOT). Номера выходов всегда меньше."""
    try:
        c = int(str(VPN_SUBNET).split(".")[2])
    except (IndexError, ValueError):
        c = 0
    return 255 - c


def issue_of(srv: dict) -> dict:
    """Настройки выдачи с умолчаниями: обычный сервер — только напрямую, как до
    каскада; вход — по умолчанию никак (он дверь, а не страна), «напрямую» у него
    включает админ: пункт с выходом в интернет с IP входа."""
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
        return "🔀 Вход" + (" + 🔗 напрямую" if issue_of(srv)["direct"] else "")
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
        if not s.get("endpoints") or not issue_of(s)["direct"]:
            continue
        if is_entry(s):
            # Вход «напрямую»: адрес и ключ входа, выход в интернет с его IP.
            # Подпись и имя файла — как у остальных стран: путать не с чем,
            # через вход этой страны нет
            opts.append({"key": f"k{ENTRY_DIRECT_BASE + si}", "kind": "entry_direct",
                         "slot": direct_slot(), "srv": s, "si": si,
                         "label": srv_label(s), "tag": s.get("name", ""),
                         "emoji": s.get("emoji", ""), "name": s.get("name", ""),
                         "country": s.get("country", "")})
            continue
        opts.append({"key": f"d{si}", "kind": "direct", "slot": 0, "srv": s, "si": si,
                     "label": srv_label(s) + (" · напрямую" if active else ""),
                     "tag": s.get("name", "") + ("-direct" if active else ""),
                     "emoji": s.get("emoji", ""), "name": s.get("name", ""),
                     "country": s.get("country", "") + (" · напрямую" if active else "")})
    return opts


def find_option(key: str, servers: list = None) -> dict | None:
    return next((o for o in issue_options(servers) if o["key"] == key), None)


def option_key(srv: dict, slot: int, servers: list = None) -> str:
    """Ключ пункта по серверу и слоту — для кнопок, которые повторяют пункт
    («2 кода крупнее»): у пункта «напрямую» входа ключ свой, не номер слота."""
    if slot == direct_slot() and is_entry(srv):
        o = next((o for o in issue_options(servers) if o["kind"] == "entry_direct"
                  and o["srv"].get("id") == srv.get("id")), None)
        if o:
            return o["key"]
    return f"k{slot}"


def describe(srv: dict, slot: int = 0, servers: list = None) -> tuple:
    """(tag, emoji, подпись) для конфига на сервер srv со слотом slot — когда
    сервер выбран не пунктом списка, а адресом в «Расширенной настройке»."""
    servers = load_servers() if servers is None else servers
    if slot and slot == direct_slot() and is_entry(srv):
        return srv.get("name", ""), srv.get("emoji", ""), srv_label(srv)
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

def _primary(servers: list) -> dict | None:
    return next((s for s in servers if s.get("is_primary")), None)


def retired_slots(servers: list) -> dict:
    """{K: подпись} — слоты удалённых серверов: cascade_retired у основного (и у
    входов — так хранилось в первой версии)."""
    out = {}
    for s in [_primary(servers) or {}] + entries(servers):
        for k, label in (s.get("cascade_retired") or {}).items():
            if str(k).isdigit():
                out.setdefault(int(k), label)
    taken = {int(s["slot"]) for s in servers if s.get("slot")}
    return {k: v for k, v in sorted(out.items()) if k not in taken}


def free_slots(servers: list, srv: dict = None) -> dict:
    """{K: подпись} — что можно занять, становясь выходом: слоты удалённых серверов
    и слоты серверов, которые сейчас не выходы (номер у них остаётся, пока его не
    забрали). Свой номер srv сюда не входит — его он получит и так."""
    out = dict(retired_slots(servers))
    for s in servers:
        if s is not srv and s.get("slot") and not is_exit(s):
            out[int(s["slot"])] = srv_label(s) + " (выключен)"
    return dict(sorted(out.items()))


def _retire(servers: list, slot: int, label: str):
    p = _primary(servers)
    if p is not None:
        p.setdefault("cascade_retired", {})[str(slot)] = label


def _unretire(servers: list, slot: int):
    for s in [_primary(servers) or {}] + entries(servers):
        (s.get("cascade_retired") or {}).pop(str(slot), None)


def _take_slot(servers: list, srv: dict, slot: int):
    """srv занимает номер: из освободившихся или у сервера, который сейчас не выход."""
    for s in servers:
        if s is not srv and s.get("slot") == slot:
            if is_exit(s):
                raise RuntimeError(f"слот {slot} занят выходом {srv_label(s)}")
            s.pop("slot", None)
    _unretire(servers, slot)
    srv["slot"] = slot


def _new_slot(servers: list) -> int:
    used = {int(s["slot"]) for s in servers if s.get("slot")}
    used |= set(retired_slots(servers))
    k = max(used | {0}) + 1
    if k > MAX_SLOT or k >= direct_slot():
        raise RuntimeError("свободных номеров выходов не осталось")
    return k


# ── Выполнение cascade.sh на серверах ─────────────────────────────────────────

def _host_of(srv: dict) -> str:
    host = SERVER_IP if srv.get("is_primary") else srv.get("ssh", {}).get("ip", "")
    if not host:
        raise RuntimeError(f"{srv_label(srv)}: не известен IP сервера")
    return shlex.quote(host)


def _tail(out: str, n: int = 2) -> str:
    lines = [ln.strip() for ln in _ANSI_RE.sub("", out).splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) or "нет вывода"


def _run(srv: dict, args: str, stdin: str = "", timeout: int = 600) -> tuple:
    """`bash /root/cascade.sh ARGS` на сервере: основной — локально, слейв — по
    SSH. Возвращает (код, вывод вместе с ошибками)."""
    if RUNNER:
        return RUNNER(srv, args, stdin, timeout)
    rc, out = _exec(srv, args, stdin, timeout)
    if rc == 127 and not srv.get("is_primary"):
        # Скрипта нет — сервер переустановили после прошлой заливки: залить и повторить
        _SCRIPT_SEEN.pop(srv.get("id"), None)
        ensure_script(srv)
        rc, out = _exec(srv, args, stdin, timeout)
    return rc, out


def _exec(srv: dict, args: str, stdin: str, timeout: int) -> tuple:
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


# {id сервера: md5 залитой версии} — проверять версию по SSH раз за жизнь
# процесса, а не на каждый опрос (_check_cascade ходит на входы раз в минуту)
_SCRIPT_SEEN: dict = {}


def ensure_script(srv: dict):
    """Та же версия cascade.sh, что на основном: бот заливает её сам, обновлять
    каждый сервер вручную не нужно. Основной — источник, ему заливать нечего.
    После заливки — перезапуск службы входа: работающая служба держит старую
    версию в памяти. Перезапуск правила не снимает — трафик не прерывается."""
    if RUNNER or srv.get("is_primary"):
        return
    try:
        with open(CASCADE_SH, "rb") as f:
            data = f.read()
    except OSError:
        raise RuntimeError("на основном нет /root/cascade.sh — обновите проект: "
                           "bash /root/setup.sh --update")
    want = hashlib.md5(data).hexdigest()
    if _SCRIPT_SEEN.get(srv.get("id")) == want:
        return
    client = _ssh_connect(srv.get("ssh", {}), timeout=10)
    try:
        _, so, _ = client.exec_command(f"md5sum {CASCADE_SH} 2>/dev/null | cut -c1-32", timeout=10)
        if so.read().decode().strip() != want:
            chan = client.get_transport().open_session()
            chan.exec_command(f"cat > {CASCADE_SH}.new && chmod 755 {CASCADE_SH}.new "
                              f"&& mv {CASCADE_SH}.new {CASCADE_SH} "
                              f"&& (systemctl try-restart awg-cascade.service 2>/dev/null; true)")
            chan.sendall(data)
            chan.shutdown_write()
            if chan.recv_exit_status() != 0:
                raise RuntimeError("не удалось записать /root/cascade.sh")
        _SCRIPT_SEEN[srv.get("id")] = want
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


# {id входа: (время, {N: K})} — из последнего health(): через какой выход сейчас
# идёт устройство N этого входа (K=0 — напрямую с его IP). Обновляет _check_cascade
_ROUTES: dict = {}
ROUTES_TTL = 300   # данные старше — выход устройства считаем неизвестным


def health(srv: dict) -> dict:
    """Состояние входа: {daemon, ru_count, ru_age, exits: {K: {state, since, rtt, hs, auto}},
    slots, routes: {N: K}, tg_via, tg_count}."""
    d = info(srv)
    routes = {}
    for pair in d.get("ROUTES", "").split():
        n, _, k = pair.partition(":")
        if n.isdigit() and k.isdigit():
            routes[int(n)] = int(k)
    _ROUTES[srv.get("id")] = (time.time(), routes)
    exs = {}
    for k, v in d.items():
        if not k.startswith("HEALTH_") or not k[7:].isdigit():
            continue
        p = v.split() + ["", "", "", "", ""]
        exs[int(k[7:])] = {"state": p[0], "since": p[1], "rtt": p[2], "hs": p[3], "auto": p[4]}
    num = lambda x, dflt: int(x) if str(x).lstrip("-").isdigit() else dflt
    return {"daemon": d.get("DAEMON") == "1", "ru_count": num(d.get("RU_COUNT"), 0),
            "ru_age": num(d.get("RU_AGE"), -1), "exits": exs,
            "slots": [int(x) for x in d.get("ENTRY_SLOTS", "").split() if x.isdigit()],
            "routes": routes,
            # Через какой выход сам вход ходит в Telegram: K, 0 — напрямую (живых
            # выходов нет), None — старая версия cascade.sh или служба ещё не решила
            "tg_via": num(d.get("TG_VIA"), None), "tg_count": num(d.get("TG_COUNT"), 0)}


def _dump_label(srv: dict) -> str:
    """Метка сервера в поле server у combined dump (awg_stats): основной — пусто."""
    return "" if srv.get("is_primary") else f"{srv.get('emoji', '')} {srv.get('name', 'Slave')}".strip()


def placement(servers: list, dump: dict, ips: dict, online_s: int = 180) -> dict:
    """Кто где сейчас: {pub: (сервер подключения, сервер выхода)}. Онлайн — хендшейк
    за online_s; подключение — сервер с последним хендшейком (поле server у combined
    dump). Выход — откуда устройство выходит в интернет: сам сервер подключения,
    если это не вход; у входа — выход по его ROUTES (K=0 — сам вход), нет свежих
    данных — None. Без каскада выход всегда совпадает с подключением."""
    now = time.time()
    by_label = {_dump_label(s): s for s in servers}
    by_slot = {int(s["slot"]): s for s in exits(servers)}
    out = {}
    for pub, p in dump.items():
        hs = p.get("handshake") or 0
        conn = by_label.get(p.get("server", ""))
        if not hs or now - hs >= online_s or conn is None:
            continue
        via = conn
        if is_entry(conn):
            ts, routes = _ROUTES.get(conn.get("id"), (0, {}))
            last = ips.get(pub, "").rpartition(".")[2]
            k = routes.get(int(last)) if last.isdigit() and now - ts < ROUTES_TTL else None
            via = None if k is None else conn if k == 0 else by_slot.get(k)
        out[pub] = (conn, via)
    return out


def short_label(srv: dict) -> str:
    """«🇳🇱 NLD» — коротко, для строк статистики."""
    return f"{srv.get('emoji', '🖥')} {srv.get('name', '')}".strip()


def same_srv(a: dict, b: dict) -> bool:
    return bool(a and b) and (a.get("id") or a.get("name")) == (b.get("id") or b.get("name"))


def route_label(conn: dict, via: dict) -> str:
    """Путь устройства для карточки и списка: «🇷🇺 RUS → 🇳🇱 NLD», без каскада — сервер."""
    if via is None:
        return f"{short_label(conn)} → ?"
    return short_label(conn) if same_srv(conn, via) else f"{short_label(conn)} → {short_label(via)}"


def who_where() -> dict:
    """{pub: (имя устройства, сервер подключения, сервер выхода)} — для статистики бота."""
    from awg_clients import get_all_clients, get_client_keys, get_client_pub
    from awg_stats import get_combined_awg_dump
    names, ips = {}, {}
    for name in get_all_clients():
        pub = get_client_pub(name)
        if pub:
            names[pub] = name
            ips[pub] = (get_client_keys(name) or {}).get("ip", "")
    return {pub: (names.get(pub, pub[:8]), conn, via)
            for pub, (conn, via) in placement(load_servers(), get_combined_awg_dump(), ips).items()}


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


def _assign_slot(servers: list, srv: dict, take_slot: int = None):
    """Номер для нового выхода: «занятый» take_slot, свой прежний, поднятый на
    сервере вручную (cascade.sh info) или новый."""
    if take_slot:
        _take_slot(servers, srv, int(take_slot))
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


def _apply_issue(srv_id: str, changes: dict, take_slot: int = None) -> list:
    """Меняет выдачу сервера и приводит каскад в соответствие: стал выходом —
    номер и связка со всеми входами; перестал — снять со входов (конфиги его
    слота уходят в «Авто») и погасить; поменялось «Авто» — сказать входам.
    Синхронная, с SSH — из run_in_executor. Возвращает строки отчёта."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or is_entry(srv):
        return ["❌ Сервер не найден."]
    was = is_exit(srv)
    old_auto = issue_of(srv)["auto"]
    iss = issue_of(srv)
    iss.update({k: bool(v) for k, v in changes.items()})
    srv["issue"] = iss
    lines = []
    if not was and (iss["via"] or iss["auto"]):
        _assign_slot(servers, srv, take_slot)
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
    elif now and iss["auto"] != old_auto:
        k = int(srv["slot"])
        for e in entries(servers):
            try:
                rc, out = _run(e, f"auto {k} {k if iss['auto'] else 0}")
                if rc:
                    lines.append(f"⚠️ {srv_label(e)}: {_tail(out)}")
            except Exception as err:
                lines.append(f"⚠️ {srv_label(e)}: {err}")
    return lines


def set_entry_direct(srv_id: str, value: bool) -> list:
    """Пункт «напрямую» у входа: только выдача, на серверах ничего не меняется —
    адреса пункта вход держит всегда (cascade.sh). Выключил — уже выданные
    конфиги продолжают работать."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv or not is_entry(srv):
        return ["❌ Вход не найден."]
    srv["issue"] = {**(srv.get("issue") or {}), "direct": bool(value)}
    save_servers(servers)
    return []


def set_issue(srv_id: str, field: str, value: bool, take_slot: int = None) -> list:
    """Один переключатель выдачи: direct / via / auto."""
    if field not in ("direct", "via", "auto"):
        return ["❌ Сервер не найден."]
    return _apply_issue(srv_id, {field: value}, take_slot)


def make_exit(srv_id: str, take_slot: int = None) -> list:
    """«Сделать выходом»: в «🌍 Авто» и отдельным пунктом через вход (любое из
    двух потом убирается в карточке), связка со всеми входами."""
    return _apply_issue(srv_id, {"via": True, "auto": True}, take_slot)


def unmake_exit(srv_id: str) -> list:
    """«Перестать быть выходом»: входы снимают туннель, номер остаётся за сервером."""
    return _apply_issue(srv_id, {"via": False, "auto": False})


def make_entry(srv_id: str) -> list:
    """Сервер (любой, и основной) становится входом: роль, номер входа (поднятый
    вручную или свободный), выдача напрямую выключается (вход — не страна), связь
    со всеми выходами."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv:
        return ["❌ Сервер не найден."]
    lines = []
    if is_exit(srv):
        for e in entries(servers):
            lines += _unlink(e, srv)
        lines += _down(srv)
    if srv.get("slot"):
        # Бывший выход: его номер — в освободившиеся, иначе он достался бы новому
        # серверу, и конфиги этого слота молча ушли бы в другую страну
        _retire(servers, int(srv["slot"]), srv_label(srv))
    used = {e.get("entry_no") for e in entries(servers)}
    try:
        n = info(srv).get("ENTRY_NO", "")
    except Exception as err:
        return [f"❌ {srv_label(srv)}: {err}"]
    no = int(n) if n.isdigit() and int(n) not in used else next(i for i in range(1, 201) if i not in used)
    srv.update(role="entry", entry_no=no, issue={"direct": False, "via": False, "auto": False})
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
    """Вход становится обычным сервером: выходы снимают его, сам каскад гасится.
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
    # Номера удалённых серверов, хранившиеся у входа (первая версия), — к основному
    for k, label in (srv.get("cascade_retired") or {}).items():
        if str(k).isdigit() and not any(s.get("slot") == int(k) for s in servers):
            _retire(servers, int(k), label)
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
    запоминается у основного — новому серверу он не выдаётся (без SSH)."""
    if srv.get("slot") and not is_entry(srv):
        _retire(servers, int(srv["slot"]), srv_label(srv))


def after_ip_change(srv_id: str) -> list:
    """Сменился IP входа или выхода: у входа в туннелях старый адрес выхода, у выхода
    в фаерволе — старый адрес входа. Пересвязка обновляет и то и другое."""
    servers = load_servers()
    srv = _find(servers, srv_id)
    if not srv:
        return []
    if is_entry(srv):
        return relink(srv_id)
    if is_exit(srv):
        return _link_all(servers, srv)
    return []


def involved(srv: dict) -> bool:
    return is_entry(srv) or is_exit(srv)


def request_relink(reason: str = ""):
    """Пересвязать все входы после следующего запуска бота (relink_pending)."""
    try:
        with open(RELINK_FLAG, "w") as f:
            f.write(reason + "\n")
    except OSError as e:
        logger.warning(f"cascade: флаг пересвязки не записан: {e}")


def relink_pending() -> list | None:
    """Флаг стоит — пересвязать все входы и снять флаг. None — флага нет."""
    if not os.path.exists(RELINK_FLAG):
        return None
    try:
        with open(RELINK_FLAG) as f:
            reason = f.read().strip()
    except OSError:
        reason = ""
    lines = [reason] if reason else []
    servers = load_servers()
    for e in entries(servers):
        lines.append(f"🔀 {srv_label(e)}:")
        lines += ["   " + ln for ln in relink(e["id"])]
    if not entries(servers):
        lines.append("Входов нет — пересвязывать нечего.")
    try:
        os.remove(RELINK_FLAG)
    except OSError:
        pass
    return lines


def on_delete(servers: list, srv: dict) -> list:
    """Перед удалением сервера из бота (srv_del_ok): вход — гасится и снимается с
    выходов; выход — снимается со входов, его слот запоминается у основного (конфиги
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
