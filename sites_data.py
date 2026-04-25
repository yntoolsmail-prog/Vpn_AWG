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
        "domains": [
            "sber.ru", "sberbank.ru", "online.sberbank.ru", "sberbank.com", "sbrf.ru",
            "securepayments.sberbank.ru", "3dsec.sberbank.ru",
        ],
    },
    "tbank": {
        "name": "Т-Банк (Тинькофф)", "emoji": "🟡",
        "domains": ["tbank.ru", "tinkoff.ru", "acdn.tinkoff.ru", "cdn.tbank.ru", "cdn-tinkoff.ru"],
    },
    "alfa": {
        "name": "Альфа-Банк", "emoji": "🔴",
        "domains": ["alfabank.ru", "www.alfabank.ru", "alfabank.com", "mobile.auth.alfabank.ru"],
    },
    "vtb": {
        "name": "ВТБ", "emoji": "🔵",
        "domains": [
            "vtb.ru", "www.vtb.ru", "online.vtb.ru", "lk.vtb.ru",
            "auth.lk.vtb.ru", "vtb24.ru", "втб.рф",
        ],
    },
    "raiffeisen": {
        "name": "Райффайзен", "emoji": "🟠",
        "domains": [
            "raiffeisen.ru", "www.raiffeisen.ru", "online.raiffeisen.ru",
            "raiffeisenbank.ru", "raif.ru", "pay.raif.ru", "rzb.ru",
        ],
    },
    "sbp": {
        "name": "СБП / НСПК", "emoji": "⚡",
        "domains": [
            "sbp.nspk.ru", "nspk.ru", "qr.nspk.ru",
            "mironline.ru", "pay.mironline.ru", "vamprivet.ru",
        ],
    },
    # Маркетплейсы
    "ozon": {
        "name": "Ozon", "emoji": "🔵",
        "domains": ["ozon.ru", "ozone.ru", "cdn1.ozone.ru", "o3.ru", "ozon-dostavka.ru", "finance.ozon.ru"],
    },
    "wildberries": {
        "name": "Wildberries", "emoji": "🟣",
        "domains": ["wildberries.ru", "wb.ru", "wbstatic.net", "wbbasket.ru", "paywb.ru", "wb-bank.ru"],
    },
    "avito": {
        "name": "Авито", "emoji": "🟢",
        "domains": ["avito.ru", "cdn.avito.ru", "m.avito.ru", "avito.st"],
    },
    # Яндекс (одиночный — без заголовка категории)
    "yandex": {
        "name": "Яндекс (все сервисы)", "emoji": "🔴",
        "domains": [
            "yandex.ru", "ya.ru", "yandex.com", "yandex.net",
            "maps.yandex.ru", "taxi.yandex.ru", "go.yandex.ru",
            "music.yandex.ru", "storage.mds.yandex.net",
            "market.yandex.ru", "yastatic.net", "avatars.mds.yandex.net",
            "passport.yandex.ru", "yandex.st", "strm.yandex.net", "mc.yandex.ru",
        ],
    },
    # Видео и стриминг
    "kinopoisk": {
        "name": "Кинопоиск", "emoji": "🎬",
        "domains": [
            "kinopoisk.ru", "www.kinopoisk.ru", "hd.kinopoisk.ru", "graphql.kinopoisk.ru",
            "ott.yandex.net", "widevine-proxy.ott.yandex.ru", "fairplay-proxy.ott.yandex.ru",
        ],
    },
    "rutube": {
        "name": "Rutube", "emoji": "📺",
        "domains": ["rutube.ru", "rutube.sport", "strm.rutube.ru", "pic.rutube.ru"],
    },
    "vkvideo": {
        "name": "VK Видео", "emoji": "📱",
        "domains": ["vkvideo.ru", "live.vkvideo.ru", "vkuservideo.net", "vkuservideo.ru"],
    },
    "ivi": {
        "name": "Иви", "emoji": "🍿",
        "domains": ["ivi.ru", "cdn.ivi.ru", "api.digitalaccess.ru", "ivi.tv"],
    },
    "okko": {
        "name": "Okko", "emoji": "🎥",
        "domains": ["okko.tv", "static.okko.tv", "okko.sport", "cdnvideo.ru"],
    },
    "trikolor": {
        "name": "Триколор", "emoji": "📡",
        "domains": ["tricolor.ru", "tricolor.tv", "trikolor.tv", "kino.tricolor.ru", "sso.tricolor.ru"],
    },
    "altplayers": {
        "name": "Прочие плееры", "emoji": "▶️",
        "domains": ["wiggle-as.newplayjj.com"],
    },
    # Транспорт и доставка
    "rzd": {
        "name": "РЖД", "emoji": "🚂",
        "domains": [
            "rzd.ru", "www.rzd.ru", "pass.rzd.ru", "ticket.rzd.ru",
            "travel.rzd.ru", "rzd-bonus.ru",
        ],
    },
    "pochta": {
        "name": "Почта России", "emoji": "📬",
        "domains": [
            "pochta.ru", "www.pochta.ru", "tracking.pochta.ru",
            "otpravka.pochta.ru", "lk.pochta.ru",
        ],
    },
    "delivery": {
        "name": "Яндекс Еда / Самокат", "emoji": "🍔",
        "domains": ["eda.yandex.ru", "lavka.yandex.ru", "market-delivery.yandex.ru", "samokat.ru"],
    },
    # ЖКХ
    "zhkh_msk": {
        "name": "ЖКХ Москва (Мосэнергосбыт)", "emoji": "🏘",
        "domains": ["mosenergosbyt.ru", "my.mosenergosbyt.ru", "lkkjr.mosenergosbyt.ru"],
    },
    "zhkh_fed": {
        "name": "ГИС ЖКХ (федеральный)", "emoji": "🏠",
        "domains": ["dom.gosuslugi.ru", "my.dom.gosuslugi.ru", "reformagkh.ru", "ais.reformagkh.ru"],
    },
    # Государственные сервисы
    "gosuslugi": {
        "name": "Госуслуги", "emoji": "🏛",
        "domains": [
            "gosuslugi.ru", "www.gosuslugi.ru", "esia.gosuslugi.ru",
            "lk.gosuslugi.ru", "oplata.gosuslugi.ru",
        ],
    },
    "mos": {
        "name": "Mos.ru", "emoji": "🏙",
        "domains": ["mos.ru", "www.mos.ru", "my.mos.ru", "id.mos.ru", "crowd.mos.ru"],
    },
    "nalog": {
        "name": "ФНС / Налоги", "emoji": "📋",
        "domains": [
            "nalog.gov.ru", "lkfl.nalog.ru", "lkfl2.nalog.ru", "lkul.nalog.ru",
            "lkip.nalog.ru", "lknpd.nalog.ru", "service.nalog.ru", "pb.nalog.ru",
        ],
    },
    "emias": {
        "name": "ЕМИАС / Здоровье", "emoji": "🏥",
        "domains": ["emias.info", "emias.mos.ru", "lk.emias.mos.ru", "mosgorzdrav.ru"],
    },
    "gibdd": {
        "name": "ГИБДД / Штрафы", "emoji": "🚗",
        "domains": ["gibdd.ru", "www.gibdd.ru", "service.gibdd.ru", "xn--b1aew.xn--p1ai"],
    },
    "rosreestr": {
        "name": "Росреестр", "emoji": "🏦",
        "domains": ["rosreestr.gov.ru", "lk.rosreestr.ru", "pkk.rosreestr.ru"],
    },
    "fssp": {
        "name": "ФССП (Приставы)", "emoji": "⚖️",
        "domains": ["fssp.gov.ru", "fssprus.ru", "lk.fssprus.ru"],
    },
    # Соцсети
    "vk": {
        "name": "ВКонтакте", "emoji": "💙",
        "domains": [
            "vk.com", "vk.ru", "vk.me", "vkontakte.ru",
            "userapi.com", "vkuseraudio.net", "vkuserphoto.ru",
        ],
    },
    "ok": {
        "name": "Одноклассники", "emoji": "🟠",
        "domains": ["ok.ru", "www.ok.ru", "odnoklassniki.ru", "mycdn.me", "okcdn.ru"],
    },
    # Ростелеком
    "rostelecom": {
        "name": "Ростелеком", "emoji": "📡",
        "domains": [
            "rt.ru", "www.rt.ru", "rostelecom.ru", "www.rostelecom.ru",
            "msk.rt.ru", "company.rt.ru", "lk.rt.ru", "id.rt.ru",
            "wink.ru", "wink.rt.ru", "rtcloud.ru",
        ],
    },
    # Прочее
    "hh": {
        "name": "HeadHunter (hh.ru)", "emoji": "💼",
        "domains": ["hh.ru", "api.hh.ru", "hhcdn.ru", "chatik.hh.ru", "websocket.hh.ru", "zarplata.ru"],
    },
    "2gis": {
        "name": "2ГИС", "emoji": "🗺",
        "domains": ["2gis.ru", "2gis.com", "maps.2gis.com", "account.2gis.com"],
    },
}

