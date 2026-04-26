"""slave_servers.py — диалог добавления slave-сервера к AmneziaWG.

Содержит ConversationHandler для пошагового добавления сервера через SSH.
Регистрируется через register_handlers(app).
"""

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from awg_core import (
    PARAMIKO_AVAILABLE,
    SERVER_PORT,
    SERVER_PUBLIC,
    load_servers,
    save_servers,
    ssh_clone_awg_to_slave,
    ssh_read_slave_env,
)

# Состояния диалога
WAITING_SRV_IP       = 20
WAITING_SRV_PORT     = 21
WAITING_SRV_LOGIN    = 22
WAITING_SRV_PASSWORD = 23
WAITING_SRV_NAME     = 24
WAITING_SRV_EMOJI    = 25


def _back_kb(target: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ В меню", callback_data=target)]])


# ── Шаги диалога ──────────────────────────────────────────────────────────────

async def srv_add_start(update, context: ContextTypes.DEFAULT_TYPE):
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

    if not PARAMIKO_AVAILABLE:
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
        await asyncio.get_event_loop().run_in_executor(
            None, ssh_read_slave_env, ip, port, login, pwd
        )
        await status_msg.edit_text(
            "✅ SSH подключение успешно — AmneziaWG установлен.\n"
            "Конфиг будет клонирован с primary после сохранения.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Не удалось подключиться к `{ip}:{port}`:\n`{e}`\n\n"
            f"Проверьте данные SSH и попробуйте ещё раз, или /cancel для отмены.",
            parse_mode="Markdown"
        )
        return WAITING_SRV_PASSWORD

    await update.message.reply_text(
        "Введите название сервера:\n"
        "_Рекомендуем 3 заглавные буквы: NLD, FIN, RUS, GER, USA…_\n"
        "Используйте только латиницу — оно войдёт в имя файлов конфигов.",
        parse_mode="Markdown"
    )
    return WAITING_SRV_NAME


async def srv_add_name(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["srv_add"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите эмодзи/флаг (например: 🇩🇪 🇫🇮 🇷🇺), или Enter для 🖥:"
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
        "awg_public_key": SERVER_PUBLIC,
        "awg_port": int(SERVER_PORT) if SERVER_PORT else 51820,
        "endpoints": [{"value": d.get("ip", ""), "type": "ip"}],
    }
    servers.append(new_srv)
    save_servers(servers)

    srv_label = f"*{d.get('emoji', '🖥')} {d.get('name', 'Сервер')}*"
    await update.message.reply_text(
        f"✅ Сервер {srv_label} добавлен!\n\n"
        f"IP `{d.get('ip', '')}` добавлен как первый эндпоинт.\n"
        f"Откройте карточку сервера чтобы добавить домены.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🖥 Серверы", callback_data="servers")
        ]])
    )

    if PARAMIKO_AVAILABLE:
        clone_msg = await update.message.reply_text(
            "🔄 Клонирую конфиг AWG на slave (ключи + клиенты)…"
        )
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, ssh_clone_awg_to_slave, new_srv
            )
            await clone_msg.edit_text(
                f"✅ Slave {srv_label} готов — конфиг скопирован, интерфейс перезапущен.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await clone_msg.edit_text(
                f"⚠️ Не удалось склонировать конфиг: `{e}`\n"
                f"Запустите `bash /root/setup.sh --update` на slave вручную.",
                parse_mode="Markdown"
            )

    return ConversationHandler.END


async def srv_add_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("srv_add", None)
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Отмена.", reply_markup=_back_kb("servers"))
    else:
        await update.message.reply_text("Отмена.")
    return ConversationHandler.END


# ── Регистрация хендлеров ──────────────────────────────────────────────────────

def register_handlers(app) -> None:
    conv = ConversationHandler(
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
    app.add_handler(conv)
