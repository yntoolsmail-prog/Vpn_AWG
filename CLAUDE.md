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
├── awg_clients.py       # Клиенты AWG: ключи, конфиги, CRUD, обфускация, перевод на AWG 3.1
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
│   │       ├── support.py      # 📣 Рассылка всем, 🆘 Помощь: пользователь ↔ админ через бота
│   │       └── help.py         # Экраны справки; вызывает get_help_main(TMA_URL, is_awg3(gen_obfs()))
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
├── vpn.sh               # TUI управления (меню, диагностика, бэкапы, п. 15 — перевод на AWG 3.1)
├── docs/AWG_3.1.md      # Разбор протокола 2.0 → 3.0 → 3.1: параметры, совместимость, выбранные значения
└── lib/                 # Вспомогательные shell-файлы (подключаются через source)
    ├── colors.sh        # Цветовые константы (RED, GREEN, CYAN, BOLD, NC)
    ├── utils.sh         # Функции log/ok/warn/err/info
    ├── diagnostics.sh   # Диагностика системы (из vpn.sh); ролевая — см. ниже
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
        ├── 📣 Уведомления  → рассылка всем одобренным (с подтверждением)
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
  └── 🆘 Помощь  → help_menu
        ├── 📖 Инструкция  (Назад → help_menu)
        ├── ✉️ Написать админу
        └── ◀️ В меню
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
| `/etc/amnezia/amneziawg/server.env` | `SERVER_IP`, `SERVER_PORT`, `VPN_SUBNET`, `VPN_IFACE`, `TIMEZONE`, ...; обфускация `JC…H4`, у AWG 3.1 ещё `S3`, `S4`, `I1` (в кавычках), `HEADER_PROTECTION_KEY`, `REKEY_AFTER_TIME`, `REKEY_TIMEOUT`, `REJECT_AFTER_TIME`, `KEEPALIVE_TIMEOUT`, `MAX_HANDSHAKE_ATTEMPTS`, `RANDOM_TRAILERS`, `DISABLE_COOKIES`, `CLIENT_MTU`, `PERSISTENT_KEEPALIVE`; опционально `CONTENT_PADDING_ADDITION`, `I2–I5` |
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
| `RESERVED_USER_NAMES` | `{"admin","user"}` — запрещены при регистрации: владелец устройства определяется по префиксу имени, заняв «Admin» юзер получил бы устройства админа |
| `awg_file_lock()` | Межпроцессный `flock` на `.awg.lock` — бот и TMA разные процессы, `threading.Lock` их не разводит |
| `load_servers()` / `save_servers()` | Список серверов (primary + slaves) |
| `create_backup()` | Архив всех конфигов |
| `post_restore_fixup()` | **После распаковки бэкапа**: раскладывает `ssh/awg_admin_key`→`/root/.ssh/`, `modules.conf`→`/root/`, `bot_persistence.pkl`→`/etc/awg-bot/`, `proxy_bot.env`→`/etc/proxy-bot/`; переписывает NAT-интерфейс в PostUp/PostDown под текущий хост, `SERVER_IP` и primary в servers.json под новый VPS, пересобирает `server_public/private.key` из конфига. Ключ из бэкапа заменяет сгенерированный установщиком и прописывается в `authorized_keys` вместо него (строки с `awg-admin` чистятся, чужие ключи не трогаются); при отсутствии ключа в архиве — явное предупреждение, т.к. slave'ы пускают только по ключу. Отдельной строкой сообщает состояние `PasswordAuthentication`: `sshd_config` в бэкап не входит намеренно. Если бэкап с сервера на AWG 3.1, а пакеты здесь старше — предупреждает (`awg31_blockers`): интерфейс не поднимется. Без всей функции переезд на другой ВПС даёт VPN без интернета и потерю управления slave'ами |
| `_harden_secret_perms()` | `0600` на конфиги клиентов, бэкапы, users.json, server.env |
| `is_safe_client_name(name)` | Имя безопасно для подстановки в путь: только `[A-Za-z0-9_.-]`, без `..`, не начинается с `.`/`-`. Пропускает все реальные форматы — `User.Device` (бот), дефисы (TMA), имена без точки (vpn.sh) |
| `can_access_device(user_id, name)` | Права на устройство. Сначала `is_safe_client_name()` — имя с выходом из каталога отклоняется для всех, включая админа |
| `build_allowed_ips(keys, domains)` | Split tunneling: AllowedIPs строка |
| `process_domain(domain)` | DNS-зондирование домена в подсети |
| `get_allowed_ips_for_client(name)` | AllowedIPs с учётом исключений клиента |
| `get_sites_json()` | Список сайтов для UI/TMA |

