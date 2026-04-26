"""modules/slave_servers — модуль добавления slave-серверов.

Регистрирует ConversationHandler для диалога ➕ Добавить сервер
и добавляет соответствующую кнопку в список серверов через get_admin_menu_buttons().
"""

from .slave_servers import register_handlers  # noqa: F401


def setup() -> None:
    pass


def get_admin_menu_buttons() -> list:
    return []
