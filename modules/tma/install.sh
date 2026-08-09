#!/bin/bash
# modules/tma/install.sh — установка TMA (Telegram Mini App) веб-панели
# Вызывается из setup.sh --modules при активации модуля tma, либо setup.sh --tma.
# Предполагает что AWG + бот уже установлены (server.env и bot.env должны существовать).

set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

AWG_DIR="/etc/amnezia/amneziawg"
TMA_DIR="${AWG_DIR}/tma"
TMA_SERVER_DST="/root/modules/tma/tma_server.py"
TMA_HTML_DST="${TMA_DIR}/index.html"
NGINX_CONF="/etc/nginx/sites-available/awg-tma"
NGINX_ENABLED="/etc/nginx/sites-enabled/awg-tma"
TMA_PORT=8080

[[ ! -f "${AWG_DIR}/server.env" ]] && err "Не найден server.env — сначала установите AWG (setup.sh)"
[[ ! -f "${AWG_DIR}/bot.env"    ]] && err "Не найден bot.env — сначала установите бота (setup.sh)"

source <(grep -v '^#' "${AWG_DIR}/server.env" | grep '=')
source <(grep -v '^#' "${AWG_DIR}/bot.env"    | grep '=')

# ── Определяем URL репозитория для скачивания файлов ─────────────────────────
REPO_ORG="yntoolsmail-prog"
REPO_NAME="Vpn_AWG"
_branch=$(grep "^REPO_BRANCH=" "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2)
REPO_RAW="https://raw.githubusercontent.com/${REPO_ORG}/${REPO_NAME}/${_branch:-main}"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      TMA — Установка веб-панели         ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Поиск доменов ─────────────────────────────────────────────────────────────
FOUND_DOMAINS=()
_ep=$(grep    "^SERVER_ENDPOINT="        "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2)
_ep_bak=$(grep "^SERVER_ENDPOINT_BACKUP=" "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2)
[[ -n "$_ep"     && "$_ep"     != "$SERVER_IP" && ! "$_ep"     =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && FOUND_DOMAINS+=("$_ep")
[[ -n "$_ep_bak" && "$_ep_bak" != "$SERVER_IP" && ! "$_ep_bak" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && FOUND_DOMAINS+=("$_ep_bak")
if command -v nginx &>/dev/null; then
    while IFS= read -r _d; do
        [[ -n "$_d" && "$_d" != "_" && "$_d" != "$SERVER_IP" && ! "$_d" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && FOUND_DOMAINS+=("$_d")
    done < <(grep -h "server_name" /etc/nginx/sites-enabled/* 2>/dev/null | awk '{print $2}' | tr -d ';' | sort -u)
fi
FOUND_DOMAINS=($(printf "%s\n" "${FOUND_DOMAINS[@]}" | sort -u))

# ── Выбор домена ──────────────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Домен для TMA${NC}"
echo -e "  Telegram не открывает страницы без HTTPS."
echo ""

_i=1
TMA_DOMAIN=""
USE_SSL=0
LE_EMAIL=""

if [[ ${#FOUND_DOMAINS[@]} -gt 0 ]]; then
    echo -e "  ${GREEN}Найдены домены в конфигурации:${NC}"
    echo ""
    for _d in "${FOUND_DOMAINS[@]}"; do
        echo -e "  ${CYAN}${_i})${NC} ${_d}"; ((_i++))
    done
    echo -e "  ${CYAN}${_i})${NC} Ввести другой домен"
    echo -e "  ${CYAN}$((_i+1)))${NC} Без домена (тест в браузере, в Telegram не работает)"
    echo ""
    _choice=""
    while true; do
        read -p "  Ваш выбор [1]: " _choice
        _choice=${_choice:-1}
        [[ "$_choice" =~ ^[0-9]+$ && "$_choice" -ge 1 && "$_choice" -le $((_i+1)) ]] && break
        warn "Неверный выбор."
    done
    if [[ "$_choice" -le ${#FOUND_DOMAINS[@]} ]]; then
        TMA_DOMAIN="${FOUND_DOMAINS[$((_choice-1))]}"; USE_SSL=1
        log "Выбран: ${TMA_DOMAIN}"
    elif [[ "$_choice" -eq $_i ]]; then
        TMA_DOMAIN=""; USE_SSL=1
    else
        TMA_DOMAIN="$SERVER_IP"; USE_SSL=0
        warn "Режим без SSL — TMA в Telegram работать не будет."
    fi
else
    echo -e "  Вариант 1 — домен с A-записью на этот сервер ${GREEN}(рекомендуется)${NC}"
    echo -e "  Вариант 2 — без домена, только тест в браузере"
    echo ""
    _input=""
    read -p "  Введите домен (или Enter чтобы пропустить SSL): " _input
    _input=$(echo "$_input" | tr -d " " | tr "[:upper:]" "[:lower:]")
    if [[ -n "$_input" ]]; then
        TMA_DOMAIN="$_input"; USE_SSL=1
    else
        TMA_DOMAIN="$SERVER_IP"; USE_SSL=0
        warn "Режим без SSL — TMA в Telegram работать не будет."
    fi
fi

# Если выбрали "ввести другой" — спрашиваем
if [[ -z "$TMA_DOMAIN" && "$USE_SSL" -eq 1 ]]; then
    echo ""
    echo -e "  A-запись домена должна указывать на ${CYAN}${SERVER_IP}${NC}"
    echo ""
    while true; do
        read -p "  Введите домен: " TMA_DOMAIN
        TMA_DOMAIN=$(echo "$TMA_DOMAIN" | tr -d " " | tr "[:upper:]" "[:lower:]")
        [[ "$TMA_DOMAIN" =~ ^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$ ]] && break
        warn "Неверный формат домена."
    done
fi

# Email для certbot
if [[ "$USE_SSL" -eq 1 ]]; then
    echo ""
    while true; do
        read -p "  Email для Let's Encrypt: " LE_EMAIL
        [[ "$LE_EMAIL" =~ ^[^@]+@[^@]+\.[^@]+$ ]] && break
        warn "Неверный формат email."
    done
fi

# ── Зависимости ───────────────────────────────────────────────────────────────
log "Обновление пакетов..."
apt-get update -qq
log "Установка nginx..."
apt-get install -y -qq nginx
if [[ "$USE_SSL" -eq 1 ]]; then
    apt-get install -y -qq certbot python3-certbot-nginx
fi
command -v dig &>/dev/null || apt-get install -y -qq dnsutils 2>/dev/null || true

# ── Файлы ─────────────────────────────────────────────────────────────────────
mkdir -p "$TMA_DIR"
mkdir -p /root/modules/tma

log "Скачиваю tma_server.py..."
curl -fsSL "${REPO_RAW}/modules/tma/tma_server.py" -o "${TMA_SERVER_DST}.new"
mv "${TMA_SERVER_DST}.new" "$TMA_SERVER_DST"
chmod 750 "$TMA_SERVER_DST"

log "Скачиваю index.html..."
curl -fsSL "${REPO_RAW}/tma/index.html" -o "${TMA_HTML_DST}.new"
mv "${TMA_HTML_DST}.new" "$TMA_HTML_DST"

chmod o+x /etc/amnezia /etc/amnezia/amneziawg "$TMA_DIR"
chmod o+r "$TMA_HTML_DST"

# ── systemd-сервис ────────────────────────────────────────────────────────────
log "Создаю сервис awg-tma..."
cat > /etc/systemd/system/awg-tma.service << 'EOF2'
[Unit]
Description=AmneziaWG TMA Stats Server
After=network.target awg-bot.service
Wants=awg-bot.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/modules/tma/tma_server.py
Restart=always
RestartSec=5
User=root
WorkingDirectory=/root
Environment=PYTHONPATH=/root
StandardOutput=journal
StandardError=journal
SyslogIdentifier=awg-tma

[Install]
WantedBy=multi-user.target
EOF2

systemctl daemon-reload
systemctl enable awg-tma
systemctl restart awg-tma
sleep 2
systemctl is-active --quiet awg-tma || err "awg-tma не запустился. Проверьте: journalctl -u awg-tma -n 30"

# ── nginx ─────────────────────────────────────────────────────────────────────
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

if [[ "$USE_SSL" -eq 1 ]]; then
    cat > "$NGINX_CONF" << EOF2
server {
    listen 80;
    server_name ${TMA_DOMAIN};
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF2
    ln -sf "$NGINX_CONF" "$NGINX_ENABLED" 2>/dev/null || true
    nginx -t -q && systemctl reload nginx

    certbot certonly --nginx --non-interactive --agree-tos \
        --email "$LE_EMAIL" --domain "$TMA_DOMAIN" 2>&1 | sed 's/^/  /'

    cat > "$NGINX_CONF" << EOF2
server {
    listen 80;
    server_name ${TMA_DOMAIN};
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    server_name ${TMA_DOMAIN};
    ssl_certificate     /etc/letsencrypt/live/${TMA_DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${TMA_DOMAIN}/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;
    root ${TMA_DIR};
    index index.html;
    # index.html — без кэша на стороне Telegram WebView, чтобы свежие
    # деплои (--update) подхватывались сразу. Ревалидация дешёвая:
    # 304 Not Modified, тело не качается, пока ETag/Last-Modified не меняется.
    location = /index.html {
        add_header Cache-Control "no-cache" always;
        add_header X-Frame-Options        "SAMEORIGIN" always;
        add_header X-Content-Type-Options "nosniff"    always;
    }
    location / { try_files \$uri \$uri/ /index.html; }
    location /api/ {
        proxy_pass         http://127.0.0.1:${TMA_PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Init-Data \$http_x_init_data;
        proxy_read_timeout 10s;
    }
    add_header X-Frame-Options        "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff"    always;
    access_log /var/log/nginx/awg-tma-access.log;
    error_log  /var/log/nginx/awg-tma-error.log;
}
EOF2
    nginx -t -q && systemctl reload nginx
    (crontab -l 2>/dev/null | grep -q certbot) || \
        (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --nginx && systemctl reload nginx") | crontab -
    TMA_URL="https://${TMA_DOMAIN}"
else
    # ВАЖНО: nginx НЕ может слушать тот же порт, что Flask (${TMA_PORT}) —
    # порт занят, а proxy_pass указывал бы сам в себя. Отдаём панель на 80.
    cat > "$NGINX_CONF" << EOF2
server {
    listen 80 default_server;
    server_name _;
    root ${TMA_DIR};
    index index.html;
    # index.html — без кэша на стороне Telegram WebView, чтобы свежие
    # деплои (--update) подхватывались сразу.
    location = /index.html {
        add_header Cache-Control "no-cache" always;
    }
    location / { try_files \$uri \$uri/ /index.html; }
    location /api/ {
        proxy_pass         http://127.0.0.1:${TMA_PORT};
        proxy_set_header   X-Init-Data \$http_x_init_data;
        proxy_read_timeout 10s;
    }
}
EOF2
    ln -sf "$NGINX_CONF" "$NGINX_ENABLED" 2>/dev/null || true
    nginx -t -q && systemctl reload nginx || warn "nginx не принял конфиг — панель может не открыться"
    TMA_URL="http://${SERVER_IP}"
fi

# ── Сохраняем TMA_URL в server.env ───────────────────────────────────────────
if grep -q "^TMA_URL=" "${AWG_DIR}/server.env" 2>/dev/null; then
    sed -i "s|^TMA_URL=.*|TMA_URL=${TMA_URL}|" "${AWG_DIR}/server.env"
else
    printf "TMA_URL=%s\n" "$TMA_URL" >> "${AWG_DIR}/server.env"
fi

# ── Перезапускаем бота чтобы подхватил TMA_URL ───────────────────────────────
if systemctl is-active --quiet awg-bot 2>/dev/null; then
    systemctl restart awg-bot && log "awg-bot перезапущен с новым TMA_URL"
fi

# ── Sentinel: модуль установлен ───────────────────────────────────────────────
touch "/root/modules/tma/.installed"

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
log "TMA установлена!"
info "URL: ${TMA_URL}"
echo ""
echo -e "  ${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  ${BOLD}  Команды бота — настройка в BotFather (Edit Commands)${NC}"
echo -e "  ${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${CYAN}Команды регистрируются автоматически при запуске бота.${NC}"
echo -e "  Если хотите проверить или настроить вручную:"
echo ""
echo -e "  ${CYAN}1.${NC} Открой Telegram и найди ${YELLOW}@BotFather${NC}"
echo -e "  ${CYAN}2.${NC} Отправь команду: ${YELLOW}/mybots${NC}"
echo -e "  ${CYAN}3.${NC} Выбери своего бота → ${YELLOW}Bot Settings${NC} → ${YELLOW}Edit Commands${NC}"
echo -e "  ${CYAN}4.${NC} Вставь блок команд:"
echo ""
echo -e "  ${GREEN}start - 🏠 Главная${NC}"
echo -e "  ${GREEN}cancel - ❌ Отмена${NC}"
echo ""
echo -e "  ${YELLOW}Готово!${NC} Команды появятся в кнопке «/» рядом с полем ввода."
echo -e "  ${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
