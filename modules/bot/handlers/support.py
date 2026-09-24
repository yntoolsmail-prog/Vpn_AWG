"""Связь администратора с пользователями внутри бота.

📣 Уведомления (админ, «Настройки»): следующее сообщение админа — текст, фото,
файл — после подтверждения уходит всем одобренным пользователям.

🆘 Помощь (пользователь, вместо «Инструкции»): инструкция и «✉️ Написать
админу». Сообщение приходит админу с кнопкой «↩️ Ответить», ответ — обратно
пользователю с кнопкой «✉️ Ответить». Всё идёт через бота: личный аккаунт
админа пользователям не виден. Писать могут только одобренные пользователи.

Чужой текст пересылается с HTML-разметкой (Message.text_html), а не Markdown:
Markdown v1 не умеет подчёркивание и зачёркивание и ломается на спецсимволах
из сообщения пользователя.
"""
import asyncio, html, logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes, ConversationHandler

from awg_core import ADMIN_ID, get_user_clients, get_user_name, is_approved, load_users
from .common import (
    BTN_BACK_MENU, BTN_CANCEL,
    WAITING_BROADCAST_MSG, WAITING_BROADCAST_CONFIRM,
    WAITING_SUPPORT_MSG, WAITING_SUPPORT_REPLY,
)

logger = logging.getLogger(__name__)

_HTML = ParseMode.HTML
# Типы сообщений, которым можно задать подпись при copy_message
_CAPTION_TYPES = ("photo", "video", "document", "audio", "animation", "voice")
# Черновик рассылки ждёт подтверждения здесь, а не в user_data: user_data
# сохраняется PicklePersistence, а объект Message туда класть незачем
_PENDING_BROADCAST: dict = {}


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=cb)] for text, cb in rows])


async def _send_with_header(bot, chat_id: int, msg: Message, header: str, reply_markup=None):
    """Заголовок и содержимое msg одним сообщением.

    Текст — через send_message, медиа с подписью — copy_message с новой подписью.
    Стикер, кружок или текст на пределе длины места под заголовок не оставляют —
    тогда заголовок уходит отдельно, а содержимое копией как есть."""
    if msg.text:
        text = f"{header}\n\n{msg.text_html}"
        if len(text) <= 4096:
            return await bot.send_message(chat_id, text, parse_mode=_HTML, reply_markup=reply_markup)
    elif any(getattr(msg, t, None) for t in _CAPTION_TYPES):
        caption = f"{header}\n\n{msg.caption_html}" if msg.caption else header
        if len(caption) <= 1024:
            return await bot.copy_message(chat_id, msg.chat_id, msg.message_id,
                                          caption=caption, parse_mode=_HTML,
                                          reply_markup=reply_markup)
    await bot.send_message(chat_id, header, parse_mode=_HTML)
    return await bot.copy_message(chat_id, msg.chat_id, msg.message_id, reply_markup=reply_markup)


def _recipients() -> list:
    return [int(uid) for uid in load_users().get("approved", {}) if int(uid) != ADMIN_ID]


# ── 📣 Уведомления: рассылка всем пользователям ───────────────────────────────

