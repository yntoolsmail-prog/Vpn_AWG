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
│   │       ├── maintenance.py  # Техобслуживание, бэкап/восстановление
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

## Структура меню бота

### Администратор (`ADMIN_ID`)

```
/start → main_menu
  ├── ▶️ Веб интерфейс 🔑   (TMA WebApp, только если TMA_URL задан)
  ├── 📱 Клиенты  → clients_menu
  │     ├── 🧲 Добавить устройство
  │     ├── 📋 Мои устройства
  │     ├── 🌍 Все устройства
  │     └── ◀️ В меню
  ├── 👥 Пользователи
  └── ⚙️ Настройки  → settings_menu
        ├── 🖥 Серверы (N)  → show_servers_list  [шапка со статистикой каждого сервера]
        ├── 📈 Трафик/пики
        ├── 🔄 Перезапустить бота
        ├── ⚡ Перезапустить AWG
        ├── 🔧 Техобслуживание
        ├── 🔑 SSH-доступ
        ├── ♻️ Обновить IP исключений
        ├── 📖 Инструкция
        └── ◀️ В меню
```

### Обычный пользователь

```
/start → main_menu
  ├── ▶️ ОТКРЫТЬ VPN 🔑   (TMA WebApp, если настроен)
  ├── 📋 Мои устройства
  ├── 🧲 Добавить устройство
  ├── 📊 Статус сервера
  └── 📖 Инструкция
```

### Навигация «Назад»
- Серверы / Трафик-пики / Техобслуживание / SSH / Инструкция → `settings_menu`
- Мои устройства / Все устройства → `clients_menu`
- SSH-доступ: `BTN_BACK_MAINT → settings_menu`

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
| `/etc/amnezia/amneziawg/servers.json` | Список slave-серверов (поле `country` — русское название страны) |
| `/var/log/awg-bw.log` | Лог трафика |

---

## Ключевые функции по модулям

### awg_core.py
| Функция | Назначение |
|---------|-----------|
| `load_users()` / `save_users()` | Работа с users.json |
| `is_approved(user_id)` | Проверка доступа пользователя |
| `load_servers()` / `save_servers()` | Список серверов (primary + slaves) |
| `create_backup()` | Архив всех конфигов |
| `build_allowed_ips(keys, domains)` | Split tunneling: AllowedIPs строка |
| `process_domain(domain)` | DNS-зондирование домена в подсети |
| `get_allowed_ips_for_client(name)` | AllowedIPs с учётом исключений клиента |
| `get_sites_json()` | Список сайтов для UI/TMA |

### awg_clients.py
| Функция | Назначение |
|---------|-----------|
| `create_client(name)` | Создать клиента (ключи + awg конфиг) |
| `remove_client_from_awg(name)` | Удалить клиента из AWG |
| `get_all_clients()` | Список всех клиентов |
| `get_awg_dump()` | `awg show` dump — трафик и handshake |
| `make_conf_for_client(name, endpoint)` | Генерация .conf файла для клиента |
| `load_client_excl(name)` / `save_client_excl(name, data)` | Исключения сплит-туннелинга |
| `make_wg_conf(...)` / `make_vpn_link(...)` | Генерация конфига / vpn:// ссылки |

### awg_stats.py
| Функция | Назначение |
|---------|-----------|
| `get_system_stats()` | CPU/RAM/диск/uptime сервера. CPU% — неблокирующая дельта против кэшированных `/proc/stat`, без `time.sleep` |
| `collect_stats_full()` | Полная статистика (для ADMIN) |
| `collect_stats_basic()` | Урезанная статистика (для юзеров) |
| `fmt_bytes(n)` | Форматирование трафика (KB/MB/GB) |
| `get_bw_histogram(days)` | Гистограмма нагрузки |
| `get_vnstat_monthly()` | Помесячный трафик через vnstat |
| `load_bw_peak()` / `save_bw_peak(data)` | Пики трафика (combined primary+slaves) |
| `get_combined_awg_dump()` | AWG dump primary+slaves, кэш 30 с; поле `server` = метка сервера с макс. handshake. На холодном кэше делает синхронный SSH к slave'ам |
| `refresh_combined_awg_dump()` | Принудительно пересобирает кэш (вызывается через executor из `bw_monitor_job`, чтобы async-хендлеры всегда получали тёплый кэш) |
| `read_iface_bytes(iface)` | Счётчики rx/tx интерфейса из /sys |