### awg_clients.py
| Функция | Назначение |
|---------|-----------|
| `create_client(name)` | Создать клиента (ключи + awg конфиг), под `awg_file_lock()` |
| `remove_client_from_awg(name)` | Удалить клиента из AWG **и со всех slave** (через `_remove_peer_from_all_slaves`) |
| `_remove_peer_from_all_slaves(name, pub)` | Фоновые потоки: снимает peer с каждого slave. Вызывается изнутри `remove_client_from_awg`, поэтому покрывает все пути удаления — бот, TMA, kick пользователя |
| `get_all_clients()` | Список всех клиентов |
| `get_awg_dump()` | `awg show` dump — трафик и handshake |
| `make_conf_for_client(name, endpoint)` | Генерация .conf файла для клиента |
| `load_client_excl(name)` / `save_client_excl(name, data)` | Исключения сплит-туннелинга |
| `make_wg_conf(...)` / `make_vpn_link(...)` | Генерация конфига / vpn:// ссылки. Параметры AWG — по `AWG_PARAMS`, пустые пропускаются. Для конфига 3.x ещё `MTU` (1376) и `PersistentKeepalive` диапазоном (`_client_tunnel_opts`); в `vpn://` — `protocol_version: 3.1`, ключи как у AmneziaVPN (`I1`, `HeaderProtectionKey`, …) |
| `AWG_PARAMS` | Кортеж «ключ конфига ↔ переменная server.env» для всех параметров обфускации 1.0–3.1, в порядке записи. Единственный источник набора ключей: `gen_obfs`, `get_client_keys`, `make_*`, перевод на 3.1 |
| `gen_obfs(env=None)` | Параметры для конфигов клиентов из server.env (или переданного `env`). Параметр 3.x попадает, только если задан — сервер на 2.0 выдаёт прежние конфиги. `I1–I5` без тегов `<…>` (старое `i1 = <число>`, пакет нулевой длины) отбрасываются |
| `is_awg3(obfs)` | Есть ли параметры 3.x (как `hasAwg3Markers` в AmneziaVPN): ключ защиты, тайминги, `ContentPaddingAddition` или включённые `RandomTrailers`/`DisableCookies` |
| `conf_awg_params(text)` | Параметры AWG из секции `[Interface]` текста конфига (регистр ключей не важен) |
| `gen_awg31_env(current)` | Новые переменные server.env для 3.1 поверх текущих: сохраняет Jc/Jmin/Jmax и одиночные H1–H4, S1/S2 15–64, S3 12–32, S4 12–20, новый ключ `awg genkey`, тайминги и прочее из `AWG31_DEFAULTS`. `setup.sh` генерирует то же самое — менять оба места |
| `migrate_to_awg31(skip_unready_slaves)` | Перевод работающего сервера 2.0 → 3.1: версии здесь и на слейвах (`awg31_blockers`) → бэкап `pre_awg31_*` → server.env, `awg0.conf`, все `clients/*.conf` под `awg_file_lock()` → перезапуск AWG с проверкой ключа и `random-trailers` на интерфейсе (при сбое файлы возвращаются, интерфейс пересоздаётся) → `ssh_clone_awg_to_slave` на готовые слейвы. Возвращает `(ok, отчёт, число_неготовых_слейвов)`. Бот и TMA держат server.env в памяти — после перевода их перезапускают (`vpn.sh` делает сам) |

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
| `ssh_clone_awg_to_slave(server)` | Клонировать AWG-конфиг на slave; сохраняет PostUp/PostDown слейва (у него свой внешний интерфейс; если их нет — строит под его default-интерфейс) и ставит маркер `/etc/awg-slave`. Перезапускает AWG через systemd (`stop` → `awg-quick down` → `enable --now`), чтобы служба знала о поднятом интерфейсе. Проверяет результат: код записи конфига, код `awg-quick up`, ключ сервера и Jc/Jmin/Jmax/S1–S4/H1–H4/ключ защиты/`random-trailers` на интерфейсе слейва против primary (`_OBFS_PARAMS`, ключ в тексте ошибки маскируется); при расхождении бросает исключение — слейв с чужими H1–H4 молча отбрасывает пакеты клиентов. Если конфиг primary на 3.x, **до** любых действий сверяет версии слейва (`awg31_blockers`) и отказывается, не трогая его рабочий конфиг: иначе интерфейс уже остановлен, а новый не поднимется |
| `awg_versions_local()` / `ssh_get_awg_versions(server)` | `{tools, mod_loaded, mod_disk}` — версии утилит и модуля (загруженного и на диске) здесь / на слейве, скрипт `_VERSIONS_SCRIPT` |
| `awg31_blockers(versions)` | Список причин, почему AWG 3.1 не поднимется (утилиты < 3.1, модуль < 3.1, «установлен новый — нужна перезагрузка»); пустой — готов |
| `ssh_sync_peer_to_slave(server, ...)` | Добавить peer на slave. Проверяет коды возврата и контрольно грепает ключ в `awg0.conf` и в `awg show`; бросает исключение при провале — раньше коды отбрасывались и молчаливый несинк был не виден |
| `ssh_remove_peer_from_slave(server, name, pub)` | Снять peer со slave: `awg set … remove` + вырезание блока из `awg0.conf` через `sed '/^# Client: name$/,/^AllowedIPs/d'`; бросает исключение, если ключ остался |
| `ssh_push_admin_key(server)` | Скопировать SSH-ключ на slave |
| `ssh_sync_mtproxy_secret(server, ...)` | Синхронизировать MTProxy на slave |
| `ssh_apply_socks5_on_slave(server, ...)` | Настроить SOCKS5 на slave |
| `ssh_regen_admin_key()` | Перегенерировать awg_admin_key |
| `ssh_get_slave_sys_stats(server)` | Системные метрики slave за одно SSH-подключение: `{awg_ok, uptime, ram_pct, disk_pct, rx_bytes, tx_bytes}`. `awg_ok` — по наличию интерфейса (`/sys/class/net/<iface>`), а не по `systemctl is-active`: поднятый вручную AWG служба считает inactive |
| `ssh_get_slave_awg_dump(server)` | AWG dump со slave по SSH |
| `ssh_read_slave_awg_bytes(server)` | Счётчики rx/tx AWG-интерфейса со slave |
| `upgrade_all_servers()` | Обновление пакетов на primary (локально, `upgrade_packages_local`) и всех slave (`ssh_upgrade_packages`) параллельно. Общий `_UPGRADE_SCRIPT`: `apt-get upgrade --with-new-pkgs` (без флага новые ядра остаются «kept back»), `DEBIAN_FRONTEND=noninteractive` + `--force-confold` (иначе вопрос dpkg вешает обновление), `NEEDRESTART_MODE=l`, `DPkg::Lock::Timeout`. Вывод apt — в `/var/log/awg-upgrade.log`, наружу итог `KEY=VALUE`: код, число пакетов, нужна ли перезагрузка, версии утилит awg и модуля (загружен / на диске). Возвращает `[{label, primary, ok, upgraded, reboot, tools, mod_loaded, mod_disk, error}]`, primary первым |
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