# ── Категории для отображения в меню ──────────────────────────────────────────
CATEGORIES = {
    "🏠 Локальная сеть":          ["local"],
    "🏦 Банки и платежи":         ["sber", "tbank", "alfa", "vtb", "raiffeisen", "sbp"],
    "🛒 Маркетплейсы":            ["ozon", "wildberries", "avito"],
    "🔴 Яндекс":                  ["yandex"],
    "🎬 Видео и стриминг":        ["kinopoisk", "rutube", "vkvideo", "ivi", "okko", "trikolor", "altplayers"],
    "🚂 Транспорт и доставка":    ["rzd", "pochta", "delivery"],
    "🏘 ЖКХ":                     ["zhkh_msk", "zhkh_fed"],
    "🏛 Государственные сервисы": ["gosuslugi", "mos", "nalog", "emias", "gibdd", "rosreestr", "fssp"],
    "💬 Соцсети":                 ["vk", "ok"],
    "📡 Ростелеком":              ["rostelecom"],
    "📦 Прочее":                  ["hh", "2gis"],
}

# Всегда включены — пользователь снять не может
DEFAULT_SELECTED = {"local"}

# Все ключи которые пользователь может включить (без заблокированных)
ALL_SELECTABLE = {k for k in SITES if k not in DEFAULT_SELECTED}
