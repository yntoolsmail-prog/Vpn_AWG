#!/usr/bin/env python3
"""
awg_gen_test.py — тестовый скрипт генерации AWG конфига с исключёнными сайтами.

Читает server.env, запрашивает имя пользователя и список сайтов для исключения,
резолвит их IP и генерирует конфиг с правильным AllowedIPs.

Запуск на сервере:
    python3 awg_gen_test.py

Зависимости: только stdlib (socket, ipaddress, subprocess).
"""

import os
import socket
import ipaddress
import subprocess
import sys

# ─────────────────────────────────────────────────────────────
# КОНФИГ — пути совпадают с bot.py
# ─────────────────────────────────────────────────────────────
ENV_FILE    = "/etc/amnezia/amneziawg/server.env"
CLIENTS_DIR = "/etc/amnezia/amneziawg/clients"

# ─────────────────────────────────────────────────────────────
# БАЗА САЙТОВ
# ─────────────────────────────────────────────────────────────
SITES = {
    # ГОССЕРВИСЫ
    "gosuslugi": {
        "name": "Госуслуги", "emoji": "🏛",
        "domains": [
            "gosuslugi.ru", "www.gosuslugi.ru", "esia.gosuslugi.ru",
            "lk.gosuslugi.ru", "beta.gosuslugi.ru", "pay.gosuslugi.ru",
            "id.gosuslugi.ru", "oplata.gosuslugi.ru",
        ],
    },
    "nalog": {
        "name": "Налоговая (ФНС)", "emoji": "📋",
        "domains": [
            "nalog.gov.ru", "nalog.ru",
            "lkfl2.nalog.ru", "lkip2.nalog.ru", "service.nalog.ru",
        ],
    },
    "mos": {
        "name": "Mos.ru", "emoji": "🏙",
        "domains": ["mos.ru", "www.mos.ru", "login.mos.ru", "id.mos.ru", "ag.mos.ru"],
    },
    "pfr": {
        "name": "СФР (Пенсионный фонд)", "emoji": "👴",
        "domains": ["sfr.gov.ru", "pfr.gov.ru", "es.pfrf.ru"],
    },
    "gibdd": {
        "name": "ГИБДД", "emoji": "🚗",
        "domains": ["gibdd.ru", "www.gibdd.ru"],
    },
    # БАНКИ
    "sber": {
        "name": "Сбербанк", "emoji": "💚",
        "domains": ["sber.ru", "sberbank.ru", "online.sberbank.ru", "sberbank.com", "sbrf.ru"],
    },
    "tbank": {
        "name": "Т-Банк (Тинькофф)", "emoji": "🟡",
        "domains": ["tbank.ru", "tinkoff.ru", "acdn.tinkoff.ru"],
    },
    "alfa": {
        "name": "Альфа-Банк", "emoji": "🔴",
        "domains": ["alfabank.ru", "click.alfabank.ru"],
    },
    "vtb": {
        "name": "ВТБ", "emoji": "🔵",
        "domains": ["vtb.ru", "online.vtb.ru", "mb.vtb.ru"],
    },
    "raiffeisen": {
        "name": "Райффайзен", "emoji": "🟠",
        "domains": ["raiffeisen.ru", "ecom.raiffeisen.ru"],
    },
    "sbp": {
        "name": "СБП / НСПК", "emoji": "⚡",
        "domains": ["sbp.nspk.ru", "nspk.ru", "qr.nspk.ru"],
    },
    # МАРКЕТПЛЕЙСЫ
    "ozon": {
        "name": "Ozon", "emoji": "🔵",
        "domains": ["ozon.ru", "static.ozon.ru", "cdn1.ozone.ru"],
    },
    "wildberries": {
        "name": "Wildberries", "emoji": "🟣",
        "domains": ["wildberries.ru", "wbstatic.net", "wbbasket.ru", "wbx-static.com"],
    },
    "avito": {
        "name": "Авито", "emoji": "🟢",
        "domains": ["avito.ru", "cdn.avito.ru", "m.avito.ru"],
    },
    # ЯНДЕКС
    "yandex": {
        "name": "Яндекс (все сервисы)", "emoji": "🔴",
        "domains": [
            "yandex.ru", "ya.ru", "yandex.com", "yandex.net",
            "maps.yandex.ru", "static-maps.yandex.ru",
            "taxi.yandex.ru", "go.yandex.ru",
            "music.yandex.ru", "storage.mds.yandex.net",
            "market.yandex.ru", "pokupki.market.yandex.ru",
            "yastatic.net", "avatars.mds.yandex.net",
        ],
    },
    # РАЗВЛЕЧЕНИЯ
    "kinopoisk": {
        "name": "Кинопоиск", "emoji": "🎬",
        "domains": ["kinopoisk.ru", "www.kinopoisk.ru"],
    },
    "ivi": {
        "name": "Иви", "emoji": "🎥",
        "domains": ["ivi.ru", "api.digitalaccess.ru", "ccr.ivi.ru"],
    },
    "okko": {
        "name": "Okko", "emoji": "📺",
        "domains": ["okko.tv", "api.okko.tv", "static.okko.tv"],
    },
    # ТРАНСПОРТ И ДОСТАВКА
    "rzd": {
        "name": "РЖД", "emoji": "🚂",
        "domains": ["rzd.ru", "www.rzd.ru", "pass.rzd.ru", "ticket.rzd.ru"],
    },
    "pochta": {
        "name": "Почта России", "emoji": "📬",
        "domains": ["pochta.ru", "www.pochta.ru", "tracking.pochta.ru"],
    },
    "delivery": {
        "name": "Яндекс Еда / Самокат", "emoji": "🍔",
        "domains": ["eda.yandex.ru", "eats.yandex.ru", "samokat.ru", "delivery-club.ru"],
    },
    # ЖКХ
    "zhkh": {
        "name": "ЖКХ / Энергосбыт", "emoji": "🏠",
        "domains": ["mosenergosbyt.ru", "lkk.mosenergosbyt.ru", "eirc-mo.ru", "dom.gosuslugi.ru"],
    },
    # СОЦСЕТИ
    "vk": {
        "name": "ВКонтакте", "emoji": "💙",
        "domains": ["vk.com", "vk.me", "userapi.com", "vkuseraudio.net", "vk-cdn.net"],
    },
    # ПРОЧЕЕ
    "hh": {
        "name": "HeadHunter (hh.ru)", "emoji": "💼",
        "domains": ["hh.ru", "api.hh.ru", "hhcdn.ru"],
    },
    "2gis": {
        "name": "2ГИС", "emoji": "🗺",
        "domains": ["2gis.ru", "2gis.com"],
    },
}

