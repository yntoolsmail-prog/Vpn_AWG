"""modules/tma — Telegram Mini App (TMA) модуль.

Базовый TMA-модуль. tma_server.py регистрирует все маршруты напрямую при запуске.
Этот манифест предоставляет интерфейс для module_loader'а — сторонние модули
могут дополнять TMA через TMA_BLUEPRINTS в своих __init__.py.
"""

TMA_BLUEPRINTS = []


def register_handlers(app) -> None:
    pass


def get_admin_menu_buttons() -> list:
    return []


def get_user_menu_buttons(user_id: int) -> list:
    return []


def get_background_jobs() -> list:
    return []


def health_check() -> bool:
    return True
