# AmneziaWG VPN — Архитектурная карта

## Что это

Telegram-бот для управления AmneziaWG VPN (форк WireGuard с обфускацией).
Администратор управляет сервером через бота; пользователи получают конфиги через бота или веб-панель.

---

## Файловая карта

```
Vpn_AWG/
├── awg_core.py          # Ядро: пути/константы, пользователи, серверы, subnet-кэш, бэкап
│                        # Re-экспортирует всё из awg_clients/awg_stats/awg_ssh для совместимости
├── awg_clients.py       # Клиенты AWG: ключи, конфиги, CRUD, обфускация
├── awg_stats.py         # Трафик, полоса, гистограмма, vnstat, системная статистика
├── awg_ssh.py           # SSH-управление slave-серверами: AWG, MTProxy, SOCKS5
├── sites_data.py        # Данные сайтов для сплит-туннелинга (SITES, CATEGORIES)
├── module_loader.py     # Динамическая загрузка модулей из modules.conf
├── subnet_daemon.py     # Фоновый демон обновления подсетей сайтов
│
├── modules/
│   ├── modules.conf     # Включение/отключение модулей (bot=enabled, ...)
│   ├── bot/
│   │   ├── bot.py       # Точка входа бота: setup, start, main_menu, button_handler, main()
│   │   ├── strings.py   # Текстовые блоки: get_help_main(tma_url), HELP_DNS
│   │   └── handlers/    # Логика по функциональным группам
│   │       ├── common.py       # BTN_* константы, back_kb, _md, sites_keyboard, WAITING_*
│   │       ├── bandwidth.py    # Мониторинг трафика, статистика, пики
│   │       ├── clients.py      # Устройства: конфиги, QR, удаление, сплит-туннелинг
│   │       ├── maintenance.py  # Техобслуживание, SSH, бэкап/восстановление, часовой пояс
│   │       ├── servers.py      # Slave-серверы, DNS, синхронизация; _sync_peer_to_all_slaves()
│   │       ├── sites.py        # Исключения сайтов (split tunneling)
│   │       ├── updates.py      # Обновления репозитория, проверка IP
│   │       ├── users.py        # Управление пользователями (approve/kick)
│   │       └── help.py         # Экраны справки; вызывает get_help_main(TMA_URL)
│   ├── tma/
│   │   ├── tma_server.py # Flask HTTP API для веб-панели (TMA)
│   │   └── install.sh    # Установщик TMA-модуля
│   ├── mtproxy/
│   │   ├── __init__.py  # Управление MTProxy для Telegram
│   │   └── strings.py   # UI-строки MTProxy
│   ├── socks5/
│   │   └── __init__.py  # Управление SOCKS5-прокси
│   └── slave_servers/
│       └── slave_servers.py # Синхронизация с дополнительными серверами по SSH
│
├── setup.sh             # Интерактивный установщик; --modules/_REEXEC_BRANCH через mktemp dir
├── vpn.sh               # TUI управления (меню, диагностика, бэкапы)
└── lib/                 # Вспомогательные shell-файлы (подключаются через source)
    ├── colors.sh        # Цветовые константы (RED, GREEN, CYAN, BOLD, NC)
    ├── utils.sh         # Функции log/ok/warn/err/info
    ├── diagnostics.sh   # Диагностика системы (из vpn.sh)
    ├── ssh_setup.sh     # SSH-безопасность, fail2ban, ключи (из setup.sh)
    └── modules_setup.sh # Управление модулями (из setup.sh): установка/удаление, перезапуск awg-bot
```

---

## Поток данных

