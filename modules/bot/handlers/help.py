from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from strings import get_help_main, HELP_DNS
from awg_core import TMA_URL, ADMIN_ID
from .common import BTN_BACK_MENU


async def show_help(query):
    back_target = "settings_menu" if query.from_user.id == ADMIN_ID else "back"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 DNS — почему это важно", callback_data="help_dns")],
        [InlineKeyboardButton(BTN_BACK_MENU, callback_data=back_target)],
    ])
    await query.edit_message_text(get_help_main(TMA_URL), reply_markup=kb, parse_mode="Markdown")


async def show_help_dns(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад к инструкции", callback_data="help")],
    ])
    await query.edit_message_text(HELP_DNS, reply_markup=kb, parse_mode="Markdown")
