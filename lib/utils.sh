#!/bin/bash
# Вспомогательные функции вывода — подключается через: source "$(dirname "$0")/lib/utils.sh"
# Требует предварительного подключения lib/colors.sh
log()  { echo -e "${GREEN}[+]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }
