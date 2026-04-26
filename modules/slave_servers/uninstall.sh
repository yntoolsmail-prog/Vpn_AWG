#!/bin/bash
# modules/slave_servers/uninstall.sh — удаление модуля управления slave-серверами
# Вызывается из setup.sh при команде 'd<N>' в меню модулей.
#
# Что удаляется:   Python-файлы модуля, sentinel .installed
# Что остаётся:    servers.json (данные о серверах — сохраняются для восстановления)
#                  paramiko (общий пакет, мог использоваться до установки модуля)

set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && { echo "Запускать от root"; exit 1; }

MOD_DIR="/root/modules/slave_servers"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Slave Servers — Удаление модуля       ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# Удаляем Python-файлы модуля (не трогаем __init__.py — его удалит _delete_module)
if [[ -f "${MOD_DIR}/slave_servers.py" ]]; then
    log "Удаляю ${MOD_DIR}/slave_servers.py..."
    rm -f "${MOD_DIR}/slave_servers.py"
fi

# Снимаем sentinel
rm -f "${MOD_DIR}/.installed"

echo ""
echo -e "  ${GREEN}${BOLD}Модуль slave_servers удалён.${NC}"
echo ""
info "servers.json сохранён — данные о серверах не удалены."
info "paramiko не удалён (общий пакет)."
info "Перезапустите бота: systemctl restart awg-bot"
echo ""
