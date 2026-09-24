from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from strings import get_help_main, HELP_DNS
from awg_core import TMA_URL, ADMIN_ID, gen_obfs, is_awg3
from .common import BTN_BACK, BTN_BACK_MENU


async def show_help(query):
    # У пользователя инструкция открывается из меню «🆘 Помощь» — туда и назад
    if query.from_user.id == ADMIN_ID:
        back = InlineKeyboardButton(BTN_BACK_MENU, callback_data="settings_menu")
    else:
        back = InlineKeyboardButton(BTN_BACK, callback_data="help_menu")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 DNS — почему это важно", callback_data="help_dns")],
        [back],
    ])
    await query.edit_message_text(get_help_main(TMA_URL, is_awg3(gen_obfs())), reply_markup=kb, parse_mode="Markdown")


async def show_help_dns(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад к инструкции", callback_data="help")],
    ])
    await query.edit_message_text(HELP_DNS, reply_markup=kb, parse_mode="Markdown")
