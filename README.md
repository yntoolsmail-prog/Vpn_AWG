# Vpn_AWG — AmneziaWG + Telegram Bot

AmneziaWG нативно в kernelspace (300+ мбит) с управлением через Telegram бота.

## Особенности

- **AWG нативно** — модуль ядра, не userspace/Go, полная скорость
- **Обфускация** — не детектируется РКН/DPI
- **Telegram бот** — добавление клиентов, список, удаление, статус, бэкап, техобслуживание
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
- Техобслуживание (только для администратора)

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

## Техобслуживание

Бот напоминает о техобслуживании раз в 6 месяцев. В меню администратора кнопка **🔧 Техобслуживание** позволяет:

- Сделать бэкап и запустить `apt upgrade` одной кнопкой
- Проверить версию `python-telegram-bot` и сравнить с актуальной

### Зависимости

| Компонент | Версия | Где смотреть обновления |
|---|---|---|
| python-telegram-bot | 22.x | [Releases](https://github.com/python-telegram-bot/python-telegram-bot/releases) |
| Ubuntu | 22.04 / 24.04 | `apt upgrade` |

### Обновление python-telegram-bot вручную

```bash
pip3 install "python-telegram-bot[job-queue]>=22.0,<23" && systemctl restart awg-bot
```

### Что смотреть при выходе новой мажорной версии (23.x и выше)

Открыть [релизы на GitHub](https://github.com/python-telegram-bot/python-telegram-bot/releases) и найти раздел **Breaking Changes**. Если он пустой или не касается базовых хендлеров и ConversationHandler — обновляйте спокойно. Если есть изменения — потребуется небольшая правка `bot.py`.

---

## История версий

| Версия | Что изменилось |
|---|---|
| 1.5 | Переход на python-telegram-bot 22.x |
| 1.4 | Техобслуживание, напоминания, кнопки перезапуска |
| 1.3 | allow_reentry для диалога добавления устройства, исправлены файловые дескрипторы |
| 1.2 | Начальная версия |