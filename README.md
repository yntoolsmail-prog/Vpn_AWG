# Vpn_AWG — AmneziaWG + Telegram Bot

AmneziaWG нативно в kernelspace (300+ мбит) с управлением через Telegram бота.

## Особенности

- **AWG нативно** — модуль ядра, не userspace/Go, полная скорость
- **Обфускация** — не детектируется РКН/DPI
- **Telegram бот** — полное управление сервером прямо из чата
- **Многопользовательский** — регистрация по запросу, администратор одобряет доступ
- **Мониторинг трафика** — текущая скорость, пики, месячное потребление через vnstat
- **Терминальное меню** — управление через vpn.sh
- **Автозапуск** — AWG и бот стартуют при перезагрузке сервера

## Требования

- Ubuntu 22.04 или 24.04
- VPS с root доступом
- Telegram бот (получить у @BotFather)

---

## Установка

```bash
bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/setup.sh)
```

Установщик автоматически:
1. Проверит и при необходимости обновит ядро
2. Установит AmneziaWG (нативный модуль ядра)
3. Сгенерирует случайные параметры обфускации
4. Настроит сетевой интерфейс и IP-форвардинг
5. Установит vnstat для мониторинга трафика
6. Установит Telegram бота и настроит автозапуск

---

## Управление через Telegram бот

Напишите `/start` вашему боту.

### Для всех пользователей

- **➕ Добавить устройство** — создать VPN-профиль, получить `.conf` файл и QR-код
- **📋 Мои устройства** — список устройств с трафиком и временем последнего хендшейка. На каждом устройстве: скачать конфиг, QR-код, поделиться ссылкой для AmneziaVPN, удалить
- **📊 Статус сервера** — uptime, load average, RAM, диск, количество клиентов и онлайн
- **📖 Инструкция** — встроенная справка

### Только для администратора

- **🌍 Все клиенты** — список всех устройств всех пользователей с трафиком
- **👥 Пользователи** — одобрение/отклонение запросов на доступ, удаление пользователей со всеми устройствами
- **📈 Трафик / пики** — текущая скорость, пик за сегодня, абсолютный пик, топ-5 нагруженных минут, месячное потребление за последние 6 месяцев с индикатором приближения к лимиту хостера
- **🧹 Очистить мусор** — удалить висячие пиры из AWG которых нет в конфиге
- **💾 Бэкап** — архив конфигов, ключей и данных пользователей
- **📥 Восстановить из бэкапа** — перенос на новый сервер
- **🔧 Техобслуживание** — apt upgrade, проверка версии библиотеки, напоминание раз в 6 месяцев

### Регистрация пользователей

Новый пользователь пишет `/start`, вводит имя латиницей — администратор получает уведомление с кнопками одобрить/отклонить. После одобрения пользователь может добавлять устройства самостоятельно.

---

## Управление через терминал

```bash
bash /root/vpn.sh
```

**Управление сервисами:**
```bash
systemctl status awg-bot          # статус бота
systemctl restart awg-bot         # перезапуск бота
journalctl -u awg-bot -f          # логи бота в реальном времени
systemctl status awg-quick@awg0   # статус AWG интерфейса
```

---

## Мониторинг трафика

Бот ведёт поминутный лог скорости в `/var/log/awg-bw.log` и хранит пики в `/etc/amnezia/amneziawg/bw_peak.json`. Месячная статистика берётся из vnstat.

**Просмотр из терминала:**
```bash
vnstat -i eth0 --months        # трафик по месяцам
vnstat -i eth0 -h 24           # последние 24 часа
vnstat -i eth0 --top10         # топ-10 часов
cat /var/log/awg-bw.log | sort -t= -k2 -rn | head -20   # пики по минутам
```

Индикаторы месячного потребления в боте: 🟢 >1 TB / 🟡 >2 TB / 🟠 >3 TB / 🔴 >4 TB.

---

## Клиентские приложения

