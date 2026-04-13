#!/usr/bin/env python3
# Version: 2.0
# proxy_bot.py — MTProxy + WhatsApp прокси с управлением через Telegram бота

import os, subprocess, time, secrets, logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

PROXY_CONF    = "/etc/proxy-bot/proxy_bot.env"
MTP_DIR       = "/opt/mtproxy"
MTP_BIN       = f"{MTP_DIR}/objs/bin/mtproto-proxy"
MTP_SECRET_F  = f"{MTP_DIR}/proxy-secret"
MTP_MULTI_F   = f"{MTP_DIR}/proxy-multi.conf"
MTP_SERVICE   = "mtproxy"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# КОНФИГ
# ══════════════════════════════════════════════════════════════════════════════

def load_conf() -> dict:
    conf = {}
    try:
        with open(PROXY_CONF) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip()
    except:
        pass
    return conf

def save_conf(data: dict):
    os.makedirs(os.path.dirname(PROXY_CONF), exist_ok=True)
    existing = load_conf()
    existing.update(data)
    with open(PROXY_CONF, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
    os.chmod(PROXY_CONF, 0o600)

def get_server_ip() -> str:
    conf = load_conf()
    if conf.get("SERVER_IP"):
        return conf["SERVER_IP"]
    try:
        with open("/etc/amnezia/amneziawg/server.env") as f:
            for line in f:
                if line.startswith("SERVER_IP="):
                    return line.split("=", 1)[1].strip()
    except:
        pass
    try:
        return subprocess.check_output(
            ["curl", "-sf", "--max-time", "5", "https://api.ipify.org"], text=True
        ).strip()
    except:
        return "—"

def setup_interactive():
    R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'; B='\033[1m'; NC='\033[0m'
    print(f"\n{C}{B}{'='*50}{NC}")
    print(f"{C}{B}   Proxy Bot — Первичная настройка{NC}")
    print(f"{C}{B}{'='*50}{NC}\n")
    while True:
        token = input("  Вставьте токен бота: ").strip()
        if ":" in token and len(token) > 20:
            break
        print(f"  {R}Неверный формат токена{NC}")
    while True:
        admin_id = input("  Вставьте ваш Telegram ID: ").strip()
        if admin_id.isdigit():
            break
        print(f"  {R}ID должен быть числом{NC}")
    print("  Определяю IP...", end="", flush=True)
    try:
        ip = subprocess.check_output(
            ["curl", "-sf", "--max-time", "5", "https://api.ipify.org"], text=True
        ).strip()
        print(f" {ip}")
    except:
        print()
        ip = input("  Введите IP сервера: ").strip()
    save_conf({"BOT_TOKEN": token, "ADMIN_ID": admin_id, "SERVER_IP": ip})
    print(f"\n  {G}Готово!{NC}\n")

# ══════════════════════════════════════════════════════════════════════════════
# ОБЩИЕ ХЕЛПЕРЫ
# ══════════════════════════════════════════════════════════════════════════════

def _svc_action(service: str, action: str, ok_msg: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", action, service],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return True, ok_msg
        return False, f"❌ Ошибка: {r.stderr.strip() or f'journalctl -u {service}'}"
    except Exception as e:
        return False, f"❌ {e}"

def _svc_status(service: str) -> dict:
    try:
        active = subprocess.check_output(
            ["systemctl", "is-active", service],
            text=True, stderr=subprocess.DEVNULL
        ).strip() == "active"
    except:
        active = False
    uptime_str = ""
    if active:
        try:
            import datetime
            raw = subprocess.check_output(
                ["systemctl", "show", service, "--property=ActiveEnterTimestamp", "--value"],
                text=True
            ).strip()
            parts = raw.split()
            if len(parts) >= 3:
                started = datetime.datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
                delta = datetime.datetime.utcnow() - started
                h, r = divmod(int(delta.total_seconds()), 3600)
                uptime_str = f"{h}ч {r // 60}м"
        except:
            pass
    return {"running": active, "uptime": uptime_str}

def _status_line(running: bool, uptime: str) -> str:
    icon = "🟢 Работает" if running else "🔴 Остановлен"
    return f"{icon}{f' ({uptime})' if uptime else ''}"

def _toggle_btn(running: bool, cb_stop: str, cb_start: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        "⏹ Остановить" if running else "▶️ Запустить",
        callback_data=cb_stop if running else cb_start
    )

def _back_kb(target="proxy_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=target)]])

def server_stats() -> str:
    lines = []
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        d, r = divmod(int(secs), 86400); h, r = divmod(r, 3600)
        lines.append(f"⏱ Аптайм: {d}д {h}ч {r // 60}м")
    except: pass
    try:
        la = open("/proc/loadavg").read().split()[:3]
        lines.append(f"⚙️ Load avg: {' '.join(la)}")
    except: pass
    try:
        mem = {}
        for line in open("/proc/meminfo"):
            k = line.split()[0].rstrip(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(line.split()[1])
        total = mem.get("MemTotal", 0) // 1024
        used  = total - mem.get("MemAvailable", 0) // 1024
        lines.append(f"🧠 RAM: {used} / {total} MB ({round(used/total*100) if total else 0}%)")
    except: pass
    try:
        p = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()[1].split()
        lines.append(f"💾 Диск: {p[2]} / {p[1]} ({p[4]})")
    except: pass
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# MTPROXY
# ══════════════════════════════════════════════════════════════════════════════

def mtp_installed() -> bool:
    return os.path.exists(MTP_BIN)

def mtp_get_secret() -> str: return load_conf().get("MTP_SECRET", "")
def mtp_get_port()   -> str: return load_conf().get("MTP_PORT", "443")

def mtp_build_link(secret: str, port: str, ip: str) -> str:
    return f"https://t.me/proxy?server={ip}&port={port}&secret={secret}"

def mtp_generate_secret(mode: str = "ee") -> str:
    """mode: ee | dd | plain"""
    raw = secrets.token_hex(16)
    if mode == "ee":
        domain_hex = "www.google.com".encode().hex()
        return f"ee{raw}{domain_hex}"
    elif mode == "dd":
        return f"dd{raw}"
    return raw

def mtp_update_tg_configs() -> tuple[bool, str]:
    try:
        r1 = subprocess.run(["curl", "-sf", "--max-time", "15",
            "https://core.telegram.org/getProxySecret", "-o", MTP_SECRET_F],
            capture_output=True, timeout=20)
        r2 = subprocess.run(["curl", "-sf", "--max-time", "15",
            "https://core.telegram.org/getProxyConfig", "-o", MTP_MULTI_F],
            capture_output=True, timeout=20)
        if r1.returncode == 0 and r2.returncode == 0:
            return True, "✅ Конфиги Telegram обновлены"
        return False, "❌ Не удалось скачать конфиги"
    except Exception as e:
        return False, f"❌ {e}"

def _write_mtp_service(port: str, secret: str):
    """secret — полный секрет как хранится в конфиге (с префиксом ee/dd или без).
    В -S флаг передаём чистые 32 hex символа без префикса.
    Флаг -D только для EE — домен зашит прямо в секрете. DD работает без -D."""
    stored = load_conf().get("MTP_SECRET", "")
    if stored.startswith("ee"):
        clean = stored[2:34]           # 32 hex символа после префикса ee
        faketls_flag = "-D www.google.com"
    elif stored.startswith("dd"):
        clean = stored[2:]             # 32 hex символа после префикса dd
        faketls_flag = ""              # DD не использует -D
    else:
        clean = stored
        faketls_flag = ""
    extra = f" {faketls_flag}" if faketls_flag else ""
    with open(f"/etc/systemd/system/{MTP_SERVICE}.service", "w") as f:
        f.write(
            f"[Unit]\nDescription=MTProxy for Telegram\n"
            f"After=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\n"
            f"ExecStartPre=/bin/sh -c 'curl -sf https://core.telegram.org/getProxySecret"
            f" -o {MTP_SECRET_F}; curl -sf https://core.telegram.org/getProxyConfig"
            f" -o {MTP_MULTI_F}'\n"
            f"ExecStart={MTP_BIN} -u nobody -p 8888 -H {port} -S {clean}"
            f"{extra} --aes-pwd {MTP_SECRET_F} {MTP_MULTI_F} -M 1\n"
            f"Restart=on-failure\nRestartSec=10\n"
            f"StandardOutput=journal\nStandardError=journal\n\n"
            f"[Install]\nWantedBy=multi-user.target\n"
        )
    subprocess.run(["systemctl", "daemon-reload"])

def mtp_apply_secret(new_secret: str) -> tuple[bool, str]:
    """new_secret — полный секрет с префиксом (ee.../dd...) или без (plain).
    Сохраняем как есть, в сервис передаём через _write_mtp_service."""
    save_conf({"MTP_SECRET": new_secret})
    _write_mtp_service(mtp_get_port(), new_secret)
    return _svc_action(MTP_SERVICE, "restart", "🔄 MTProxy перезапущен с новым секретом")

def mtp_start()   -> tuple[bool, str]: return _svc_action(MTP_SERVICE, "start",   "▶️ MTProxy запущен")
def mtp_stop()    -> tuple[bool, str]: return _svc_action(MTP_SERVICE, "stop",    "⏹ MTProxy остановлен")
def mtp_restart() -> tuple[bool, str]: return _svc_action(MTP_SERVICE, "restart", "🔄 MTProxy перезапущен")

async def show_mtp_help(query):
    await query.edit_message_text(
        "📖 *MTProxy — инструкция*\n\n"
        "*▶️ Старт / ⏹ Стоп*\n"
        "Запускает или останавливает прокси. Пока остановлен — Telegram через него не работает ни у кого.\n\n"
        "*🔄 Рестарт*\n"
        "Перезапуск без смены настроек. Помогает если прокси завис или перестал отвечать.\n\n"
        "*🔑 Сменить секрет*\n"
        "Генерирует новый секрет и перезапускает прокси. Все подключённые пользователи отвалятся — нужно разослать новую ссылку.\n\n"
        "*📥 Обновить конфиги TG*\n"
        "Скачивает свежие конфиги с серверов Telegram. Если прокси работает но соединение нестабильное — попробуйте это.\n\n"
        "*Как подключиться:*\n"
        "• *Android:* Настройки → Данные и память → Прокси\n"
        "• *ПК:* Настройки → Продвинутые настройки → Тип соединения\n"
        "Или просто откройте ссылку из главного экрана MTProxy.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="proxy_mtp_menu")]
        ]),
        parse_mode="Markdown"
    )

async def show_wa_help(query):
    await query.edit_message_text(
        "📖 *WhatsApp прокси — инструкция*\n\n"
        "*▶️ Старт / ⏹ Стоп*\n"
        "Запускает или останавливает контейнер. Пока остановлен — WhatsApp через него не работает.\n\n"
        "*🔄 Рестарт*\n"
        "Перезапуск контейнера. Помогает при зависании.\n\n"
        "*⬆️ Обновить образ*\n"
        "Скачивает свежую версию образа от Meta и перезапускает контейнер. Соединение прерывается на 1-2 минуты. Адрес подключения не меняется — повторно раздавать не нужно.\n\n"
        "*Как подключиться:*\n"
        "• *Android / iOS:* Настройки → Конфиденциальность → Расширенные → Использовать прокси\n"
        "• Введите IP сервера и порт 443\n\n"
        "⚠️ У WhatsApp прокси нет секрета — любой знающий IP может использовать его для подключения.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data="proxy_wa_menu")]
        ]),
        parse_mode="Markdown"
    )

async def show_mtp_menu(query):
    if not mtp_installed():
        await query.edit_message_text(
            "⚫ MTProxy не установлен.\n\n"
            "Запустите:\n`bash <(curl -s https://raw.githubusercontent.com/"
            "yntoolsmail-prog/Proxy-Telegram-Whatsapp/main/setup_proxy.sh)`",
            reply_markup=_back_kb(), parse_mode="Markdown"
        )
        return
    st     = _svc_status(MTP_SERVICE)
    secret = mtp_get_secret()
    port   = mtp_get_port()
    ip     = get_server_ip()
    link   = mtp_build_link(secret, port, ip) if secret and ip != "—" else "—"
    if secret.startswith("ee"):
        mode = "EE (TLS 1.3)"
    elif secret.startswith("dd"):
        mode = "fake-TLS (DD)"
    else:
        mode = "plain"
    await query.edit_message_text(
        f"📡 MTProxy\n\n"
        f"Статус: {_status_line(st['running'], st['uptime'])}\n"
        f"🌐 {ip}:{port}  |  🔑 {mode}\n\n"
        f"🔗 Ссылка:\n`{link}`\n\n"
        f"_Telegram → Настройки → Данные → Тип соединения_",
        reply_markup=InlineKeyboardMarkup([
            [_toggle_btn(st["running"], "proxy_mtp_stop", "proxy_mtp_start"),
             InlineKeyboardButton("🔄 Рестарт",             callback_data="proxy_mtp_restart")],
            [InlineKeyboardButton("🔑 Сменить секрет",      callback_data="proxy_mtp_secret_ask")],
            [InlineKeyboardButton("📥 Обновить конфиги TG", callback_data="proxy_mtp_update_cfg")],
            [InlineKeyboardButton("📖 Инструкция",          callback_data="proxy_mtp_help")],
            [InlineKeyboardButton("🔄 Обновить",            callback_data="proxy_mtp_menu")],
            [InlineKeyboardButton("◀️ Назад",               callback_data="proxy_menu")],
        ]),
        parse_mode="Markdown"
    )

async def show_mtp_secret_ask(query):
    await query.edit_message_text(
        "🔑 *Смена секрета MTProxy*\n\n"
        "⚠️ *Внимание!* После смены секрета:\n"
        "• Все подключённые пользователи отвалятся\n"
        "• Старая ссылка перестанет работать\n"
        "• Нужно разослать новую ссылку всем\n\n"
        "Выберите тип нового секрета или отмените:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 EE — TLS 1.3 (рекомендуется)", callback_data="proxy_mtp_secret_ee")],
            [InlineKeyboardButton("🔑 fake-TLS (DD)",                 callback_data="proxy_mtp_secret_dd")],
            [InlineKeyboardButton("🔓 plain",                         callback_data="proxy_mtp_secret_plain")],
            [InlineKeyboardButton("❌ Отмена",                        callback_data="proxy_mtp_menu")],
        ]),
        parse_mode="Markdown"
    )

