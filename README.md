# Vpn_AWG — AmneziaWG + Telegram Bot

AmneziaWG нативно в kernelspace (300+ мбит) с управлением через Telegram бота.

## Особенности

- **AWG нативно** — модуль ядра, не userspace/Go, полная скорость
- **Обфускация** — не детектируется РКН/DPI
- **Telegram бот** — добавление клиентов, список, удаление, статус, бэкап
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

Установщик сам:
1. Установит AmneziaWG (модуль ядра)
2. Настроит сетевой интерфейс
3. Установит Telegram бота
4. Настроит автозапуск

---

## Управление

**Через Telegram бот** — напишите `/start` вашему боту:
- Добавить устройство → получить .conf файл и QR-код
- Список устройств с трафиком и статусом подключения
- Удалить устройство (с подтверждением)
- Статус сервера + кнопки перезапуска бота и AWG
- Бэкап конфигурации (только для администратора)

**Через терминал:**
```bash
bash /root/vpn.sh
```

**Управление ботом:**
```bash
systemctl status awg-bot    # статус
systemctl restart awg-bot   # перезапуск
journalctl -u awg-bot -f    # логи
```

---

## Клиентские приложения

**AmneziaWG** — рекомендуется для большинства устройств. Простое подключение через .conf файл или QR-код.

**AmneziaVPN** — если нужно раздельное туннелирование (часть трафика через VPN, часть напрямую). Использует vpn:// ссылку или .vpn файл.

Скачать: [amnezia.org](https://amnezia.org)

---

## Смена DNS

DNS-серверы хранятся в `/etc/amnezia/amneziawg/server.env`.

Поменять DNS для новых клиентов:
```bash
sed -i 's/PRIMARY_DNS=.*/PRIMARY_DNS=8.8.8.8/' /etc/amnezia/amneziawg/server.env
sed -i 's/SECONDARY_DNS=.*/SECONDARY_DNS=8.8.4.4/' /etc/amnezia/amneziawg/server.env
systemctl restart awg-bot
```

> Новый DNS применяется только к новым устройствам. Существующим — пересоздать профиль.

---

## Автообновление

Установщик настраивает автообновление `bot.py` и `vpn.sh` из этого репозитория. Скрипт `/root/update.sh` запускается каждые 5 минут через cron, проверяет последний коммит в `main` и при изменении скачивает новые версии файлов и перезапускает бота.

### Управление автообновлением

**Проверить статус:**
```bash
crontab -l | grep update.sh          # есть ли задача в cron
cat /root/.bot_version               # текущая версия
cat /var/log/awg-update.log          # лог обновлений
```

**Отключить автообновление:**
```bash
crontab -l | grep -v update.sh | crontab -
```

**Включить обратно:**
```bash
(crontab -l 2>/dev/null; echo "*/5 * * * * /root/update.sh") | crontab -
```

**Обновить вручную прямо сейчас:**
```bash
bash /root/update.sh
```

**Изменить частоту** (например раз в час вместо каждые 5 минут):
```bash
crontab -l | grep -v update.sh | crontab -
(crontab -l 2>/dev/null; echo "0 * * * * /root/update.sh") | crontab -
```

> Автообновление тянет только `bot.py` и `vpn.sh`. Конфиги, ключи и данные клиентов не затрагиваются.

---

## Файл amnezia.gpg.asc

В репозитории хранится GPG-ключ PPA Amnezia (`amnezia.gpg.asc`). Установщик берёт его оттуда в первую очередь — это позволяет установке работать даже на серверах где заблокирован `keyserver.ubuntu.com` и `api.launchpad.net`.

> **Для владельца репозитория** — если ключ истечёт и установка начнёт падать на этом шаге, его нужно обновить. Пользователям этого делать не нужно.

<details>
<summary>Как обновить ключ</summary>

```bash
apt-get install -y git
git clone https://github.com/yntoolsmail-prog/Vpn_AWG.git
cd Vpn_AWG
curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x57290828" > amnezia.gpg.asc
git add amnezia.gpg.asc
git commit -m "update amnezia gpg key"
git push
```

</details>