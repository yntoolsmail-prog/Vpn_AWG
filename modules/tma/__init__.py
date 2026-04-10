"""modules/tma — Telegram Mini App (TMA) модуль.

Этот модуль — обёртка над tma_server.py.
Основной процесс запускается напрямую через `python tma_server.py` или systemd.
При использовании module_loader этот манифест позволяет модулю регистрировать
дополнительные Flask Blueprint'ы, которые другие модули могут добавить в TMA.

BOT_HANDLERS и TMA_BLUEPRINTS остаются пустыми для базового TMA модуля —
сам tma_server.py регистрирует свои маршруты внутри при запуске.
"""

BOT_HANDLERS   = []
TMA_BLUEPRINTS = []


def setup():
    """Вызывается module_loader'ом при инициализации."""
    pass
