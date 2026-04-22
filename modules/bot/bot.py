#!/usr/bin/env python3
# bot.py — Telegram-бот AmneziaWG
# Version: 3.0
# Вся бизнес-логика — в awg_core.py и sites_data.py
import os, subprocess, logging, json, time, tempfile, shutil, re, asyncio
try:
    import paramiko
    _PARAMIKO_AVAILABLE = True
except ImportError:
    _PARAMIKO_AVAILABLE = False
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from awg_core import (
    ADMIN_ID, AWG_IFACE, BOT_SERVICE, BOT_TOKEN, BW_LOG_FILE,
    CLIENTS_DIR, CONFIG_FILE, ENV_FILE, EXCL_EXT, QRENCODE_BIN,
    RESTART_FLAG_FILE, SERVER_ENDPOINT, SERVER_ENDPOINT_BACKUP,
    SERVER_IP, SERVER_PORT, TMA_URL, TZ,
    can_access_device, create_backup, create_client, device_short_name,
    fmt_bytes, fmt_handshake, fmt_histogram,
    gen_obfs, get_all_clients, get_allowed_ips_for_client,
    get_awg_dump, get_bw_histogram, get_bw_histogram_day,
    get_bw_top, get_client_keys, get_client_pub,
    get_host_iface, get_kernel_version, get_log_days,
    get_maintenance, get_real_server_ip, get_system_stats,
    get_ubuntu_version, get_user_clients, get_user_display,
    get_user_name, get_vnstat_monthly,
    is_approved, load_bw_peak, load_client_excl, load_users,
    log_maintenance_done, make_conf_for_client, make_conf_for_client_ep,
    make_vpn_link, read_iface_bytes, remove_client_from_awg, resolve_endpoint,
    save_bw_peak, save_client_excl, save_users,
    load_servers, save_servers, invalidate_servers_cache,
)
from sites_data import (
    SITES, CATEGORIES, DEFAULT_SELECTED, ALL_SELECTABLE,
)
from module_loader import load_modules
_modules = load_modules()