async def show_mtp_secret_confirm(query, mode: str):
    mode_labels = {"ee": "EE (TLS 1.3)", "dd": "fake-TLS (DD)", "plain": "plain"}
    label = mode_labels.get(mode, mode)
    await query.edit_message_text(
        f"🔑 *Подтверждение смены секрета*\n\n"
        f"Тип: *{label}*\n\n"
        f"❗ Все пользователи будут отключены.\n"
        f"Вы уверены?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, сменить", callback_data=f"proxy_mtp_secret_confirm_{mode}")],
            [InlineKeyboardButton("❌ Отмена",      callback_data="proxy_mtp_menu")],
        ]),
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# WHATSAPP PROXY
# ══════════════════════════════════════════════════════════════════════════════

def wa_docker_ok() -> bool:
    try:
        subprocess.check_output(["docker", "info"], stderr=subprocess.DEVNULL, timeout=10)
        return True
    except:
        return False

def wa_installed() -> bool:
    try:
        r = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=whatsapp-proxy", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10
        )
        return "whatsapp-proxy" in r.stdout
    except:
        return False

def wa_status() -> dict:
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", "whatsapp-proxy"],
            text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
        running = out == "running"
    except:
        running = False
    uptime_str = ""
    if running:
        try:
            import datetime
            raw = subprocess.check_output(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", "whatsapp-proxy"],
                text=True, timeout=10
            ).strip()
            started = datetime.datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
            delta = datetime.datetime.utcnow() - started
            h, r = divmod(int(delta.total_seconds()), 3600)
            uptime_str = f"{h}ч {r // 60}м"
        except:
            pass
    return {"running": running, "uptime": uptime_str}