### handlers/maintenance.py
| Функция | Назначение |
|---------|-----------|
| `do_maint_upgrade(query)` | «💿 Бэкап + обновление всех серверов»: бэкап, затем `upgrade_all_servers()` в фоне (`_UPGRADE_TASK`, повторное нажатие блокирует `_UPGRADE_RUNNING`). Отчёт по каждому серверу: пакеты, версии AWG «загружен → после перезагрузки», нужна ли перезагрузка, предупреждение о разных версиях модуля. Серверы бот не перезагружает (клиенты отключились бы) — только советует порядок: слейв → проверка → остальные → основной. Всё прошло — `log_maintenance_done()`; пакеты на основном менялись — перезапуск бота уже после отчёта |
| `maintenance_reminder` | Раз в 6 месяцев напоминает обновить пакеты на всех серверах |

### handlers/servers.py
| Функция | Назначение |
|---------|-----------|
| `show_servers_list(query)` | Шапка с блоком статистики по каждому серверу подряд + кнопки карточек |
| `_srv_block_primary()` | Синхронный блок статистики основного сервера (local stats) |
| `_srv_block_slave(srv, idx)` | Async блок статистики slave: SSH с timeout 8 с через `asyncio.wait_for` |
| `show_server_card(query, idx)` | Карточка сервера: метка _(Основной)_/_(Слейв)_, только domain-эндпоинты |
| `srv_rename_start/name/emoji/country` | 3-шаговый диалог: name → emoji → country (WAITING_SRV_COUNTRY) |
| `_sync_peer_to_all_slaves()` | Async синк нового пира на все slave-серверы; возвращает список ошибок — их показывают пользователю и шлют админу, а не только пишут в лог |
| `_check_slaves_sync(context)` | Job раз в 30 мин: сверяет число пиров primary ↔ каждый slave, шлёт админу алерт с кнопкой «Синхронизировать» при появлении расхождения и «восстановлено» при устранении. Состояние в `_slave_sync_state` — сообщение только на смену состояния, без спама. Потеря связи (`_slave_unreachable`) сообщается после 3 неудач подряд (~1.5 ч) и один раз: это главный симптом «SSH-ключ не подошёл» после восстановления из бэкапа |