# ── Первый запуск: создаём bot.env если не существует ─────────────────────────
def setup():
    R='\033[0;31m'; G='\033[0;32m'; C='\033[0;36m'; B='\033[1m'; NC='\033[0m'
    print(f"\n{C}{B}{'='*50}{NC}")
    print(f"{C}{B}   AmneziaWG — Настройка Telegram бота{NC}")
    print(f"\n{C}{B}{'='*50}{NC}\n")
    while True:
        token = input("  Вставьте токен бота: ").strip()
        if ":" in token and len(token) > 20: break
        print(f"  {R}Неверный формат токена{NC}")
    while True:
        admin_id = input("  Вставьте ваш Telegram ID: ").strip()
        if admin_id.isdigit(): break
        print(f"  {R}ID должен быть числом{NC}")
    os.makedirs("/etc/amnezia/amneziawg", exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        f.write(f"BOT_TOKEN={token}\nADMIN_ID={admin_id}\n")
    os.chmod(CONFIG_FILE, 0o600)
    print(f"\n{G}✓ Готово!{NC}\n")

if not os.path.exists(CONFIG_FILE):
    setup()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
WAITING_REGISTER_NAME  = 10
WAITING_DEVICE_NAME    = 11
WAITING_RESTORE_FILE   = 12
WAITING_TZ_INPUT       = 15   # ждём ручной ввод часового пояса
WAITING_SITES_DOMAIN   = 16   # ждём домен/IP для добавления в исключения
# Добавление вторичного сервера
WAITING_SRV_IP         = 20
WAITING_SRV_PORT       = 21
WAITING_SRV_LOGIN      = 22
WAITING_SRV_PASSWORD   = 23
WAITING_SRV_NAME       = 24
WAITING_SRV_EMOJI      = 25
# Добавление домена к серверу
WAITING_SRV_DOMAIN     = 26

IMG_BASE = "https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/.images"

def _md(s: str) -> str:
    """Экранирует спецсимволы Markdown v1 в пользовательских строках."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s




def sites_keyboard(selected: set, device_name: str, expanded: set | None = None,
                   custom_domains: list | None = None) -> InlineKeyboardMarkup:
    """Строит клавиатуру исключений сайтов.
    Категории с 1 пунктом — всегда развёрнуты без заголовка.
    Категории с 2+ пунктами — сворачиваются/разворачиваются кнопкой заголовка.
    expanded — множество ключей категорий которые сейчас раскрыты.
    custom_domains — список кастомных доменов/IP пользователя."""
    if expanded is None:
        expanded = set()
    if custom_domains is None:
        custom_domains = []

    rows = []
    all_selected   = ALL_SELECTABLE.issubset(selected)  # для кнопки "выбрать все"

    for cat_name, keys in CATEGORIES.items():
        if len(keys) == 1:
            # Одиночный пункт — показываем без заголовка категории
            key    = keys[0]
            site   = SITES[key]
            locked = key in DEFAULT_SELECTED
            is_on  = key in selected
            mark   = "✅" if locked else ("☑️" if is_on else "☐")
            label  = f"{mark} {site['emoji']} {site['name']}"
            cb     = "noop" if locked else f"ts_{key}"
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
        else:
            # Многопунктовая категория — сворачиваемый заголовок
            is_expanded  = cat_name in expanded
            arrow        = "🔽" if is_expanded else "▶️"
            # Считаем сколько пунктов выбрано в категории (для подсказки)
            cat_on = sum(1 for k in keys if k in selected and k not in DEFAULT_SELECTED)
            hint   = f" [{cat_on}/{len(keys)}]" if cat_on > 0 else f" [{len(keys)}]"
            rows.append([InlineKeyboardButton(
                f"{arrow} {cat_name}{hint}",
                callback_data=f"cat_toggle_{cat_name}"
            )])
            if is_expanded:
                for key in keys:
                    site   = SITES[key]
                    locked = key in DEFAULT_SELECTED
                    is_on  = key in selected
                    mark   = "✅" if locked else ("☑️" if is_on else "☐")
                    label  = f"  {mark} {site['emoji']} {site['name']}"
                    cb     = "noop" if locked else f"ts_{key}"
                    rows.append([InlineKeyboardButton(label, callback_data=cb)])

    # Кнопка "Выбрать все" / "Снять все"
    if all_selected:
        rows.append([InlineKeyboardButton("☐ Снять все", callback_data="ts_deselect_all")])
    else:
        rows.append([InlineKeyboardButton("☑️ Выбрать все", callback_data="ts_select_all")])

    # Блок кастомных доменов
    if custom_domains:
        rows.append([InlineKeyboardButton("──── 🌐 Свои сайты ────", callback_data="noop")])
        for i, d in enumerate(custom_domains):
            rows.append([InlineKeyboardButton(f"❌ {d}", callback_data=f"ts_rm_custom_{i}")])
    rows.append([InlineKeyboardButton("➕ Добавить свой сайт", callback_data="sites_add_custom")])

    rows.append([
        InlineKeyboardButton("✅ Готово",   callback_data="sites_done"),
        InlineKeyboardButton("❌ Отмена",  callback_data=f"device_{device_name}"),
    ])
    return InlineKeyboardMarkup(rows)

async def bw_monitor_job(context: ContextTypes.DEFAULT_TYPE):
    """Job: каждые 5 секунд замеряет скорость по awg0 (клиенты) и eth0 (сервер).
    В лог пишет раз в минуту максимальные значения за окно.

    Логика RX/TX для awg0 с точки зрения клиента:
      TX awg0 = сервер отдаёт клиентам  = клиенты скачивают (↓ download)
      RX awg0 = сервер получает от клиентов = клиенты отдают (↑ upload)
    Для eth0 аналогично с точки зрения сервера.
    """
    now       = int(time.time())
    eth_iface = get_host_iface()

    # Читаем оба интерфейса
    eth_r2, eth_t2 = read_iface_bytes(eth_iface)
    awg_r2, awg_t2 = read_iface_bytes(AWG_IFACE)

    prev = context.bot_data.get("bw_prev")

    if prev:
        dt = now - prev["ts"]
        if dt > 0:
            def _mbit(new, old):
                diff = new - old
                if diff < 0:
                    return None  # перезагрузка счётчика
                return round(diff * 8 / 1_000_000 / dt, 2)

            # AWG: клиентский трафик
            awg_down = _mbit(awg_t2, prev["awg_t"])  # TX awg0 = клиенты скачивают
            awg_up   = _mbit(awg_r2, prev["awg_r"])  # RX awg0 = клиенты отдают
            # ETH: серверный трафик
            eth_down = _mbit(eth_r2, prev["eth_r"])  # RX eth0 = сервер получает
            eth_up   = _mbit(eth_t2, prev["eth_t"])  # TX eth0 = сервер отдаёт

            # Защита от фантомных всплесков
            if None in (awg_down, awg_up, eth_down, eth_up) or \
               max(awg_down, awg_up, eth_down, eth_up) > 10_000:
                context.bot_data["bw_prev"] = {
                    "awg_r": awg_r2, "awg_t": awg_t2,
                    "eth_r": eth_r2, "eth_t": eth_t2, "ts": now,
                }
                return

            # Пики — по клиентской нагрузке (awg), пики сервера (eth) рядом
            awg_load = round(max(awg_down, awg_up), 2)
            eth_load = round(max(eth_down, eth_up), 2)
            peak     = load_bw_peak()
            today    = time.strftime("%Y-%m-%d")

            day_peak = peak.get("day", {})
            if day_peak.get("date") != today:
                day_peak = {"date": today,
                            "awg_down": 0, "awg_up": 0,
                            "eth_down": 0, "eth_up": 0}
            if awg_load > max(day_peak.get("awg_down", 0), day_peak.get("awg_up", 0)):
                day_peak.update({"date": today,
                                 "awg_down": awg_down, "awg_up": awg_up,
                                 "eth_down": eth_down, "eth_up": eth_up})

            all_peak = peak.get("all", {"awg_down": 0, "awg_up": 0,
                                        "eth_down": 0, "eth_up": 0})
            if awg_load > max(all_peak.get("awg_down", 0), all_peak.get("awg_up", 0)):
                all_peak = {"awg_down": awg_down, "awg_up": awg_up,
                            "eth_down": eth_down, "eth_up": eth_up}

            save_bw_peak({
                "day":  day_peak,
                "all":  all_peak,
                "last": {"awg_down": awg_down, "awg_up": awg_up,
                         "eth_down": eth_down, "eth_up": eth_up, "ts": now},
            })

            # В лог — раз в минуту, пишем максимальные значения за окно
            mm = context.bot_data.get("bw_minute_max", {
                "awg_down": 0, "awg_up": 0,
                "eth_down": 0, "eth_up": 0,
                "awg_load": 0, "ts": now,
            })
            if awg_load > mm.get("awg_load", 0):
                mm.update({"awg_down": awg_down, "awg_up": awg_up,
                           "eth_down": eth_down, "eth_up": eth_up,
                           "awg_load": awg_load})
            context.bot_data["bw_minute_max"] = mm

            if now - mm["ts"] >= 60:
                try:
                    with open(BW_LOG_FILE, "a") as f:
                        f.write(
                            f"{time.strftime('%Y-%m-%d %H:%M')} "
                            f"AWG_DOWN={mm['awg_down']} AWG_UP={mm['awg_up']} "
                            f"ETH_DOWN={mm['eth_down']} ETH_UP={mm['eth_up']}\n"
                        )
                    with open(BW_LOG_FILE) as f:
                        lines = f.readlines()
                    if len(lines) > 10080:
                        with open(BW_LOG_FILE, "w") as f:
                            f.writelines(lines[-10080:])
                except Exception:
                    pass
                context.bot_data["bw_minute_max"] = {
                    "awg_down": 0, "awg_up": 0,
                    "eth_down": 0, "eth_up": 0,
                    "awg_load": 0, "ts": now,
                }

    context.bot_data["bw_prev"] = {
        "awg_r": awg_r2, "awg_t": awg_t2,
        "eth_r": eth_r2, "eth_t": eth_t2, "ts": now,
    }


async def show_bandwidth(query, period_days: int = 0):
    """Экран статистики трафика для админа."""
    peak = load_bw_peak()
    last = peak.get("last", {})
    day  = peak.get("day",  {})
    allp = peak.get("all",  {})
    top  = get_bw_top(5)

    lines = ["📈 Статистика трафика\n"]

    # ── Скорость прямо сейчас ──
    if last:
        last_time = time.strftime("%H:%M", time.localtime(last.get("ts", 0)))
        lines.append(f"⚡ Клиенты ({last_time}):")
        lines.append(f"   ↓ скачивают {last.get('awg_down', 0)} Mbit/s  "
                     f"↑ отдают {last.get('awg_up', 0)} Mbit/s")
        lines.append(f"🌐 Сервер ({last_time}):")
        lines.append(f"   ↓ получает {last.get('eth_down', 0)} Mbit/s  "
                     f"↑ отдаёт {last.get('eth_up', 0)} Mbit/s")
        awg_d = last.get("awg_down", 0)
        eth_d = last.get("eth_down", 0)
        if eth_d > 0:
            overhead = round(eth_d - awg_d, 2)
            lines.append(f"   overhead: {overhead:+.2f} Mbit/s")

    # ── Пики клиентов (awg0) ──
    if day:
        lines.append(f"\n🏅 Пик клиентов сегодня ({day.get('date', '—')}):")
        lines.append(f"   ↓ {day.get('awg_down', 0)}  ↑ {day.get('awg_up', 0)} Mbit/s")
        lines.append(f"   сервер: ↓ {day.get('eth_down', 0)}  ↑ {day.get('eth_up', 0)} Mbit/s")
    if allp:
        lines.append(f"\n🏆 Пик клиентов за всё время:")
        lines.append(f"   ↓ {allp.get('awg_down', 0)}  ↑ {allp.get('awg_up', 0)} Mbit/s")
        lines.append(f"   сервер: ↓ {allp.get('eth_down', 0)}  ↑ {allp.get('eth_up', 0)} Mbit/s")

    # ── Топ-5 минут по клиентам ──
    if top:
        lines.append(f"\n🔝 Топ-5 минут (клиенты):")
        for rec in top:
            lines.append(
                f"   {rec['dt']}  "
                f"↓{rec['awg_down']} ↑{rec['awg_up']}  "
                f"(сервер ↓{rec['eth_down']} ↑{rec['eth_up']})"
            )

    # ── Месячный трафик сервера (vnstat/eth0) — для контроля лимита ──
    monthly = get_vnstat_monthly()
    if monthly:
        lines.append(f"\n📦 Трафик сервера по месяцам (eth0, лимит провайдера):")
        for m in monthly:
            cur_mark = " ◀ текущий" if m.get("current") else ""
            lines.append(
                f"   {m['label']}  "
                f"↓{m['rx_gb']} + ↑{m['tx_gb']} = {m['total_gb']} GB{cur_mark}"
            )
        cur = monthly[-1]
        if cur.get("current"):
            total = cur["total_gb"]
            warn = ""
            if total >= 4000:   warn = "  🔴 >4 TB!"
            elif total >= 3000: warn = "  🟠 >3 TB"
            elif total >= 2000: warn = "  🟡 >2 TB"
            elif total >= 1000: warn = "  🟢 >1 TB"
            if warn:
                lines.append(f"   ⚠️ Текущий месяц: {total} GB{warn}")
    else:
        lines.append("\n📦 Трафик сервера: vnstat ещё собирает данные.")

    # ── Гистограмма клиентской нагрузки ──
    period_label = {0: "всё время", 7: "7 дней", 30: "30 дней"}.get(period_days, f"{period_days} дней")
    hist = get_bw_histogram(period_days)
    if hist:
        lines += fmt_histogram(hist, period_label)
    else:
        lines.append("\n📊 Гистограмма: данных пока нет, накапливается...")

    # Кнопки периода — подсвечиваем активный
    def p(label, days):
        mark = "✅ " if days == period_days else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"bw_period_{days}")

    kb = InlineKeyboardMarkup([
        [p("Всё время", 0), p("30 дней", 30), p("7 дней", 7)],
        [InlineKeyboardButton("📅 По дням",      callback_data="bw_days_0")],
        [InlineKeyboardButton("🗑 Сбросить пики", callback_data="bw_reset_ask")],
        [InlineKeyboardButton("🔄 Обновить",      callback_data=f"bw_period_{period_days}")],
        [InlineKeyboardButton("◀️ Статус",        callback_data="status")],
        [InlineKeyboardButton("◀️ В меню",        callback_data="back")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_days(query, page: int = 0):
    """Гистограмма по конкретному дню с листалкой"""
    days = get_log_days()
    if not days:
        await query.edit_message_text(
            "📊 Данных по дням пока нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="bandwidth")
            ]])
        )
        return

    # page=0 — последний день, листаем назад
    total_days = len(days)
    idx = total_days - 1 - page
    idx = max(0, min(idx, total_days - 1))
    date_str = days[idx]

    hist = get_bw_histogram_day(date_str)
    lines = [f"📅 Статистика за {date_str}"]
    if hist:
        lines += fmt_histogram(hist)
    else:
        lines.append("Нет данных за этот день.")

    has_prev = idx > 0            # есть более ранние дни
    has_next = idx < total_days - 1  # есть более поздние дни

    nav = []
    if has_prev:
        nav.append(InlineKeyboardButton("◀️ Раньше", callback_data=f"bw_days_{page + 1}"))
    nav.append(InlineKeyboardButton(f"{idx + 1}/{total_days}", callback_data="noop"))
    if has_next:
        nav.append(InlineKeyboardButton("Позже ▶️", callback_data=f"bw_days_{page - 1}"))

    kb = InlineKeyboardMarkup([
        nav,
        [InlineKeyboardButton("◀️ Назад", callback_data="bandwidth")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_reset_ask(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Сбросить только пики",    callback_data="bw_reset_confirm")],
        [InlineKeyboardButton("💣 Сбросить всё (с нуля)",  callback_data="bw_reset_all_confirm")],
        [InlineKeyboardButton("❌ Отмена",                  callback_data="bandwidth")],
    ])
    await query.edit_message_text(
        "🗑 Сброс данных трафика\n\n"
        "🗑 *Только пики* — обнуляет абсолютный пик и пик дня.\n"
        "Лог и статистика vnstat сохраняются.\n\n"
        "💣 *Всё с нуля* — удаляет пики И лог замеров.\n"
        "Гистограмма и топ-5 начнут собираться заново.\n"
        "Статистика vnstat не затрагивается — её хранит система.",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def do_bw_reset(query):
    """Сброс только пиков"""
    peak = load_bw_peak()
    peak["all"] = {"total": 0, "rx": 0, "tx": 0}
    peak["day"]  = {}
    save_bw_peak(peak)
    await query.answer("✅ Пики сброшены", show_alert=False)
    await show_bandwidth(query)

async def do_bw_reset_all(query):
    """Полный сброс — пики + лог"""
    peak = {"all": {"total": 0, "rx": 0, "tx": 0}, "day": {}, "last": {}}
    save_bw_peak(peak)
    try:
        open(BW_LOG_FILE, "w").close()
    except:
        pass
    await query.answer("✅ Все данные сброшены", show_alert=False)
    await show_bandwidth(query)



# ── Бэкап ──────────────────────────────────────────────────────────────────────
async def do_backup(query):
    try:
        backup_path = create_backup()
        ts = time.strftime('%d.%m.%Y %H:%M:%S')
        with open(backup_path, "rb") as fh:
            await query.message.reply_document(
                document=fh,
                filename=os.path.basename(backup_path),
                caption=f"💾 Бэкап от {ts}\nКлиентов: {len(get_all_clients())}"
            )
        await query.edit_message_text(
            f"✅ Бэкап создан и отправлен.\n\nФайл также сохранён на сервере:\n`{backup_path}`",
            reply_markup=back_kb(), parse_mode="Markdown"
        )
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка при создании бэкапа: {e}", reply_markup=back_kb())

def back_kb(target="back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data=target)]])

# ══════════════════════════════════════════════════════════════════════════════
# РЕГИСТРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def _tma_button() -> InlineKeyboardButton | None:
    """Возвращает кнопку открытия TMA или None если URL не настроен."""
    if not TMA_URL:
        return None
    if TMA_URL.startswith("https://"):
        return InlineKeyboardButton("▶️ ОТКРЫТЬ VPN 🔑", web_app=WebAppInfo(url=TMA_URL))
    return InlineKeyboardButton("▶️ ОТКРЫТЬ VPN 🔑", url=TMA_URL)


async def show_start_screen(msg, user_id: int, edit: bool = False):
    """Стартовый экран:
    - Обычный пользователь → главное меню.
    - Администратор с TMA → статус-экран с кнопкой «Открыть VPN» + «Режим бота».
    - Администратор без TMA → сразу главное меню (промежуточный экран бессмысленен).
    """
    is_admin = (user_id == ADMIN_ID)

    if not is_admin:
        await main_menu(msg, user_id, edit=edit)
        return

    # Если TMA не настроен — показываем сразу полное меню администратора
    tma_btn = _tma_button()
    if not tma_btn:
        await main_menu(msg, user_id, edit=edit)
        return

    peers   = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)
    total  = len(get_all_clients())
    sys_s  = get_system_stats()
    bw     = load_bw_peak().get("last", {})
    ram_pct = round(sys_s["ram_used"] / sys_s["ram_total"] * 100) if sys_s.get("ram_total") else 0

    users         = load_users()
    pending_count = len(users["pending"])
    pending_note  = f"  🔴 Ожидают: {pending_count}" if pending_count else ""

    text = (
        f"🔐 AmneziaWG\n"
        f"━━━━━━━━━━━━━━\n"
        f"🟢 Онлайн: {online} из {total}\n"
        f"💾 RAM: {ram_pct}%  💿 Диск: {sys_s['disk_pct']}%\n"
        f"⬇️ {bw.get('awg_down', 0)} / ⬆️ {bw.get('awg_up', 0)} Mbit/s\n\n"
        f"🖥 IP: {SERVER_IP}\n"
        f"📱 Клиентов: {total} | 👥 Польз.: {len(users['approved'])}{pending_note}"
    )

    markup = InlineKeyboardMarkup([
        [tma_btn],
        [InlineKeyboardButton("📱 Режим бота", callback_data="back")],
    ])
    if edit:
        await msg.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await msg.reply_text(text, reply_markup=markup, parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_approved(user_id):
        await show_start_screen(update.message, user_id)
        return ConversationHandler.END

    users = load_users()
    if str(user_id) in users["pending"]:
        await update.message.reply_text(
            "⏳ Ваш запрос уже отправлен администратору.\n"
            "Ожидайте подтверждения."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Добро пожаловать в семейный VPN!\n\n"
        "Введите ваше имя *латиницей* (только буквы, без пробелов).\n"
        "Именно оно будет использоваться для ваших устройств.\n\n"
        "Например: `Ivan`, `Lev`, `Artem`, `Marina`",
        parse_mode="Markdown"
    )
    return WAITING_REGISTER_NAME

async def receive_register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    tg_name  = update.effective_user.first_name or "Unknown"
    raw      = update.message.text.strip()

    latin_name = "".join(c for c in raw if c.isascii() and (c.isalpha() or c.isdigit()))
    latin_name = latin_name.capitalize()

    if not latin_name:
        await update.message.reply_text(
            "❌ Пожалуйста, введите имя *латиницей*. Например: `Ivan`",
            parse_mode="Markdown"
        )
        return WAITING_REGISTER_NAME

    users = load_users()
    taken = [u["name"].lower() for u in users["approved"].values()] + \
            [u["name"].lower() for u in users["pending"].values()]
    if latin_name.lower() in taken:
        await update.message.reply_text(
            f"❌ Имя *{latin_name}* уже занято. Попробуйте другое.",
            parse_mode="Markdown"
        )
        return WAITING_REGISTER_NAME

    users["pending"][str(user_id)] = {
        "name":         latin_name,
        "display":      tg_name,
        "requested_at": int(time.time())
    }
    save_users(users)

    await update.message.reply_text(
        f"✅ Запрос отправлен!\n\n"
        f"Ваше имя в системе: *{latin_name}*\n"
        f"Ожидайте подтверждения администратора.",
        parse_mode="Markdown"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ Разрешить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(f"❌ Отклонить", callback_data=f"reject_{user_id}"),
        ]
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🔔 Новый запрос на доступ к VPN\n\n"
            f"👤 Telegram: {tg_name} (@{update.effective_user.username or '—'})\n"
            f"🆔 ID: `{user_id}`\n"
            f"📝 Имя в системе: *{latin_name}*"
        ),
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ МЕНЮ
# ══════════════════════════════════════════════════════════════════════════════

async def main_menu(msg, user_id: int, edit=False):
    is_admin = (user_id == ADMIN_ID)

    if is_admin:
        peers_a  = get_awg_dump()
        now_a    = int(time.time())
        online_a = sum(1 for p in peers_a.values() if p.get("handshake") and now_a - p["handshake"] < 180)
        total_a  = len(get_all_clients())
        sys_a    = get_system_stats()
        bw_a     = load_bw_peak().get("last", {})
        ram_a    = round(sys_a["ram_used"] / sys_a["ram_total"] * 100) if sys_a.get("ram_total") else 0
        users         = load_users()
        pending_count = len(users["pending"])
        pending_note  = f"  🔴 Ожидают: {pending_count}" if pending_count else ""
        pending_label = f"👥 Пользователи" + (f" 🔴{pending_count}" if pending_count else "")
        text = (
            f"🔐 AmneziaWG\n"
            f"━━━━━━━━━━━━━━\n"
            f"🟢 Онлайн: {online_a} из {total_a}\n"
            f"💾 RAM: {ram_a}%  💿 Диск: {sys_a['disk_pct']}%\n"
            f"⬇️ {bw_a.get('awg_down', 0)} / ⬆️ {bw_a.get('awg_up', 0)} Mbit/s\n\n"
            f"🖥 IP: {SERVER_IP}\n"
            f"📱 Клиентов: {total_a} | 👥 Польз.: {len(users['approved'])}{pending_note}"
        )
        servers = load_servers()
        srv_label = f"🖥 Серверы ({len(servers)})"
        kb = [
            [InlineKeyboardButton("🧲 Добавить устройство",  callback_data="add")],
            [InlineKeyboardButton("📋 Мои устройства",       callback_data="my_devices")],
            [InlineKeyboardButton("🌍 Все клиенты",          callback_data="all_clients")],
            [InlineKeyboardButton(pending_label,             callback_data="manage_users")],
            [InlineKeyboardButton(srv_label,                 callback_data="servers")],
            [InlineKeyboardButton("📊 Статус сервера",       callback_data="status")],
            [InlineKeyboardButton("🔧 Техобслуживание",      callback_data="maintenance")],
            [InlineKeyboardButton("📖 Инструкция",           callback_data="help")],
        ]
        kb.extend(_modules.get_user_menu_buttons(user_id))
        kb.extend(_modules.get_admin_menu_buttons())
    else:
        my_clients   = get_user_clients(user_id)
        display_name = get_user_display(user_id)
        n = len(my_clients)
        word = "устройство" if n == 1 else ("устройства" if 2 <= n <= 4 else "устройств")

        peers   = get_awg_dump()
        now_ts  = int(time.time())
        online  = sum(1 for p in peers.values() if p.get("handshake") and now_ts - p["handshake"] < 180)
        total   = len(get_all_clients())
        sys_s   = get_system_stats()
        bw      = load_bw_peak().get("last", {})
        ram_pct = round(sys_s["ram_used"] / sys_s["ram_total"] * 100) if sys_s.get("ram_total") else 0

        text = (
            f"🔐 Семейный VPN\n"
            f"━━━━━━━━━━━━━━\n"
            f"🟢 Онлайн: {online} из {total}\n"
            f"💾 RAM: {ram_pct}%  💿 Диск: {sys_s['disk_pct']}%\n"
            f"⬇️ {bw.get('awg_down', 0)} / ⬆️ {bw.get('awg_up', 0)} Mbit/s\n\n"
            f"👋 Привет, {_md(display_name)}!\n"
            f"📱 Ваших устройств: {n} {word}"
        )

        tma_btn = _tma_button()
        kb = []
        if tma_btn:
            kb.append([tma_btn])
        kb.append([InlineKeyboardButton("📋 Мои устройства",      callback_data="my_devices")])
        kb.append([InlineKeyboardButton("🧲 Добавить устройство",  callback_data="add")])
        kb.append([InlineKeyboardButton("📊 Статус сервера",       callback_data="status")])
        kb.append([InlineKeyboardButton("📖 Инструкция",           callback_data="help")])
        kb.extend(_modules.get_user_menu_buttons(user_id))

    if edit:
        await msg.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КНОПОК
# ══════════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    data = query.data

    # Одобрение/отклонение — только для админа
    if data.startswith("approve_") or data.startswith("reject_"):
        if user_id != ADMIN_ID:
            await query.answer("⛔ Только для администратора", show_alert=True)
            return
        target_id = int(data.split("_", 1)[1])
        users = load_users()
        info  = users["pending"].get(str(target_id))
        if not info:
            await query.edit_message_text("⚠️ Запрос уже обработан.")
            return
        if data.startswith("approve_"):
            users["approved"][str(target_id)] = info
            del users["pending"][str(target_id)]
            save_users(users)
            await query.edit_message_text(
                f"✅ Пользователь *{info['name']}* ({info['display']}) одобрен.",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    f"🎉 Доступ к VPN открыт!\n\n"
                    f"Ваше имя в системе: *{info['name']}*\n\n"
                    f"Нажмите /start чтобы начать."
                ),
                parse_mode="Markdown"
            )
        else:
            del users["pending"][str(target_id)]
            save_users(users)
            await query.edit_message_text(
                f"❌ Пользователь *{info['name']}* ({info['display']}) отклонён.",
                parse_mode="Markdown"
            )
            await context.bot.send_message(
                chat_id=target_id,
                text="❌ Ваш запрос на доступ к VPN отклонён администратором."
            )
        return

    # Все остальные кнопки — только для одобренных
    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return

    is_admin = (user_id == ADMIN_ID)

    if data == "back":
        await main_menu(query, user_id, edit=True)
    elif data == "my_devices":
        await show_my_devices(query, user_id)
    elif data == "all_clients" and is_admin:
        await show_all_clients(query)
    elif data == "manage_users" and is_admin:
        await show_manage_users(query)
    elif data == "servers" and is_admin:
        await show_servers_list(query)
    elif data.startswith("srv_card_") and is_admin:
        await show_server_card(query, int(data[9:]))
    elif data.startswith("srv_del_ok_") and is_admin:
        await srv_del_ok(query, int(data[11:]))
    elif data.startswith("srv_del_") and not data.startswith("srv_del_ok_") and is_admin:
        await srv_del_confirm(query, int(data[8:]))
    elif data == "status":
        await show_status(query)
    elif data == "restart_bot":
        await query.edit_message_text("🔄 Перезапускаю бота...\n\nЧерез несколько секунд появится кнопка меню.")
        # Пишем флаг с chat_id — при старте бот пришлёт меню именно этому пользователю
        try:
            with open(RESTART_FLAG_FILE, "w") as _rf:
                _rf.write(str(query.from_user.id))
        except Exception:
            pass
        subprocess.Popen(["systemctl", "restart", BOT_SERVICE])
    elif data == "restart_awg" and is_admin:
        await query.edit_message_text("⚡ Перезапускаю AWG...\n\nVPN будет недоступен ~5 секунд.")
        async def _awg_restart_check(ctx: ContextTypes.DEFAULT_TYPE):
            result = subprocess.run(
                ["systemctl", "is-active", f"awg-quick@{AWG_IFACE}"],
                capture_output=True, text=True
            )
            st = result.stdout.strip()
            if st == "active":
                status_text = "🟢 AWG работает"
            else:
                # Интерфейс может быть жив даже если сервис failed
                iface_up = subprocess.run(
                    ["ip", "link", "show", AWG_IFACE],
                    capture_output=True
                ).returncode == 0
                status_text = "🟢 AWG работает (интерфейс активен)" if iface_up else "🔴 AWG не запустился — проверьте логи"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 В меню", callback_data="back")],
                [InlineKeyboardButton("📊 Статус", callback_data="status")],
            ])
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚡ Перезапуск AWG завершён\n{status_text}",
                reply_markup=kb,
            )
        context.application.job_queue.run_once(_awg_restart_check, when=8)
        subprocess.Popen(["awg-quick", "down", AWG_IFACE], stderr=subprocess.DEVNULL)
        subprocess.Popen(["systemctl", "restart", f"awg-quick@{AWG_IFACE}"])
    elif data == "bandwidth" and is_admin:
        await show_bandwidth(query)
    elif data.startswith("bw_period_") and is_admin:
        await show_bandwidth(query, int(data.split("_")[2]))
    elif data.startswith("bw_days_") and is_admin:
        await show_bw_days(query, int(data.split("_")[2]))
    elif data == "bw_reset_ask" and is_admin:
        await show_bw_reset_ask(query)
    elif data == "bw_reset_confirm" and is_admin:
        await do_bw_reset(query)
    elif data == "bw_reset_all_confirm" and is_admin:
        await do_bw_reset_all(query)
    elif data == "noop":
        await query.answer()
    elif data == "diagnostics" and is_admin:
        await do_diagnostics(query)
    elif data == "backup" and is_admin:
        await do_backup(query)
    elif data == "maintenance" and is_admin:
        await show_maintenance(query)
    elif data == "maint_upgrade" and is_admin:
        await do_maint_upgrade(query)
    elif data == "maint_ptb" and is_admin:
        await do_maint_ptb(query)
    elif data == "maint_tz" and is_admin:
        await show_maint_tz(query)
    elif data == "set_tz_manual" and is_admin:
        await ask_tz_manual(update, context)
    elif data.startswith("set_tz_") and is_admin:
        await do_set_tz(query, data[7:])
    elif data == "maint_done" and is_admin:
        await do_maint_done(query)
    elif data == "maint_update_ip" and is_admin:
        await query.edit_message_text("⏳ Определяю текущий IP сервера...")
        real_ip = get_real_server_ip()
        if not real_ip:
            await query.edit_message_text(
                "❌ Не удалось определить внешний IP.\nПроверьте интернет-соединение сервера.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="maintenance")]])
            )
            return
        if real_ip == SERVER_IP:
            await query.edit_message_text(
                f"✅ IP актуален: `{SERVER_IP}`\nОбновление не требуется.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="maintenance")]]),
                parse_mode="Markdown"
            )
            return
        ep_note = ""
        if SERVER_ENDPOINT == SERVER_IP:
            ep_note = f"\n⚠️ SERVER_ENDPOINT тоже будет обновлён на `{real_ip}`."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Обновить", callback_data=f"update_ip_{real_ip}")],
            [InlineKeyboardButton("❌ Отмена",   callback_data="maintenance")],
        ])
        await query.edit_message_text(
            f"🔄 Обновление IP сервера\n\n"
            f"В настройках: `{SERVER_IP}`\n"
            f"Реальный IP:  `{real_ip}`{ep_note}\n\n"
            f"Подтвердить обновление?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif data.startswith("update_ip_") and is_admin:
        new_ip = data[10:]
        if new_ip == "skip":
            await query.edit_message_text("Пропущено. IP не изменён.", reply_markup=back_kb())
        else:
            await do_update_ip(query, new_ip)
    elif data.startswith("repo_update_") and is_admin:
        sha = data[12:]
        await do_repo_update(query, sha)
    elif data.startswith("repo_skip_") and is_admin:
        sha = data[10:]
        _write_last_commit(sha)
        await query.edit_message_text(
            f"⏭ Пропущено. Коммит `{sha[:7]}` отмечен как известный.",
            parse_mode="Markdown",
        )
    elif data == "help":
        await show_help(query)
    elif data == "help_dns":
        await show_help_dns(query)
    elif data == "add_cancel":
        await main_menu(query, user_id, edit=True)
    elif data == "my_devices_back":
        await show_my_devices(query, user_id)
    elif data.startswith("device_"):
        await show_device(query, data[7:], user_id)
    # ── .conf: выбор сервера/эндпоинта ──
    elif data.startswith("conf_"):
        rest = data[5:]
        if rest.startswith("srv_"):
            # conf_srv_{idx}_{name} → список эндпоинтов сервера
            parts = rest[4:].split("_", 1)
            if len(parts) == 2 and parts[0].isdigit():
                await show_server_eps(query, parts[1], user_id, "conf", int(parts[0]))
        elif rest.startswith("s") and "_e" in rest:
            # conf_s{si}_e{ei}_{name} → отправить с конкретным эндпоинтом
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        srv = servers[si]
                        ep = eps[ei]["value"]
                        await _do_send_action(query, n, "conf", ep, srv)
        elif rest.startswith("ep_"):
            # Обратная совместимость: conf_ep_<epkey>_<name>
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_conf(query, parts[1], parts[0])
        else:
            await show_conf_ep_select(query, rest, user_id)
    # ── QR: выбор сервера/эндпоинта ──
    elif data.startswith("qr_"):
        rest = data[3:]
        if rest.startswith("srv_"):
            parts = rest[4:].split("_", 1)
            if len(parts) == 2 and parts[0].isdigit():
                await show_server_eps(query, parts[1], user_id, "qr", int(parts[0]))
        elif rest.startswith("s") and "_e" in rest:
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        await _do_send_action(query, n, "qr", eps[ei]["value"], servers[si])
        elif rest.startswith("ep_"):
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_qr(query, parts[1], parts[0])
        else:
            await show_qr_ep_select(query, rest, user_id)
    # ── Поделиться: выбор сервера/эндпоинта ──
    elif data.startswith("share_"):
        rest = data[6:]
        if rest.startswith("srv_"):
            parts = rest[4:].split("_", 1)
            if len(parts) == 2 and parts[0].isdigit():
                await show_server_eps(query, parts[1], user_id, "share", int(parts[0]))
        elif rest.startswith("s") and "_e" in rest:
            m = re.match(r's(\d+)_e(\d+)_(.*)', rest)
            if m:
                si, ei, n = int(m.group(1)), int(m.group(2)), m.group(3)
                servers = load_servers()
                if si < len(servers):
                    eps = servers[si].get("endpoints", [])
                    if ei < len(eps):
                        await _do_send_action(query, n, "share", eps[ei]["value"], servers[si])
        elif rest.startswith("ep_"):
            parts = rest[3:].split("_", 1)
            if len(parts) == 2:
                await do_send_share(query, parts[1], parts[0])
        else:
            await show_share_ep_select(query, rest, user_id)
    elif data.startswith("del_"):
        await do_delete(query, data[4:], user_id)
    elif data.startswith("confirm_del_"):
        await confirm_delete(query, data[12:], user_id)
    elif data.startswith("kick_user_") and is_admin:
        await do_kick_user(query, int(data[10:]))
    elif data.startswith("confirm_kick_") and is_admin:
        await confirm_kick_user(query, int(data[13:]))
    elif data == "sites_done":
        await apply_sites(query, user_id, context)
    elif data.startswith("ts_"):
        await toggle_site_handler(query, data[3:], user_id, context)
    elif data.startswith("cat_toggle_"):
        await toggle_category_handler(query, data[11:], context)
    elif data.startswith("sites_"):
        # sites_<name> → меню исключений; sites_add_custom перехватывается ConversationHandler раньше
        await show_sites_menu(query, data[6:], user_id, context)

# ══════════════════════════════════════════════════════════════════════════════
# МОИ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════

async def show_my_devices(query, user_id: int):
    clients = get_user_clients(user_id)
    peers   = get_awg_dump()

    if not clients:
        kb = [
            [InlineKeyboardButton("➕ Добавить первое устройство", callback_data="add")],
            [InlineKeyboardButton("◀️ В меню", callback_data="back")],
        ]
        await query.edit_message_text(
            "📱 У вас пока нет устройств.\nДобавьте первое!",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    lines = [f"📱 Ваши устройства ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
        ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
        lines.append(f"• {device_short_name(name)} | {hs} | ↓{dl} ↑{ul}")

    kb = [[InlineKeyboardButton(f"📋 {device_short_name(name)}",
           callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def show_device(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    peers = get_awg_dump()
    pub   = get_client_pub(name)
    stats = peers.get(pub, {}) if pub else {}

    short = device_short_name(name)
    hs    = fmt_handshake(stats.get("handshake", 0))
    dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
    ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
    ep    = stats.get("endpoint", "—")

    info = (
        f"📱 Устройство: *{short}*\n"
        f"👤 Пользователь: {name.split('.')[0]}\n\n"
        f"🕐 Хендшейк: {hs}\n"
        f"📍 Endpoint: {ep}\n"
        f"📶 Трафик: ↓{dl} ↑{ul}"
    )
    # Статус исключений
    saved = load_client_excl(name)
    excl_count = len(saved.get("sites", [])) if saved else 0
    excl_label = f"🌐 Исключения сайтов [{excl_count}]" if excl_count else "🌐 Исключения сайтов [нет]"

    back_target = "my_devices" if user_id != ADMIN_ID else "all_clients"
    kb = [
        [InlineKeyboardButton("📄 Скачать .conf (AmneziaWG)", callback_data=f"conf_{name}")],
        [InlineKeyboardButton("📱 QR-код (AmneziaWG)",        callback_data=f"qr_{name}")],
        [InlineKeyboardButton("📤 Поделиться кодом (AmneziaVPN)", callback_data=f"share_{name}")],
        [InlineKeyboardButton(excl_label,                      callback_data=f"sites_{name}")],
        [InlineKeyboardButton("🗑 Удалить",                    callback_data=f"del_{name}")],
        [InlineKeyboardButton("◀️ Назад",                      callback_data=back_target)],
    ]
    await query.edit_message_text(info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: ВСЕ КЛИЕНТЫ
# ══════════════════════════════════════════════════════════════════════════════

async def show_all_clients(query):
    clients = get_all_clients()
    peers   = get_awg_dump()

    if not clients:
        await query.edit_message_text("👥 Клиентов нет.", reply_markup=back_kb())
        return

    lines = [f"🌍 Все клиенты ({len(clients)}):\n"]
    for name in clients:
        pub   = get_client_pub(name)
        stats = peers.get(pub, {}) if pub else {}
        hs    = fmt_handshake(stats.get("handshake", 0))
        dl    = fmt_bytes(stats.get("tx", 0))  # tx сервера = клиент скачал (↓)
        ul    = fmt_bytes(stats.get("rx", 0))  # rx сервера = клиент отдал (↑)
        lines.append(f"• {name} | {hs} | ↓{dl} ↑{ul}")

    kb = [[InlineKeyboardButton(f"📋 {name}", callback_data=f"device_{name}")] for name in clients]
    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN: УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ══════════════════════════════════════════════════════════════════════════════

async def show_manage_users(query):
    users = load_users()
    lines = ["👥 Пользователи:\n"]

    if users["pending"]:
        lines.append("⏳ Ожидают одобрения:")
        for uid, info in users["pending"].items():
            lines.append(f"  • {info['name']} ({info['display']}) — ID: {uid}")
        lines.append("")

    if users["approved"]:
        lines.append("✅ Одобренные:")
        for uid, info in users["approved"].items():
            count = len(get_user_clients(int(uid)))
            lines.append(f"  • {info['name']} ({info['display']}) — {count} уст.")
    else:
        lines.append("✅ Одобренных пользователей пока нет.")

    kb = []
    for uid, info in users["pending"].items():
        kb.append([
            InlineKeyboardButton(f"✅ {info['name']}", callback_data=f"approve_{uid}"),
            InlineKeyboardButton(f"❌ {info['name']}", callback_data=f"reject_{uid}"),
        ])
    for uid, info in users["approved"].items():
        kb.append([InlineKeyboardButton(f"🚫 Удалить {info['name']}", callback_data=f"kick_user_{uid}")])

    kb.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))

async def do_kick_user(query, target_id: int):
    users = load_users()
    info  = users["approved"].get(str(target_id))
    if not info:
        await query.edit_message_text("⚠️ Пользователь не найден.", reply_markup=back_kb())
        return
    count = len(get_user_clients(target_id))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить всё", callback_data=f"confirm_kick_{target_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="manage_users")],
    ])
    await query.edit_message_text(
        f"🚫 Удаление пользователя *{info['name']}*\n\n"
        f"Будут удалены все его устройства: {count} шт.\n"
        f"Действие необратимо!",
        reply_markup=kb, parse_mode="Markdown"
    )

async def confirm_kick_user(query, target_id: int):
    users = load_users()
    info  = users["approved"].get(str(target_id))
    if not info:
        await query.edit_message_text("⚠️ Пользователь не найден.", reply_markup=back_kb())
        return

    for name in get_user_clients(target_id):
        remove_client_from_awg(name)

    del users["approved"][str(target_id)]
    save_users(users)

    try:
        await query.bot.send_message(
            chat_id=target_id,
            text="⛔ Ваш доступ к VPN был отозван администратором."
        )
    except:
        pass

    await query.edit_message_text(
        f"✅ Пользователь *{info['name']}* удалён со всеми устройствами.",
        reply_markup=back_kb("manage_users"), parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ВЫБОР СЕРВЕРА / ЭНДПОИНТА ДЛЯ .CONF
# ══════════════════════════════════════════════════════════════════════════════

def _server_kb(name: str, action: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора VPS-сервера. action = 'conf' | 'qr' | 'share'"""
    servers = load_servers()
    rows = []
    for i, srv in enumerate(servers):
        label = f"{srv.get('emoji', '🖥')} {srv.get('name', f'Сервер {i+1}')}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{action}_srv_{i}_{name}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"device_{name}")])
    return InlineKeyboardMarkup(rows)

def _server_eps_kb(name: str, action: str, srv_idx: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора эндпоинта внутри конкретного сервера."""
    servers = load_servers()
    srv = servers[srv_idx] if srv_idx < len(servers) else None
    rows = []
    if srv:
        for ei, ep in enumerate(srv.get("endpoints", [])):
            rows.append([InlineKeyboardButton(
                ep["value"], callback_data=f"{action}_s{srv_idx}_e{ei}_{name}"
            )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"{action}_{name}")])
    return InlineKeyboardMarkup(rows)

def _action_icon(action: str) -> str:
    return {"conf": "📄", "qr": "📱", "share": "📤"}.get(action, "📄")

def _action_label(action: str) -> str:
    return {"conf": "Скачать .conf", "qr": "QR-код", "share": "Поделиться"}.get(action, action)

async def _show_ep_select(query, name: str, user_id: int, action: str):
    """Общий экран выбора сервера/эндпоинта для заданного действия."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    short = device_short_name(name)
    icon  = _action_icon(action)

    # Если один сервер — переходим к выбору эндпоинта сразу
    if len(servers) == 1:
        eps = servers[0].get("endpoints", [])
        if len(eps) <= 1:
            ep = eps[0]["value"] if eps else SERVER_ENDPOINT
            await _do_send_action(query, name, action, ep, servers[0])
            return
        # Один сервер, несколько эндпоинтов
        srv = servers[0]
        emoji = srv.get("emoji", "🖥")
        sname = srv.get("name", "Сервер")
        await query.edit_message_text(
            f"{icon} *{_action_label(action)}* — {short}\n{emoji} {_md(sname)} — выберите эндпоинт:",
            reply_markup=_server_eps_kb(name, action, 0),
            parse_mode="Markdown"
        )
        return

    # Несколько серверов — показываем выбор VPS
    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n\nВыберите сервер:",
        reply_markup=_server_kb(name, action),
        parse_mode="Markdown"
    )

async def show_conf_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "conf")

