import asyncio, ipaddress, threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from awg_core import (
    ADMIN_ID, CLIENTS_DIR, EXCL_EXT, AWG_IFACE,
    can_access_device, device_short_name,
    get_allowed_ips_for_client, load_client_excl, save_client_excl,
    process_domain, run_subnet_daemon,
)
from sites_data import SITES, CATEGORIES, DEFAULT_SELECTED, ALL_SELECTABLE
from .common import sites_keyboard, WAITING_SITES_DOMAIN, BTN_BACK, BTN_BACK_MENU, BTN_CANCEL
import os


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
    from .clients import show_device
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
        from .common import back_kb
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
            InlineKeyboardButton(BTN_CANCEL, callback_data=f"sites_{name}")
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
        # Если это домен (не IP/CIDR) — запускаем DNS-зондирование в фоне
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            threading.Thread(target=process_domain, args=(entry,), daemon=True).start()
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