```
Пользователь (Telegram)           Пользователь (браузер/TMA)
        ↓                                   ↓
   modules/bot/bot.py          modules/tma/tma_server.py
   (Telegram PTB Application)  (Flask HTTP API, порт 8080)
        ↓                                   ↓
   ╔══════════════════════════════════════════════╗
   ║              awg_core.py                     ║
   ║  (бизнес-логика, re-экспорт awg_clients/     ║
   ║   awg_stats/awg_ssh)                         ║
   ╚══════════════════════════════════════════════╝
     ├── /etc/amnezia/amneziawg/<iface>.conf  (awg конфиг)
     ├── /etc/amnezia/amneziawg/users.json    (права пользователей)
     ├── /etc/amnezia/amneziawg/clients/      (конфиги клиентов)
     └── awg-quick / wg команды               (системные утилиты)
        ↓
   sites_data.py               ← данные сайтов для сплит-туннелинга
   subnet_daemon.py            ← фоновое обновление подсетей

Синк на slave: awg_ssh.ssh_sync_peer_to_slave() — общее ядро;
  бот вызывает через _sync_peer_to_all_slaves() (async, handlers/servers.py),
  TMA — через _sync_new_peer_to_slaves() (threading, tma_server.py).
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

## Ключевые функции по модулям

### awg_core.py (542 строки)
| Функция | Назначение |
|---------|-----------|
| `load_users()` / `save_users()` | Работа с users.json |
| `is_approved(user_id)` | Проверка доступа пользователя |
| `load_servers()` / `save_servers()` | Список slave-серверов |
| `create_backup()` | Архив всех конфигов |
| `build_allowed_ips(keys, domains)` | Split tunneling: AllowedIPs строка |
| `process_domain(domain)` | DNS-зондирование домена в подсети |
| `get_allowed_ips_for_client(name)` | AllowedIPs с учётом исключений клиента |
| `get_sites_json()` | Список сайтов для UI/TMA |

### awg_clients.py (346 строк)
| Функция | Назначение |
|---------|-----------|
| `create_client(name)` | Создать клиента (ключи + awg конфиг) |
| `remove_client_from_awg(name)` | Удалить клиента из AWG |
| `get_all_clients()` | Список всех клиентов |
| `get_awg_dump()` | `awg show` dump — трафик и handshake |
| `make_conf_for_client(name, endpoint)` | Генерация .conf файла для клиента |
| `load_client_excl(name)` / `save_client_excl(name, data)` | Исключения сплит-туннелинга |
| `make_wg_conf(...)` / `make_vpn_link(...)` | Генерация конфига / vpn:// ссылки |

### awg_stats.py (481 строка)
| Функция | Назначение |
|---------|-----------|
| `get_system_stats()` | CPU/RAM/диск сервера |
| `collect_stats_full()` | Полная статистика (для ADMIN) |
| `collect_stats_basic()` | Урезанная статистика (для юзеров) |
| `fmt_bytes(n)` | Форматирование трафика (KB/MB/GB) |
| `get_bw_histogram(days)` | Гистограмма нагрузки |
| `get_vnstat_monthly()` | Помесячный трафик через vnstat |
| `load_bw_peak()` / `save_bw_peak(data)` | Пики трафика |

### awg_ssh.py (728 строк)
| Функция | Назначение |
|---------|-----------|
| `ssh_clone_awg_to_slave(server)` | Клонировать AWG-конфиг на slave |
| `ssh_sync_peer_to_slave(server, ...)` | Добавить peer на slave |
| `ssh_push_admin_key(server)` | Скопировать SSH-ключ на slave |
| `ssh_sync_mtproxy_secret(server, ...)` | Синхронизировать MTProxy на slave |
| `ssh_apply_socks5_on_slave(server, ...)` | Настроить SOCKS5 на slave |
| `ssh_regen_admin_key()` | Перегенерировать awg_admin_key |
| `PARAMIKO_AVAILABLE` | Флаг доступности paramiko |

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

## UI-строки и кнопки

- `modules/bot/handlers/common.py` — все кнопки навигации (BTN_BACK, BTN_CANCEL и др.)
- `modules/bot/strings.py` — только большие тексты: `get_help_main(tma_url)`, `HELP_DNS`
- `modules/mtproxy/strings.py` — тексты и кнопки MTProxy

---

## Соглашения

- **Язык кода:** Python 3.10+, Bash
- **Комментарии и UI:** русский язык
- **Форматирование Telegram:** `parse_mode="Markdown"` (не MarkdownV2)
- **Права:** только `ADMIN_ID` имеет полный доступ; остальные через `is_approved()`
- **Блокировки:** `_AWG_LOCK` в awg_clients.py защищает одновременное создание клиентов
- **Shell-скрипты:** вспомогательные функции в `lib/*.sh`, подключаются через `source`
- **Re-экспорт `awg_core`:** `from awg_core import *` не реэкспортирует функции с `_` префиксом — для них нужен явный импорт из оригинального модуля (`awg_stats`, `awg_clients`, `awg_ssh`)
- **Кнопки меню:** каждая кнопка на отдельной строке (`[btn]`), не группировать по две в строку
- **Удалённый функционал:** «Сменить часовой пояс» и «Обновить IP сервера» — убраны из бота полностью (handlers, ConversationHandler, job)
- **Статистика со slaves:** `get_combined_awg_dump()` в `awg_stats.py` — агрегирует awg dump primary + slaves с кэшем 30 с; `slave_bw_poll_job` каждые 5 с опрашивает slave по SSH для real-time Mbit/s (5 с = тот же интервал что bw_monitor_job, чтобы захватывать пики)

---

## Что не трогать

- `modules/tma/tma_server.py` — Flask API (все вызовы get_awg_dump заменены на get_combined_awg_dump — статистика учитывает slaves):
  - исключения сайтов хранятся только в `.excl.json` и применяются динамически при генерации конфига
  - `/excl PUT` валидирует домены + запускает `process_domain()` в фоне; IP/CIDR применяются напрямую без DNS
  - создание клиента синкает peer на все slave-серверы через `_sync_new_peer_to_slaves()` в фоновых потоках
  - `/backups/<name>/restore` валидирует содержимое архива перед восстановлением (наличие `*.conf` + `server.env`)
  - `/send` принимает `srv_name` в теле запроса; `_make_conf_filename(name, srv_name)` формирует нейм `User.Server.Device.conf`
  - `/vpnlink` принимает `srv_id` в query; ищет сервер в `load_servers()`, берёт `awg_public_key`/`awg_port` — аналогично боту
- `tma/index.html` — фронтенд TMA:
  - `_selectedSrvRawName` хранит имя сервера без emoji для нейминга; передаётся в `srv_name` при отправке `.conf`
  - `_selectedSrvId` хранит id сервера; передаётся в `srv_id` при генерации vpnlink
  - `_getSelectedEndpoint()` возвращает пустую строку если сервер не выбран; `sendConf`/`showQR`/`showVpnLink` блокируют выполнение с алертом "Выберите сервер"
  - Twemoji (`twemoji.parse`) применяется для корректного рендера эмодзи (включая флаги) в лейбле сервера и пикере
- Конфигурационные файлы в `/etc/amnezia/` — создаются при установке
- `amnezia.gpg.asc` — GPG-ключ для верификации пакетов
