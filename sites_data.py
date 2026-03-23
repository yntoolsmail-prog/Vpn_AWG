#!/usr/bin/env python3
# sites_data.py — база сайтов для split tunneling
# Этот файл содержит только данные — никакой логики.
# Меняй его когда нужно добавить/убрать сайт или домен из списка исключений.
# Используется: awg_core.py → bot.py, tma_server.py

# ── База сайтов ────────────────────────────────────────────────────────────────
SITES = {
    # Локальная сеть
    "local": {
        "name": "Локальная сеть (роутер, NAS...)", "emoji": "🏠",
        "domains": [], "subnets": ["192.168.0.0/16"],
    },
    # Банки и платежи
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
    # Маркетплейсы
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
    # Яндекс (одиночный — без заголовка категории)
    "yandex": {
        "name": "Яндекс (все сервисы)", "emoji": "🔴",
        "domains": [
            "yandex.ru", "ya.ru", "yandex.com", "yandex.net",
            "maps.yandex.ru", "taxi.yandex.ru", "go.yandex.ru",
            "music.yandex.ru", "storage.mds.yandex.net",
            "market.yandex.ru", "yastatic.net", "avatars.mds.yandex.net",
        ],
    },
    # Видео и стриминг
    "kinopoisk": {
        "name": "Кинопоиск", "emoji": "🎬",
        "domains": ["kinopoisk.ru", "www.kinopoisk.ru"],
    },
    "rutube": {
        "name": "Rutube", "emoji": "📺",
        "domains": ["rutube.ru", "pics.rutube.ru", "strm.rutube.ru"],
    },
    "vkvideo": {
        "name": "VK Видео", "emoji": "📱",
        "domains": ["vkvideo.ru"],
    },
    "ivi": {
        "name": "Иви", "emoji": "🍿",
        "domains": ["ivi.ru", "cdn.ivi.ru"],
    },
    "okko": {
        "name": "Okko", "emoji": "🎥",
        "domains": ["okko.tv", "static.okko.tv"],
    },
    "trikolor": {
        "name": "Триколор", "emoji": "📡",
        "domains": ["trikolor.tv"],
    },
    # Транспорт и доставка
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
    "zhkh_msk": {
        "name": "ЖКХ Москва (Мосэнергосбыт, ЕИРЦ)", "emoji": "🏘",
        "domains": ["mosenergosbyt.ru", "lkk.mosenergosbyt.ru", "eirc-mo.ru"],
    },
    "zhkh_fed": {
        "name": "ГИС ЖКХ (федеральный)", "emoji": "🏠",
        "domains": ["dom.gosuslugi.ru", "lkr.reformagkh.ru"],
    },
    # Государственные сервисы
    "gosuslugi": {
        "name": "Госуслуги", "emoji": "🏛",
        "domains": [
            "gosuslugi.ru", "esia.gosuslugi.ru", "lk.gosuslugi.ru",
            "beta.gosuslugi.ru", "oplata.gosuslugi.ru",
        ],
    },
    "mos": {
        "name": "Mos.ru", "emoji": "🏙",
        "domains": ["mos.ru", "www.mos.ru", "lk.mos.ru"],
    },
    "nalog": {
        "name": "ФНС / Налоги", "emoji": "📋",
        "domains": ["nalog.gov.ru", "lkfl.nalog.ru", "lkul.nalog.ru", "pb.nalog.ru"],
    },
    "emias": {
        "name": "ЕМИАС / Здоровье", "emoji": "🏥",
        "domains": ["emias.info", "moscow.emias.info"],
    },
    "gibdd": {
        "name": "ГИБДД / Штрафы", "emoji": "🚗",
        "domains": ["gibdd.ru", "www.gibdd.ru", "xn--b1aew.xn--p1ai"],
    },
    "rosreestr": {
        "name": "Росреестр", "emoji": "🏦",
        "domains": ["rosreestr.gov.ru", "lk.rosreestr.gov.ru"],
    },
    "fssp": {
        "name": "ФССП (Приставы)", "emoji": "⚖️",
        "domains": ["fssprus.ru", "lk.fssprus.ru"],
    },
    # Соцсети
    "vk": {
        "name": "ВКонтакте", "emoji": "💙",
        "domains": ["vk.com", "vk.me", "userapi.com", "vkuseraudio.net", "vk-cdn.net"],
    },
    "ok": {
        "name": "Одноклассники", "emoji": "🟠",
        "domains": ["ok.ru", "www.ok.ru", "udn.odnoklassniki.ru"],
    },
    # Прочее
    "hh": {
        "name": "HeadHunter (hh.ru)", "emoji": "💼",
        "domains": ["hh.ru", "api.hh.ru", "hhcdn.ru"],
    },
    "2gis": {
        "name": "2ГИС", "emoji": "🗺",
        "domains": ["2gis.ru", "2gis.com"],
    },
}

# ── Категории для отображения в меню ──────────────────────────────────────────
CATEGORIES = {
    "🏠 Локальная сеть":          ["local"],
    "🏦 Банки и платежи":         ["sber", "tbank", "alfa", "vtb", "raiffeisen", "sbp"],
    "🛒 Маркетплейсы":            ["ozon", "wildberries", "avito"],
    "🔴 Яндекс":                  ["yandex"],
    "🎬 Видео и стриминг":        ["kinopoisk", "rutube", "vkvideo", "ivi", "okko", "trikolor"],
    "🚂 Транспорт и доставка":    ["rzd", "pochta", "delivery"],
    "🏘 ЖКХ":                     ["zhkh_msk", "zhkh_fed"],
    "🏛 Государственные сервисы": ["gosuslugi", "mos", "nalog", "emias", "gibdd", "rosreestr", "fssp"],
    "💬 Соцсети":                 ["vk", "ok"],
    "📦 Прочее":                  ["hh", "2gis"],
}

# Всегда включены — пользователь снять не может
DEFAULT_SELECTED = {"local"}

# Все ключи которые пользователь может включить (без заблокированных)
ALL_SELECTABLE = {k for k in SITES if k not in DEFAULT_SELECTED}