def wa_get_port() -> str: return load_conf().get("WA_PORT", "443")

def wa_start()   -> tuple[bool, str]:
    r = subprocess.run(["docker", "start",   "whatsapp-proxy"], capture_output=True, text=True, timeout=15)
    return (True, "▶️ WhatsApp прокси запущен") if r.returncode == 0 else (False, f"❌ {r.stderr.strip()}")

def wa_stop()    -> tuple[bool, str]:
    r = subprocess.run(["docker", "stop",    "whatsapp-proxy"], capture_output=True, text=True, timeout=15)
    return (True, "⏹ WhatsApp прокси остановлен") if r.returncode == 0 else (False, f"❌ {r.stderr.strip()}")

def wa_restart() -> tuple[bool, str]:
    r = subprocess.run(["docker", "restart", "whatsapp-proxy"], capture_output=True, text=True, timeout=20)
    return (True, "🔄 WhatsApp прокси перезапущен") if r.returncode == 0 else (False, f"❌ {r.stderr.strip()}")

def wa_update() -> tuple[bool, str]:
    try:
        subprocess.run(["docker", "stop", "whatsapp-proxy"], capture_output=True)
        subprocess.run(["docker", "rm",   "whatsapp-proxy"], capture_output=True)

        # Пробуем Docker Hub, фоллбэк — пересборка из исходников
        r_pull = subprocess.run(
            ["docker", "pull", "facebook/whatsapp_proxy:latest"],
            capture_output=True, timeout=180
        )
        if r_pull.returncode == 0:
            image = "facebook/whatsapp_proxy:latest"
        else:
            wa_src = "/opt/whatsapp-proxy-src"
            subprocess.run(
                ["git", "-C", wa_src, "pull"] if os.path.exists(wa_src)
                else ["git", "clone", "--depth=1", "https://github.com/WhatsApp/proxy", wa_src],
                check=True, timeout=60
            )
            subprocess.run(
                ["docker", "build", "-t", "whatsapp-proxy", f"{wa_src}/proxy"],
                check=True, timeout=300
            )
            image = "whatsapp-proxy"

        r = subprocess.run([
            "docker", "run", "-d", "--name", "whatsapp-proxy", "--restart", "always",
            "-p", "80:80", "-p", "443:443", "-p", "5222:5222",
            image
        ], capture_output=True, text=True, timeout=30)
        return (True, "✅ Обновлён и перезапущен") if r.returncode == 0 else (False, f"❌ {r.stderr.strip()}")
    except subprocess.TimeoutExpired:
        return False, "❌ Таймаут при обновлении образа"
    except Exception as e:
        return False, f"❌ {e}"

