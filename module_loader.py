#!/usr/bin/env python3
"""module_loader.py — загрузчик модулей AmneziaWG.

Читает modules.conf, импортирует активные модули и собирает:
  - bot_handlers  : список (handler_type, handler) для регистрации в Application
  - tma_blueprints: список Flask Blueprint для регистрации в app

Использование в bot.py:
    from module_loader import load_modules
    extra = load_modules()
    for h in extra.bot_handlers:
        application.add_handler(h)

Использование в tma_server.py:
    from module_loader import load_modules
    extra = load_modules()
    for bp in extra.tma_blueprints:
        app.register_blueprint(bp)

Каждый модуль — это подпапка в modules/ с файлом __init__.py, который
может определять:
  BOT_HANDLERS   : list  — хендлеры для Telegram-бота
  TMA_BLUEPRINTS : list  — Flask Blueprint'ы для TMA
  setup()        : func  — вызывается один раз при загрузке модуля
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
    bot_handlers:   list[Any] = field(default_factory=list)
    tma_blueprints: list[Any] = field(default_factory=list)


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
            logger.debug("Модуль '%s' включён в modules.conf, но папка не найдена — пропуск", mod_name)
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

        # Собираем хендлеры бота
        for h in getattr(mod, "BOT_HANDLERS", []):
            registry.bot_handlers.append(h)

        # Собираем Flask Blueprint'ы
        for bp in getattr(mod, "TMA_BLUEPRINTS", []):
            registry.tma_blueprints.append(bp)

        logger.info("Модуль '%s' загружен", mod_name)

    return registry