CATEGORIES = {
    "🏛 Госсервисы":           ["gosuslugi", "nalog", "mos", "pfr", "gibdd"],
    "🏦 Банки и платежи":      ["sber", "tbank", "alfa", "vtb", "raiffeisen", "sbp"],
    "🛒 Маркетплейсы":         ["ozon", "wildberries", "avito"],
    "🔴 Яндекс":               ["yandex", "kinopoisk"],
    "🎬 Развлечения":          ["ivi", "okko"],
    "🚂 Транспорт и доставка": ["rzd", "pochta", "delivery"],
    "🏠 ЖКХ":                  ["zhkh"],
    "💬 Соцсети":              ["vk"],
    "📦 Прочее":               ["hh", "2gis"],
}

# ─────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────────────────────

def load_env(path: str) -> dict:
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def resolve_domains(domains: list) -> set:
    """Резолвит домены → множество IPv4."""
    ips = set()
    for domain in domains:
        try:
            results = socket.getaddrinfo(domain, None, socket.AF_INET)
            for r in results:
                ips.add(r[4][0])
            print(f"    {domain} → {', '.join(r[4][0] for r in results)}")
        except Exception as e:
            print(f"    {domain} → ошибка: {e}")
    return ips


def subtract_ips(excluded: set) -> str:
    """Вычитает IP из 0.0.0.0/0, возвращает строку AllowedIPs."""
    allowed = [ipaddress.ip_network("0.0.0.0/0")]
    for ip_str in excluded:
        try:
            target = ipaddress.ip_network(f"{ip_str}/32", strict=False)
            new_allowed = []
            for net in allowed:
                if target.overlaps(net):
                    new_allowed.extend(net.address_exclude(target))
                else:
                    new_allowed.append(net)
            allowed = new_allowed
        except Exception as e:
            print(f"    [warn] не удалось обработать {ip_str}: {e}")

    sorted_nets = sorted(allowed, key=lambda n: (n.network_address, n.prefixlen))
    return ", ".join(str(n) for n in sorted_nets)