async def show_wa_menu(query):
    if not wa_docker_ok():
        await query.edit_message_text(
            "⚫ Docker не установлен.\n\n"
            "WhatsApp прокси работает только через Docker.\n"
            "Переустановите аддон и выберите WhatsApp при установке.",
            reply_markup=_back_kb()
        )
        return
    if not wa_installed():
        await query.edit_message_text(
            "⚫ WhatsApp прокси не установлен.\n\n"
            "Запустите:\n`bash <(curl -s https://raw.githubusercontent.com/"
            "yntoolsmail-prog/Proxy-Telegram-Whatsapp/main/setup_proxy.sh)`",
            reply_markup=_back_kb(), parse_mode="Markdown"
        )
        return
    st   = wa_status()
    ip   = get_server_ip()
    await query.edit_message_text(
        f"💬 WhatsApp прокси\n\n"
        f"Статус: {_status_line(st['running'], st['uptime'])}\n"
        f"🌐 {ip}  |  Порты: 80, 443, 5222\n\n"
        f"📱 Адрес для WhatsApp:\n`{ip}:443`\n\n"
        f"_WhatsApp → Настройки → Конфиденциальность →_\n"
        f"_Расширенные → Использовать прокси_",
        reply_markup=InlineKeyboardMarkup([
            [_toggle_btn(st["running"], "proxy_wa_stop", "proxy_wa_start"),
             InlineKeyboardButton("🔄 Рестарт",       callback_data="proxy_wa_restart")],
            [InlineKeyboardButton("⬆️ Обновить образ", callback_data="proxy_wa_update")],
            [InlineKeyboardButton("📖 Инструкция",     callback_data="proxy_wa_help")],
            [InlineKeyboardButton("🔄 Обновить",       callback_data="proxy_wa_menu")],
            [InlineKeyboardButton("◀️ Назад",          callback_data="proxy_menu")],
        ]),
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ
# ══════════════════════════════════════════════════════════════════════════════

async def show_proxy_menu(query):
    ip = get_server_ip()
    lines = [f"📡 Proxy Manager\n\n🌐 {ip}\n"]

    if mtp_installed():
        st = _svc_status(MTP_SERVICE)
        lines.append(f"{'🟢' if st['running'] else '🔴'} MTProxy{f' ({st['uptime']})' if st['uptime'] else ''}")
    else:
        lines.append("⚫ MTProxy — не установлен")


    if wa_docker_ok() and wa_installed():
        st = wa_status()
        lines.append(f"{'🟢' if st['running'] else '🔴'} WhatsApp прокси{f' ({st['uptime']})' if st['uptime'] else ''}")
    elif wa_docker_ok():
        lines.append("⚫ WhatsApp прокси — не установлен")
    else:
        lines.append("⚫ WhatsApp прокси — нет Docker")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 MTProxy",         callback_data="proxy_mtp_menu")],
                [InlineKeyboardButton("💬 WhatsApp прокси", callback_data="proxy_wa_menu")],
            [InlineKeyboardButton("🖥 Сервер",          callback_data="proxy_server")],
            [InlineKeyboardButton("🔄 Обновить",        callback_data="proxy_menu")],
            [InlineKeyboardButton("◀️ В меню",          callback_data="back")],
        ])
    )