### handlers/support.py
| Функция | Назначение |
|---------|-----------|
| `notify_start/receive/send/cancel` | «📣 Уведомления»: админ пишет любое сообщение (текст, фото, файл) → «Отправить N пользователям?» → `_broadcast()` в фоне шлёт всем одобренным, кроме админа; `RetryAfter` — ждём и повторяем, `Forbidden` — в итог «не доставлено» с именами. Черновик — в `_PENDING_BROADCAST`, не в `user_data` (та пишется в PicklePersistence) |
| `show_help_menu(query)` | «🆘 Помощь» пользователя: инструкция / написать админу |
| `support_start/receive/cancel` | Пользователь → админ. Только одобренные, без лимитов. `support_start` из меню заменяет экран, `support_again` (кнопка под ответом админа) шлёт новое сообщение, чтобы не затереть ответ. Админу приходит «✉️ Сообщение от Имя (@ник), устройства» + кнопка «↩️ Ответить» (`support_reply_<uid>`) |
| `support_reply_start/receive/cancel` | Админ → пользователь: «💬 Ответ администратора» + кнопка «✉️ Ответить» |
| `_send_with_header(bot, chat, msg, header)` | Заголовок + содержимое одним сообщением: текст — `send_message` с `text_html`, медиа — `copy_message` с новой подписью; стикер/кружок/текст на пределе длины — заголовок отдельно, содержимое копией |

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
- Пропустить/оставить текущее — inline-кнопкой под вопросом (`skip_kb` / `read_text_or_skip` в common.py, в модуле slave_servers — своя копия). «Нажмите Enter» не работает: Telegram не отправляет пустое сообщение и сообщение из одних пробелов

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
| `WAITING_BROADCAST_MSG` | 30 | Рассылка: админ пишет сообщение |
| `WAITING_BROADCAST_CONFIRM` | 31 | Рассылка: подтверждение |
| `WAITING_SUPPORT_MSG` | 32 | Пользователь пишет админу |
| `WAITING_SUPPORT_REPLY` | 33 | Админ отвечает пользователю |

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
- **Форматирование Telegram:** `parse_mode="Markdown"` (не MarkdownV2). Исключение — пересылка чужих сообщений в `support.py`: там HTML (`Message.text_html`/`caption_html` + `html.escape` для имён), потому что Markdown v1 не умеет подчёркивание/зачёркивание и ломается на спецсимволах из текста пользователя
- **Права:** только `ADMIN_ID` имеет полный доступ; остальные через `is_approved()`
- **Паритет slave ↔ primary:** slave — полная копия primary, поэтому ЛЮБАЯ операция с устройством должна доезжать до всех slave. Добавление — `_sync_peer_to_all_slaves` (бот) / `_sync_new_peer_to_slaves` (TMA); удаление — внутри самого `remove_client_from_awg`, чтобы ни один путь удаления не мог его пропустить. Исключения сайтов серверного состояния не имеют (живут в `.conf` клиента) и синка не требуют
- **Блокировки:** `awg_file_lock()` (flock на `/etc/amnezia/amneziawg/.awg.lock`) защищает `awg0.conf` при создании и удалении клиентов между процессами бота и TMA. `_AWG_LOCK` оставлен только для обратной совместимости
- **Ответы на callback:** `button_handler` — тонкая обёртка, гасит «часики» ПОСЛЕ `_button_dispatch`. Ранний `query.answer()` съедал ответ, и алерты `show_alert=True` не показывались
- **Исключения сайтов — только IPv4:** `build_allowed_ips` считает дополнение IPv4-пространства; IPv6-запись роняет `collapse_addresses` с `TypeError`. Валидация в `sites.py` и `tma_server.py` отклоняет IPv6 явно
- **Протокол AWG 3.1 (ветка `experimental`, подробно — `docs/AWG_3.1.md`):** новый сервер получает 3.1, если утилиты и загруженный модуль ≥ 3.1 (иначе `setup.sh` ставит 2.0 и предупреждает); работающий переводится явно — `vpn.sh` → 15 (`migrate_to_awg31`). Интерфейс с `HeaderProtectionKey` не пускает клиентов 2.0 — перевод ломает все старые конфиги. Обязаны совпадать S1–S4, H1–H4, `HeaderProtectionKey`, `RandomTrailers`; I1–I5 — только в конфигах клиентов, в `awg0.conf` сервера их нет. S1–S4 ≥ 12 (нонс защиты заголовков), S4 ≤ 20 (MTU). H1–H4 — одиночные числа: с `RandomTrailers` ядро опознаёт тип по «размер ≥ и H в диапазоне», и широкий диапазон выдавал бы пакеты данных за хендшейк. Живой интерфейс с ключом не перенастроить на S1=0 через `setconf` (`EINVAL`) — только пересоздание (`awg-quick down/up`)
- **Кавычки в server.env:** его читают и `load_env()`, и bash (`source` в `vpn.sh`). Значения с пробелами или `< >` (теги `I1`) пишутся в одинарных кавычках (`_env_quote`), `load_env()` их снимает
- **Никаких приватных ключей на диске вне `CLIENTS_DIR`:** `.conf`/QR отправляются из памяти, qrencode вызывается через stdin→stdout (`-t PNG -o -`)
- **Имя устройства из URL — два барьера:** роуты TMA объявлены как `<string:name>` (конвертер не пропускает слэш), плюс `is_safe_client_name()` внутри `can_access_device`. Не менять на `<path:name>`: имя клеится в `f"{CLIENTS_DIR}/{name}.conf"`, и `Ivan./../../awg0` дал бы QR с приватным ключом сервера
- **Shell-скрипты:** вспомогательные функции в `lib/*.sh`, подключаются через `source`
- **AWG поднимается только через systemd** (`systemctl enable --now awg-quick@<iface>`), не голым `awg-quick up`: иначе служба до перезагрузки числится inactive, «Перезапустить AWG» падает на «already exists», а статус в боте врёт
- **Чистая установка `bash <(curl …/setup.sh)`:** `$0` — это `/dev/fd/63`, рядом `lib/` нет, `/root/lib` ещё нет. Поэтому `setup.sh` до выбора ветки работает на встроенных минимальных цветах и `log/warn/err` (`_LIB_READY=0`), а после выбора скачивает `lib/*.sh` из этой ветки в `/root/lib` и только тогда подключает `modules_setup.sh`/`ssh_setup.sh`. Шаг 10 качает весь `PROJECT_FILES` (тот же список, что у `--update`), кроме модулей TMA и slave_servers. Любой новый файл проекта добавлять в `PROJECT_FILES` — иначе его не получит ни чистая установка, ни обновление
- **Диагностика ролевая (`lib/diagnostics.sh`):** слейв определяется по маркеру `/etc/awg-slave`, а при его отсутствии — по признаку «нет `bot.env` И нет `users.json`» (только на primary), после чего маркер ставится сам. `bot.py` признаком не является: `setup.sh` качает его в `/root/modules/bot/` в обеих ролях. Запасной путь нужен, потому что `setup.sh --slave` — не единственный способ получить слейв: сервер, добавленный через бота, проходит через `ssh_clone_awg_to_slave`. На слейве пропускаются проверки, которых там не бывает (файлы клиентов, `.pub`, `users.json`, бот, `bw_peak.json`) — иначе они давали ложные тревоги; вместо них печатается таблица клиентов, собранная из `awg0.conf` + `awg show`, и блок обфускации для сверки с primary. Общая для обеих ролей проверка — **сверка «интерфейс ↔ awg0.conf»**: ловит пир, снятый командой, но оставшийся в конфиге (вернётся при перезапуске AWG) и наоборот. Плюс проверка дублей `AllowedIPs`. Для «клиенты не подключаются» в обеих ролях: **версии** утилит `awg` и модуля ядра (загруженного и на диске — DKMS-модуль подхватывается только при перезагрузке; утилиты 1.0.x/2.0 с модулем AWG 3 не умеют H1–H4: netlink-тип сменился со строки на u64), **обфускация «awg0.conf ↔ интерфейс»** по `awg show <iface> <param>`, на primary ещё и против `server.env` (из него `gen_obfs()` собирает конфиги клиентов; на слейве `server.env` от его собственной установки и эталоном не является). H1–H4 = 1–4 — только информационная пометка, не тревога: так ставил `setup.sh` 13 апреля (коммит 0b85a2d), и клиенты тех установок выпущены с ними же; при заданном ключе защиты заголовков пометка не выводится. В сверку входят S3/S4, `HeaderProtectionKey` (в отчёте — первые 6 символов; «нет ключа» — это `(none)` у модуля ядра и нулевой ключ `AAAA…=` у amneziawg-go) и `RandomTrailers`; строки параметров 3.x, которых нет ни в конфиге, ни в утилитах, не печатаются. Отдельно — тревога, если `awg0.conf` на 3.1, а утилиты или модуль старше, **сводка хендшейков** с тревогой, если за ≥30 мин работы AWG не было ни одного. iptables печатается через `-S` — `-L -n` не показывает интерфейсы
- **Счётчики в shell — только через awk:** `grep -c` при нуле совпадений печатает `0` И возвращает код 1, поэтому связка `$(grep -c ... || echo 0)` даёт строку `"0\n0"` и ломает последующие сравнения
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
