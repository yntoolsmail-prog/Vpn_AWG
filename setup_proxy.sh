#!/bin/bash
# =============================================================================
# Proxy Bot — установщик (MTProxy + WhatsApp прокси)
# Использование: bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Proxy-Telegram-Whatsapp/main/setup_proxy.sh)
# =============================================================================
# Version: 1.0

set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
sep()  { echo -e "${CYAN}${BOLD}── $1 ──────────────────────────────────────${NC}"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

RAW_BASE="https://raw.githubusercontent.com/yntoolsmail-prog/Proxy-Telegram-Whatsapp/main"
MTP_DIR="/opt/mtproxy"
MTP_BIN="$MTP_DIR/objs/bin/mtproto-proxy"
CONF_DIR="/etc/proxy-bot"
CONF_FILE="$CONF_DIR/proxy_bot.env"
PROXY_BOT_FILE="/root/proxy_bot.py"

clear
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║         Proxy Bot — Установка            ║"
echo "  ║     MTProxy + WhatsApp для Telegram     ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ══════════════════════════════════════════════════════════════════════════════
# РЕЖИМ ВЫВОДА
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "  ${CYAN}1)${NC} Тихая установка     — только ключевые шаги ${GREEN}(рекомендуется)${NC}"
echo -e "  ${CYAN}2)${NC} Подробная установка — показывать все процессы"
echo ""
while true; do
    read -p "  Ваш выбор [1]: " VERBOSE_MODE
    VERBOSE_MODE=${VERBOSE_MODE:-1}
    [[ "$VERBOSE_MODE" == "1" || "$VERBOSE_MODE" == "2" ]] && break
    warn "Введите 1 или 2."
done
if [[ "$VERBOSE_MODE" == "1" ]]; then
    APT_FLAGS="-qq"
    info "Режим: тихая установка"
else
    APT_FLAGS=""
    info "Режим: подробная установка"
fi

run() {
    if [[ "$VERBOSE_MODE" == "1" ]]; then
        eval "$@" > /dev/null
    else
        eval "$@"
    fi
}

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 1: ПРОКСИ
# ══════════════════════════════════════════════════════════════════════════════
echo ""
sep "Выбор прокси"
echo ""
echo -e "  ${CYAN}1)${NC} ${BOLD}MTProxy + WhatsApp${NC} ${GREEN}(рекомендуется)${NC}"
echo -e "     MTProxy для Telegram + официальный прокси для WhatsApp."
echo -e "     ${YELLOW}⚠ WhatsApp требует Docker${NC} (~200 MB установка, ~50 MB RAM постоянно)"
echo ""
echo -e "  ${CYAN}2)${NC} ${BOLD}Только MTProxy${NC} — для Telegram"
echo -e "     Нативная поддержка в Telegram. Быстро, без Docker."
echo ""
echo -e "  ${CYAN}3)${NC} ${BOLD}Только WhatsApp прокси${NC}"
echo -e "     Официальный контейнер от Meta."
echo -e "     ${YELLOW}⚠ Требует Docker${NC} (~200 MB установка, ~50 MB RAM постоянно)"
echo ""
while true; do
    read -p "  Ваш выбор [1]: " PROXY_CHOICE
    PROXY_CHOICE=${PROXY_CHOICE:-1}
    [[ "$PROXY_CHOICE" =~ ^[1-3]$ ]] && break
    warn "Введите 1, 2 или 3."
done

INSTALL_MTP=false; INSTALL_WA=false
case "$PROXY_CHOICE" in
    1) INSTALL_MTP=true; INSTALL_WA=true ;;
    2) INSTALL_MTP=true                  ;;
    3)                   INSTALL_WA=true ;;
esac