def next_ip(awg_conf: str, subnet: str) -> int:
    """Находит следующий свободный номер IP в подсети."""
    try:
        with open(awg_conf) as f:
            content = f.read()
    except FileNotFoundError:
        content = ""
    i = 2
    while f"{subnet}.{i}/32" in content:
        i += 1
    return i


def make_conf(priv, ip, psk, srv, allowed_ips: str) -> str:
    """Генерирует текст .conf файла клиента."""
    dns1 = srv.get("PRIMARY_DNS", "1.1.1.1")
    dns2 = srv.get("SECONDARY_DNS", "1.0.0.1")
    return "\n".join([
        "[Interface]",
        f"PrivateKey = {priv}",
        f"Address = {ip}/32",
        f"DNS = {dns1}, {dns2}",
        f"Jc = {srv.get('JC', '4')}",
        f"Jmin = {srv.get('JMIN', '40')}",
        f"Jmax = {srv.get('JMAX', '70')}",
        f"S1 = {srv.get('S1', '0')}",
        f"S2 = {srv.get('S2', '0')}",
        f"H1 = {srv.get('H1', '1')}",
        f"H2 = {srv.get('H2', '2')}",
        f"H3 = {srv.get('H3', '3')}",
        f"H4 = {srv.get('H4', '4')}",
        "",
        "[Peer]",
        f"PublicKey = {srv['SERVER_PUBLIC']}",
        f"PresharedKey = {psk}",
        f"Endpoint = {srv['SERVER_IP']}:{srv['SERVER_PORT']}",
        f"AllowedIPs = {allowed_ips}",
        "PersistentKeepalive = 25",
    ]) + "\n"


# ─────────────────────────────────────────────────────────────
# UI — выбор сайтов
# ─────────────────────────────────────────────────────────────

