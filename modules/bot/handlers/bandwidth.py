import os, time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from awg_core import (
    AWG_IFACE, BW_LOG_FILE, CLIENTS_DIR,
    create_backup, fmt_bytes, fmt_histogram,
    get_all_clients, get_awg_dump, get_bw_histogram, get_bw_histogram_day,
    get_bw_top, get_log_days, get_vnstat_monthly,
    load_bw_peak, read_iface_bytes, save_bw_peak, get_host_iface,
)
from .common import back_kb, BTN_BACK, BTN_BACK_MENU, BTN_CANCEL


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

    def p(label, days):
        mark = "✅ " if days == period_days else ""
        return InlineKeyboardButton(f"{mark}{label}", callback_data=f"bw_period_{days}")

    kb = InlineKeyboardMarkup([
        [p("Всё время", 0), p("30 дней", 30), p("7 дней", 7)],
        [InlineKeyboardButton("📅 По дням",      callback_data="bw_days_0")],
        [InlineKeyboardButton("🗑 Сбросить пики", callback_data="bw_reset_ask")],
        [InlineKeyboardButton("🔄 Обновить",      callback_data=f"bw_period_{period_days}")],
        [InlineKeyboardButton("◀️ Статус",        callback_data="status")],
        [InlineKeyboardButton(BTN_BACK_MENU,        callback_data="back")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_days(query, page: int = 0):
    """Гистограмма по конкретному дню с листалкой"""
    days = get_log_days()
    if not days:
        await query.edit_message_text(
            "📊 Данных по дням пока нет.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(BTN_BACK, callback_data="bandwidth")
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
        [InlineKeyboardButton(BTN_BACK, callback_data="bandwidth")],
    ])
    await query.edit_message_text("\n".join(lines), reply_markup=kb)

async def show_bw_reset_ask(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Сбросить только пики",    callback_data="bw_reset_confirm")],
        [InlineKeyboardButton("💣 Сбросить всё (с нуля)",  callback_data="bw_reset_all_confirm")],
        [InlineKeyboardButton(BTN_CANCEL,                  callback_data="bandwidth")],
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
