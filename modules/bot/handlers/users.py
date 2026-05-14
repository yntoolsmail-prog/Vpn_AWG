from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from awg_core import ADMIN_ID, get_user_clients, get_user_display, load_users, save_users, remove_client_from_awg
from strings import BTN_BACK, BTN_CANCEL, BTN_BACK_MENU
from .common import back_kb


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

    kb.append([InlineKeyboardButton(BTN_BACK_MENU, callback_data="back")])
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
        [InlineKeyboardButton(BTN_CANCEL, callback_data="manage_users")],
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
