#!/bin/bash
# modules/mtproxy/install.sh — установка MTProxy для Telegram
# Вызывается из setup.sh --modules при активации модуля mtproxy.
# Предполагает что AWG уже установлен (/etc/amnezia/amneziawg/server.env существует).
# Токен бота и Admin ID берутся из уже существующего bot.env — здесь не спрашиваем.

set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

MTP_DIR="/opt/mtproxy"
MTP_BIN="$MTP_DIR/objs/bin/mtproto-proxy"
CONF_DIR="/etc/proxy-bot"
CONF_FILE="$CONF_DIR/proxy_bot.env"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      MTProxy — Установка                ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Зависимости ───────────────────────────────────────────────────────────────
log "Установка зависимостей..."
apt-get update -qq
apt-get install -y -qq git build-essential libssl-dev zlib1g-dev xxd curl

# ── Сборка MTProxy ────────────────────────────────────────────────────────────
if [[ -f "$MTP_BIN" ]]; then
    warn "MTProxy уже собран: $MTP_BIN"
else
    log "Клонирование MTProxy (GetPageSpeed fork)..."
    rm -rf "$MTP_DIR"
    git clone --depth=1 https://github.com/GetPageSpeed/MTProxy "$MTP_DIR" \
        || err "Не удалось скачать MTProxy."
    log "Сборка (~1-2 минуты)..."
    cd "$MTP_DIR" && make -j"$(nproc)" > /dev/null
    cd /root
    [[ -f "$MTP_BIN" ]] || err "Сборка не удалась — проверьте: journalctl -u awg-bot"
    log "MTProxy собран: $MTP_BIN"
fi

# ── Конфиги Telegram ──────────────────────────────────────────────────────────
log "Загрузка конфигов Telegram..."
curl -sf --max-time 15 https://core.telegram.org/getProxySecret \
    -o "$MTP_DIR/proxy-secret" || err "Не удалось скачать proxy-secret."
curl -sf --max-time 15 https://core.telegram.org/getProxyConfig \
    -o "$MTP_DIR/proxy-multi.conf" || err "Не удалось скачать proxy-multi.conf."

# ── Конфиг-директория ─────────────────────────────────────────────────────────
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
touch "$CONF_FILE"
chmod 600 "$CONF_FILE"

# Вспомогательная функция: записать/обновить ключ в конфиг-файле
_set_conf() {
    local key="$1" val="$2"
    if grep -q "^${key}=" "$CONF_FILE" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$CONF_FILE"
    else
        echo "${key}=${val}" >> "$CONF_FILE"
    fi
}

# SERVER_IP — берём из AWG, не спрашиваем пользователя
SERVER_IP=$(grep "^SERVER_IP=" /etc/amnezia/amneziawg/server.env 2>/dev/null | cut -d= -f2 || true)
if [[ -z "$SERVER_IP" ]]; then
    SERVER_IP=$(curl -4 -sf --max-time 10 https://ifconfig.me 2>/dev/null || \
                curl -4 -sf --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
fi
[[ -n "$SERVER_IP" ]] && info "IP сервера: $SERVER_IP"

# ── Порт ──────────────────────────────────────────────────────────────────────
echo ""
while true; do
    read -p "  Порт MTProxy [443]: " MTP_PORT
    MTP_PORT=${MTP_PORT:-443}
    [[ "$MTP_PORT" =~ ^[0-9]+$ && "$MTP_PORT" -ge 1 && "$MTP_PORT" -le 65535 ]] && break
    warn "Введите корректный порт (1–65535)."
done
ss -tlnp 2>/dev/null | grep -q ":${MTP_PORT} " && \
    warn "Порт ${MTP_PORT} уже занят. Проверьте после установки."

# ── Тип секрета ───────────────────────────────────────────────────────────────
echo ""
echo -e "  ${CYAN}${BOLD}Тип секрета (маскировка трафика):${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} EE — TLS 1.3  ${GREEN}(рекомендуется)${NC}"
echo -e "  ${CYAN}2)${NC} DD — fake-TLS"
echo -e "  ${CYAN}3)${NC} plain"
echo ""
while true; do
    read -p "  Ваш выбор [1]: " ST; ST=${ST:-1}
    [[ "$ST" == "1" || "$ST" == "2" || "$ST" == "3" ]] && break
    warn "Введите 1, 2 или 3."
done

RAW_SECRET=$(head -c 16 /dev/urandom | xxd -ps)
case "$ST" in
    1)
        DOMAIN_HEX=$(echo -n "www.google.com" | xxd -ps)
        MTP_SECRET="ee${RAW_SECRET}${DOMAIN_HEX}"
        MTP_CLEAN="${RAW_SECRET}"
        FAKETLS_FLAG="-D www.google.com"
        SECRET_MODE="EE (TLS 1.3)"
        ;;
    2)
        MTP_SECRET="dd${RAW_SECRET}"
        MTP_CLEAN="${RAW_SECRET}"
        FAKETLS_FLAG=""
        SECRET_MODE="fake-TLS (DD)"
        ;;
    3)
        MTP_SECRET="${RAW_SECRET}"
        MTP_CLEAN="${RAW_SECRET}"
        FAKETLS_FLAG=""
        SECRET_MODE="plain"
        ;;
esac

# ── Сохраняем в конфиг ────────────────────────────────────────────────────────
[[ -n "$SERVER_IP" ]] && _set_conf "SERVER_IP" "$SERVER_IP"
_set_conf "MTP_PORT"   "$MTP_PORT"
_set_conf "MTP_SECRET" "$MTP_SECRET"

# ── systemd сервис ────────────────────────────────────────────────────────────
cat > /etc/systemd/system/mtproxy.service << EOF
[Unit]
Description=MTProxy for Telegram
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c 'curl -sf https://core.telegram.org/getProxySecret -o ${MTP_DIR}/proxy-secret; curl -sf https://core.telegram.org/getProxyConfig -o ${MTP_DIR}/proxy-multi.conf'
ExecStart=${MTP_BIN} -u nobody -p 8888 -H ${MTP_PORT} -S ${MTP_CLEAN} ${FAKETLS_FLAG} --aes-pwd ${MTP_DIR}/proxy-secret ${MTP_DIR}/proxy-multi.conf -M 1
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable mtproxy
systemctl start mtproxy
sleep 2
systemctl is-active --quiet mtproxy \
    && log "MTProxy запущен ✓" \
    || warn "MTProxy не запустился — journalctl -u mtproxy"

# ── ufw (если есть) ───────────────────────────────────────────────────────────
command -v ufw &>/dev/null && ufw allow "${MTP_PORT}/tcp" comment "MTProxy" 2>/dev/null || true

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}${BOLD}MTProxy установлен!${NC}"
echo ""
echo -e "  Режим:  ${SECRET_MODE}"
echo -e "  Порт:   ${MTP_PORT}"
if [[ -n "$SERVER_IP" ]]; then
    MTP_LINK="https://t.me/proxy?server=${SERVER_IP}&port=${MTP_PORT}&secret=${MTP_SECRET}"
    echo -e "  Ссылка: ${CYAN}${MTP_LINK}${NC}"
fi
echo ""
echo -e "  Управление (старт/стоп/смена секрета) — через бота: 📡 Прокси Telegram"
echo ""
