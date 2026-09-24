#!/bin/bash
# Вспомогательные функции вывода — подключается через: source "$(dirname "$0")/lib/utils.sh"
# Требует предварительного подключения lib/colors.sh
log()  { echo -e "${GREEN}[+]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

# pip в системный Python. Ubuntu 23.04+ (PEP 668) требует --break-system-packages,
# старый pip (Ubuntu 22.04) такого флага не знает — тогда ставим без него.
# Зависимость новее пакета из apt pip обновить не может: «Cannot uninstall X,
# RECORD file not found» (сентябрь 2026: свежий anyio из python-telegram-bot
# требует typing_extensions ≥ 4.16, в Ubuntu 24.04 из apt — 4.10). Такой пакет
# ставим рядом свежей копией (--ignore-installed): она ложится в /usr/local и
# перекрывает apt-версию, сам apt-пакет не трогаем. Затем повторяем установку.
# Вывод pip — при ошибке (последние строки) или при PIP_VERBOSE=1.
pip_install_sys() {
    local out pkg n bsp="--break-system-packages"
    for n in 1 2 3 4 5 6; do
        if out=$(pip3 install $bsp "$@" 2>&1); then
            [[ "${PIP_VERBOSE:-0}" == "1" ]] && printf '%s\n' "$out"
            return 0
        fi
        if [[ -n "$bsp" ]] && grep -q "no such option: --break-system-packages" <<< "$out"; then
            bsp=""
            continue
        fi
        pkg=$(sed -n "s/.*Cannot uninstall '\{0,1\}\([A-Za-z0-9_.-]*\).*/\1/p" <<< "$out" | head -1)
        [[ -n "$pkg" ]] || break
        echo -e "${YELLOW}[!]${NC} ${pkg} стоит из apt, pip его не обновит — ставлю свежую копию рядом" >&2
        pip3 install $bsp --ignore-installed "$pkg" > /dev/null 2>&1 || break
    done
    printf '%s\n' "$out" | tail -n 15 >&2
    return 1
}
