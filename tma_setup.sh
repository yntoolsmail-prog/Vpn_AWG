#!/bin/bash
# =============================================================================
# AmneziaWG TMA — Установщик веб-панели статистики
# Установка:  bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/tma_setup.sh)
# Обновление: bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/tma_setup.sh) --update
# =============================================================================
# Version: 1.1

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }

REPO_RAW="https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main"
AWG_DIR="/etc/amnezia/amneziawg"
TMA_DIR="${AWG_DIR}/tma"
TMA_SERVER_DST="${AWG_DIR}/tma_server.py"
TMA_HTML_DST="${TMA_DIR}/index.html"
NGINX_CONF="/etc/nginx/sites-available/awg-tma"
NGINX_ENABLED="/etc/nginx/sites-enabled/awg-tma"
SYSTEMD_SERVICE="/etc/systemd/system/awg-tma.service"
TMA_PORT=8080

[[ $EUID -ne 0 ]] && err "Запускать от root: sudo bash tma_setup.sh"

# =============================================================================
# РЕЖИМ ОБНОВЛЕНИЯ
# =============================================================================
if [[ "${1}" == "--update" ]]; then
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║       AmneziaWG TMA — Обновление         ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"

    [[ ! -f "$TMA_SERVER_DST" ]] && err "TMA не установлена. Запустите без --update для первичной установки."

    log "Скачиваю tma_server.py из репо..."
    curl -fsSL "${REPO_RAW}/tma_server.py" -o "${TMA_SERVER_DST}.new"
    mv "${TMA_SERVER_DST}.new" "$TMA_SERVER_DST"
    chmod 750 "$TMA_SERVER_DST"
    ok "tma_server.py обновлён"

    log "Скачиваю index.html из репо..."
    mkdir -p "$TMA_DIR"
    curl -fsSL "${REPO_RAW}/tma/index.html" -o "${TMA_HTML_DST}.new"
    mv "${TMA_HTML_DST}.new" "$TMA_HTML_DST"
    ok "index.html обновлён"

    log "Перезапускаю awg-tma..."
    systemctl restart awg-tma
    sleep 2

    if systemctl is-active --quiet awg-tma; then
        ok "awg-tma перезапущен"
    else
        err "awg-tma не запустился. Проверьте: journalctl -u awg-tma -n 30"
    fi

    echo ""
    echo -e "  ${GREEN}${BOLD}✓ Обновление завершено${NC}"
    echo ""
    echo -e "  ${BOLD}Полезные команды:${NC}"
    echo -e "  Логи:    ${CYAN}journalctl -u awg-tma -f${NC}"
    echo -e "  Статус:  ${CYAN}systemctl status awg-tma${NC}"
    echo ""
    exit 0
fi

# =============================================================================
# ПЕРВИЧНАЯ УСТАНОВКА
# =============================================================================

[[ ! -f "/etc/amnezia/amneziawg/bot.env" ]]    && err "Не найден bot.env — сначала установите бота (setup.sh)"
[[ ! -f "/etc/amnezia/amneziawg/server.env" ]] && err "Не найден server.env — сначала установите AWG (setup.sh)"

source <(grep -v '^#' /etc/amnezia/amneziawg/server.env | grep '=')
source <(grep -v '^#' /etc/amnezia/amneziawg/bot.env    | grep '=')

clear
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║     AmneziaWG TMA — Установка панели     ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

info "Сервер: ${SERVER_IP}:${SERVER_PORT}"
info "Admin ID: ${ADMIN_ID}"
echo ""

# ── Шаг 1: Домен ──────────────────────────────────────────────────────────────
echo -e "  ${BOLD}Шаг 1 из 5 — Домен${NC}"
echo ""
echo -e "  Для работы TMA нужен HTTPS-адрес."
echo -e "  Telegram не открывает страницы без SSL."
echo ""
echo -e "  ┌──────────────────────────────────────────────────────────────────┐"
echo -e "  │  ${CYAN}Вариант 1 — Свой домен или поддомен${NC}  ${GREEN}(рекомендуется)${NC}          │"
echo -e "  │  Например: stats.mydomain.com                                    │"
echo -e "  │  Домен должен уже указывать A-записью на IP этого сервера.       │"
echo -e "  │  Сертификат Let's Encrypt выдаётся автоматически, бесплатно.    │"
echo -e "  └──────────────────────────────────────────────────────────────────┘"
echo ""
echo -e "  ┌──────────────────────────────────────────────────────────────────┐"
echo -e "  │  ${YELLOW}Вариант 2 — Без домена (только тест в браузере)${NC}                  │"
echo -e "  │  HTTP без SSL — TMA в Telegram работать НЕ будет.               │"
echo -e "  └──────────────────────────────────────────────────────────────────┘"
echo ""

while true; do
    read -p "  Ваш выбор [1]: " DOMAIN_CHOICE
    DOMAIN_CHOICE=${DOMAIN_CHOICE:-1}
    [[ "$DOMAIN_CHOICE" == "1" || "$DOMAIN_CHOICE" == "2" ]] && break
    warn "Введите 1 или 2."
done

USE_SSL=0
DOMAIN=""