**AmneziaWG** — рекомендуется для большинства устройств. Подключение через `.conf` файл или QR-код.

**AmneziaVPN** — если нужно раздельное туннелирование (часть трафика через VPN, часть напрямую). Использует `vpn://` ссылку или `.vpn` файл.

Скачать: [amnezia.org](https://amnezia.org)

---

## Смена DNS

DNS-серверы хранятся в `/etc/amnezia/amneziawg/server.env`.

```bash
sed -i 's/PRIMARY_DNS=.*/PRIMARY_DNS=8.8.8.8/' /etc/amnezia/amneziawg/server.env
sed -i 's/SECONDARY_DNS=.*/SECONDARY_DNS=8.8.4.4/' /etc/amnezia/amneziawg/server.env
systemctl restart awg-bot
```

> Новый DNS применяется только к новым устройствам. Существующим — пересоздать профиль.

---

## Обновление

**Обновить все файлы бота из репозитория:**
```bash
git clone --depth=1 https://github.com/yntoolsmail-prog/Vpn_AWG.git /tmp/vpn-update \
  && cp /tmp/vpn-update/bot.py /root/bot.py \
  && cp /tmp/vpn-update/vpn.sh /root/vpn.sh && chmod +x /root/vpn.sh \
  && rm -rf /tmp/vpn-update \
  && systemctl restart awg-bot \
  && echo "✅ Обновление завершено"
```

Конфиги, ключи и данные клиентов не затрагиваются.

**Проверить текущую версию (последний коммит):**
```bash
git ls-remote https://github.com/yntoolsmail-prog/Vpn_AWG.git HEAD
```

**Обновить систему:**
```bash
apt-get update && apt-get upgrade -y && systemctl restart awg-bot
```

**Обновить python-telegram-bot:**
```bash
pip install -U python-telegram-bot && systemctl restart awg-bot
```

---

## Файл amnezia.gpg.asc

В репозитории хранится GPG-ключ PPA Amnezia (`amnezia.gpg.asc`). Установщик берёт его оттуда в первую очередь — это позволяет установке работать даже на серверах где заблокирован `keyserver.ubuntu.com` и `api.launchpad.net`.

> **Для владельца репозитория** — если ключ истечёт и установка начнёт падать на этом шаге, его нужно обновить. Пользователям этого делать не нужно.

<details>
<summary>Как обновить ключ</summary>

1. Скачать файл с ключом: [keyserver.ubuntu.com](https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x57290828) → сохранить как `amnezia.gpg.asc`
2. Открыть репозиторий на GitHub
3. Нажать **Add file → Upload files**
4. Перетащить скачанный файл
5. Нажать **Commit changes**

</details>

---

## Дополнение — прокси для Telegram и WhatsApp

Если нужны прокси для обхода блокировок Telegram и WhatsApp (MTProxy, SOCKS5, WhatsApp прокси) — смотри связанный репозиторий:

**[Proxy-Telegram-Whatsapp](https://github.com/yntoolsmail-prog/Proxy-Telegram-Whatsapp)** — устанавливается отдельно и может встраиваться в этого бота как аддон (кнопка 📡 Прокси появится в меню администратора).

```bash
bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Proxy-Telegram-Whatsapp/main/setup_proxy.sh)
```

---

## Благодарности

Проект построен на следующих открытых решениях:

- [AmneziaWG](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module) — форк WireGuard с обфускацией трафика
- [Amnezia VPN](https://github.com/amnezia-vpn/amnezia-client) — клиентское приложение с поддержкой AmneziaWG
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — библиотека для Telegram Bot API (LGPLv3)
- [vnstat](https://humdi.net/vnstat/) — мониторинг сетевого трафика (GPLv2)
- [qrencode](https://fukuchi.org/works/qrencode/) — генерация QR-кодов (LGPLv2.1)

Код в этом репозитории распространяется под лицензией [MIT](LICENSE).