async def notify_start(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Только для администратора", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    _PENDING_BROADCAST.pop(query.from_user.id, None)
    await query.edit_message_text(
        f"📣 Уведомление всем пользователям ({len(_recipients())})\n\n"
        "Напишите сообщение — текст, фото или файл. "
        "Перед отправкой бот попросит подтверждение.",
        reply_markup=_kb((BTN_CANCEL, "notify_cancel")),
    )
    return WAITING_BROADCAST_MSG


async def notify_receive(update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    _PENDING_BROADCAST[msg.from_user.id] = msg
    await msg.reply_text(
        f"Отправить это сообщение {len(_recipients())} пользователям?",
        reply_markup=_kb(("📣 Отправить", "notify_send"), (BTN_CANCEL, "notify_cancel")),
    )
    return WAITING_BROADCAST_CONFIRM


async def notify_send(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    msg = _PENDING_BROADCAST.pop(query.from_user.id, None)
    if msg is None:
        await query.edit_message_text("⚠️ Черновик не найден — начните заново.",
                                      reply_markup=_kb((BTN_BACK_MENU, "settings_menu")))
        return ConversationHandler.END
    recipients = _recipients()
    await query.edit_message_text(f"⏳ Отправляю {len(recipients)} пользователям…")
    # В фоне: при десятках получателей рассылка идёт секунды, бот не должен ждать
    context.application.create_task(_broadcast(context.bot, msg, recipients, query.message))
    return ConversationHandler.END


async def _broadcast(bot, msg: Message, recipients: list, status_msg: Message):
    header = "📣 <b>Сообщение от администратора</b>"
    ok, failed = 0, []
    for uid in recipients:
        for _ in range(3):
            try:
                await _send_with_header(bot, uid, msg, header)
                ok += 1
                break
            except RetryAfter as e:
                # Лимит Telegram (~30 сообщений/с) — ждём, сколько просят, и повторяем
                ra = e.retry_after
                await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else ra) + 1)
            except Forbidden:
                failed.append(uid)       # заблокировал бота или удалил аккаунт
                break
            except Exception as e:
                logger.warning("Рассылка: не доставлено %s: %s", uid, e)
                failed.append(uid)
                break
        else:
            failed.append(uid)
        await asyncio.sleep(0.05)

    text = f"📣 Рассылка завершена\n\n✅ Доставлено: {ok} из {len(recipients)}"
    if failed:
        names = ", ".join(get_user_name(uid) for uid in failed)
        text += (f"\n❌ Не доставлено: {len(failed)} — {names}\n"
                 "Обычно это те, кто заблокировал бота.")
    try:
        await status_msg.edit_text(text, reply_markup=_kb((BTN_BACK_MENU, "settings_menu")))
    except Exception:
        await bot.send_message(ADMIN_ID, text)


async def notify_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _PENDING_BROADCAST.pop(query.from_user.id, None)
    await query.edit_message_text("❌ Рассылка отменена.",
                                  reply_markup=_kb((BTN_BACK_MENU, "settings_menu")))
    return ConversationHandler.END


# ── 🆘 Помощь: пользователь → админ ───────────────────────────────────────────

async def show_help_menu(query):
    await query.edit_message_text(
        "🆘 Помощь\n\n"
        "Инструкция по подключению — или напишите администратору, "
        "если что-то не работает.",
        reply_markup=_kb(("📖 Инструкция", "help"),
                         ("✉️ Написать админу", "support_start"),
                         (BTN_BACK_MENU, "back")),
    )


async def support_start(update, context: ContextTypes.DEFAULT_TYPE):
    """support_start — из меню «Помощь» (экран заменяется), support_again — кнопка
    под ответом админа (новое сообщение, чтобы не затереть сам ответ)."""
    query = update.callback_query
    uid = query.from_user.id
    if uid == ADMIN_ID or not is_approved(uid):
        await query.answer("⛔ Нет доступа.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    text = ("✉️ Сообщение администратору\n\n"
            "Опишите вопрос или проблему одним сообщением — "
            "можно приложить фото или скриншот.")
    kb = _kb((BTN_CANCEL, "support_cancel"))
    if query.data == "support_start":
        await query.edit_message_text(text, reply_markup=kb)
    else:
        await query.message.reply_text(text, reply_markup=kb)
    return WAITING_SUPPORT_MSG


async def support_receive(update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    if not is_approved(user.id):
        return ConversationHandler.END
    header = f"✉️ <b>Сообщение от {html.escape(get_user_name(user.id))}</b>"
    if user.username:
        header += f" (@{html.escape(user.username)})"
    devices = get_user_clients(user.id)
    if devices:
        header += f"\nУстройства: {html.escape(', '.join(devices))}"
    try:
        await _send_with_header(context.bot, ADMIN_ID, msg, header,
                                reply_markup=_kb(("↩️ Ответить", f"support_reply_{user.id}")))
    except Exception as e:
        logger.warning("Сообщение админу от %s не доставлено: %s", user.id, e)
        await msg.reply_text("❌ Не удалось отправить сообщение, попробуйте позже.",
                             reply_markup=_kb((BTN_BACK_MENU, "back")))
        return ConversationHandler.END
    await msg.reply_text("✅ Сообщение отправлено администратору. Ответ придёт в этот чат.",
                         reply_markup=_kb((BTN_BACK_MENU, "back")))
    return ConversationHandler.END


async def support_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Отменено.", reply_markup=_kb((BTN_BACK_MENU, "back")))
    return ConversationHandler.END


# ── ↩️ Ответ админа пользователю ──────────────────────────────────────────────

async def support_reply_start(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Только для администратора", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    uid = int(query.data.rsplit("_", 1)[1])
    context.user_data["support_reply_to"] = uid
    # Новым сообщением: сообщение пользователя с кнопкой остаётся в чате как было
    await query.message.reply_text(
        f"↩️ Ответ для {get_user_name(uid)} — напишите сообщение (текст, фото или файл).",
        reply_markup=_kb((BTN_CANCEL, "support_reply_cancel")),
    )
    return WAITING_SUPPORT_REPLY


async def support_reply_receive(update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = context.user_data.pop("support_reply_to", None)
    if uid is None:
        return ConversationHandler.END
    name = get_user_name(uid)
    try:
        await _send_with_header(context.bot, uid, msg, "💬 <b>Ответ администратора</b>",
                                reply_markup=_kb(("✉️ Ответить", "support_again")))
        await msg.reply_text(f"✅ Ответ отправлен: {name}")
    except Forbidden:
        await msg.reply_text(f"❌ Не доставлено: у {name} бот заблокирован.")
    except Exception as e:
        logger.warning("Ответ пользователю %s не доставлен: %s", uid, e)
        await msg.reply_text(f"❌ Не удалось отправить ответ: {e}")
    return ConversationHandler.END


async def support_reply_cancel(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("support_reply_to", None)
    await query.edit_message_text("❌ Ответ отменён.")
    return ConversationHandler.END
