#!/bin/bash
# modules/tma/uninstall.sh — удаление TMA (Telegram Mini App) веб-панели
# Вызывается из setup.sh при команде 'd<N>' в меню модулей.
# Удаляет сервис awg-tma, nginx-конфиг и TMA-файлы.
# server.env и конфиг AWG сохраняются.

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

AWG_DIR="/etc/amnezia/amneziawg"
NGINX_CONF="/etc/nginx/sites-available/awg-tma"
NGINX_ENABLED="/etc/nginx/sites-enabled/awg-tma"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      TMA — Удаление                     ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Остановка и отключение сервиса ───────────────────────────────────────────
if systemctl is-active --quiet awg-tma 2>/dev/null; then
    log "Останавливаю awg-tma..."
    systemctl stop awg-tma
fi
if systemctl is-enabled --quiet awg-tma 2>/dev/null; then
    systemctl disable awg-tma
fi

# ── Удаление systemd-юнита ────────────────────────────────────────────────────
if [[ -f /etc/systemd/system/awg-tma.service ]]; then
    log "Удаляю /etc/systemd/system/awg-tma.service..."
    rm -f /etc/systemd/system/awg-tma.service
    systemctl daemon-reload
fi

# ── Удаление nginx-конфига ────────────────────────────────────────────────────
if [[ -f "$NGINX_ENABLED" || -f "$NGINX_CONF" ]]; then
    log "Удаляю nginx-конфиг awg-tma..."
    rm -f "$NGINX_ENABLED" "$NGINX_CONF"
    if command -v nginx &>/dev/null && systemctl is-active --quiet nginx 2>/dev/null; then
        nginx -t -q 2>/dev/null && systemctl reload nginx || true
    fi
fi

# ── Удаление TMA-файлов ───────────────────────────────────────────────────────
TMA_DIR="${AWG_DIR}/tma"
if [[ -d "$TMA_DIR" ]]; then
    log "Удаляю ${TMA_DIR}..."
    rm -rf "$TMA_DIR"
fi

# ── Убираем TMA_URL из server.env ─────────────────────────────────────────────
if [[ -f "${AWG_DIR}/server.env" ]] && grep -q "^TMA_URL=" "${AWG_DIR}/server.env"; then
    log "Убираю TMA_URL из server.env..."
    sed -i "/^TMA_URL=/d" "${AWG_DIR}/server.env"
fi

# ── Перезапускаем бота чтобы убрать кнопку TMA ───────────────────────────────
if systemctl is-active --quiet awg-bot 2>/dev/null; then
    systemctl restart awg-bot && log "awg-bot перезапущен" || true
fi

# ── Sentinel ──────────────────────────────────────────────────────────────────
rm -f "/root/modules/tma/.installed"

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}${BOLD}TMA удалена.${NC}"
echo ""
info "Чтобы переустановить: bash /root/setup.sh --modules"
echo ""
