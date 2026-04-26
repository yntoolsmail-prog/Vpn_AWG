#!/bin/bash
# modules/slave_servers/install.sh — установка модуля управления slave-серверами
# Вызывается из setup.sh --modules при первой активации модуля slave_servers.
# Системные зависимости (paramiko) уже установлены основным setup.sh.
# Этот скрипт только загружает slave_servers.py из репозитория.

set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && { echo "Запускать от root"; exit 1; }

AWG_DIR="/etc/amnezia/amneziawg"
REPO_ORG="yntoolsmail-prog"
REPO_NAME="Vpn_AWG"
REPO_BRANCH=$(grep "^REPO_BRANCH=" "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2)
[[ -z "$REPO_BRANCH" ]] && REPO_BRANCH="main"

REPO_RAW="https://raw.githubusercontent.com/${REPO_ORG}/${REPO_NAME}/${REPO_BRANCH}"
MOD_DIR="/root/modules/slave_servers"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Slave Servers — Установка модуля      ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

mkdir -p "$MOD_DIR"

log "Загрузка slave_servers.py (ветка: ${REPO_BRANCH})..."
if curl -fsSL "${REPO_RAW}/modules/slave_servers/slave_servers.py" \
        -o "${MOD_DIR}/slave_servers.py.new" 2>/dev/null; then
    mv "${MOD_DIR}/slave_servers.py.new" "${MOD_DIR}/slave_servers.py"
    log "slave_servers.py загружен ✓"
else
    rm -f "${MOD_DIR}/slave_servers.py.new"
    warn "Не удалось загрузить slave_servers.py — модуль будет недоступен до следующего --update."
fi

# Проверяем наличие paramiko
if python3 -c "import paramiko" 2>/dev/null; then
    info "paramiko доступен ✓"
else
    warn "paramiko не найден — пробую установить..."
    pip3 install "paramiko>=3.0" --break-system-packages > /dev/null 2>&1 \
        || pip3 install "paramiko>=3.0" > /dev/null 2>&1 \
        || warn "Не удалось установить paramiko — добавление slave-серверов потребует ручного ввода ключа."
fi

touch "${MOD_DIR}/.installed"

echo ""
echo -e "  ${GREEN}${BOLD}Модуль slave_servers установлен!${NC}"
echo ""
echo -e "  ${CYAN}Что добавлено:${NC}"
echo -e "  • Кнопка «➕ Добавить сервер» в меню 🖥 Серверы"
echo -e "  • SSH-диалог добавления secondary VPS (6 шагов)"
echo -e "  • Автоматическое клонирование AWG-конфига на slave"
echo ""
echo -e "  ${CYAN}Следующий шаг:${NC}"
echo -e "  Перезапустите бота: systemctl restart awg-bot"
echo ""