async def show_qr_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "qr")

async def show_share_ep_select(query, name: str, user_id: int):
    await _show_ep_select(query, name, user_id, "share")

async def show_server_eps(query, name: str, user_id: int, action: str, srv_idx: int):
    """Показывает список эндпоинтов выбранного сервера."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv  = servers[srv_idx]
    eps  = srv.get("endpoints", [])
    short = device_short_name(name)
    icon  = _action_icon(action)
    emoji = srv.get("emoji", "🖥")
    sname = srv.get("name", f"Сервер {srv_idx + 1}")

    if not eps:
        await query.answer("У этого сервера нет эндпоинтов.", show_alert=True)
        return
    if len(eps) == 1:
        await _do_send_action(query, name, action, eps[0]["value"], srv)
        return
    await query.edit_message_text(
        f"{icon} *{_action_label(action)}* — {short}\n{emoji} {_md(sname)} — выберите эндпоинт:",
        reply_markup=_server_eps_kb(name, action, srv_idx),
        parse_mode="Markdown"
    )

async def _do_send_action(query, name: str, action: str, ep: str, server: dict):
    """Выполняет нужное действие (conf/qr/share) с конкретным эндпоинтом и сервером."""
    spub = server.get("awg_public_key") or SERVER_PUBLIC
    sprt = str(server.get("awg_port") or SERVER_PORT)
    if action == "conf":
        await do_send_conf_direct(query, name, ep, spub, sprt)
    elif action == "qr":
        await do_send_qr_direct(query, name, ep, spub, sprt)
    else:
        await do_send_share_direct(query, name, ep, spub, sprt)

# ── Финальная отправка .conf ──────────────────────────────────────────────────

async def do_send_conf(query, name: str, ep_key: str):
    """Генерирует .conf в памяти (с сохранёнными исключениями) и отправляет в чат."""
    allowed_ips = get_allowed_ips_for_client(name)
    short    = device_short_name(name)
    ep       = resolve_endpoint(ep_key)
    content  = (make_conf_for_client(name, ep, allowed_ips) or "").encode()
    filename = f"{name}.conf"
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    await query.message.reply_document(
        document=content,
        filename=filename,
        caption=(
            f"📄 Конфиг *{short}* ({ep_label})\n"
            f"🌐 Endpoint: `{ep}:{SERVER_PORT}`{excl_note}\n\n"
            f"Импортируйте в AmneziaWG."
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ── Финальная отправка QR ─────────────────────────────────────────────────────

async def do_send_qr(query, name: str, ep_key: str):
    """Генерирует .conf в памяти (с сохранёнными исключениями) → QR → отправляет."""
    allowed_ips = get_allowed_ips_for_client(name)
    short   = device_short_name(name)
    ep      = resolve_endpoint(ep_key)
    content = (make_conf_for_client(name, ep, allowed_ips) or "").encode()
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)

    # Пишем во временный файл только для qrencode — сразу удаляем
    tmp_conf = f"/tmp/qr_{name}_{ep_key}.conf"
    qr_path  = f"/tmp/qr_{name}_{ep_key}.png"
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        with open(tmp_conf, "wb") as f:
            f.write(content)
        # Сначала .conf файл, затем QR
        safe_name = name.replace(".", "_")
        await query.message.reply_document(
            document=open(tmp_conf, "rb"),
            filename=f"{safe_name}_{ep_key}.conf",
            caption=f"📄 .conf для AmneziaWG — *{short}* ({ep_label}){excl_note}",
            parse_mode="Markdown"
        )
        if len(content) > 2900:
            await query.message.reply_text(
                "ℹ️ QR-код недоступен: конфиг слишком большой из-за исключений сайтов.\n"
                "Используйте *.conf* файл выше.",
                parse_mode="Markdown"
            )
        else:
            subprocess.run([QRENCODE_BIN, "-o", qr_path, "-r", tmp_conf], check=True)
            await query.message.reply_photo(
                photo=open(qr_path, "rb"),
                caption=(
                    f"📱 QR для AmneziaWG — *{short}* ({ep_label})\n"
                    f"🌐 Endpoint: `{ep}:{SERVER_PORT}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    finally:
        for p in [tmp_conf, qr_path]:
            if os.path.exists(p):
                os.remove(p)
    await show_device(query, name, query.from_user.id)

# ── Финальная отправка vpn:// (AmneziaVPN) ───────────────────────────────────

async def do_send_share(query, name: str, ep_key: str):
    """Генерирует vpn:// ссылку и .vpn файл на лету, отправляет в чат."""
    keys = get_client_keys(name)
    if not keys:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short    = device_short_name(name)
    ep       = resolve_endpoint(ep_key)
    ep_label = {"main": "Основной", "backup": "Резервный", "ip": "По IP"}.get(ep_key, ep_key)
    vpn_link = make_vpn_link(
        keys["priv"], keys["pub"], keys["ip"], keys["psk"],
        keys.get("obfs", gen_obfs()), name, endpoint=ep
    )
    vpn_bytes = vpn_link.encode()
    # Сначала ссылка — потом файл
    await query.message.reply_text(
        f"🔗 Ссылка AmneziaVPN *{short}* ({ep_label})\n"
        f"🌐 Endpoint: `{ep}:{SERVER_PORT}`\n\n"
        f"Нажмите чтобы скопировать:\n`{vpn_link}`",
        parse_mode="Markdown"
    )
    await query.message.reply_document(
        document=vpn_bytes,
        filename=f"{name}.vpn",
        caption=(
            f"📤 Файл для AmneziaVPN — *{short}* ({ep_label})\n"
            f"Вставьте в приложении: + → Открыть файл"
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ── Прямая отправка с конкретным эндпоинтом (мультисервер) ───────────────────

async def do_send_conf_direct(query, name: str, ep: str,
                               spub: str = None, sprt: str = None):
    """Отправляет .conf с указанным эндпоинтом и параметрами сервера."""
    allowed_ips = get_allowed_ips_for_client(name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips) or "").encode()
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    await query.message.reply_document(
        document=content,
        filename=f"{name}.conf",
        caption=(
            f"📄 Конфиг *{short}*\n"
            f"🌐 Endpoint: `{ep}:{prt}`{excl_note}\n\n"
            f"Импортируйте в AmneziaWG."
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

async def do_send_qr_direct(query, name: str, ep: str,
                             spub: str = None, sprt: str = None):
    """Отправляет QR с указанным эндпоинтом."""
    allowed_ips = get_allowed_ips_for_client(name)
    short   = device_short_name(name)
    prt     = sprt or SERVER_PORT
    content = (make_conf_for_client_ep(name, ep, spub, sprt, allowed_ips) or "").encode()
    tmp_conf = f"/tmp/qr_{name}_direct.conf"
    qr_path  = f"/tmp/qr_{name}_direct.png"
    excl_note = "" if allowed_ips == "0.0.0.0/0" else "\n🌐 С исключениями сайтов"
    try:
        with open(tmp_conf, "wb") as f:
            f.write(content)
        safe_name = name.replace(".", "_")
        await query.message.reply_document(
            document=open(tmp_conf, "rb"),
            filename=f"{safe_name}.conf",
            caption=f"📄 .conf для AmneziaWG — *{short}*{excl_note}",
            parse_mode="Markdown"
        )
        if len(content) > 2900:
            await query.message.reply_text(
                "ℹ️ QR-код недоступен: конфиг слишком большой из-за исключений сайтов.\n"
                "Используйте *.conf* файл выше.",
                parse_mode="Markdown"
            )
        else:
            subprocess.run([QRENCODE_BIN, "-o", qr_path, "-r", tmp_conf], check=True)
            await query.message.reply_photo(
                photo=open(qr_path, "rb"),
                caption=(
                    f"📱 QR для AmneziaWG — *{short}*\n"
                    f"🌐 Endpoint: `{ep}:{prt}`{excl_note}"
                ),
                parse_mode="Markdown"
            )
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка QR: {e}")
    finally:
        for p in [tmp_conf, qr_path]:
            if os.path.exists(p):
                os.remove(p)
    await show_device(query, name, query.from_user.id)

async def do_send_share_direct(query, name: str, ep: str,
                                spub: str = None, sprt: str = None):
    """Отправляет vpn:// ссылку с указанным эндпоинтом."""
    keys = get_client_keys(name)
    if not keys:
        await query.message.reply_text(f"❌ Не удалось прочитать ключи для {name}")
        return
    short    = device_short_name(name)
    prt      = sprt or SERVER_PORT
    vpn_link = make_vpn_link(
        keys["priv"], keys["pub"], keys["ip"], keys["psk"],
        keys.get("obfs", gen_obfs()), name,
        endpoint=ep, server_public=spub, server_port=sprt
    )
    vpn_bytes = vpn_link.encode()
    await query.message.reply_text(
        f"🔗 Ссылка AmneziaVPN *{short}*\n"
        f"🌐 Endpoint: `{ep}:{prt}`\n\n"
        f"Нажмите чтобы скопировать:\n`{vpn_link}`",
        parse_mode="Markdown"
    )
    await query.message.reply_document(
        document=vpn_bytes,
        filename=f"{name}.vpn",
        caption=(
            f"📤 Файл для AmneziaVPN — *{short}*\n"
            f"Вставьте в приложении: + → Открыть файл"
        ),
        parse_mode="Markdown"
    )
    await show_device(query, name, query.from_user.id)

# ══════════════════════════════════════════════════════════════════════════════
# УДАЛЕНИЕ УСТРОЙСТВА
# ══════════════════════════════════════════════════════════════════════════════

async def do_delete(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    if not os.path.exists(f"{CLIENTS_DIR}/{name}.conf"):
        await query.edit_message_text("❌ Устройство не найдено.", reply_markup=back_kb())
        return

    short = device_short_name(name)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del_{name}")],
        [InlineKeyboardButton("❌ Отмена",      callback_data=f"device_{name}")],
    ])
    await query.edit_message_text(
        f"🗑 Удалить устройство *{short}*?\n\nЭто действие необратимо.",
        reply_markup=kb, parse_mode="Markdown"
    )

async def confirm_delete(query, name: str, user_id: int):
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    remove_client_from_awg(name)
    short = device_short_name(name)
    await query.edit_message_text(
        f"✅ Устройство *{short}* удалено.",
        reply_markup=back_kb("my_devices"), parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════════════════════════════
# УПРАВЛЕНИЕ СЕРВЕРАМИ (АДМИНИСТРАТОР)
# ══════════════════════════════════════════════════════════════════════════════

async def show_servers_list(query):
    """Список всех VPS-серверов."""
    servers = load_servers()
    lines = ["🖥 *Серверы*\n"]
    for i, srv in enumerate(servers):
        emoji = srv.get("emoji", "🖥")
        name  = srv.get("name", f"Сервер {i+1}")
        primary_mark = " *(PRIMARY)*" if srv.get("is_primary") else ""
        eps = srv.get("endpoints", [])
        ep_list = ", ".join(e["value"] for e in eps) if eps else "нет"
        lines.append(f"{emoji} {name}{primary_mark}\n   Эндпоинты: {ep_list}")
    rows = []
    for i, srv in enumerate(servers):
        emoji = srv.get("emoji", "🖥")
        sname = srv.get("name", f"Сервер {i+1}")
        rows.append([InlineKeyboardButton(
            f"{emoji} {sname}", callback_data=f"srv_card_{i}"
        )])
    rows.append([InlineKeyboardButton("➕ Добавить сервер", callback_data="srv_add")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )

async def show_server_card(query, srv_idx: int):
    """Карточка конкретного VPS."""
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv    = servers[srv_idx]
    emoji  = srv.get("emoji", "🖥")
    name   = srv.get("name", f"Сервер {srv_idx+1}")
    is_pri = srv.get("is_primary", False)
    ssh    = srv.get("ssh", {})
    port   = srv.get("awg_port", SERVER_PORT)

    eps = srv.get("endpoints", [])
    ep_lines = []
    for ep in eps:
        t = "🌐" if ep.get("type") == "domain" else "🔢"
        v_mark = " ✅" if ep.get("verified") else ""
        ep_lines.append(f"  {t} {ep['value']}{v_mark}")
    ep_text = "\n".join(ep_lines) if ep_lines else "  нет"

    text = (
        f"{emoji} *{name}*" + (" _(PRIMARY)_" if is_pri else "") + "\n"
        f"SSH: `{ssh.get('login', 'root')}@{ssh.get('ip', '—')}:{ssh.get('port', 22)}`\n"
        f"AWG-порт: `{port}`\n\n"
        f"*Эндпоинты:*\n{ep_text}"
    )
    rows = [
        [InlineKeyboardButton("➕ Добавить домен", callback_data=f"srv_adddomain_{srv_idx}")],
    ]
    if not is_pri:
        rows.append([InlineKeyboardButton("🗑 Удалить сервер", callback_data=f"srv_del_{srv_idx}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="servers")])
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="Markdown"
    )

def _ssh_read_slave_env(ip: str, port: int, login: str, password: str) -> dict:
    """Подключается к slave по SSH и читает /etc/amnezia/amneziawg/server.env.
    Возвращает dict с ключами awg_public_key и awg_port, либо бросает исключение."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, port=port, username=login, password=password, timeout=10, banner_timeout=15)
    try:
        _, stdout, _ = client.exec_command(
            "cat /etc/amnezia/amneziawg/server.env", timeout=10
        )
        raw = stdout.read().decode()
    finally:
        client.close()

    result = {}
    for line in raw.splitlines():
        if line.startswith("SERVER_PUBLIC="):
            result["awg_public_key"] = line.split("=", 1)[1].strip()
        elif line.startswith("SERVER_PORT="):
            try:
                result["awg_port"] = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
    if not result.get("awg_public_key"):
        raise ValueError("SERVER_PUBLIC не найден в server.env — возможно, AmneziaWG не установлен на этом сервере")
    return result


async def srv_add_start(update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога добавления сервера."""
    query = update.callback_query
    await query.answer()
    context.user_data["srv_add"] = {}
    await query.edit_message_text(
        "🖥 *Добавление сервера*\n\nВведите IP-адрес нового сервера:",
        parse_mode="Markdown"
    )
    return WAITING_SRV_IP

async def srv_add_ip(update, context: ContextTypes.DEFAULT_TYPE):
    ip = update.message.text.strip()
    context.user_data["srv_add"]["ip"] = ip
    await update.message.reply_text(
        f"IP: `{ip}`\n\nВведите SSH-порт (Enter = 22):",
        parse_mode="Markdown"
    )
    return WAITING_SRV_PORT

async def srv_add_port(update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    port = int(text) if text.isdigit() else 22
    context.user_data["srv_add"]["port"] = port
    await update.message.reply_text("Введите логин SSH:")
    return WAITING_SRV_LOGIN

async def srv_add_login(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["srv_add"]["login"] = update.message.text.strip()
    await update.message.reply_text("Введите пароль SSH:")
    return WAITING_SRV_PASSWORD

async def srv_add_password(update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["srv_add"]
    d["password"] = update.message.text.strip()

    if not _PARAMIKO_AVAILABLE:
        await update.message.reply_text("Введите название сервера (например: Германия):")
        return WAITING_SRV_NAME

    ip    = d.get("ip", "")
    port  = d.get("port", 22)
    login = d.get("login", "root")
    pwd   = d["password"]

    status_msg = await update.message.reply_text(
        f"🔌 Подключаюсь к `{ip}:{port}`…", parse_mode="Markdown"
    )
    try:
        env_data = await asyncio.get_event_loop().run_in_executor(
            None, _ssh_read_slave_env, ip, port, login, pwd
        )
        d["awg_public_key"] = env_data["awg_public_key"]
        d["awg_port"]       = env_data.get("awg_port", 51820)
        await status_msg.edit_text(
            f"✅ Подключение успешно!\n"
            f"🔑 AWG Public Key: `{d['awg_public_key']}`\n"
            f"🔌 AWG Port: `{d['awg_port']}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Не удалось подключиться к `{ip}:{port}`:\n`{e}`\n\n"
            f"Проверьте данные SSH и попробуйте ещё раз, или /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_SRV_PASSWORD

    await update.message.reply_text("Введите название сервера (например: Германия):")
    return WAITING_SRV_NAME

async def srv_add_name(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["srv_add"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите эмодзи/флаг (например: 🇩🇪), или Enter для 🖥:"
    )
    return WAITING_SRV_EMOJI

async def srv_add_emoji(update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    emoji = text if text else "🖥"
    d = context.user_data.pop("srv_add", {})
    d["emoji"] = emoji

    servers = load_servers()
    new_srv = {
        "id": f"server_{len(servers)}",
        "name": d.get("name", "Сервер"),
        "emoji": d.get("emoji", "🖥"),
        "is_primary": False,
        "ssh": {
            "ip": d.get("ip", ""),
            "port": d.get("port", 22),
            "login": d.get("login", "root"),
            "password": d.get("password", ""),
        },
        "awg_public_key": d.get("awg_public_key") or SERVER_PUBLIC,
        "awg_port": d.get("awg_port") or (int(SERVER_PORT) if SERVER_PORT else 51820),
        "endpoints": [{"value": d.get("ip", ""), "type": "ip"}],
    }
    servers.append(new_srv)
    save_servers(servers)

    await update.message.reply_text(
        f"✅ Сервер *{d.get('emoji', '🖥')} {d.get('name', 'Сервер')}* добавлен!\n\n"
        f"IP `{d.get('ip', '')}` добавлен как первый эндпоинт.\n"
        f"Откройте карточку сервера чтобы добавить домены.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🖥 Серверы", callback_data="servers")
        ]])
    )
    return ConversationHandler.END

async def srv_add_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("srv_add", None)
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Отмена.", reply_markup=back_kb("servers"))
    else:
        await update.message.reply_text("Отмена.")
    return ConversationHandler.END

async def srv_del_confirm(query, srv_idx: int):
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers[srv_idx]
    if srv.get("is_primary"):
        await query.answer("Нельзя удалить PRIMARY сервер.", show_alert=True)
        return
    emoji = srv.get("emoji", "🖥")
    name  = srv.get("name", f"Сервер {srv_idx+1}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data=f"srv_del_ok_{srv_idx}")],
        [InlineKeyboardButton("◀️ Назад",        callback_data=f"srv_card_{srv_idx}")],
    ])
    await query.edit_message_text(
        f"Удалить сервер *{emoji} {name}*?",
        reply_markup=kb, parse_mode="Markdown"
    )

async def srv_del_ok(query, srv_idx: int):
    servers = load_servers()
    if srv_idx >= len(servers):
        await query.answer("Сервер не найден.", show_alert=True)
        return
    srv = servers.pop(srv_idx)
    save_servers(servers)
    name = srv.get("name", "Сервер")
    await query.edit_message_text(
        f"✅ Сервер *{name}* удалён.",
        reply_markup=back_kb("servers"),
        parse_mode="Markdown"
    )

# ── Добавление домена к серверу ───────────────────────────────────────────────

async def srv_adddomain_start(update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога добавления домена к серверу."""
    query = update.callback_query
    await query.answer()
    srv_idx = int(query.data.split("_")[-1])
    context.user_data["srv_domain"] = {"srv_idx": srv_idx}
    await query.edit_message_text(
        "🌐 *Добавить домен*\n\nВведите домен (например: vpn.example.com):",
        parse_mode="Markdown"
    )
    return WAITING_SRV_DOMAIN

async def srv_adddomain_receive(update, context: ContextTypes.DEFAULT_TYPE):
    """Получает домен, проверяет DNS, добавляет к серверу."""
    import socket as _sock
    domain = update.message.text.strip().lower().lstrip("https://").lstrip("http://").split("/")[0]
    d = context.user_data.pop("srv_domain", {})
    srv_idx = d.get("srv_idx", 0)

    servers = load_servers()
    if srv_idx >= len(servers):
        await update.message.reply_text("❌ Сервер не найден.")
        return ConversationHandler.END
    srv = servers[srv_idx]

    # Попытка DNS-резолва
    verified = False
    dns_ip   = None
    try:
        dns_ip = _sock.gethostbyname(domain)
        # Проверяем совпадение с IP этого или любого сервера
        for s in servers:
            ssh_ip = s.get("ssh", {}).get("ip", "")
            if dns_ip == ssh_ip:
                verified = True
                break
    except Exception:
        pass

    eps = srv.get("endpoints", [])
    # Не добавлять дубль
    if any(e["value"] == domain for e in eps):
        await update.message.reply_text(
            f"⚠️ Домен `{domain}` уже есть у этого сервера.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Карточка", callback_data=f"srv_card_{srv_idx}")
            ]])
        )
        return ConversationHandler.END

    eps.append({"value": domain, "type": "domain", "verified": verified})
    srv["endpoints"] = eps
    servers[srv_idx] = srv
    save_servers(servers)

    dns_note = f"✅ DNS → `{dns_ip}` (совпадает)" if verified else (
        f"⚠️ DNS → `{dns_ip}` (не совпадает с IP сервера)" if dns_ip else "⚠️ DNS не разрешился"
    )
    await update.message.reply_text(
        f"✅ Домен `{domain}` добавлен к серверу *{srv.get('name', '')}*\n{dns_note}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Карточка", callback_data=f"srv_card_{srv_idx}")
        ]])
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# СТАТУС, ОЧИСТКА, ИНСТРУКЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def show_status(query):
    peers  = get_awg_dump()
    now    = int(time.time())
    online = sum(1 for p in peers.values() if p.get("handshake") and now - p["handshake"] < 180)
    sys    = get_system_stats()
    total_dl  = sum(p.get("tx", 0) for p in peers.values())  # tx сервера = клиенты скачали (↓)
    total_ul  = sum(p.get("rx", 0) for p in peers.values())  # rx сервера = клиенты отдали (↑)
    users     = load_users()

    load = sys["load"]
    text = (
        f"📊 Статус сервера\n\n"
        f"🟢 AWG: работает\n"
        f"🖥 IP: {SERVER_IP}:{SERVER_PORT}\n"
        f"⏱ Uptime: {sys['uptime']}\n\n"
        f"📈 Load: {load[0]} {load[1]} {load[2]}\n"
        f"💾 RAM: {sys['ram_used']}/{sys['ram_total']} MB\n"
        f"💿 Диск: {sys['disk_used']}/{sys['disk_total']} ({sys['disk_pct']}%)\n\n"
        f"👤 Клиентов: {len(get_all_clients())}\n"
        f"👥 Пользователей: {len(users['approved'])}\n"
        f"🟢 Онлайн: {online}\n"
        f"📶 Трафик (с перезагрузки): ↓{fmt_bytes(total_dl)} ↑{fmt_bytes(total_ul)}"
    )

    is_admin = (query.from_user.id == ADMIN_ID)
    kb_rows = [[InlineKeyboardButton("🔄 Перезапустить бота", callback_data="restart_bot")]]
    if is_admin:
        kb_rows.append([InlineKeyboardButton("⚡ Перезапустить AWG",  callback_data="restart_awg")])
        kb_rows.append([InlineKeyboardButton("📈 Трафик / пики",      callback_data="bandwidth")])
    kb_rows.append([InlineKeyboardButton("◀️ В меню", callback_data="back")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))

async def do_diagnostics(query):
    """Диагностика конфигов: orphan peers, orphan files, orphan excl.json."""
    await query.edit_message_text("⏳ Проверяю конфиги...")

    peers      = get_awg_dump()
    clients    = get_all_clients()
    known_pubs = {get_client_pub(n) for n in clients} - {None}
    report     = []
    removed_peers = 0
    removed_excl  = 0

    # 1. Пиры в AWG без файлов в CLIENTS_DIR
    orphan_peers = [pub for pub in peers if pub not in known_pubs]
    if orphan_peers:
        for pub in orphan_peers:
            if subprocess.run(["awg", "set", AWG_IFACE, "peer", pub, "remove"]).returncode == 0:
                removed_peers += 1
        report.append(f"🔴 Пиров без конфига: {len(orphan_peers)} → удалено {removed_peers}")
    else:
        report.append("✅ Пиры в AWG — всё в порядке")

    # 2. Файлы .conf без пира в AWG
    if os.path.isdir(CLIENTS_DIR):
        conf_files = [f[:-5] for f in os.listdir(CLIENTS_DIR) if f.endswith(".conf")]
        orphan_files = [n for n in conf_files if get_client_pub(n) not in peers]
        if orphan_files:
            report.append(f"⚠️ Конфиги без пира в AWG ({len(orphan_files)}):\n" +
                          "\n".join(f"  • {n}" for n in orphan_files))
        else:
            report.append("✅ Конфиги — всё в порядке")

        # 3. Orphan .excl.json без соответствующего .conf
        excl_files = [f[:-len(EXCL_EXT)] for f in os.listdir(CLIENTS_DIR) if f.endswith(EXCL_EXT)]
        orphan_excl = [n for n in excl_files if not os.path.exists(f"{CLIENTS_DIR}/{n}.conf")]
        if orphan_excl:
            for n in orphan_excl:
                try:
                    os.remove(f"{CLIENTS_DIR}/{n}{EXCL_EXT}")
                    removed_excl += 1
                except Exception:
                    pass
            report.append(f"🗑 Мусорных excl.json: {len(orphan_excl)} → удалено {removed_excl}")
        else:
            report.append("✅ Файлы исключений — всё в порядке")

    text = "🔍 Диагностика конфигов\n\n" + "\n".join(report)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Техобслуживание", callback_data="maintenance")]
    ]))

# ══════════════════════════════════════════════════════════════════════════════
# ВОССТАНОВЛЕНИЕ ИЗ БЭКАПА
# ══════════════════════════════════════════════════════════════════════════════

async def start_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — предупреждение и запрос файла"""
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="restore_cancel")],
    ])
    await query.edit_message_text(
        "📥 Восстановление из бэкапа\n\n"
        "⚠️ *Внимание* — текущие конфиги будут перезаписаны!\n"
        "Используйте только при переезде на новый сервер.\n\n"
        "Перед восстановлением будет автоматически создан бэкап текущего состояния.\n\n"
        "Отправьте файл бэкапа (`awg_backup_*.tar.gz`) в этот чат.\n\n"
        "Для отмены нажмите кнопку ниже или напишите /cancel",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return WAITING_RESTORE_FILE


async def cancel_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена восстановления через кнопку"""
    query = update.callback_query
    await query.answer()
    tmp_path = context.user_data.pop("restore_path", None)
    if tmp_path and os.path.exists(tmp_path):
        os.remove(tmp_path)
    await query.edit_message_text("❌ Восстановление отменено.", reply_markup=back_kb())
    return ConversationHandler.END

async def receive_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — получаем файл, показываем что внутри и просим подтверждение"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".tar.gz"):
        await update.message.reply_text(
            "❌ Ожидается файл `.tar.gz`\n\nОтправьте файл бэкапа или напишите /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_RESTORE_FILE

    await update.message.reply_text("⏳ Проверяю бэкап...")

    # Скачиваем во временный файл
    tmp_path = f"/tmp/restore_{int(time.time())}.tar.gz"
    tg_file  = await doc.get_file()
    await tg_file.download_to_drive(tmp_path)

    # Проверяем содержимое архива
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            names = tar.getnames()
    except Exception as e:
        os.remove(tmp_path)
        await update.message.reply_text(f"❌ Не удалось открыть архив: {e}")
        return ConversationHandler.END

    # Ищем ключевые файлы
    has_conf    = any(n.endswith(".conf") and "awg" in n for n in names)
    has_env     = "server.env" in names
    has_clients = any(n.startswith("clients/") for n in names)
    clients_count = len([n for n in names if n.startswith("clients/") and n.endswith(".conf")])
    has_users   = "users.json" in names

    if not has_conf or not has_env:
        os.remove(tmp_path)
        await update.message.reply_text(
            "❌ Файл не похож на бэкап AmneziaWG.\n"
            "Не найдены обязательные файлы (конфиг интерфейса, server.env)."
        )
        return ConversationHandler.END

    # Сохраняем путь к файлу в user_data
    context.user_data["restore_path"] = tmp_path

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить восстановление", callback_data="restore_confirm")],
        [InlineKeyboardButton("❌ Отмена",                     callback_data="restore_cancel")],
    ])
    await update.message.reply_text(
        f"📦 Содержимое бэкапа:\n\n"
        f"{'✅' if has_conf else '❌'} Конфиг интерфейса\n"
        f"{'✅' if has_env else '❌'} server.env\n"
        f"{'✅' if has_clients else '❌'} Клиенты: {clients_count} шт.\n"
        f"{'✅' if has_users else '⚠️'} users.json {'(найден)' if has_users else '(не найден — пользователи бота не восстановятся)'}\n\n"
        f"⚠️ Текущие конфиги будут перезаписаны.\n"
        f"Сначала будет создан автобэкап текущего состояния.",
        reply_markup=kb
    )
    return WAITING_RESTORE_FILE

async def confirm_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3 — подтверждение через callback кнопку"""
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if user_id != ADMIN_ID:
        return ConversationHandler.END

    tmp_path = context.user_data.get("restore_path")
    if not tmp_path or not os.path.exists(tmp_path):
        await query.edit_message_text("❌ Файл бэкапа не найден. Начните заново.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Создаю бэкап текущего состояния...")

    # Автобэкап перед восстановлением
    try:
        auto_backup = create_backup(prefix="pre_restore")
    except Exception as e:
        await query.message.reply_text(f"⚠️ Не удалось создать автобэкап: {e}\nВосстановление отменено.")
        os.remove(tmp_path)
        return ConversationHandler.END

    await query.message.reply_text("⏳ Восстанавливаю конфиги...")

    # 1. Останавливаем AWG — PostDown почистит iptables по живому конфигу
    subprocess.run(["systemctl", "stop", f"awg-quick@{AWG_IFACE}"])

    # 2. Чистим clients/ чтобы не осталось мусора от старых клиентов
    if os.path.exists(CLIENTS_DIR):
        shutil.rmtree(CLIENTS_DIR)
    os.makedirs(CLIENTS_DIR)

    # 3. Распаковываем бэкап
    try:
        with tarfile.open(tmp_path, "r:gz") as tar:
            tar.extractall("/etc/amnezia/amneziawg/")
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка при распаковке: {e}")
        os.remove(tmp_path)
        return ConversationHandler.END

    os.remove(tmp_path)
    context.user_data.pop("restore_path", None)

    await query.message.reply_text(
        f"✅ Конфиги восстановлены!\n\n"
        f"Автобэкап сохранён: `{auto_backup}`\n\n"
        f"⏳ Перезапускаю AWG и бота...",
        parse_mode="Markdown"
    )

    # 4. Поднимаем AWG с новым конфигом, перезапускаем бота
    subprocess.Popen(
        ["bash", "-c",
         f"sleep 2 && systemctl start awg-quick@{AWG_IFACE} && systemctl restart {BOT_SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ТЕХОБСЛУЖИВАНИЕ
# ══════════════════════════════════════════════════════════════════════════════

def get_ptb_version() -> str:
    try:
        import telegram
        return telegram.__version__
    except:
        return "неизвестно"

async def show_maintenance(query):
    m         = get_maintenance()
    last_date = m.get("last_date") or "никогда"
    ptb_ver   = get_ptb_version()
    ubuntu    = get_ubuntu_version()
    kernel    = get_kernel_version()

    # Текущий часовой пояс
    try:
        tz_sys = subprocess.check_output(["cat", "/etc/timezone"], text=True).strip()
    except:
        tz_sys = TZ

    text = (
        f"🔧 Техобслуживание\n\n"
        f"📅 Последнее: {last_date}\n\n"
        f"🖥 Система: {ubuntu}\n"
        f"⚙️ Ядро: {kernel}\n"
        f"🐍 python-telegram-bot: {ptb_ver}\n"
        f"🕐 Часовой пояс: {tz_sys} (бот: {TZ})\n\n"
        f"Рекомендуется проводить раз в 6 месяцев."
    )
    # Проверяем актуальность IP для отображения в меню
    real_ip = get_real_server_ip()
    ip_status = ""
    if real_ip and real_ip != SERVER_IP:
        ip_status = f"\n\n⚠️ IP расходится!\nВ настройках: {SERVER_IP}\nРеальный: {real_ip}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Создать бэкап",               callback_data="backup")],
        [InlineKeyboardButton("📥 Восстановить из бэкапа",      callback_data="restore")],
        [InlineKeyboardButton("🔍 Диагностика конфигов",        callback_data="diagnostics")],
        [InlineKeyboardButton("💿 Бэкап + apt upgrade",         callback_data="maint_upgrade")],
        [InlineKeyboardButton("📦 Проверить версию библиотеки", callback_data="maint_ptb")],
        [InlineKeyboardButton("🕐 Сменить часовой пояс",        callback_data="maint_tz")],
        [InlineKeyboardButton("🔄 Обновить IP сервера",         callback_data="maint_update_ip")],
        [InlineKeyboardButton("✅ Отмечено — всё ок",            callback_data="maint_done")],
        [InlineKeyboardButton("◀️ В меню",                       callback_data="back")],
    ])
    await query.edit_message_text(text + ip_status, reply_markup=kb)

async def show_maint_tz(query):
    """Экран выбора часового пояса"""
    popular_tz = [
        ("🇷🇺 Москва",       "Europe/Moscow"),
        ("🇷🇺 Екатеринбург", "Asia/Yekaterinburg"),
        ("🇷🇺 Новосибирск",  "Asia/Novosibirsk"),
        ("🇷🇺 Владивосток",  "Asia/Vladivostok"),
        ("🇺🇦 Киев",         "Europe/Kiev"),
        ("🇰🇿 Алматы",       "Asia/Almaty"),
        ("🇩🇪 Берлин",       "Europe/Berlin"),
        ("🌍 UTC",           "UTC"),
    ]
    try:
        sys_tz = subprocess.check_output(["cat", "/etc/timezone"], text=True).strip()
    except:
        sys_tz = "неизвестно"

    now_local = time.strftime("%H:%M %d.%m.%Y")
    kb_rows = []
    for label, tz in popular_tz:
        mark = "✅ " if tz == TZ else ""
        kb_rows.append([InlineKeyboardButton(
            f"{mark}{label}", callback_data=f"set_tz_{tz}"
        )])
    mark_sys = "✅ " if sys_tz == TZ else ""
    kb_rows.append([InlineKeyboardButton(
        f"{mark_sys}🖥 Как у сервера ({sys_tz})", callback_data=f"set_tz_{sys_tz}"
    )])
    kb_rows.append([InlineKeyboardButton("⌨️ Ввести вручную", callback_data="set_tz_manual")])
    kb_rows.append([InlineKeyboardButton("◀️ Назад", callback_data="maintenance")])
    await query.edit_message_text(
        f"🕐 Часовой пояс\n\n"
        f"Текущий бота: *{TZ}*\n"
        f"Системный:    *{sys_tz}*\n"
        f"Время бота:   {now_local}\n\n"
        f"Выберите из списка, укажите как у сервера, или введите вручную.\n"
        f"Бот перезапустится автоматически.",
        reply_markup=InlineKeyboardMarkup(kb_rows),
        parse_mode="Markdown"
    )

async def ask_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просим ввести пояс вручную"""
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="maint_tz")],
    ])
    await query.edit_message_text(
        "⌨️ *Ввод часового пояса вручную*\n\n"
        "Напишите код пояса в этот чат, например:\n"
        "`Europe/Helsinki` `Europe/Moscow` `UTC`\n"
        "`Asia/Tokyo` `America/New_York`\n\n"
        "Полный список: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones\n\n"
        "Или нажмите *Отмена* для возврата.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    return WAITING_TZ_INPUT

async def receive_tz_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем ручной ввод пояса и применяем"""
    tz = update.message.text.strip()
    try:
        all_tz = subprocess.check_output(["timedatectl", "list-timezones"], text=True).splitlines()
    except:
        all_tz = []
    if all_tz and tz not in all_tz:
        await update.message.reply_text(
            f"❌ Часовой пояс `{tz}` не найден.\n\n"
            f"Проверьте написание и попробуйте снова, или /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_TZ_INPUT
    # Применяем
    try:
        env_lines = open(ENV_FILE).readlines()
        new_lines = [l for l in env_lines if not l.startswith("TIMEZONE=")]
        new_lines.append(f"TIMEZONE={tz}\n")
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        subprocess.run(["timedatectl", "set-timezone", tz], check=True)
        await update.message.reply_text(
            f"✅ Часовой пояс изменён на *{tz}*\n\nБот перезапускается...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", f"sleep 2 && systemctl restart {BOT_SERVICE}"])
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    return ConversationHandler.END


async def do_set_tz(query, tz: str):
    """Применяет новый часовой пояс — пишет в server.env и перезапускает бота"""
    try:
        # Проверяем что пояс существует
        subprocess.check_output(["timedatectl", "list-timezones"], text=True)
        # Пишем в server.env
        env_lines = open(ENV_FILE).readlines()
        new_lines = [l for l in env_lines if not l.startswith("TIMEZONE=")]
        new_lines.append(f"TIMEZONE={tz}\n")
        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)
        # Устанавливаем системный часовой пояс тоже
        subprocess.run(["timedatectl", "set-timezone", tz], check=True)
        await query.edit_message_text(
            f"✅ Часовой пояс изменён на *{tz}*\n\nБот перезапустится через 3 секунды...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", f"sleep 3 && systemctl restart {BOT_SERVICE}"])
    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка: {e}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="maintenance")
            ]])
        )

