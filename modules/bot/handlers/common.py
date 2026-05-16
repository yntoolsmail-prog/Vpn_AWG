from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from awg_core import TMA_URL, ADMIN_ID
from sites_data import SITES, CATEGORIES, DEFAULT_SELECTED, ALL_SELECTABLE

# Кнопки навигации — единая точка определения для всех хендлеров
BTN_BACK       = "◀️ Назад"
BTN_BACK_MENU  = "◀️ В меню"
BTN_BACK_CARD  = "◀️ Карточка"
BTN_BACK_MAINT = "◀️ Техобслуживание"
BTN_CANCEL     = "❌ Отмена"
BTN_DONE       = "✅ Готово"
BTN_REFRESH    = "🔄 Обновить"
BTN_MY_DEVICES = "📋 Мои устройства"

# Состояния ConversationHandler
WAITING_REGISTER_NAME  = 10
WAITING_DEVICE_NAME    = 11
WAITING_RESTORE_FILE   = 12
WAITING_TZ_INPUT       = 15   # ждём ручной ввод часового пояса
WAITING_SITES_DOMAIN   = 16   # ждём домен/IP для добавления в исключения
# Добавление домена к серверу
WAITING_SRV_DOMAIN     = 26
# Переименование сервера (primary или slave)
WAITING_SRV_EDIT_NAME  = 27
WAITING_SRV_EDIT_EMOJI = 28

IMG_BASE = "https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/.images"

def _md(s: str) -> str:
    """Экранирует спецсимволы Markdown v1 в пользовательских строках."""
    for ch in ("*", "_", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


def back_kb(target="back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK_MENU, callback_data=target)]])


def _tma_button() -> InlineKeyboardButton | None:
    """Возвращает кнопку открытия TMA или None если URL не настроен."""
    if not TMA_URL:
        return None
    if TMA_URL.startswith("https://"):
        return InlineKeyboardButton("▶️ ОТКРЫТЬ VPN 🔑", web_app=WebAppInfo(url=TMA_URL))
    return InlineKeyboardButton("▶️ ОТКРЫТЬ VPN 🔑", url=TMA_URL)


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
        InlineKeyboardButton(BTN_DONE,   callback_data="sites_done"),
        InlineKeyboardButton(BTN_CANCEL,  callback_data=f"device_{device_name}"),
    ])
    return InlineKeyboardMarkup(rows)