if [[ "$DOMAIN_CHOICE" == "1" ]]; then
    USE_SSL=1
    echo ""
    echo -e "  ${BOLD}Важно:${NC} A-запись домена должна уже указывать на ${CYAN}${SERVER_IP}${NC}"
    echo ""
    while true; do
        read -p "  Введите домен (например stats.mydomain.com): " DOMAIN
        DOMAIN=$(echo "$DOMAIN" | tr -d ' ' | tr '[:upper:]' '[:lower:]')
        [[ "$DOMAIN" =~ ^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$ ]] && break
        warn "Неверный формат домена."
    done

    echo ""
    info "Проверяю что ${DOMAIN} указывает на ${SERVER_IP}..."
    RESOLVED_IP=$(dig +short "$DOMAIN" A 2>/dev/null | tail -1 || \
                  getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1 || echo "")

    if [[ -z "$RESOLVED_IP" ]]; then
        warn "Не удалось определить IP для ${DOMAIN}."
        read -p "  Продолжить несмотря на это? (y/N): " FORCE_CONT
        [[ "$FORCE_CONT" != "y" && "$FORCE_CONT" != "Y" ]] && info "Выход." && exit 0
    elif [[ "$RESOLVED_IP" != "$SERVER_IP" ]]; then
        warn "${DOMAIN} указывает на ${RESOLVED_IP}, а не на ${SERVER_IP}."
        read -p "  Продолжить несмотря на это? (y/N): " FORCE_CONT
        [[ "$FORCE_CONT" != "y" && "$FORCE_CONT" != "Y" ]] && info "Выход. Исправьте A-запись и запустите снова." && exit 0
    else
        ok "${DOMAIN} → ${RESOLVED_IP} ✓"
    fi

    echo ""
    while true; do
        read -p "  Email для Let's Encrypt: " LE_EMAIL
        [[ "$LE_EMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]] && break
        warn "Неверный формат email."
    done

else
    DOMAIN="$SERVER_IP"
    warn "Режим тестирования — HTTPS не будет."
fi

echo ""

# ── Шаг 2: Проверка портов ────────────────────────────────────────────────────
echo -e "  ${BOLD}Шаг 2 из 5 — Проверка портов${NC}"
echo ""

if [[ "$USE_SSL" -eq 1 ]]; then
    if ss -tlnp 2>/dev/null | grep -q ':80 ' && ! systemctl is-active --quiet nginx 2>/dev/null; then
        err "Порт 80 занят другим процессом (не nginx)."
    fi
    ok "Порты 80 и 443 свободны"
else
    if ss -tlnp 2>/dev/null | grep -q ":${TMA_PORT} "; then
        warn "Порт ${TMA_PORT} занят."
        read -p "  Введите другой порт [8081]: " TMA_PORT_NEW
        TMA_PORT=${TMA_PORT_NEW:-8081}
    fi
    ok "Порт ${TMA_PORT} свободен"
fi

echo ""

# ── Шаг 3: Пакеты ─────────────────────────────────────────────────────────────
echo -e "  ${BOLD}Шаг 3 из 5 — Установка пакетов${NC}"
echo ""

export DEBIAN_FRONTEND=noninteractive

if command -v nginx &>/dev/null; then
    ok "nginx уже установлен"
else
    log "Устанавливаю nginx..."
    apt-get install -y -qq nginx
    ok "nginx установлен"
fi

if [[ "$USE_SSL" -eq 1 ]]; then
    if command -v certbot &>/dev/null; then
        ok "certbot уже установлен"
    else
        log "Устанавливаю certbot..."
        apt-get install -y -qq certbot python3-certbot-nginx
        ok "certbot установлен"
    fi
fi

command -v dig  &>/dev/null || apt-get install -y -qq dnsutils 2>/dev/null || true
command -v curl &>/dev/null || apt-get install -y -qq curl     2>/dev/null || true

echo ""

# ── Шаг 4: Файлы ──────────────────────────────────────────────────────────────
echo -e "  ${BOLD}Шаг 4 из 5 — Установка файлов${NC}"
echo ""

mkdir -p "$TMA_DIR"
chmod 755 "$TMA_DIR"

log "Скачиваю tma_server.py..."
curl -fsSL "${REPO_RAW}/tma_server.py" -o "${TMA_SERVER_DST}.new"
mv "${TMA_SERVER_DST}.new" "$TMA_SERVER_DST"
chmod 750 "$TMA_SERVER_DST"
chown root:root "$TMA_SERVER_DST"
ok "tma_server.py → ${TMA_SERVER_DST}"

log "Скачиваю index.html..."
curl -fsSL "${REPO_RAW}/tma/index.html" -o "${TMA_HTML_DST}.new"
mv "${TMA_HTML_DST}.new" "$TMA_HTML_DST"
ok "index.html → ${TMA_HTML_DST}"

log "Создаю systemd-сервис awg-tma..."
cat > "$SYSTEMD_SERVICE" << EOF
[Unit]
Description=AmneziaWG TMA Stats Server
After=network.target awg-bot.service
Wants=awg-bot.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${TMA_SERVER_DST}
Restart=always
RestartSec=5
User=root
WorkingDirectory=${AWG_DIR}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=awg-tma

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable awg-tma
systemctl restart awg-tma
sleep 2

if systemctl is-active --quiet awg-tma; then
    ok "awg-tma запущен"
else
    err "awg-tma не запустился. Проверьте: journalctl -u awg-tma -n 30"
fi

echo ""

# ── Шаг 5: nginx + SSL ────────────────────────────────────────────────────────
echo -e "  ${BOLD}Шаг 5 из 5 — Настройка nginx${NC}"
echo ""

rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

if [[ "$USE_SSL" -eq 1 ]]; then

    cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
    ln -sf "$NGINX_CONF" "$NGINX_ENABLED" 2>/dev/null || true
    nginx -t -q && systemctl reload nginx
    ok "Временный HTTP-конфиг активирован"

    echo ""
    log "Получаю сертификат Let's Encrypt для ${DOMAIN}..."
    echo ""

    certbot certonly \
        --nginx \
        --non-interactive \
        --agree-tos \
        --email "$LE_EMAIL" \
        --domain "$DOMAIN" 2>&1 | sed 's/^/  /'

    echo ""
    ok "Сертификат получен"

    log "Создаю HTTPS-конфиг nginx..."
    cat > "$NGINX_CONF" << EOF
# AmneziaWG TMA — auto-generated by tma_setup.sh
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    root ${TMA_DIR};
    index index.html;

    location / { try_files \$uri \$uri/ /index.html; }

    location /api/ {
        proxy_pass         http://127.0.0.1:${TMA_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_read_timeout 10s;
    }

    add_header X-Frame-Options        "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff"    always;

    access_log /var/log/nginx/awg-tma-access.log;
    error_log  /var/log/nginx/awg-tma-error.log;
}
EOF

    nginx -t -q && systemctl reload nginx
    ok "HTTPS-конфиг активирован"

    if systemctl is-active --quiet certbot.timer 2>/dev/null || \
       crontab -l 2>/dev/null | grep -q certbot; then
        ok "Автообновление сертификата уже настроено"
    else
        (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --nginx && systemctl reload nginx") | crontab -
        ok "Автообновление сертификата добавлено в cron"
    fi

    TMA_URL="https://${DOMAIN}"

else
    cat > "$NGINX_CONF" << EOF
server {
    listen ${TMA_PORT} default_server;
    server_name _;
    root ${TMA_DIR};
    index index.html;
    location / { try_files \$uri \$uri/ /index.html; }
    location /api/ {
        proxy_pass http://127.0.0.1:${TMA_PORT};
        proxy_read_timeout 10s;
    }
    access_log /var/log/nginx/awg-tma-access.log;
    error_log  /var/log/nginx/awg-tma-error.log;
}
EOF
    ln -sf "$NGINX_CONF" "$NGINX_ENABLED" 2>/dev/null || true
    nginx -t -q && systemctl reload nginx
    ok "HTTP-конфиг активирован"
    TMA_URL="http://${SERVER_IP}:${TMA_PORT}"
fi

# ── Финал ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════════════════════╗"
echo "  ║                    ✓  Установка завершена!                      ║"
echo "  ╚══════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

echo -e "  ${BOLD}Сервисы:${NC}"
systemctl is-active --quiet awg-tma && echo -e "  ${GREEN}●${NC} awg-tma — запущен" || echo -e "  ${RED}●${NC} awg-tma — не запущен"
systemctl is-active --quiet nginx   && echo -e "  ${GREEN}●${NC} nginx   — запущен" || echo -e "  ${RED}●${NC} nginx   — не запущен"
echo ""
echo -e "  ${BOLD}URL панели:${NC}  ${CYAN}${TMA_URL}${NC}"
echo ""

if [[ "$USE_SSL" -eq 1 ]]; then
    echo -e "  ${BOLD}Следующий шаг — кнопка в Telegram:${NC}"
    echo ""
    echo -e "  1. Откройте ${CYAN}@BotFather${NC}"
    echo -e "  2. ${CYAN}/mybots${NC} → ваш бот → ${CYAN}Bot Settings${NC} → ${CYAN}Menu Button${NC} → ${CYAN}Configure menu button${NC}"
    echo -e "  3. URL:      ${CYAN}${TMA_URL}${NC}"
    echo -e "  4. Название: ${CYAN}Монитор${NC}"
    echo ""
fi

echo -e "  ${BOLD}Команды:${NC}"
echo -e "  Статус:    ${CYAN}systemctl status awg-tma${NC}"
echo -e "  Логи:      ${CYAN}journalctl -u awg-tma -f${NC}"
echo -e "  Обновить:  ${CYAN}bash <(curl -s ${REPO_RAW}/tma_setup.sh) --update${NC}"
echo ""
echo -e "  ${BOLD}Файлы:${NC}"
echo -e "  Сервер:  ${CYAN}${TMA_SERVER_DST}${NC}"
echo -e "  HTML:    ${CYAN}${TMA_HTML_DST}${NC}"
echo -e "  nginx:   ${CYAN}${NGINX_CONF}${NC}"
echo ""