async def do_maint_upgrade(query):
    """Бэкап + apt upgrade + перезапуск бота"""
    await do_backup(query)
    await query.message.reply_text(
        "⏳ Запускаю apt upgrade...\n\nЭто займёт пару минут. Бот перезапустится автоматически."
    )
    subprocess.Popen(
        ["bash", "-c", f"apt-get update -qq && apt-get upgrade -y -qq && systemctl restart {BOT_SERVICE}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

async def do_maint_ptb(query):
    ptb_ver = get_ptb_version()
    text = (
        f"📦 python-telegram-bot\n\n"
        f"Установлена: *{ptb_ver}*\n\n"
        f"Список релизов и Breaking Changes:\n"
        f"https://github.com/python-telegram-bot/python-telegram-bot/releases\n\n"
        f"Если мажорная версия не изменилась (например всё ещё 20.x) — "
        f"достаточно нажать «Бэкап + apt upgrade».\n\n"
        f"Если мажорная версия выросла (20.x → 21.x) — загляните в Breaking Changes. "
        f"Скорее всего потребуется небольшая правка bot.py."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="maintenance")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

async def do_maint_done(query):
    log_maintenance_done()
    await query.edit_message_text(
        f"✅ Техобслуживание отмечено\n\nДата: {now}\nСледующее напоминание через 6 месяцев.",
        reply_markup=back_kb()
    )

async def maintenance_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Напоминание раз в 6 месяцев — запускается через job_queue"""
    m       = get_maintenance()
    last_ts = m.get("last_ts", 0)
    now_ts  = int(time.time())
    # 6 месяцев = 183 дня
    if now_ts - last_ts < 183 * 86400:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 Перейти к обслуживанию", callback_data="maintenance")],
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "🔔 Напоминание о техобслуживании\n\n"
            "Прошло 6 месяцев с последнего обслуживания.\n"
            "Рекомендуется сделать бэкап и обновить систему."
        ),
        reply_markup=kb
    )

async def show_help(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 DNS — почему это важно", callback_data="help_dns")],
        [InlineKeyboardButton("◀️ В меню", callback_data="back")],
    ])
    lines = [
        "📖 *Инструкция*\n",
        "▶️ *ОТКРЫТЬ VPN* 🔑 — веб-панель управления прямо в Telegram.",
        "🧲 *Добавить устройство* — создать VPN-профиль.",
        "📋 *Мои устройства* — ваши профили и настройки.",
        "📊 *Статус сервера* — онлайн и нагрузка сервера.\n",
        "━━━━━━━━━━━━",
        "⚠️ *Важно — один гаджет = один профиль!*",
        "Не используйте один конфиг на нескольких устройствах —"
        " подключение будет нестабильным. Создайте отдельный профиль для каждого.\n",
        "📱 *Два приложения на выбор:*",
        "• *AmneziaVPN* — раздельное туннелирование, для телефонов и планшетов.",
        "  Только нужные сайты идут через VPN, остальные — напрямую.",
        "• *AmneziaWG* — лёгкий клиент, для ПК и телевизоров.",
        "  Весь трафик через VPN или с настраиваемыми исключениями.\n",
        "━━━━━━━━━━━━",
        "📲 *Подключение через AmneziaVPN:*",
        "1. Установите *AmneziaVPN* (Android, iOS, Mac — Play Market / App Store)",
        "2. 📋 Мои устройства → карточка → *📤 Поделиться кодом*",
        "3. Ссылка скопируется в буфер обмена",
        "4. В AmneziaVPN: + → вставьте ссылку → *Добавить*",
        "5. Включите VPN — готово!\n",
        "━━━━━━━━━━━━",
        "💻 *Подключение через AmneziaWG:*",
        "1. Установите *AmneziaWG* (Android, iOS, Windows, Mac — amnezia.org)",
        "2. 📋 Мои устройства → карточка → *📄 .conf* или *📱 QR-код*",
        "3. В AmneziaWG: + → «Добавить из файла» или «Сканировать QR»",
        "4. Включите тоннель — готово!\n",
        "━━━━━━━━━━━━",
        "🌐 *DNS — важно для стабильной работы*",
        "Нажмите кнопку ниже чтобы узнать почему нужно настроить DNS на устройстве.",
    ]
    text = "\n".join(lines).replace("\n\n\n", "\n\n")
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


async def show_help_dns(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад к инструкции", callback_data="help")],
    ])
    text = (
        "🌐 *DNS — почему это важно*\n\n"
        "Если VPN вдруг перестал подключаться после того как всё работало — "
        "скорее всего сменился IP-адрес сервера, а ваш роутер или провайдер "
        "ещё не узнал об этом.\n\n"
        "*Почему так происходит?*\n"
        "Когда меняется IP в настройках домена, это изменение расходится по интернету "
        "постепенно. На мобильном интернете это занимает *2–5 минут*. "
        "На Wi-Fi через некоторых провайдеров — *до 3–4 часов*, потому что роутер "
        "кэширует старый адрес и игнорирует настройки DNS даже если вы выставили 1.1.1.1.\n\n"
        "Это не проблема только нашего VPN — так работает интернет в целом. "
        "Любые изменения в домене (сайт, почта, сервисы) будут доходить медленно "
        "если на устройстве стоит медленный DNS провайдера.\n\n"
        "✅ *Решение — поставить быстрый DNS на каждое устройство:*\n"
        "Основной: `1.1.1.1` (Cloudflare)\n"
        "Резервный: `1.0.0.1`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📱 *Android:*\n"
        "Настройки → Wi-Fi → удержать сеть → Изменить → "
        "Дополнительно → DNS 1: `1.1.1.1` DNS 2: `1.0.0.1`\n"
        "Или: Настройки → Подключения → Другие настройки → "
        "Частный DNS → ввести `one.one.one.one`\n\n"
        "💻 *Windows:*\n"
        "Панель управления → Центр управления сетями → "
        "Изменение параметров адаптера → ПКМ на Wi-Fi → Свойства → "
        "IP версии 4 → Свойства → вручную: `1.1.1.1` и `1.0.0.1`\n\n"
        "🍎 *iPhone / iPad:*\n"
        "Настройки → Wi-Fi → (i) рядом с сетью → "
        "Настроить DNS → Вручную → удалить старые → добавить `1.1.1.1` и `1.0.0.1`\n\n"
        "🖥 *macOS:*\n"
        "Системные настройки → Сеть → Wi-Fi → Дополнительно → "
        "DNS → `+` → `1.1.1.1` и `1.0.0.1`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "После настройки DNS изменения IP-адресов будут доходить до вас "
        "за *2–5 минут* вместо нескольких часов."
    )
    await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# ИСКЛЮЧЕНИЯ САЙТОВ (split tunneling)
# ══════════════════════════════════════════════════════════════════════════════

def _sites_text(name: str) -> str:
    short = device_short_name(name)
    return (
        f"🌐 Исключения сайтов для *{short}*\n\n"
        f"Отмеченные сайты будут работать *без VPN*.\n"
        f"🏠 Локальная сеть включена всегда.\n"
        f"Нажмите на категорию чтобы раскрыть её."
    )


async def show_sites_menu(query, name: str, user_id: int, context):
    """Меню настройки исключений сайтов для устройства. Загружает сохранённые настройки из excl.json."""
    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    # Загружаем сохранённые исключения (если есть)
    saved = load_client_excl(name)
    if saved and "sites" in saved:
        selected = set(saved["sites"]) | set(DEFAULT_SELECTED)
    else:
        selected = set(DEFAULT_SELECTED)
    custom_domains = list(saved.get("custom_domains", [])) if saved else []

    context.user_data["sites_device"]   = name
    context.user_data["sites_selected"] = selected
    context.user_data["sites_custom"]   = custom_domains
    context.user_data["sites_expanded"] = set()

    await query.edit_message_text(
        _sites_text(name),
        reply_markup=sites_keyboard(selected, name, set(), custom_domains),
        parse_mode="Markdown",
    )


async def toggle_site_handler(query, key: str, user_id: int, context):
    """Переключение отдельного сайта, выбрать/снять все."""
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return

    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    selected = context.user_data.get("sites_selected", set(DEFAULT_SELECTED))
    expanded = context.user_data.get("sites_expanded", set())
    custom   = context.user_data.get("sites_custom", [])

    if key == "select_all":
        selected = set(DEFAULT_SELECTED) | ALL_SELECTABLE
        context.user_data["sites_selected"] = selected
    elif key == "deselect_all":
        selected = set(DEFAULT_SELECTED)
        context.user_data["sites_selected"] = selected
    elif key.startswith("rm_custom_"):
        idx = int(key[10:])
        if 0 <= idx < len(custom):
            custom.pop(idx)
        context.user_data["sites_custom"] = custom
    elif key in DEFAULT_SELECTED:
        await query.answer()
        return
    else:
        if key in selected:
            selected.discard(key)
        else:
            selected.add(key)
        context.user_data["sites_selected"] = selected

    await query.edit_message_text(
        _sites_text(name),
        reply_markup=sites_keyboard(selected, name, expanded, custom),
        parse_mode="Markdown",
    )


async def toggle_category_handler(query, cat_name: str, context):
    """Разворачивает/сворачивает категорию в меню исключений."""
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return

    selected = context.user_data.get("sites_selected", set(DEFAULT_SELECTED))
    expanded = context.user_data.get("sites_expanded", set())
    custom   = context.user_data.get("sites_custom", [])

    if cat_name in expanded:
        expanded.discard(cat_name)
    else:
        expanded.add(cat_name)
    context.user_data["sites_expanded"] = expanded

    await query.edit_message_text(
        _sites_text(name),
        reply_markup=sites_keyboard(selected, name, expanded, custom),
        parse_mode="Markdown",
    )


async def apply_sites(query, user_id: int, context):
    """Сохраняет исключения сайтов в excl.json и возвращает в меню устройства."""
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return

    if not can_access_device(user_id, name):
        await query.answer("⛔ Это не ваше устройство.", show_alert=True)
        return

    selected  = context.user_data.get("sites_selected", set(DEFAULT_SELECTED))
    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if not os.path.exists(conf_path):
        await query.edit_message_text("❌ Файл конфига не найден.", reply_markup=back_kb())
        return

    # Сохраняем исключения (без DEFAULT_SELECTED — они всегда включены)
    user_sites     = list(selected - set(DEFAULT_SELECTED))
    custom_domains = context.user_data.get("sites_custom", [])
    save_client_excl(name, {"sites": user_sites, "custom_domains": custom_domains})

    context.user_data.pop("sites_device", None)
    context.user_data.pop("sites_selected", None)
    context.user_data.pop("sites_expanded", None)
    context.user_data.pop("sites_custom", None)

    short = device_short_name(name)
    excl_count = len(user_sites)
    note = f"{excl_count} {'сайт' if excl_count == 1 else 'сайта' if 2 <= excl_count <= 4 else 'сайтов'}" if excl_count else "только локальная сеть"
    await query.answer(f"✅ Исключения сохранены: {note}", show_alert=False)
    await show_device(query, name, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# ДОБАВИТЬ СВОЙ САЙТ В ИСКЛЮЧЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def sites_add_custom_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1 — спрашиваем домен или IP для добавления в исключения."""
    query = update.callback_query
    await query.answer()
    name = context.user_data.get("sites_device")
    if not name:
        await query.answer("Сессия устарела, откройте меню заново.", show_alert=True)
        return ConversationHandler.END
    await query.edit_message_text(
        "➕ *Добавить свой сайт в исключения*\n\n"
        "Введите домен или IP-адрес:\n\n"
        "Примеры:\n"
        "`netflix.com`\n"
        "`142.250.0.0/16`\n"
        "`1.2.3.4`\n\n"
        "Домен будет исключён из VPN-туннеля.\n"
        "Или нажмите Отмена.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data=f"sites_{name}")
        ]]),
        parse_mode="Markdown",
    )
    return WAITING_SITES_DOMAIN


