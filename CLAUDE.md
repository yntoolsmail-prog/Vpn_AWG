# AmneziaWG VPN — Архитектурная карта

## Что это

Telegram-бот для управления AmneziaWG VPN (форк WireGuard с обфускацией).
Администратор управляет сервером через бота; пользователи получают конфиги через бота или веб-панель.

---

## Файловая карта

```
Vpn_AWG/
├── awg_core.py          # Ядро: конфиги, клиенты, статистика, SSH, утилиты
├── sites_data.py        # Данные сайтов для сплит-туннелинга (SITES, CATEGORIES)
├── module_loader.py     # Динамическая загрузка модулей из modules.conf
├── subnet_daemon.py     # Фоновый демон обновления подсетей сайтов
│
├── modules/
│   ├── modules.conf     # Включение/отключение модулей (bot=enabled, ...)
│   ├── bot/
│   │   ├── bot.py       # Точка входа бота: setup, start, main_menu, button_handler, main()
│   │   ├── strings.py   # Все статичные UI-строки и кнопочные константы
│   │   └── handlers/    # Логика по функциональным группам
│   │       ├── common.py       # Хелперы: back_kb, _md, sites_keyboard, WAITING_* константы
│   │       ├── bandwidth.py    # Мониторинг трафика, статистика, пики
│   │       ├── clients.py      # Устройства: конфиги, QR, удаление, сплит-туннелинг
│   │       ├── maintenance.py  # Техобслуживание, SSH, бэкап/восстановление, часовой пояс
│   │       ├── servers.py      # Slave-серверы, DNS, синхронизация
│   │       ├── sites.py        # Исключения сайтов (split tunneling)
│   │       ├── updates.py      # Обновления репозитория, проверка IP
│   │       ├── users.py        # Управление пользователями (approve/kick)
│   │       └── help.py         # Экраны справки
│   ├── tma/
│   │   ├── tma_server.py # Flask HTTP API для веб-панели (TMA)
│   │   └── install.sh    # Установщик TMA-модуля
│   ├── mtproxy/
│   │   ├── __init__.py  # Управление MTProxy для Telegram
│   │   └── strings.py   # UI-строки MTProxy
│   ├── socks5/
│   │   ├── __init__.py  # Управление SOCKS5-прокси
│   │   └── strings.py   # UI-строки SOCKS5
│   └── slave_servers/
│       └── slave_servers.py # Синхронизация с дополнительными серверами по SSH
│
├── setup.sh             # Интерактивный установщик системы
├── vpn.sh               # TUI управления (меню, диагностика, бэкапы)
└── lib/                 # Вспомогательные shell-файлы (подключаются через source)
    ├── colors.sh        # Цветовые константы (RED, GREEN, CYAN, BOLD, NC)
    └── utils.sh         # Функции log/ok/warn/err/info
```

---

## Поток данных

```
Пользователь (Telegram)
        ↓
   modules/bot/bot.py          ← Telegram PTB Application
        ↓
   awg_core.py                 ← вся бизнес-логика
     ├── /etc/amnezia/amneziawg/<iface>.conf  (awg конфиг)
     ├── /etc/amnezia/amneziawg/users.json    (права пользователей)
     ├── /etc/amnezia/amneziawg/clients/      (конфиги клиентов)
     └── awg-quick / wg команды               (системные утилиты)
        ↓
   sites_data.py               ← данные сайтов для сплит-туннелинга
   subnet_daemon.py            ← фоновое обновление подсетей
```

---

## Конфигурационные файлы (runtime, не в репо)

| Путь | Содержимое |
|------|-----------|
| `/etc/amnezia/amneziawg/bot.env` | `BOT_TOKEN`, `ADMIN_ID` |
| `/etc/amnezia/amneziawg/server.env` | `SERVER_IP`, `SERVER_PORT`, `VPN_SUBNET`, `VPN_IFACE`, `TIMEZONE`, ... |
| `/etc/amnezia/amneziawg/users.json` | Пользователи и их права (approved/admin) |
| `/etc/amnezia/amneziawg/clients/` | Конфиги и ключи каждого клиента |
| `/etc/amnezia/amneziawg/servers.json` | Список slave-серверов |
| `/var/log/awg-bw.log` | Лог трафика |

---

## Ключевые функции awg_core.py

| Функция | Назначение |
|---------|-----------|
| `create_client(user_id, name)` | Создать клиента (ключи + awg конфиг) |
| `remove_client_from_awg(pubkey)` | Удалить клиента из AWG |
| `get_all_clients()` | Список всех клиентов из конфига AWG |
| `get_user_clients(user_id)` | Клиенты конкретного пользователя |
| `get_awg_dump()` | `wg show` dump — трафик и handshake |
| `make_conf_for_client(name)` | Генерация .conf файла для клиента |
| `load_users()` / `save_users()` | Работа с users.json |
| `is_approved(user_id)` | Проверка доступа пользователя |
| `create_backup()` | Архив всех конфигов |
| `get_system_stats()` | CPU/RAM/диск сервера |
| `fmt_bytes(n)` | Форматирование трафика (KB/MB/GB) |
| `process_domain(domain)` | Резолв домена в подсети |

---

## Модульная система

`module_loader.py` читает `modules.conf`, импортирует активные модули.
Каждый модуль в `modules/*/` может определять:

- `register_handlers(app)` — PTB-хендлеры
- `get_admin_menu_buttons()` — кнопки в админ-меню
- `get_user_menu_buttons(uid)` — кнопки для пользователя
- `get_background_jobs()` — фоновые задачи
- `TMA_BLUEPRINTS` — Flask Blueprint'ы для tma_server.py
- `setup()` — инициализация при загрузке

---

## UI-строки

Все статичные строки Telegram-интерфейса вынесены в `strings.py` внутри каждого модуля:
- `modules/bot/strings.py` — строки бота
- `modules/mtproxy/strings.py` — строки MTProxy
- `modules/socks5/strings.py` — строки SOCKS5

---

## Соглашения

- **Язык кода:** Python 3.10+, Bash
- **Комментарии и UI:** русский язык
- **Форматирование Telegram:** `parse_mode="Markdown"` (не MarkdownV2)
- **Права:** только `ADMIN_ID` имеет полный доступ; остальные через `is_approved()`
- **Блокировки:** `_AWG_LOCK` в awg_core.py защищает одновременное создание клиентов
- **Shell-скрипты:** вспомогательные функции в `lib/*.sh`, подключаются через `source`

---

## Что не трогать

- `modules/tma/tma_server.py` — Flask API, минимум текста, хорошо структурирован
- Конфигурационные файлы в `/etc/amnezia/` — создаются при установке
- `amnezia.gpg.asc` — GPG-ключ для верификации пакетов