async def show_server(query):
    await query.edit_message_text(
        f"🖥 Статус сервера\n\n{server_stats()}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="proxy_server")],
            [InlineKeyboardButton("◀️ Назад",    callback_data="proxy_menu")],
        ])
    )

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ РОУТЕР
# ══════════════════════════════════════════════════════════════════════════════

async def handle_proxy_callback(query, data: str, user_id: int, admin_id: int) -> bool:
    if user_id != admin_id:
        await query.answer("⛔ Только для администратора", show_alert=True)
        return True

    if   data == "proxy_menu":   await show_proxy_menu(query)
    elif data == "proxy_server": await show_server(query)

    elif data == "proxy_mtp_menu":    await show_mtp_menu(query)
    elif data == "proxy_mtp_start":
        ok, msg = mtp_start();   await query.answer(msg, show_alert=not ok); await show_mtp_menu(query)
    elif data == "proxy_mtp_stop":
        ok, msg = mtp_stop();    await query.answer(msg, show_alert=not ok); await show_mtp_menu(query)
    elif data == "proxy_mtp_restart":
        ok, msg = mtp_restart(); await query.answer(msg, show_alert=not ok); await show_mtp_menu(query)
    elif data == "proxy_mtp_secret_ask": await show_mtp_secret_ask(query)
    elif data in ("proxy_mtp_secret_ee", "proxy_mtp_secret_dd", "proxy_mtp_secret_plain"):
        mode_map = {"proxy_mtp_secret_ee": "ee", "proxy_mtp_secret_dd": "dd", "proxy_mtp_secret_plain": "plain"}
        await show_mtp_secret_confirm(query, mode_map[data])
    elif data.startswith("proxy_mtp_secret_confirm_"):
        mode = data.replace("proxy_mtp_secret_confirm_", "")
        new_s = mtp_generate_secret(mode)
        await query.edit_message_text("⏳ Применяю новый секрет...")
        ok, msg = mtp_apply_secret(new_s)
        await query.answer(msg, show_alert=True); await show_mtp_menu(query)
    elif data == "proxy_mtp_help": await show_mtp_help(query)
    elif data == "proxy_mtp_update_cfg":
        await query.edit_message_text("⏳ Загружаю конфиги Telegram...")
        ok, msg = mtp_update_tg_configs()
        if ok: mtp_restart()
        await query.answer(msg, show_alert=True); await show_mtp_menu(query)

    elif data == "proxy_wa_menu":    await show_wa_menu(query)
    elif data == "proxy_wa_start":
        ok, msg = wa_start();   await query.answer(msg, show_alert=not ok); await show_wa_menu(query)
    elif data == "proxy_wa_stop":
        ok, msg = wa_stop();    await query.answer(msg, show_alert=not ok); await show_wa_menu(query)
    elif data == "proxy_wa_restart":
        ok, msg = wa_restart(); await query.answer(msg, show_alert=not ok); await show_wa_menu(query)
    elif data == "proxy_wa_update":
        await query.edit_message_text("⏳ Обновляю образ...\nЭто займёт пару минут.")
        ok, msg = wa_update()
        await query.answer(msg, show_alert=True); await show_wa_menu(query)
    elif data == "proxy_wa_help": await show_wa_help(query)

    else:
        return False
    return True

# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conf    = load_conf()
    user_id = update.effective_user.id
    admin   = int(conf.get("ADMIN_ID", 0))
    if user_id != admin:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(
        f"📡 Proxy Manager\n\n🌐 {get_server_ip()}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📡 MTProxy",         callback_data="proxy_mtp_menu")],
                [InlineKeyboardButton("💬 WhatsApp прокси", callback_data="proxy_wa_menu")],
            [InlineKeyboardButton("🖥 Сервер",          callback_data="proxy_server")],
        ])
    )

async def _button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    conf  = load_conf()
    admin = int(conf.get("ADMIN_ID", 0))
    data  = "proxy_menu" if query.data == "back" else query.data
    await handle_proxy_callback(query, data, user_id, admin)

async def check_updates_job(context):
    """Ежедневная проверка обновлений — уведомляет админа если есть новая версия."""
    try:
        import json, urllib.request
        url = "https://api.github.com/repos/yntoolsmail-prog/Proxy-Telegram-Whatsapp/commits/main"
        req = urllib.request.Request(url, headers={"User-Agent": "proxy-bot"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        latest_sha  = data["sha"][:7]
        latest_msg  = data["commit"]["message"].split("\n")[0]
        latest_date = data["commit"]["committer"]["date"][:10]

        ver_file = "/root/.proxy_bot_version"
        current = ""
        try:
            with open(ver_file) as f:
                current = f.read().strip()
        except:
            pass

        if current != latest_sha:
            conf    = load_conf()
            admin   = int(conf.get("ADMIN_ID", 0))
            repo    = "https://github.com/yntoolsmail-prog/Proxy-Telegram-Whatsapp"
            await context.bot.send_message(
                chat_id=admin,
                text=(
                    f"🔔 *Доступно обновление Proxy Bot*\n\n"
                    f"Текущая версия: `{current or 'неизвестна'}`\n"
                    f"Новая версия:   `{latest_sha}` от {latest_date}\n\n"
                    f"📝 {latest_msg}\n\n"
                    f"[Посмотреть изменения]({repo}/commits/main)"
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
    except Exception as e:
        logger.warning(f"Проверка обновлений не удалась: {e}")

def main_standalone():
    conf = load_conf()
    if not conf.get("BOT_TOKEN"):
        setup_interactive()
        conf = load_conf()

    app = Application.builder().token(conf["BOT_TOKEN"]).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(_button_handler))

    # Ежедневная проверка обновлений в 10:00
    import datetime as dt
    app.job_queue.run_daily(
        check_updates_job,
        time=dt.time(hour=10, minute=0),
        name="update_check"
    )

    admin_id = int(conf.get("ADMIN_ID", 0))
    logger.info(f"Proxy Bot запущен. Admin: {admin_id}")
    print(f"\n\033[0;32m✓ Proxy Bot запущен! Admin ID: {admin_id}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main_standalone()