async def sites_add_custom_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2 — получаем домен/IP, добавляем в список и возвращаем меню."""
    import ipaddress as _ip
    raw = update.message.text.strip()

    # Очищаем — убираем схему и путь
    entry = raw.replace("https://", "").replace("http://", "")
    entry = entry.replace("www.", "").split("/")[0].strip().lower()

    # Проверяем: CIDR или домен
    valid = False
    try:
        _ip.ip_network(entry, strict=False)
        valid = True
    except ValueError:
        if "." in entry and len(entry) >= 4 and not entry.startswith("."):
            valid = True

    if not valid:
        await update.message.reply_text(
            "❌ Неверный формат. Введите домен (`site.ru`) или IP/CIDR (`1.2.3.0/24`).",
            parse_mode="Markdown",
        )
        return WAITING_SITES_DOMAIN

    custom = context.user_data.get("sites_custom", [])
    if entry not in custom:
        custom.append(entry)
        context.user_data["sites_custom"] = custom
        note = f"✅ `{entry}` добавлен в исключения."
    else:
        note = f"ℹ️ `{entry}` уже есть в списке."

    name = context.user_data.get("sites_device", "")
    await update.message.reply_text(
        note + f"\n\n{_sites_text(name)}",
        reply_markup=sites_keyboard(
            context.user_data.get("sites_selected", set(DEFAULT_SELECTED)),
            name,
            context.user_data.get("sites_expanded", set()),
            custom,
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END





# ══════════════════════════════════════════════════════════════════════════════
# ДОБАВЛЕНИЕ УСТРОЙСТВА (ConversationHandler)
# ══════════════════════════════════════════════════════════════════════════════

async def cancel_add_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена добавления устройства через кнопку Отмена"""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("adding_user_id", None)
    await main_menu(query, query.from_user.id, edit=True)
    return ConversationHandler.END

