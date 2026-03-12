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

## ⚠️ Перед установкой — обязательно обновить систему

AmneziaWG устанавливается как модуль ядра через DKMS и компилируется под **текущее работающее ядро**. Если система не обновлена и ядро устарело — компиляция завершится ошибкой.

**Выполните перед установкой:**

```bash
apt-get update && apt-get upgrade -y
reboot
```

После перезагрузки дождитесь запуска сервера и запускайте установщик.

> Пропуск этого шага — самая частая причина ошибки `Bad return status for module build on kernel`.

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

## Управление

**Через Telegram бот** — напишите `/start` вашему боту:
- Добавить устройство → получить .conf файл и QR-код
- Список устройств с трафиком
- Удалить устройство (с подтверждением)
- Статус сервера
- Бэкап конфигурации (только для администратора)

**Через терминал:**
```bash
bash /root/vpn.sh
```

**Управление ботом:**
```bash
systemctl status awg-bot   # статус
systemctl restart awg-bot  # перезапуск
journalctl -u awg-bot -f   # логи
```

## Клиент

Используйте [AmneziaVPN](https://amnezia.org) — поддерживает AWG и раздельное туннелирование.

---

## Смена DNS

DNS-серверы хранятся в `/etc/amnezia/amneziawg/server.env`.

Поменять DNS для новых клиентов:
```bash
sed -i 's/PRIMARY_DNS=.*/PRIMARY_DNS=8.8.8.8/' /etc/amnezia/amneziawg/server.env
sed -i 's/SECONDARY_DNS=.*/SECONDARY_DNS=8.8.4.4/' /etc/amnezia/amneziawg/server.env
systemctl restart awg-bot
```

> Новый DNS будет применён только к новым устройствам. Существующим — пересоздать профиль.

---

<details>
<summary>🔄 Автообновление с GitHub</summary>

### Как это работает

Скрипт `update.sh` каждые 5 минут проверяет последний коммит на GitHub. Если появился новый — скачивает обновлённые файлы и перезапускает бота автоматически.

### Запуск вручную

```bash
bash /root/update.sh
```

### Проверить лог обновлений

```bash
cat /var/log/awg-update.log
```

### Отключить автообновление

```bash
crontab -e
# удалите строку с update.sh
```

</details>