### awg_ssh.py
| Функция | Назначение |
|---------|-----------|
| `ssh_clone_awg_to_slave(server)` | Клонировать AWG-конфиг на slave |
| `ssh_sync_peer_to_slave(server, ...)` | Добавить peer на slave |
| `ssh_push_admin_key(server)` | Скопировать SSH-ключ на slave |
| `ssh_sync_mtproxy_secret(server, ...)` | Синхронизировать MTProxy на slave |
| `ssh_apply_socks5_on_slave(server, ...)` | Настроить SOCKS5 на slave |
| `ssh_regen_admin_key()` | Перегенерировать awg_admin_key |
| `ssh_get_slave_sys_stats(server)` | Системные метрики slave за одно SSH-подключение: `{awg_ok, uptime, ram_pct, disk_pct, rx_bytes, tx_bytes}` |
| `ssh_get_slave_awg_dump(server)` | AWG dump со slave по SSH |
| `ssh_read_slave_awg_bytes(server)` | Счётчики rx/tx AWG-интерфейса со slave |
| `PARAMIKO_AVAILABLE` | Флаг доступности paramiko |

### handlers/bandwidth.py
| Символ | Назначение |
|--------|-----------|
| `bw_monitor_job` | Job каждые 5 с: измеряет primary AWG скорость, добавляет slaves, пишет в peak. Параллельно прогревает `_combined_dump_cache` через `run_in_executor(refresh_combined_awg_dump)`, чтобы async-меню не блокировались SSH к slave'ам |
| `slave_bw_poll_job` | Job каждые 5 с: SSH на каждый slave, обновляет `_slave_bw_detail` и `context.bot_data["slave_bw"]` |
| `_primary_bw` | Модульный кэш — primary-only Mbit/s (до прибавления slaves). Обновляется в `bw_monitor_job`. |
| `get_primary_bw()` | Геттер `_primary_bw` — используется в `_srv_block_primary()` |
| `_slave_bw_detail` | Модульный кэш — per-server `{id: {awg_down, awg_up}}`. Обновляется в `slave_bw_poll_job`. |
| `get_slave_bw_detail()` | Геттер `_slave_bw_detail` — используется в `_srv_block_slave()` |
| `load_bw_peak()["last"]` | **Combined** (primary+slaves) — используется на экране Трафик/пики |

### handlers/servers.py
| Функция | Назначение |
|---------|-----------|
| `show_servers_list(query)` | Шапка с блоком статистики по каждому серверу подряд + кнопки карточек |
| `_srv_block_primary()` | Синхронный блок статистики основного сервера (local stats) |
| `_srv_block_slave(srv, idx)` | Async блок статистики slave: SSH с timeout 8 с через `asyncio.wait_for` |
| `show_server_card(query, idx)` | Карточка сервера: метка _(Основной)_/_(Слейв)_, только domain-эндпоинты |
| `srv_rename_start/name/emoji/country` | 3-шаговый диалог: name → emoji → country (WAITING_SRV_COUNTRY) |
| `_sync_peer_to_all_slaves()` | Async синк нового пира на все slave-серверы |

### handlers/clients.py
| Функция | Назначение |
|---------|-----------|
| `show_my_devices(query, uid)` | Список устройств пользователя, кнопка «Назад» → `clients_menu` |
| `show_all_clients(query)` | Все устройства (admin), кнопка «Назад» → `clients_menu` |
| `_show_server_select(query, name, uid, action)` | **1-й экран**: кнопка на каждый сервер (`{action}_auto_{si}_{name}`) + «Расширенная настройка». Только «Выберите сервер:», без инструкций. |
| `_show_ep_select(query, name, uid, action)` | **2-й экран** (Расширенная настройка): плоский список всех эндпоинтов с иконками 🌐/🔢 + инструкция домен/IP. Назад → `{action}_{name}` |
| `show_conf/qr/share_ep_select` | Входные точки — вызывают `_show_server_select` |

---

## Поле `country` в servers.json

```json
{
  "id": "server_1",
  "name": "NLD",
  "emoji": "🇳🇱",
  "country": "Голландия",
  ...
}
```

- Запрашивается при добавлении сервера (3-й шаг после emoji, `WAITING_SRV_COUNTRY`)
- Запрашивается при переименовании (3-й шаг, тот же `WAITING_SRV_COUNTRY = 29`)
- Используется в кнопках `_show_server_select`: `f"{emoji} {name} {country}"` → `🇳🇱 NLD Голландия`
- Enter — пропустить/оставить текущее

---

## Conversation states (common.py + slave_servers.py)

| Константа | Значение | Где используется |
|-----------|---------|-----------------|
| `WAITING_REGISTER_NAME` | 10 | Регистрация нового пользователя |
| `WAITING_DEVICE_NAME` | 11 | Добавление устройства |
| `WAITING_RESTORE_FILE` | 12 | Восстановление бэкапа |
| `WAITING_SITES_DOMAIN` | 16 | Добавление кастомного домена в исключения |
| `WAITING_SRV_IP` | 20 | Добавление slave: IP |
| `WAITING_SRV_PORT` | 21 | Добавление slave: SSH порт |
| `WAITING_SRV_LOGIN` | 22 | Добавление slave: логин |
| `WAITING_SRV_PASSWORD` | 23 | Добавление slave: пароль |
| `WAITING_SRV_NAME` | 24 | Добавление slave: имя |
| `WAITING_SRV_EMOJI` | 25 | Добавление slave: emoji |
| `WAITING_SRV_DOMAIN` | 26 | Добавление домена к серверу |
| `WAITING_SRV_EDIT_NAME` | 27 | Переименование сервера: имя |
| `WAITING_SRV_EDIT_EMOJI` | 28 | Переименование сервера: emoji |
| `WAITING_SRV_COUNTRY` | 29 | Добавление/переименование: страна |