async def add_device_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_approved(user_id):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отмена", callback_data="add_cancel")]
    ])
    await query.edit_message_text(
        f"🧲 Добавление устройства\n\n"
        f"Введите название устройства *латиницей*:\n"
        f"`Phone`, `PC`, `Nout`, `iPad`, `TV`\n\n"
        f"Или нажмите Отмена.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    context.user_data["adding_user_id"] = user_id
    return WAITING_DEVICE_NAME

async def receive_device_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_approved(user_id):
        return ConversationHandler.END

    raw        = update.message.text.strip()

    # Проверяем наличие кириллицы / не-ASCII символов ДО фильтрации
    has_non_ascii = any(not c.isascii() for c in raw)

    # Убираем всё лишнее — AmneziaWG не любит дефисы и спецсимволы
    device_raw = "".join(c for c in raw if c.isascii() and (c.isalnum() or c == "_"))
    device_raw = device_raw.capitalize()

    if not device_raw:
        await update.message.reply_text(
            "❌ Введите название *латиницей*, только буквы и цифры. Например: `Phone`",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    # Если была кириллица — не угадываем, просим переввести явно
    if has_non_ascii:
        await update.message.reply_text(
            f"❌ Название содержит кириллицу или недопустимые символы.\n\n"
            f"Используйте *только латинские буквы и цифры*, например: `Phone`, `PC`, `iPad`, `TV`\n\n"
            f"Кириллица, пробелы и спецсимволы не поддерживаются.",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    user_name = get_user_name(user_id)
    full_name = f"{user_name}.{device_raw}"

    if os.path.exists(f"{CLIENTS_DIR}/{full_name}.conf"):
        await update.message.reply_text(
            f"❌ Устройство *{device_raw}* уже существует. Введите другое название.",
            parse_mode="Markdown"
        )
        return WAITING_DEVICE_NAME

    await update.message.reply_text(f"⏳ Создаю профиль *{device_raw}*...", parse_mode="Markdown")
    try:
        await create_client(full_name)
    except Exception as e:
        logger.error(f"receive_device_name: create_client failed: {e}")
        await update.message.reply_text(
            f"❌ Не удалось создать устройство *{device_raw}*.\n\n"
            f"`{e}`\n\n"
            f"Попробуйте ещё раз или обратитесь к администратору.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Перейти к устройству", callback_data=f"device_{full_name}")],
        [InlineKeyboardButton("◀️ В меню", callback_data="back")],
    ])
    await update.message.reply_text(
        f"✅ Устройство *{device_raw}* создано!\n\n"
        f"Перейдите в карточку и выберите способ подключения:\n"
        f"• 📄 *.conf* — для AmneziaWG (рекомендуется)\n"
        f"• 📱 *QR-код* — для AmneziaWG на телефоне\n"
        f"• 📤 *Поделиться кодом* — для AmneziaVPN\n\n"
        f"Для каждого варианта можно выбрать канал и настроить исключения сайтов.\n\n"
        f"💡 Первый раз? Загляните в 📖 *Инструкцию* в главном меню — "
        f"там есть важный раздел про настройку DNS на устройстве.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ /panel и /bot
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/panel — открыть панель управления (TMA)."""
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return

    tma_btn = _tma_button()
    if tma_btn:
        # Минимальный текст — сразу кнопка, нажать один раз
        await update.message.reply_text("🖥", reply_markup=InlineKeyboardMarkup([[tma_btn]]))
    else:
        await update.message.reply_text(
            "⚠️ Панель не настроена. Используйте /bot"
        )


async def cmd_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bot — открыть главное меню бота."""
    user_id = update.effective_user.id
    if not is_approved(user_id):
        await update.message.reply_text("⛔ У вас нет доступа.")
        return
    await main_menu(update.message, user_id)


# ══════════════════════════════════════════════════════════════════════════════
# МОНИТОРИНГ РЕПОЗИТОРИЯ
# ══════════════════════════════════════════════════════════════════════════════

REPO_OWNER  = "yntoolsmail-prog"
REPO_NAME   = "Vpn_AWG"
REPO_BRANCH = "main"
REPO_URL    = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
REPO_COMMIT_FILE = "/etc/amnezia/amneziawg/last_commit.txt"

def _gh_api(path: str) -> dict | None:
    """GET к GitHub API, возвращает dict или None при ошибке."""
    try:
        out = subprocess.check_output(
            ["curl", "-s", "--max-time", "10",
             "-H", "Accept: application/vnd.github+json",
             f"https://api.github.com/{path}"],
            text=True,
        )
        return json.loads(out)
    except Exception:
        return None

def _read_last_commit() -> str:
    """Читает сохранённый SHA последнего известного коммита."""
    try:
        with open(REPO_COMMIT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

def _write_last_commit(sha: str):
    """Сохраняет SHA коммита на диск."""
    os.makedirs(os.path.dirname(REPO_COMMIT_FILE), exist_ok=True)
    with open(REPO_COMMIT_FILE, "w") as f:
        f.write(sha)

async def check_repo_updates(context: ContextTypes.DEFAULT_TYPE):
    """Job: проверяет новые коммиты в репозитории каждые 30 минут.
    При появлении изменений отправляет уведомление админу."""
    data = _gh_api(f"repos/{REPO_OWNER}/{REPO_NAME}/commits/{REPO_BRANCH}")
    if not data or "sha" not in data:
        logger.warning("check_repo_updates: не удалось получить данные из GitHub API")
        return

    latest_sha  = data["sha"]
    short_sha   = latest_sha[:7]
    last_known  = _read_last_commit()

    if not last_known:
        # Первый запуск — просто запоминаем текущий коммит, не шумим
        _write_last_commit(latest_sha)
        logger.info(f"check_repo_updates: инициализация, текущий коммит {short_sha}")
        return

    if latest_sha == last_known:
        logger.info(f"check_repo_updates: изменений нет ({short_sha})")
        return

    # Есть новый коммит — собираем данные
    commit_info  = data.get("commit", {})
    message      = commit_info.get("message", "—").split("\n")[0][:80]
    author       = commit_info.get("author", {}).get("name", "—")
    date_raw     = commit_info.get("author", {}).get("date", "")
    date_str     = date_raw[:10] if date_raw else "—"
    compare_url  = f"{REPO_URL}/compare/{last_known[:7]}...{short_sha}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Смотреть изменения", url=compare_url)],
        [InlineKeyboardButton("🔄 Обновить сейчас",    callback_data=f"repo_update_{latest_sha}")],
        [InlineKeyboardButton("⏭ Пропустить",          callback_data=f"repo_skip_{latest_sha}")],
    ])

    text = (
        f"🆕 *Новые изменения в репозитории!*\n\n"
        f"📦 [`{short_sha}`]({REPO_URL}/commit/{latest_sha})\n"
        f"✏️ {message}\n"
        f"👤 {author}  •  {date_str}\n\n"
        f"👆 Нажмите «Смотреть изменения» или перейдите на сервер\n"
        f"и выполните обновление через `vpn.sh` → Обновление."
    )

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    logger.info(f"check_repo_updates: новый коммит {short_sha}, уведомление отправлено")

