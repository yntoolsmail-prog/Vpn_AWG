#!/bin/bash
# modules/socks5/uninstall.sh — полное удаление SOCKS5-инфраструктуры (redsocks2 + iptables)
# Вызывается из setup.sh при команде 'd<N>' в меню модулей.
# dnscrypt-proxy НЕ удаляется — он мог использоваться до установки модуля.
# Конфиг SOCKS5 (/etc/awg-socks5/) удаляется полностью.

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

REDSOCKS2_BIN="/usr/local/bin/redsocks2"
REDSOCKS2_CONF_DIR="/etc/redsocks2"
STATE_FILE="/etc/awg-socks5/bot_state.json"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   SOCKS5 (redsocks2) — Удаление         ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Читаем активный клиент до удаления state.json ─────────────────────────────
_active_ip=""
if [[ -f "$STATE_FILE" ]]; then
    _active_ip=$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    print(d.get('active_client_ip', ''))
except:
    pass
" 2>/dev/null || true)
fi

# ── Снятие iptables-правил для активного клиента ─────────────────────────────
if [[ -n "$_active_ip" ]]; then
    log "Снимаю iptables правила для клиента ${_active_ip}..."
    _chain="SOCKS5_$(echo "$_active_ip" | tr '.' '_')"

    # DNS редиректы (очищаем оба возможных порта для идемпотентности)
    for _dns_port in 5399 5300; do
        iptables -t nat -D PREROUTING -s "$_active_ip" -p udp --dport 53 \
            -j DNAT --to-destination "127.0.0.1:${_dns_port}" 2>/dev/null || true
        iptables -t nat -D PREROUTING -s "$_active_ip" -p tcp --dport 53 \
            -j DNAT --to-destination "127.0.0.1:${_dns_port}" 2>/dev/null || true
    done

    # TCP REDIRECT через цепочку
    iptables -t nat -D PREROUTING -s "$_active_ip" -p tcp -j "$_chain" 2>/dev/null || true

    # Флашим и удаляем цепочку
    iptables -t nat -F "$_chain" 2>/dev/null || true
    iptables -t nat -X "$_chain" 2>/dev/null || true

    # FORWARD: блокировка UDP (правило ставится с REJECT, не DROP)
    iptables -t filter -D FORWARD -s "$_active_ip" -p udp ! --dport 53 -j REJECT 2>/dev/null || true

    # Сохраняем
    iptables-save > /etc/iptables/rules.v4 2>/dev/null || true
    log "iptables правила сняты."
else
    info "Активного клиента нет — iptables не трогаем."
fi

# ── Остановка и удаление redsocks2 ───────────────────────────────────────────
if systemctl is-active --quiet redsocks2 2>/dev/null; then
    log "Останавливаю redsocks2..."
    systemctl stop redsocks2
fi
if systemctl is-enabled --quiet redsocks2 2>/dev/null; then
    systemctl disable redsocks2
fi
if [[ -f /etc/systemd/system/redsocks2.service ]]; then
    log "Удаляю /etc/systemd/system/redsocks2.service..."
    rm -f /etc/systemd/system/redsocks2.service
    systemctl daemon-reload
fi

if [[ -f "$REDSOCKS2_BIN" ]]; then
    log "Удаляю ${REDSOCKS2_BIN}..."
    rm -f "$REDSOCKS2_BIN"
fi
if [[ -d "$REDSOCKS2_CONF_DIR" ]]; then
    log "Удаляю ${REDSOCKS2_CONF_DIR}..."
    rm -rf "$REDSOCKS2_CONF_DIR"
fi

# ── Состояние бота ───────────────────────────────────────────────────────────
if [[ -d "/etc/awg-socks5" ]]; then
    log "Удаляю /etc/awg-socks5/..."
    rm -rf "/etc/awg-socks5"
fi

# ── Sentinel ──────────────────────────────────────────────────────────────────
rm -f "/root/modules/socks5/.installed"

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}${BOLD}SOCKS5 инфраструктура удалена.${NC}"
echo ""
info "dnscrypt-proxy оставлен (используется независимо от модуля)."
info "sysctl ip_forward и route_localnet не сброшены (AWG их тоже использует)."
echo ""
