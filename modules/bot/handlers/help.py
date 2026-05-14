from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from strings import BTN_BACK_MENU, HELP_MAIN, HELP_DNS


async def show_help(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 DNS — почему это важно", callback_data="help_dns")],
        [InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")],
    ])
    await query.edit_message_text(HELP_MAIN, reply_markup=kb, parse_mode="Markdown")


async def show_help_dns(query):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад к инструкции", callback_data="help")],
    ])
    await query.edit_message_text(HELP_DNS, reply_markup=kb, parse_mode="Markdown")