async def do_repo_update(query, sha: str):
    """Обновляет все файлы проекта через setup.sh --update и перезапускает бота."""
    short_sha = sha[:7]
    await query.edit_message_text(
        f"⏳ Обновляю все файлы проекта (коммит `{short_sha}`)...",
        parse_mode="Markdown",
    )
    try:
        result = subprocess.run(
            ["bash", "/root/setup.sh", "--update"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "setup.sh --update вернул ненулевой код")

        _write_last_commit(sha)
        await query.edit_message_text(
            f"✅ *Обновление выполнено!*\n\n"
            f"📦 Коммит: `{short_sha}`\n"
            f"📄 Все файлы проекта обновлены.\n\n"
            f"⏳ Бот перезапускается...",
            parse_mode="Markdown",
        )
        subprocess.Popen(["bash", "-c", f"sleep 2 && systemctl restart {BOT_SERVICE}"])

    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка при обновлении: `{e}`\n\n"
            f"Зайдите на сервер: `bash /root/setup.sh --update`",
            parse_mode="Markdown",
        )

# ══════════════════════════════════════════════════════════════════════════════
# ПРОВЕРКА И ОБНОВЛЕНИЕ IP СЕРВЕРА
# ══════════════════════════════════════════════════════════════════════════════

async def send_start_hello(context: ContextTypes.DEFAULT_TYPE):
    """Job: запускается через 5 секунд после старта бота.
    Шлёт сообщение ТОЛЬКО если есть флаг-файл с chat_id пользователя который нажал кнопку.
    При автоматическом рестарте systemd флага нет — молчим, не спамим."""
    if not os.path.exists(RESTART_FLAG_FILE):
        return  # автоматический рестарт — не беспокоим

    try:
        with open(RESTART_FLAG_FILE) as f:
            chat_id = int(f.read().strip())
        os.remove(RESTART_FLAG_FILE)
    except Exception:
        try:
            os.remove(RESTART_FLAG_FILE)
        except Exception:
            pass
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Открыть меню", callback_data="back")],
    ])
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ Бот перезапущен и готов к работе.",
            reply_markup=kb,
        )
    except Exception as e:
        logger.warning(f"send_start_hello: не удалось отправить сообщение: {e}")

async def check_ip_on_start(context: ContextTypes.DEFAULT_TYPE):
    """Job: запускается один раз через 15 секунд после старта.
    Сравнивает реальный IP с SERVER_IP в server.env.
    Если расходятся — уведомляет админа."""
    real_ip = get_real_server_ip()
    if not real_ip:
        logger.warning("check_ip_on_start: не удалось получить внешний IP")
        return
    if real_ip == SERVER_IP:
        logger.info(f"check_ip_on_start: IP актуален ({SERVER_IP})")
        return
    # IP расходится — уведомляем админа
    ep_note = ""
    if SERVER_ENDPOINT == SERVER_IP:
        ep_note = f"\n⚠️ SERVER_ENDPOINT тоже указывает на старый IP и будет обновлён."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить IP", callback_data=f"update_ip_{real_ip}")],
        [InlineKeyboardButton("❌ Пропустить",  callback_data="update_ip_skip")],
    ])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"⚠️ IP сервера изменился!\n\n"
            f"В настройках: `{SERVER_IP}`\n"
            f"Реальный IP:  `{real_ip}`{ep_note}\n\n"
            f"Обновить server.env?"
        ),
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def do_update_ip(query, new_ip: str):
    """Обновляет SERVER_IP (и SERVER_ENDPOINT если он был равен старому IP) в server.env."""
    global SERVER_IP, SERVER_ENDPOINT
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()

        updated = []
        ep_updated = False
        for line in lines:
            if line.startswith("SERVER_IP="):
                updated.append(f"SERVER_IP={new_ip}\n")
            elif line.startswith("SERVER_ENDPOINT=") and SERVER_ENDPOINT == SERVER_IP:
                updated.append(f"SERVER_ENDPOINT={new_ip}\n")
                ep_updated = True
            else:
                updated.append(line)

        with open(ENV_FILE, "w") as f:
            f.writelines(updated)

        ep_note = f"\n✅ SERVER_ENDPOINT обновлён: `{new_ip}`" if ep_updated else ""
        old_ip = SERVER_IP
        await query.edit_message_text(
            f"✅ IP обновлён!\n\n"
            f"Старый: `{old_ip}`\n"
            f"Новый:  `{new_ip}`{ep_note}\n\n"
            f"⏳ Бот перезапускается...",
            parse_mode="Markdown"
        )
        subprocess.Popen(["bash", "-c", f"sleep 2 && systemctl restart {BOT_SERVICE}"])
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка обновления IP: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application) -> None:
    """Регистрирует команды бота в Telegram (кнопка «Меню»)."""
    commands = [
        BotCommand("start",  "🏠 Главная"),
        BotCommand("cancel", "❌ Отмена"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Команды бота зарегистрированы: /start /cancel")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_register_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
    )

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_device_entry, pattern="^add$")],
        states={
            WAITING_DEVICE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_device_name),
                CallbackQueryHandler(cancel_add_device, pattern="^add_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    restore_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_restore, pattern="^restore$")],
        states={
            WAITING_RESTORE_FILE: [
                MessageHandler(filters.Document.ALL, receive_restore_file),
                CallbackQueryHandler(confirm_restore, pattern="^restore_confirm$"),
                CallbackQueryHandler(cancel_restore, pattern="^restore_cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_restore, pattern="^restore_cancel$"),
        ],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    tz_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_tz_manual, pattern="^set_tz_manual$")],
        states={
            WAITING_TZ_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tz_manual),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    # sites_add_custom — регистрируем ДО общего button_handler,
    # чтобы колбэк "sites_add_custom" перехватывался сюда,
    # а не уходил в button_handler как sites_<name>
    sites_custom_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sites_add_custom_start, pattern="^sites_add_custom$")],
        states={
            WAITING_SITES_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sites_add_custom_receive),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    srv_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(srv_add_start, pattern="^srv_add$")],
        states={
            WAITING_SRV_IP:       [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ip)],
            WAITING_SRV_PORT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_port)],
            WAITING_SRV_LOGIN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_login)],
            WAITING_SRV_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_password)],
            WAITING_SRV_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_name)],
            WAITING_SRV_EMOJI:    [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_emoji)],
        },
        fallbacks=[
            CommandHandler("cancel", srv_add_cancel),
            CallbackQueryHandler(srv_add_cancel, pattern="^srv_add_cancel$"),
        ],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    srv_domain_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(srv_adddomain_start, pattern="^srv_adddomain_\\d+$")],
        states={
            WAITING_SRV_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_adddomain_receive)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(reg_conv)
    app.add_handler(add_conv)
    app.add_handler(restore_conv)
    app.add_handler(tz_conv)
    app.add_handler(sites_custom_conv)  # до общего button_handler
    app.add_handler(srv_add_conv)
    app.add_handler(srv_domain_conv)
    app.add_handler(CommandHandler("panel",  cmd_panel))
    app.add_handler(CommandHandler("bot",    cmd_bot))
    app.add_handler(CallbackQueryHandler(button_handler))
    _modules.register_bot_handlers(app)

    # Проверка напоминания о техобслуживании — раз в сутки
    app.job_queue.run_repeating(maintenance_reminder, interval=86400, first=60)
    # Мониторинг трафика — каждые 5 секунд (пики), в лог раз в минуту
    app.job_queue.run_repeating(bw_monitor_job, interval=5, first=10)
    # Проверка IP сервера — один раз через 15 секунд после старта
    app.job_queue.run_once(check_ip_on_start, when=15)
    # Уведомление о старте — через 5 секунд после запуска
    app.job_queue.run_once(send_start_hello, when=5)
    # Мониторинг обновлений репозитория — раз в сутки, первая проверка через 20 секунд после старта
    app.job_queue.run_repeating(check_repo_updates, interval=86400, first=20)

    logger.info(f"Бот запущен. Admin ID: {ADMIN_ID}")
    print(f"\n\033[0;32m✓ Бот запущен! Admin ID: {ADMIN_ID}\033[0m\n")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()