# Предупреждение про Docker
if [[ "$INSTALL_WA" == "true" ]]; then
    echo ""
    echo -e "  ${YELLOW}${BOLD}Внимание — WhatsApp прокси использует Docker:${NC}"
    echo -e "  • Установка Docker: ~200 MB"
    echo -e "  • Постоянное потребление RAM: ~50 MB"
    echo -e "  • Образ WhatsApp/proxy от Meta (~30 MB)"
    echo -e "  • Docker запускается при старте системы"
    echo ""
    read -p "  Продолжить? [Y/n]: " WA_CONFIRM
    WA_CONFIRM=${WA_CONFIRM:-y}
    if [[ "${WA_CONFIRM,,}" != "y" ]]; then
        INSTALL_WA=false
        warn "WhatsApp прокси исключён."
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 3: ТОКЕН И ADMIN ID
# ══════════════════════════════════════════════════════════════════════════════
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"

echo ""
sep "Токен бота"
echo ""
echo -e "  Создайте нового бота у ${YELLOW}@BotFather${NC} → /newbot"
echo ""
while true; do
    read -p "  Вставьте токен бота: " BOT_TOKEN
    [[ "$BOT_TOKEN" == *":"* && ${#BOT_TOKEN} -gt 20 ]] && break
    warn "Неверный формат."
done
echo ""
echo -e "  Узнайте ID у ${YELLOW}@userinfobot${NC}"
echo ""
while true; do
    read -p "  Вставьте ваш Telegram ID: " ADMIN_ID
    [[ "$ADMIN_ID" =~ ^[0-9]+$ ]] && break
    warn "ID должен быть числом."
done
echo ""
log "Определяю IP сервера..."
SERVER_IP=$(curl -4 -sf --max-time 10 https://ifconfig.me 2>/dev/null || \
            curl -4 -sf --max-time 10 https://api.ipify.org 2>/dev/null || \
            curl -4 -sf --max-time 10 https://ifconfig.co 2>/dev/null || echo "")
if [[ -z "$SERVER_IP" ]] || [[ "$SERVER_IP" == *":"* ]]; then
    warn "Не удалось автоматически определить внешний IPv4."
    while true; do
        read -p "  Введите внешний IPv4 сервера: " SERVER_IP
        [[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] && break
        warn "Неверный формат. Введите IPv4, например: 185.123.45.67"
    done
else
    info "IP: ${SERVER_IP}"
fi
printf "BOT_TOKEN=%s\nADMIN_ID=%s\nSERVER_IP=%s\n" \
    "$BOT_TOKEN" "$ADMIN_ID" "$SERVER_IP" > "$CONF_FILE"
chmod 600 "$CONF_FILE"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 4: ЗАВИСИМОСТИ
# ══════════════════════════════════════════════════════════════════════════════
log "Установка зависимостей..."
apt-get update $APT_FLAGS
apt-get install -y $APT_FLAGS curl git build-essential libssl-dev zlib1g-dev xxd \
    python3 python3-pip software-properties-common

log "Установка python-telegram-bot..."
pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages > /dev/null || \
pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages || \
err "Не удалось установить python-telegram-bot."

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 5: MTPROXY
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_MTP" == "true" ]]; then
    echo ""
    sep "MTProxy"

    if [[ -f "$MTP_BIN" ]]; then
        warn "MTProxy уже собран."
    else
        log "Клонирование MTProxy (GetPageSpeed fork)..."
        rm -rf "$MTP_DIR"
        git clone --depth=1 https://github.com/GetPageSpeed/MTProxy "$MTP_DIR" \
            || err "Не удалось скачать MTProxy."
        log "Сборка (~1-2 минуты)..."
        run "cd '$MTP_DIR' && make -j\$(nproc)"
        cd /root
        [[ -f "$MTP_BIN" ]] || err "Сборка не удалась."
        log "Собран ✓"
    fi

    log "Загрузка конфигов Telegram..."
    curl -sf https://core.telegram.org/getProxySecret -o "$MTP_DIR/proxy-secret" \
        || err "Не удалось скачать proxy-secret."
    curl -sf https://core.telegram.org/getProxyConfig -o "$MTP_DIR/proxy-multi.conf" \
        || err "Не удалось скачать proxy-multi.conf."

    echo ""
    # Если устанавливается вместе с WhatsApp — 443 занят, предлагаем 8443
    if [[ "$INSTALL_WA" == "true" ]]; then
        warn "WhatsApp прокси займёт порт 443 — для MTProxy используйте другой порт."
        MTP_PORT_DEFAULT="8443"
    else
        MTP_PORT_DEFAULT="443"
    fi
    while true; do
        read -p "  Порт MTProxy [${MTP_PORT_DEFAULT}]: " MTP_PORT
        MTP_PORT=${MTP_PORT:-$MTP_PORT_DEFAULT}
        [[ "$MTP_PORT" =~ ^[0-9]+$ && "$MTP_PORT" -ge 1 && "$MTP_PORT" -le 65535 ]] && break
        warn "Введите корректный порт."
    done
    if [[ "$INSTALL_WA" == "true" && "$MTP_PORT" == "443" ]]; then
        err "Порт 443 зарезервирован для WhatsApp прокси. Выберите другой порт для MTProxy."
    fi
    ss -tlnp 2>/dev/null | grep -q ":${MTP_PORT} " && \
        warn "Порт ${MTP_PORT} уже занят! Проверьте после установки."

    echo ""
    echo -e "  ${CYAN}${BOLD}Тип секрета (способ маскировки трафика)${NC}"
    echo ""
    echo -e "  ${CYAN}1)${NC} ${BOLD}EE mode${NC} ${GREEN}(рекомендуется)${NC}"
    echo -e "     Имитирует TLS 1.3 — самый сложный для обнаружения."
    echo ""
    echo -e "  ${CYAN}2)${NC} ${BOLD}fake-TLS (DD mode)${NC}"
    echo -e "     Трафик выглядит как обычный HTTPS."
    echo ""
    echo -e "  ${CYAN}3)${NC} ${BOLD}plain${NC}"
    echo -e "     Трафик виден как MTProxy — легче блокируется."
    echo -e "     Только если EE и DD не работают."
    echo ""
    while true; do
        read -p "  Тип секрета [1]: " ST; ST=${ST:-1}
        [[ "$ST" == "1" || "$ST" == "2" || "$ST" == "3" ]] && break
    done
    RAW_SECRET=$(head -c 16 /dev/urandom | xxd -ps)
    if [[ "$ST" == "1" ]]; then
        SECRET_MODE="EE (TLS 1.3)"
        # EE: ee + 32 hex + hex-encoded домен
        DOMAIN_HEX=$(echo -n "www.google.com" | xxd -ps)
        MTP_SECRET_STORE="ee${RAW_SECRET}${DOMAIN_HEX}"
        MTP_FAKETLS_FLAG="-D www.google.com"
    elif [[ "$ST" == "2" ]]; then
        SECRET_MODE="fake-TLS (DD)"
        MTP_SECRET_STORE="dd${RAW_SECRET}"
        MTP_FAKETLS_FLAG="-D google.com"
    else
        SECRET_MODE="plain"
        MTP_SECRET_STORE="${RAW_SECRET}"
        MTP_FAKETLS_FLAG=""
    fi
    MTP_SECRET="${RAW_SECRET}"

    command -v ufw &>/dev/null && ufw allow "${MTP_PORT}/tcp" comment "MTProxy" 2>/dev/null || true

    printf "MTP_PORT=%s\nMTP_SECRET=%s\n" "$MTP_PORT" "$MTP_SECRET_STORE" >> "$CONF_FILE"

    cat > /etc/systemd/system/mtproxy.service << EOF
[Unit]
Description=MTProxy for Telegram
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c 'curl -sf https://core.telegram.org/getProxySecret -o ${MTP_DIR}/proxy-secret; curl -sf https://core.telegram.org/getProxyConfig -o ${MTP_DIR}/proxy-multi.conf'
ExecStart=${MTP_BIN} -u nobody -p 8888 -H ${MTP_PORT} -S ${MTP_SECRET} ${MTP_FAKETLS_FLAG} --aes-pwd ${MTP_DIR}/proxy-secret ${MTP_DIR}/proxy-multi.conf -M 1
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable mtproxy
    systemctl start  mtproxy
    sleep 2
    systemctl is-active --quiet mtproxy && log "MTProxy запущен ✓" || warn "MTProxy не запустился — journalctl -u mtproxy"
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 6: WHATSAPP PROXY
# ══════════════════════════════════════════════════════════════════════════════
if [[ "$INSTALL_WA" == "true" ]]; then
    echo ""
    sep "WhatsApp прокси (Docker)"

    # Установка Docker если нет
    if ! command -v docker &>/dev/null; then
        log "Установка Docker..."
        curl -fsSL https://get.docker.com | sh \
            || err "Не удалось установить Docker."
        systemctl enable docker --now
        log "Docker установлен ✓"
    else
        warn "Docker уже установлен."
    fi

    command -v ufw &>/dev/null && {
        ufw allow 80/tcp   comment "WhatsApp proxy" 2>/dev/null || true
        ufw allow 443/tcp  comment "WhatsApp proxy" 2>/dev/null || true
        ufw allow 5222/tcp comment "WhatsApp proxy" 2>/dev/null || true
    }

    printf "WA_PORT=443\n" >> "$CONF_FILE"

    log "Скачиваю образ WhatsApp прокси..."
    if docker pull facebook/whatsapp_proxy:latest 2>/dev/null; then
        WA_IMAGE="facebook/whatsapp_proxy:latest"
        log "Образ загружен с Docker Hub ✓"
    else
        warn "Docker Hub недоступен — собираю из исходников..."
        WA_SRC="/opt/whatsapp-proxy-src"
        rm -rf "$WA_SRC"
        git clone --depth=1 https://github.com/WhatsApp/proxy "$WA_SRC" \
            || err "Не удалось скачать исходники WhatsApp proxy."
        run "docker build -t whatsapp-proxy '$WA_SRC/proxy'"
        WA_IMAGE="whatsapp-proxy"
        log "Образ собран из исходников ✓"
    fi

    # Удаляем старый контейнер если есть
    docker rm -f whatsapp-proxy 2>/dev/null || true

    log "Запускаю контейнер..."
    docker run -d \
        --name whatsapp-proxy \
        --restart always \
        -p "80:80" \
        -p "443:443" \
        -p "5222:5222" \
        "$WA_IMAGE"

    sleep 3
    if docker ps --filter "name=whatsapp-proxy" --filter "status=running" | grep -q whatsapp; then
        log "WhatsApp прокси запущен ✓"
    else
        warn "Контейнер не запустился — docker logs whatsapp-proxy"
    fi
fi

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 7: ЧАСОВОЙ ПОЯС
# ══════════════════════════════════════════════════════════════════════════════
echo ""
sep "Часовой пояс"
echo ""
echo -e "  Используется для корректного отображения времени в логах бота."
echo ""
SYS_TZ=$(cat /etc/timezone 2>/dev/null || timedatectl | grep "Time zone" | awk '{print $3}')
SYS_TZ=${SYS_TZ:-UTC}
echo -e "  Текущий часовой пояс сервера: ${CYAN}${SYS_TZ}${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} Europe/Moscow      — Москва (UTC+3)"
echo -e "  ${CYAN}2)${NC} Asia/Yekaterinburg — Екатеринбург (UTC+5)"
echo -e "  ${CYAN}3)${NC} Asia/Novosibirsk   — Новосибирск (UTC+7)"
echo -e "  ${CYAN}4)${NC} Asia/Vladivostok   — Владивосток (UTC+10)"
echo -e "  ${CYAN}5)${NC} Europe/Kiev        — Киев (UTC+2)"
echo -e "  ${CYAN}6)${NC} Asia/Almaty        — Алматы (UTC+5)"
echo -e "  ${CYAN}7)${NC} Europe/Berlin      — Берлин (UTC+1/2)"
echo -e "  ${CYAN}8)${NC} UTC"
echo -e "  ${CYAN}9)${NC} Использовать пояс сервера ${CYAN}(${SYS_TZ})${NC}"
echo -e "  ${CYAN}0)${NC} Ввести вручную"
echo ""
while true; do
    read -p "  Ваш выбор [9]: " TZ_CHOICE
    TZ_CHOICE=${TZ_CHOICE:-9}
    [[ "$TZ_CHOICE" =~ ^[0-9]$ ]] && break
    warn "Введите число от 0 до 9."
done
case "$TZ_CHOICE" in
    1) TIMEZONE="Europe/Moscow" ;;
    2) TIMEZONE="Asia/Yekaterinburg" ;;
    3) TIMEZONE="Asia/Novosibirsk" ;;
    4) TIMEZONE="Asia/Vladivostok" ;;
    5) TIMEZONE="Europe/Kiev" ;;
    6) TIMEZONE="Asia/Almaty" ;;
    7) TIMEZONE="Europe/Berlin" ;;
    8) TIMEZONE="UTC" ;;
    9) TIMEZONE="$SYS_TZ" ;;
    0)
        echo ""
        echo -e "  Примеры: Europe/Moscow, Asia/Tokyo, America/New_York"
        echo -e "  Полный список: timedatectl list-timezones"
        echo ""
        while true; do
            read -p "  Введите часовой пояс: " TIMEZONE
            TIMEZONE="${TIMEZONE// /}"
            [[ -z "$TIMEZONE" ]] && warn "Пояс не может быть пустым." && continue
            if timedatectl list-timezones 2>/dev/null | grep -qx "$TIMEZONE"; then
                break
            else
                warn "Пояс '${TIMEZONE}' не найден. Проверьте написание."
            fi
        done
        ;;