---

## Модульная система

`module_loader.py` читает `modules.conf`, импортирует активные модули.
Каждый модуль в `modules/*/` может определять:

- `register_handlers(app)` — PTB-хендлеры
- `get_admin_menu_buttons()` — кнопки в **конец** админ-меню (после Настройки)
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
- **Bandwidth кэши:** `_primary_bw` — только основной сервер (до сложения со slaves); `_slave_bw_detail` — per-server dict; `load_bw_peak()["last"]` — combined, для экрана Трафик/пики
- **Статистика со slaves:** `get_combined_awg_dump()` агрегирует awg dump primary+slaves с кэшем 30 с; `slave_bw_poll_job` каждые 5 с опрашивает slave по SSH; `ssh_get_slave_sys_stats` получает RAM/диск/uptime/AWG-статус одним SSH-подключением
- **Прогрев кэшей в async:** `bw_monitor_job` каждые 5 с зовёт `refresh_combined_awg_dump()` через `run_in_executor`; синхронные `get_combined_awg_dump()` в `main_menu`/`_srv_block_*`/`status` всегда читают тёплый кэш и не блокируют event loop SSH-запросами
- **`do_refresh_subnets` (handlers/maintenance.py):** флаг `_SUBNET_REFRESH_RUNNING` живёт на уровне модуля maintenance (не bot.py), запускает `run_subnet_daemon()` в `threading.Thread`. Повторное нажатие во время работы показывает alert с прошедшим временем; если флаг висит >10 мин — авто-сброс. Завершение присылает новое сообщение, чтобы не затирать экран, на который ушёл пользователь
- **DNS-сбор `_collect_ips` (awg_core.py):** 18 запросов (3 раунда × 6 серверов) идут параллельно через `ThreadPoolExecutor`; результат — тот же `set` уникальных IPv4; `_dns_query` потокобезопасен (свой сокет на вызов)
- **TMA-кнопка:** `_tma_button()` в common.py → текст `"▶️ ОТКРЫТЬ VPN 🔑"` (пользовательское меню); в admin-меню создаётся отдельно с текстом `"▶️ Веб интерфейс 🔑"`

---

## Что не трогать

- `modules/tma/tma_server.py` — Flask API:
  - исключения сайтов хранятся только в `.excl.json` и применяются динамически при генерации конфига
  - `/excl PUT` валидирует домены + запускает `process_domain()` в фоне; IP/CIDR применяются напрямую без DNS
  - создание клиента синкает peer на все slave-серверы через `_sync_new_peer_to_slaves()` в фоновых потоках
  - `/backups/<name>/restore` валидирует содержимое архива перед восстановлением (наличие `*.conf` + `server.env`)
  - `/send` принимает `srv_name` в теле запроса; `_make_conf_filename(name, srv_name)` формирует нейм `User.Server.Device.conf`
  - `/vpnlink` принимает `srv_id` в query; ищет сервер в `load_servers()`, берёт `awg_public_key`/`awg_port` — аналогично боту
  - `/api/servers` отдаёт поле `country` (русское название страны) — фронт показывает «🇳🇱 NLD Голландия»
- `tma/index.html` — фронтенд TMA:
  - `_selectedSrvRawName` хранит имя сервера без emoji для нейминга; передаётся в `srv_name` при отправке `.conf`
  - `_selectedSrvId` хранит id сервера; передаётся в `srv_id` при генерации vpnlink
  - `_getSelectedEndpoint()` возвращает пустую строку если сервер не выбран; `sendConf`/`showQR`/`showVpnLink` блокируют выполнение с алертом "Выберите сервер"
  - Twemoji (`twemoji.parse`) применяется для корректного рендера эмодзи (включая флаги); вызывать `_tw(el)` после вставки innerHTML содержащего эмодзи
  - **Server picker — две стадии (как в боте):** `_showServerCountries()` рендерит кнопки «🇳🇱 NLD Голландия», `_pickServerAuto(srvId)` подбирает домен (или первый ep). «🔧 Расширенная настройка» → `_showServerEndpoints()` с плоским списком и пояснением 🌐 домен / 🔢 IP; кнопка «◀️ Назад» возвращает на 1-й экран
  - **Возврат из экрана исключений:** `openSitesModal(name)` сохраняет устройство в `_sitesParentDevice`; Отмена / Сохранить / тап по фону вызывают `_backToDeviceFromSites()`, который заново открывает `openDeviceDetail(d)`. Без этого после закрытия sites-modal пользователь оказывался в общем списке устройств
- `amnezia.gpg.asc` — GPG-ключ для верификации пакетов