def show_sites_menu() -> list:
    """Интерактивный выбор сайтов для исключения. Возвращает список ключей."""
    selected = set()

    while True:
        print("\n" + "─" * 55)
        print("  Сайты для исключения из VPN (идут напрямую)")
        print("─" * 55)

        # Выводим по категориям
        all_keys = []
        num_map = {}  # номер → ключ
        n = 1
        for cat_name, keys in CATEGORIES.items():
            print(f"\n  {cat_name}")
            for key in keys:
                site = SITES[key]
                mark = "✓" if key in selected else " "
                print(f"    [{mark}] {n:2d}. {site['emoji']} {site['name']}")
                num_map[n] = key
                all_keys.append(key)
                n += 1

        print("\n─" * 55)
        print("  Команды: номер — переключить | a — выбрать все")
        print("           n — снять все       | q — готово")
        print("─" * 55)

        if selected:
            names = [SITES[k]["name"] for k in selected]
            print(f"  Выбрано ({len(selected)}): {', '.join(names)}")
        else:
            print("  Ничего не выбрано — конфиг будет с AllowedIPs = 0.0.0.0/0")

        cmd = input("\n  > ").strip().lower()

        if cmd == "q":
            return list(selected)
        elif cmd == "a":
            selected = set(all_keys)
        elif cmd == "n":
            selected = set()
        elif cmd.isdigit() and int(cmd) in num_map:
            key = num_map[int(cmd)]
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
        else:
            print("  Неверная команда.")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 55)
    print("  AWG конфиг с исключением сайтов — тестовый скрипт")
    print("═" * 55)

    # Загружаем server.env
    if not os.path.exists(ENV_FILE):
        print(f"\n[!] Файл {ENV_FILE} не найден.")
        print("    Укажите путь к server.env или запускайте на сервере с установленным AWG.")
        sys.exit(1)

    srv = load_env(ENV_FILE)
    awg_iface = srv.get("VPN_IFACE", "awg0")
    awg_conf  = f"/etc/amnezia/amneziawg/{awg_iface}.conf"
    subnet    = srv.get("VPN_SUBNET", "10.8.0")

    print(f"\n  Сервер:    {srv.get('SERVER_IP')}:{srv.get('SERVER_PORT')}")
    print(f"  Интерфейс: {awg_iface}  |  Подсеть: {subnet}.x")

    # Имя клиента
    print()
    while True:
        name = input("  Имя клиента (латиница): ").strip()
        name = "".join(c for c in name if c.isascii() and (c.isalnum() or c in "_-"))
        name = name.capitalize()
        if name:
            break
        print("  [!] Введите имя латиницей.")

    conf_path = f"{CLIENTS_DIR}/{name}.conf"
    if os.path.exists(conf_path):
        print(f"\n  [!] Клиент '{name}' уже существует: {conf_path}")
        sys.exit(1)

    # Выбор сайтов
    excluded_keys = show_sites_menu()

    # Резолвим домены
    excluded_ips = set()
    if excluded_keys:
        print(f"\n  Резолвим домены для {len(excluded_keys)} сайтов...")
        for key in excluded_keys:
            site = SITES[key]
            print(f"\n  {site['emoji']} {site['name']}:")
            ips = resolve_domains(site["domains"])
            excluded_ips.update(ips)
        print(f"\n  Всего уникальных IP: {len(excluded_ips)}")
    else:
        print("\n  Сайты не выбраны — используем AllowedIPs = 0.0.0.0/0")

    # Вычисляем AllowedIPs
    if excluded_ips:
        print("\n  Вычисляем AllowedIPs...")
        allowed_ips = subtract_ips(excluded_ips)
    else:
        allowed_ips = "0.0.0.0/0"

    # Генерируем ключи
    print("\n  Генерируем ключи...")
    priv = subprocess.check_output(["awg", "genkey"], text=True).strip()
    psk  = subprocess.check_output(["awg", "genpsk"], text=True).strip()
    ip   = f"{subnet}.{next_ip(awg_conf, subnet)}"

    # Собираем конфиг
    conf_text = make_conf(priv, ip, psk, srv, allowed_ips)

    # Выводим результат
    print("\n" + "═" * 55)
    print(f"  Конфиг для: {name}  |  IP: {ip}")
    if excluded_keys:
        print(f"  Исключено сайтов: {len(excluded_keys)}, IP: {len(excluded_ips)}")
        print(f"  AllowedIPs содержит {len(allowed_ips.split(','))} подсетей")
    print("═" * 55)
    print()
    print(conf_text)
    print("═" * 55)

    # Спрашиваем — сохранить?
    save = input("\n  Сохранить конфиг в файл? (y/N): ").strip().lower()
    if save == "y":
        os.makedirs(CLIENTS_DIR, exist_ok=True)
        with open(conf_path, "w") as f:
            f.write(conf_text)
        print(f"\n  ✓ Сохранено: {conf_path}")
        print(f"\n  QR-код (если установлен qrencode):")
        os.system(f"qrencode -t ansiutf8 -l L -r {conf_path} 2>/dev/null || echo '  qrencode не установлен'")
    else:
        print("\n  Конфиг не сохранён.")

    print()


if __name__ == "__main__":
    main()