esac
timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true
printf "TIMEZONE=%s\n" "$TIMEZONE" >> "$CONF_FILE"
info "Часовой пояс: ${TIMEZONE}"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 8: СКАЧИВАЕМ proxy_bot.py
# ══════════════════════════════════════════════════════════════════════════════
log "Скачиваем proxy_bot.py..."
curl -sf "${RAW_BASE}/proxy_bot.py" -o "$PROXY_BOT_FILE" \
    || err "Не удалось скачать proxy_bot.py."
chmod +x "$PROXY_BOT_FILE"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 9: ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════
log "Создаём systemd сервис proxy-bot..."
cat > /etc/systemd/system/proxy-bot.service << 'EOF'
[Unit]
Description=Proxy Bot (MTProxy + WhatsApp)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/proxy_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable proxy-bot
systemctl start  proxy-bot
sleep 2
systemctl is-active --quiet proxy-bot && log "Proxy Bot запущен ✓" || warn "Не запустился — journalctl -u proxy-bot"

# ══════════════════════════════════════════════════════════════════════════════
# ШАГ 10: СОХРАНЯЕМ ТЕКУЩУЮ ВЕРСИЮ
# ══════════════════════════════════════════════════════════════════════════════
# Запоминаем текущий коммит — бот будет сравнивать с ним при ежедневной проверке
CURRENT_SHA=$(curl -s "https://api.github.com/repos/yntoolsmail-prog/Proxy-Telegram-Whatsapp/commits/main" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'][:7])" 2>/dev/null || echo "unknown")
echo "$CURRENT_SHA" > /root/.proxy_bot_version
info "Версия бота: ${CURRENT_SHA}"

# ══════════════════════════════════════════════════════════════════════════════
# ИТОГ
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}   Установка завершена!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo ""

if [[ "$INSTALL_MTP" == "true" ]]; then
    MTP_LINK="https://t.me/proxy?server=${SERVER_IP}&port=${MTP_PORT}&secret=${MTP_SECRET_STORE}"
    echo -e "  ${CYAN}📡 MTProxy${NC} (${SECRET_MODE})  порт ${MTP_PORT}"
    echo -e "  ${GREEN}${MTP_LINK}${NC}"
    echo ""
fi

if [[ "$INSTALL_WA" == "true" ]]; then
    echo -e "  ${CYAN}💬 WhatsApp прокси${NC}  порты 80, 443, 5222"
    echo -e "  Адрес для WhatsApp: ${GREEN}${SERVER_IP}:443${NC}"
    echo ""
fi

info "Статус: systemctl status proxy-bot"
info "Логи:   journalctl -u proxy-bot -f"
echo -e "  Напишите ${CYAN}/start${NC} боту в Telegram"
info "Бот будет уведомлять об обновлениях ежедневно в 10:00"
echo ""