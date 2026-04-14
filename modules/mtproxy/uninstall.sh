#!/bin/bash
# modules/mtproxy/uninstall.sh — полное удаление MTProxy
# Вызывается из setup.sh при команде 'd<N>' в меню модулей.
# Удаляет только системные компоненты — конфиг /etc/proxy-bot/proxy_bot.env
# сохраняется, чтобы при повторной установке не вводить данные заново.

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

MTP_DIR="/opt/mtproxy"
CONF_FILE="/etc/proxy-bot/proxy_bot.env"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      MTProxy — Удаление                 ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Остановка и отключение сервиса ───────────────────────────────────────────
if systemctl is-active --quiet mtproxy 2>/dev/null; then
    log "Останавливаю mtproxy..."
    systemctl stop mtproxy
fi
if systemctl is-enabled --quiet mtproxy 2>/dev/null; then
    systemctl disable mtproxy
fi

# ── Снятие правила ufw ────────────────────────────────────────────────────────
_port=$(grep "^MTP_PORT=" "$CONF_FILE" 2>/dev/null | cut -d= -f2 || true)
if [[ -n "$_port" ]]; then
    log "Снимаю правило ufw для порта ${_port}/tcp..."
    command -v ufw &>/dev/null && ufw delete allow "${_port}/tcp" 2>/dev/null || true
fi

# ── Удаление systemd-юнита ────────────────────────────────────────────────────
if [[ -f /etc/systemd/system/mtproxy.service ]]; then
    log "Удаляю /etc/systemd/system/mtproxy.service..."
    rm -f /etc/systemd/system/mtproxy.service
    systemctl daemon-reload
fi

# ── Удаление бинарников ───────────────────────────────────────────────────────
if [[ -d "$MTP_DIR" ]]; then
    log "Удаляю ${MTP_DIR}..."
    rm -rf "$MTP_DIR"
fi

# ── Sentinel ──────────────────────────────────────────────────────────────────
rm -f "/root/modules/mtproxy/.installed"

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}${BOLD}MTProxy удалён.${NC}"
echo ""
info "Конфиг ${CONF_FILE} сохранён (порт, секрет)."
info "При повторной установке данные будут подставлены автоматически."
echo ""
