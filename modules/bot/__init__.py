"""modules/bot — Telegram-бот модуль.

Базовый модуль бота. Бот регистрирует все свои хендлеры напрямую в bot.py.
Этот манифест предоставляет интерфейс для module_loader'а — сторонние модули
могут дополнять бот через register_handlers(app) в своих __init__.py.
"""


def register_handlers(app) -> None:
    """Вызывается module_loader'ом. Базовый бот регистрирует хендлеры сам в bot.py."""
    pass


def get_admin_menu_buttons() -> list:
    return []


def get_user_menu_buttons(user_id: int) -> list:
    return []


def get_background_jobs() -> list:
    return []


def health_check() -> bool:
    return True
