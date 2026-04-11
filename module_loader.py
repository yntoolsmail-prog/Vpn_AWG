#!/usr/bin/env python3
"""module_loader.py — загрузчик модулей AmneziaWG.

Читает modules.conf, импортирует активные модули и предоставляет реестр
для регистрации хендлеров бота, Flask Blueprint'ов и вспомогательных методов.

Использование в bot.py:
    from module_loader import load_modules
    _modules = load_modules()
    # ... после регистрации собственных хендлеров:
    _modules.register_bot_handlers(application)

Использование в tma_server.py:
    from module_loader import load_modules
    _modules = load_modules()
    _modules.register_tma_blueprints(app)

Каждый модуль — подпапка в modules/ с файлом __init__.py, который
может определять:
  register_handlers(app)       : func  — регистрирует PTB-хендлеры
  get_admin_menu_buttons()     : func  — кнопки для админ-меню (list)
  get_user_menu_buttons(uid)   : func  — кнопки для пользователя (list)
  get_background_jobs()        : func  — фоновые задачи для job_queue (list)
  health_check()               : func  — статус модуля (bool)
  TMA_BLUEPRINTS               : list  — Flask Blueprint'ы для TMA
  setup()                      : func  — вызывается один раз при загрузке
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODULES_CONF = os.path.join(os.path.dirname(__file__), "modules.conf")
MODULES_DIR  = os.path.join(os.path.dirname(__file__), "modules")


@dataclass
class ModuleRegistry:
    modules: list[Any] = field(default_factory=list)

    def register_bot_handlers(self, app) -> None:
        """Вызывает register_handlers(app) у каждого модуля."""
        for mod in self.modules:
            if callable(getattr(mod, "register_handlers", None)):
                try:
                    mod.register_handlers(app)
                except Exception as e:
                    logger.error("register_handlers '%s': %s", mod.__name__, e)

    def register_tma_blueprints(self, flask_app) -> None:
        """Регистрирует TMA_BLUEPRINTS каждого модуля в Flask-приложении."""
        for mod in self.modules:
            for bp in getattr(mod, "TMA_BLUEPRINTS", []):
                try:
                    flask_app.register_blueprint(bp)
                except Exception as e:
                    logger.error("register_blueprint '%s': %s", mod.__name__, e)

    def get_admin_menu_buttons(self) -> list:
        """Собирает кнопки для админ-меню от всех модулей."""
        buttons = []
        for mod in self.modules:
            if callable(getattr(mod, "get_admin_menu_buttons", None)):
                buttons.extend(mod.get_admin_menu_buttons())
        return buttons

    def get_user_menu_buttons(self, user_id: int) -> list:
        """Собирает кнопки для пользовательского меню от всех модулей."""
        buttons = []
        for mod in self.modules:
            if callable(getattr(mod, "get_user_menu_buttons", None)):
                buttons.extend(mod.get_user_menu_buttons(user_id))
        return buttons

    def get_background_jobs(self) -> list:
        """Собирает фоновые задачи для job_queue от всех модулей."""
        jobs = []
        for mod in self.modules:
            if callable(getattr(mod, "get_background_jobs", None)):
                jobs.extend(mod.get_background_jobs())
        return jobs

    def health_check(self) -> dict:
        """Возвращает словарь {имя_модуля: bool} — статус каждого модуля."""
        result = {}
        for mod in self.modules:
            if callable(getattr(mod, "health_check", None)):
                result[mod.__name__] = mod.health_check()
        return result


def _read_enabled_modules() -> list[str]:
    """Читает modules.conf и возвращает список активных модулей."""
    enabled: list[str] = []
    if not os.path.exists(MODULES_CONF):
        logger.warning("modules.conf не найден, модули не загружены")
        return enabled
    with open(MODULES_CONF) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            name, _, state = line.partition("=")
            name  = name.strip()
            state = state.strip().lower()
            if state == "on":
                enabled.append(name)
    return enabled


def load_modules() -> ModuleRegistry:
    """Загружает все активные модули и возвращает собранный реестр."""
    registry = ModuleRegistry()
    enabled  = _read_enabled_modules()

    for mod_name in enabled:
        mod_path = os.path.join(MODULES_DIR, mod_name)
        if not os.path.isdir(mod_path):
            logger.debug(
                "Модуль '%s' включён в modules.conf, но папка не найдена — пропуск",
                mod_name,
            )
            continue

        import_target = f"modules.{mod_name}"
        try:
            mod = importlib.import_module(import_target)
        except ImportError as e:
            logger.error("Ошибка импорта модуля '%s': %s", mod_name, e)
            continue

        # Вызываем setup() если определён
        if callable(getattr(mod, "setup", None)):
            try:
                mod.setup()
            except Exception as e:
                logger.error("Ошибка setup() модуля '%s': %s", mod_name, e)

        registry.modules.append(mod)
        logger.info("Модуль '%s' загружен", mod_name)

    return registry
