#!/bin/bash
# Version: 2.0
# =============================================================================
# AmneziaWG — управление сервером
# Запускать: bash vpn.sh
# =============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

BOT_ENV="/etc/amnezia/amneziawg/bot.env"
ENV_FILE="/etc/amnezia/amneziawg/server.env"
CLIENTS_DIR="/etc/amnezia/amneziawg/clients"
BACKUP_DIR="/etc/amnezia/amneziawg/backups"
DIAG_DIR="/etc/amnezia/amneziawg/diagnostics"
USERS_FILE="/etc/amnezia/amneziawg/users.json"
BW_PEAK_FILE="/etc/amnezia/amneziawg/bw_peak.json"
BW_LOG_FILE="/var/log/awg-bw.log"

[[ $EUID -ne 0 ]] && echo -e "${RED}Запускать от root: sudo bash vpn.sh${NC}" && exit 1
[[ ! -f "$ENV_FILE" ]] && echo -e "${RED}Сначала запустите setup.sh${NC}" && exit 1

source "$ENV_FILE"
[[ -f "$BOT_ENV" ]] && source "$BOT_ENV"

# Проверка: SERVER_IP не должен быть IPv6
if [[ "$SERVER_IP" == *":"* ]]; then
    echo -e "${YELLOW}[!] В server.env записан IPv6 адрес: ${SERVER_IP}${NC}"
    read -p "  Введите правильный IPv4 сервера (или Enter чтобы продолжить): " FIX_IP
    if [[ -n "$FIX_IP" ]]; then
        SERVER_IP="$FIX_IP"
        sed -i "s/^SERVER_IP=.*/SERVER_IP=${FIX_IP}/" "$ENV_FILE"
        echo -e "${GREEN}[+] SERVER_IP исправлен на ${FIX_IP}${NC}"
        sleep 1
    fi
fi

VPN_IFACE="${VPN_IFACE:-awg0}"
AWG_CONF="/etc/amnezia/amneziawg/${VPN_IFACE}.conf"
PRIMARY_DNS="${PRIMARY_DNS:-1.1.1.1}"
SECONDARY_DNS="${SECONDARY_DNS:-1.0.0.1}"
SERVER_ENDPOINT="${SERVER_ENDPOINT:-$SERVER_IP}"

mkdir -p "$CLIENTS_DIR" "$BACKUP_DIR" "$DIAG_DIR"

# =============================================================================
# УТИЛИТЫ
# =============================================================================

show_header() {
    clear
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║        AmneziaWG — Управление v2.0       ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

press_enter() {
    echo ""
    read -p "  Нажмите Enter..."
}

next_ip() {
    local i=2
    while grep -q "AllowedIPs = ${VPN_SUBNET}.${i}/32" "$AWG_CONF" 2>/dev/null; do
        ((i++))
    done
    echo "$i"
}

fmt_handshake() {
    local ts="$1"
    [[ -z "$ts" || "$ts" == "0" ]] && echo "никогда" && return
    local diff=$(( $(date +%s) - ts ))
    if   (( diff < 60 ));    then echo "${diff}с назад 🟢"
    elif (( diff < 180 ));   then echo "$((diff/60))м назад 🟢"
    elif (( diff < 3600 ));  then echo "$((diff/60))м назад"
    elif (( diff < 86400 )); then echo "$((diff/3600))ч назад"
    else                          echo "$((diff/86400))д назад"
    fi
}

get_real_ip() {
    for url in "https://ifconfig.me" "https://api.ipify.org" "https://ifconfig.co"; do
        local ip
        ip=$(curl -4 -s --max-time 5 "$url" 2>/dev/null)
        if [[ -n "$ip" && "$ip" != *":"* && "$ip" == *.*.*.* ]]; then
            echo "$ip"; return
        fi
    done
    echo ""
}

# =============================================================================
# КЛИЕНТЫ
# =============================================================================

add_client() {
    show_header
    echo -e "${BOLD}  Добавление клиента${NC}"
    echo ""
    read -p "  Имя клиента (латиница, без дефисов): " NAME
    NAME=$(echo "$NAME" | tr -d '\r\xef\xbb\xbf' | sed 's/[[:space:]]//g' | tr -cd '[:alnum:]_')

    [[ -z "$NAME" ]] && echo -e "${RED}  Имя пустое.${NC}" && sleep 2 && return
    [[ -f "$CLIENTS_DIR/${NAME}.conf" ]] && echo -e "${RED}  Клиент '$NAME' уже существует.${NC}" && sleep 2 && return

    CLIENT_PRIVATE=$(awg genkey)
    CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | awg pubkey)
    CLIENT_PSK=$(awg genpsk)
    CLIENT_IP="${VPN_SUBNET}.$(next_ip)"

    printf "\n# Client: %s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n" \
        "$NAME" "$CLIENT_PUBLIC" "$CLIENT_PSK" "$CLIENT_IP" >> "$AWG_CONF"
    echo "$CLIENT_PSK" | awg set "$VPN_IFACE" peer "$CLIENT_PUBLIC" \
        preshared-key /dev/stdin allowed-ips "${CLIENT_IP}/32"

    {
        printf "[Interface]\nPrivateKey = %s\nAddress = %s/32\nDNS = %s, %s\n" \
            "$CLIENT_PRIVATE" "$CLIENT_IP" "$PRIMARY_DNS" "$SECONDARY_DNS"
        printf "Jc = %s\nJmin = %s\nJmax = %s\nS1 = %s\nS2 = %s\n" \
            "$JC" "$JMIN" "$JMAX" "$S1" "$S2"
        printf "H1 = %s\nH2 = %s\nH3 = %s\nH4 = %s\n" "$H1" "$H2" "$H3" "$H4"
        printf "\n[Peer]\nPublicKey = %s\nPresharedKey = %s\n" "$SERVER_PUBLIC" "$CLIENT_PSK"
        printf "Endpoint = %s:%s\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n" \
            "$SERVER_ENDPOINT" "$SERVER_PORT"
    } > "$CLIENTS_DIR/${NAME}.conf"

    # Сохраняем pub файл
    echo "$CLIENT_PUBLIC" > "$CLIENTS_DIR/${NAME}.pub"

    echo ""
    echo -e "${GREEN}  ✓ Клиент '${NAME}' добавлен — IP: ${CLIENT_IP}${NC}"
    echo -e "  Конфиг: ${CLIENTS_DIR}/${NAME}.conf"
    echo ""
    echo -e "  Скачать (выполнить локально):"
    echo -e "${YELLOW}  scp root@${SERVER_IP}:${CLIENTS_DIR}/${NAME}.conf ./${NAME}.conf${NC}"
    echo ""
    read -p "  Показать текст конфига? (y/N): " SHOW
    if [[ "$SHOW" == "y" || "$SHOW" == "Y" ]]; then
        echo ""
        echo -e "${CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        cat "$CLIENTS_DIR/${NAME}.conf"
        echo -e "${CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    fi
    press_enter
}

list_clients() {
    show_header
    echo -e "${BOLD}  Список клиентов${NC}"
    echo ""

    local CONFS=( "$CLIENTS_DIR"/*.conf )
    if [[ ! -f "${CONFS[0]}" ]]; then
        echo -e "  ${YELLOW}Клиентов нет.${NC}"
        press_enter; return
    fi

    local AWG_DUMP
    AWG_DUMP=$(awg show "$VPN_IFACE" dump 2>/dev/null)

    printf "  %-4s %-25s %-16s %-20s %s\n" "N" "ИМЯ" "IP" "ХЕНДШЕЙК" "ТРАФИК"
    echo "  ────────────────────────────────────────────────────────────────────"

    local i=1
    local NAMES=()
    for CONF in "${CONFS[@]}"; do
        local CNAME CLIENT_IP CLIENT_PRIV CLIENT_PUB HS TX RX
        CNAME=$(basename "$CONF" .conf)
        CLIENT_IP=$(awk '/^Address/{print $3}' "$CONF" | cut -d'/' -f1)
        CLIENT_PUB=""
        if [[ -f "$CLIENTS_DIR/${CNAME}.pub" ]]; then
            CLIENT_PUB=$(cat "$CLIENTS_DIR/${CNAME}.pub")
        else
            CLIENT_PRIV=$(awk '/^PrivateKey/{print $3; exit}' "$CONF")
            CLIENT_PUB=$(echo "$CLIENT_PRIV" | awg pubkey 2>/dev/null)
        fi

        if [[ -n "$CLIENT_PUB" ]]; then
            local PEER_LINE
            PEER_LINE=$(echo "$AWG_DUMP" | grep "^${CLIENT_PUB}")
            HS=$(echo "$PEER_LINE" | awk '{print $5}')
            RX=$(echo "$PEER_LINE" | awk '{print $6}')
            TX=$(echo "$PEER_LINE" | awk '{print $7}')
        fi

        local HS_FMT TX_FMT
        HS_FMT=$(fmt_handshake "$HS")
        [[ -n "$RX" ]] && TX_FMT="↓$(numfmt --to=iec $RX 2>/dev/null || echo $RX) ↑$(numfmt --to=iec $TX 2>/dev/null || echo $TX)" || TX_FMT="—"

        printf "  %-4s %-25s %-16s %-20s %s\n" "$i)" "$CNAME" "$CLIENT_IP" "$HS_FMT" "$TX_FMT"
        NAMES+=("$CNAME")
        ((i++))
    done

    echo ""
    read -p "  Открыть клиента (номер) или Enter для выхода: " NUM
    [[ -z "$NUM" ]] && return
    [[ "$NUM" -lt 1 || "$NUM" -gt "${#NAMES[@]}" ]] 2>/dev/null && return

    local SEL="${NAMES[$((NUM-1))]}"
    while true; do
        show_header
        echo -e "${BOLD}  Клиент: ${CYAN}${SEL}${NC}"
        echo ""
        echo "  1) Показать конфиг"
        echo "  2) Команда для скачивания"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " ACTION
        case $ACTION in
            1) echo ""; cat "$CLIENTS_DIR/${SEL}.conf"; press_enter ;;
            2)
                echo ""
                echo -e "  ${YELLOW}scp root@${SERVER_IP}:${CLIENTS_DIR}/${SEL}.conf ./${SEL}.conf${NC}"
                press_enter ;;
            0) return ;;
        esac
    done
}

delete_client() {
    show_header
    echo -e "${BOLD}  Удаление клиента${NC}"
    echo ""

    local CONFS=( "$CLIENTS_DIR"/*.conf )
    if [[ ! -f "${CONFS[0]}" ]]; then
        echo -e "  ${YELLOW}Клиентов нет.${NC}"; sleep 2; return
    fi

    local i=1
    local NAMES=()
    for CONF in "${CONFS[@]}"; do
        local CNAME IP
        CNAME=$(basename "$CONF" .conf)
        IP=$(awk '/^Address/{print $3}' "$CONF")
        echo -e "  $i) ${CNAME} (${IP})"
        NAMES+=("$CNAME")
        ((i++))
    done

    echo ""
    read -p "  Номер для удаления (0 — отмена): " NUM
    [[ "$NUM" == "0" || -z "$NUM" ]] && return
    [[ "$NUM" -lt 1 || "$NUM" -gt "${#NAMES[@]}" ]] && echo -e "${RED}  Неверный номер${NC}" && sleep 2 && return

    local NAME="${NAMES[$((NUM-1))]}"
    local CONF="$CLIENTS_DIR/${NAME}.conf"

    local CLIENT_PUB=""
    if [[ -f "$CLIENTS_DIR/${NAME}.pub" ]]; then
        CLIENT_PUB=$(cat "$CLIENTS_DIR/${NAME}.pub")
    else
        local CLIENT_PRIV
        CLIENT_PRIV=$(awk '/^PrivateKey/{print $3; exit}' "$CONF")
        CLIENT_PUB=$(echo "$CLIENT_PRIV" | awg pubkey 2>/dev/null)
    fi

    echo ""
    read -p "  Удалить '$NAME'? (y/N): " CONFIRM
    [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]] && return

    [[ -n "$CLIENT_PUB" ]] && awg set "$VPN_IFACE" peer "$CLIENT_PUB" remove 2>/dev/null

    python3 - "$AWG_CONF" "$NAME" << 'PYEOF'
import sys
conf_path, name = sys.argv[1], sys.argv[2]
with open(conf_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
lines = content.split('\n')
new_lines, skip = [], False
for line in lines:
    if line.strip() == f'# Client: {name}':
        skip = True
    elif skip and line.strip().startswith('[') and line.strip() != '[Peer]':
        skip = False
        new_lines.append(line)
    elif not skip:
        new_lines.append(line)
with open(conf_path, 'w') as f:
    f.write('\n'.join(new_lines))
PYEOF

    for ext in .conf .pub .vpn .vpnlink; do
        rm -f "$CLIENTS_DIR/${NAME}${ext}"
    done

    echo -e "${GREEN}  ✓ Клиент '$NAME' удалён${NC}"
    sleep 2
}

# =============================================================================
# СТАТУС
# =============================================================================

show_status() {
    show_header
    echo -e "${BOLD}  Статус сервера${NC}"
    echo ""

    local AWG_STATUS BOT_STATUS
    systemctl is-active --quiet "awg-quick@${VPN_IFACE}" \
        && AWG_STATUS="${GREEN}● работает${NC}" \
        || AWG_STATUS="${RED}● остановлен${NC}"
    systemctl is-active --quiet awg-bot \
        && BOT_STATUS="${GREEN}● работает${NC}" \
        || BOT_STATUS="${RED}● остановлен${NC}"

    echo -e "  AWG интерфейс:  ${AWG_STATUS}"
    echo -e "  Telegram бот:   ${BOT_STATUS}"
    echo -e "  Uptime:         ${CYAN}$(uptime -p 2>/dev/null || uptime)${NC}"
    echo -e "  Load average:   ${CYAN}$(cut -d' ' -f1-3 /proc/loadavg)${NC}"
    echo -e "  RAM:            ${CYAN}$(free -m | awk 'NR==2{printf "%s/%s MB (%.0f%%)",$3,$2,$3*100/$2}')${NC}"
    echo -e "  Диск (/):       ${CYAN}$(df -h / | awk 'NR==2{printf "%s/%s (%s)",$3,$2,$5}')${NC}"
    echo -e "  IP в конфиге:   ${CYAN}${SERVER_IP}:${SERVER_PORT}${NC}"
    echo -e "  Endpoint:       ${CYAN}${SERVER_ENDPOINT}${NC}"
    echo -e "  Клиентов:       ${CYAN}$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)${NC}"
    echo ""
    echo -e "${BOLD}  awg show:${NC}"
    awg show "$VPN_IFACE" 2>/dev/null
    press_enter
}

# =============================================================================
# СЕРВИСЫ
# =============================================================================

manage_services() {
    while true; do
        show_header
        echo -e "${BOLD}  Управление сервисами${NC}"
        echo ""

        local AWG_ST BOT_ST
        systemctl is-active --quiet "awg-quick@${VPN_IFACE}" \
            && AWG_ST="${GREEN}работает${NC}" || AWG_ST="${RED}остановлен${NC}"
        systemctl is-active --quiet awg-bot \
            && BOT_ST="${GREEN}работает${NC}" || BOT_ST="${RED}остановлен${NC}"

        echo -e "  AWG:  ${AWG_ST}   |   Бот: ${BOT_ST}"
        echo ""
        echo "  1) Перезапустить AWG"
        echo "  2) Остановить AWG"
        echo "  3) Запустить AWG"
        echo "  4) Перезапустить бота"
        echo "  5) Остановить бота"
        echo "  6) Запустить бота"
        echo "  7) Логи бота (последние 50 строк)"
        echo "  8) Логи бота в реальном времени (Ctrl+C для выхода)"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " CHOICE
        case $CHOICE in
            1) systemctl restart "awg-quick@${VPN_IFACE}" && echo -e "${GREEN}  ✓ AWG перезапущен${NC}" || echo -e "${RED}  ✗ Ошибка${NC}"; sleep 2 ;;
            2) systemctl stop "awg-quick@${VPN_IFACE}"    && echo -e "${YELLOW}  AWG остановлен${NC}"; sleep 2 ;;
            3) systemctl start "awg-quick@${VPN_IFACE}"   && echo -e "${GREEN}  ✓ AWG запущен${NC}" || echo -e "${RED}  ✗ Ошибка${NC}"; sleep 2 ;;
            4) systemctl restart awg-bot && echo -e "${GREEN}  ✓ Бот перезапущен${NC}" || echo -e "${RED}  ✗ Ошибка${NC}"; sleep 2 ;;
            5) systemctl stop awg-bot    && echo -e "${YELLOW}  Бот остановлен${NC}"; sleep 2 ;;
            6) systemctl start awg-bot   && echo -e "${GREEN}  ✓ Бот запущен${NC}" || echo -e "${RED}  ✗ Ошибка${NC}"; sleep 2 ;;
            7) echo ""; journalctl -u awg-bot -n 50 --no-pager; press_enter ;;
            8) journalctl -u awg-bot -f ;;
            0) return ;;
        esac
    done
}

# =============================================================================
# НАСТРОЙКИ
# =============================================================================

manage_settings() {
    while true; do
        show_header
        echo -e "${BOLD}  Настройки сервера${NC}"
        echo ""
        echo -e "  Токен бота:  ${CYAN}${BOT_TOKEN:0:20}...${NC}"
        echo -e "  Admin ID:    ${CYAN}${ADMIN_ID}${NC}"
        echo -e "  IP сервера:  ${CYAN}${SERVER_IP}${NC}"
        echo -e "  Endpoint:    ${CYAN}${SERVER_ENDPOINT}${NC}"
        echo -e "  Резерв:      ${CYAN}${SERVER_ENDPOINT_BACKUP:-не задан}${NC}"
        echo -e "  Порт AWG:    ${CYAN}${SERVER_PORT}${NC}"
        echo -e "  DNS:         ${CYAN}${PRIMARY_DNS}, ${SECONDARY_DNS}${NC}"
        echo ""
        echo "  1) Сменить токен бота"
        echo "  2) Сменить Admin ID"
        echo "  3) Обновить IP сервера (авто)"
        echo "  4) Изменить основной endpoint"
        echo "  5) Изменить резервный endpoint"
        echo "  6) Сменить DNS"
        echo "  7) Показать server.env полностью"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " CHOICE
        case $CHOICE in
            1)
                read -p "  Новый токен бота: " NEW_TOKEN
                [[ -z "$NEW_TOKEN" ]] && continue
                sed -i "s/^BOT_TOKEN=.*/BOT_TOKEN=${NEW_TOKEN}/" "$BOT_ENV"
                BOT_TOKEN="$NEW_TOKEN"
                echo -e "${GREEN}  ✓ Токен обновлён. Перезапускаю бота...${NC}"
                systemctl restart awg-bot; sleep 2
                ;;
            2)
                read -p "  Новый Admin ID (число): " NEW_ID
                [[ ! "$NEW_ID" =~ ^[0-9]+$ ]] && echo -e "${RED}  Неверный формат${NC}" && sleep 2 && continue
                sed -i "s/^ADMIN_ID=.*/ADMIN_ID=${NEW_ID}/" "$BOT_ENV"
                ADMIN_ID="$NEW_ID"
                echo -e "${GREEN}  ✓ Admin ID обновлён. Перезапускаю бота...${NC}"
                systemctl restart awg-bot; sleep 2
                ;;
            3)
                echo -e "  ${CYAN}Определяю внешний IP...${NC}"
                REAL_IP=$(get_real_ip)
                if [[ -z "$REAL_IP" ]]; then
                    echo -e "${RED}  Не удалось определить IP${NC}"; sleep 2; continue
                fi
                if [[ "$REAL_IP" == "$SERVER_IP" ]]; then
                    echo -e "${GREEN}  ✓ IP актуален: ${SERVER_IP}${NC}"; sleep 2; continue
                fi
                echo -e "  В конфиге: ${RED}${SERVER_IP}${NC}"
                echo -e "  Реальный:  ${GREEN}${REAL_IP}${NC}"
                local EP_NOTE=""
                [[ "$SERVER_ENDPOINT" == "$SERVER_IP" ]] && EP_NOTE=" (и SERVER_ENDPOINT тоже)"
                read -p "  Обновить${EP_NOTE}? (y/N): " CONFIRM
                if [[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]]; then
                    sed -i "s/^SERVER_IP=.*/SERVER_IP=${REAL_IP}/" "$ENV_FILE"
                    [[ "$SERVER_ENDPOINT" == "$SERVER_IP" ]] && \
                        sed -i "s/^SERVER_ENDPOINT=.*/SERVER_ENDPOINT=${REAL_IP}/" "$ENV_FILE"
                    SERVER_IP="$REAL_IP"
                    SERVER_ENDPOINT="${SERVER_ENDPOINT//$SERVER_IP/$REAL_IP}"
                    echo -e "${GREEN}  ✓ Обновлено. Перезапускаю бота...${NC}"
                    systemctl restart awg-bot; sleep 2
                fi
                ;;
            4)
                read -p "  Новый основной endpoint (домен или IP): " NEW_EP
                [[ -z "$NEW_EP" ]] && continue
                sed -i "s/^SERVER_ENDPOINT=.*/SERVER_ENDPOINT=${NEW_EP}/" "$ENV_FILE"
                SERVER_ENDPOINT="$NEW_EP"
                echo -e "${GREEN}  ✓ Endpoint обновлён. Перезапускаю бота...${NC}"
                systemctl restart awg-bot; sleep 2
                ;;
            5)
                read -p "  Резервный endpoint (пусто — убрать): " NEW_EP
                sed -i "s/^SERVER_ENDPOINT_BACKUP=.*/SERVER_ENDPOINT_BACKUP=${NEW_EP}/" "$ENV_FILE"
                echo -e "${GREEN}  ✓ Резервный endpoint обновлён. Перезапускаю бота...${NC}"
                systemctl restart awg-bot; sleep 2
                ;;
            6)
                read -p "  Основной DNS [${PRIMARY_DNS}]: " D1
                read -p "  Резервный DNS [${SECONDARY_DNS}]: " D2
                [[ -n "$D1" ]] && sed -i "s/^PRIMARY_DNS=.*/PRIMARY_DNS=${D1}/" "$ENV_FILE" && PRIMARY_DNS="$D1"
                [[ -n "$D2" ]] && sed -i "s/^SECONDARY_DNS=.*/SECONDARY_DNS=${D2}/" "$ENV_FILE" && SECONDARY_DNS="$D2"
                echo -e "${GREEN}  ✓ DNS обновлён. Перезапускаю бота...${NC}"
                systemctl restart awg-bot; sleep 2
                ;;
            7)
                echo ""
                echo -e "${CYAN}  ━━━━━━━━━━ server.env ━━━━━━━━━━${NC}"
                cat "$ENV_FILE"
                echo -e "${CYAN}  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                press_enter
                ;;
            0) return ;;
        esac
    done
}

# =============================================================================
# БЭКАПЫ
# =============================================================================

manage_backups() {
    while true; do
        show_header
        echo -e "${BOLD}  Бэкапы${NC}"
        echo ""
        echo "  1) Создать бэкап"
        echo "  2) Список бэкапов"
        echo "  3) Удалить старые бэкапы"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " CHOICE
        case $CHOICE in
            1)
                local TS FILE
                TS=$(date +"%Y%m%d_%H%M%S")
                FILE="${BACKUP_DIR}/awg_backup_${TS}.tar.gz"
                echo -e "  ${CYAN}Создаю бэкап...${NC}"
                if tar -czf "$FILE" \
                    -C /etc/amnezia/amneziawg \
                    "${VPN_IFACE}.conf" server.env clients/ \
                    $([ -f "$USERS_FILE" ] && echo "users.json") \
                    2>/dev/null; then
                    local SIZE
                    SIZE=$(du -sh "$FILE" | cut -f1)
                    echo -e "${GREEN}  ✓ Бэкап создан: ${FILE} (${SIZE})${NC}"
                    echo -e "  Скачать: ${YELLOW}scp root@${SERVER_IP}:${FILE} ./$(basename $FILE)${NC}"
                else
                    echo -e "${RED}  ✗ Ошибка создания бэкапа${NC}"
                fi
                press_enter
                ;;
            2)
                echo ""
                local FILES=( "$BACKUP_DIR"/*.tar.gz )
                if [[ ! -f "${FILES[0]}" ]]; then
                    echo -e "  ${YELLOW}Бэкапов нет.${NC}"
                else
                    printf "  %-50s %s\n" "Файл" "Размер"
                    echo "  ──────────────────────────────────────────────────────────"
                    for F in "${FILES[@]}"; do
                        printf "  %-50s %s\n" "$(basename $F)" "$(du -sh $F | cut -f1)"
                    done
                fi
                press_enter
                ;;
            3)
                local FILES=( "$BACKUP_DIR"/*.tar.gz )
                if [[ ! -f "${FILES[0]}" ]]; then
                    echo -e "  ${YELLOW}Бэкапов нет.${NC}"; sleep 2; continue
                fi
                echo -e "  Текущие бэкапы:"
                local i=1
                local BNAMES=()
                for F in "${FILES[@]}"; do
                    echo -e "  $i) $(basename $F) — $(du -sh $F | cut -f1)"
                    BNAMES+=("$F")
                    ((i++))
                done
                echo ""
                read -p "  Удалить бэкапы старше N дней (0 — отмена): " DAYS
                [[ "$DAYS" == "0" || -z "$DAYS" ]] && continue
                local COUNT
                COUNT=$(find "$BACKUP_DIR" -name "*.tar.gz" -mtime "+${DAYS}" | wc -l)
                read -p "  Будет удалено ${COUNT} файлов. Подтвердить? (y/N): " CONFIRM
                if [[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]]; then
                    find "$BACKUP_DIR" -name "*.tar.gz" -mtime "+${DAYS}" -delete
                    echo -e "${GREEN}  ✓ Удалено: ${COUNT} файлов${NC}"
                fi
                sleep 2
                ;;
            0) return ;;
        esac
    done
}

# =============================================================================
# ОБНОВЛЕНИЕ
# =============================================================================

manage_updates() {
    while true; do
        show_header
        echo -e "${BOLD}  Обновление${NC}"
        echo ""
        echo "  1) Обновить bot.py и vpn.sh из репозитория"
        echo "  2) Обновить систему (apt upgrade)"
        echo "  3) Обновить python-telegram-bot"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " CHOICE
        case $CHOICE in
            1)
                echo -e "  ${CYAN}Скачиваю актуальные файлы...${NC}"
                local REPO="https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main"
                if curl -s "${REPO}/bot.py" -o /root/bot.py && \
                   curl -s "${REPO}/vpn.sh" -o /root/vpn.sh; then
                    chmod +x /root/vpn.sh
                    echo -e "${GREEN}  ✓ bot.py и vpn.sh обновлены${NC}"
                    echo -e "  ${CYAN}Перезапускаю бота...${NC}"
                    systemctl restart awg-bot
                    echo -e "${GREEN}  ✓ Бот перезапущен${NC}"
                else
                    echo -e "${RED}  ✗ Ошибка скачивания. Проверьте интернет.${NC}"
                fi
                press_enter
                ;;
            2)
                echo -e "  ${CYAN}Запускаю apt update && apt upgrade...${NC}"
                apt-get update && apt-get upgrade -y
                # Устанавливаем инструменты диагностики сети если нет
                apt-get install -y mtr-tiny traceroute &>/dev/null || true
                echo -e "${GREEN}  ✓ Система обновлена. Перезапускаю бота...${NC}"
                systemctl restart awg-bot
                press_enter
                ;;
            3)
                echo -e "  ${CYAN}Обновляю python-telegram-bot...${NC}"
                pip3 install -U "python-telegram-bot[job-queue]" --break-system-packages 2>/dev/null || \
                pip3 install -U "python-telegram-bot[job-queue]"
                echo -e "${GREEN}  ✓ Готово. Перезапускаю бота...${NC}"
                systemctl restart awg-bot
                press_enter
                ;;
            0) return ;;
        esac
    done
}

# =============================================================================
# ДИАГНОСТИКА — ПОЛНЫЙ ОТЧЁТ
# =============================================================================

run_diagnostics() {
    show_header
    echo -e "${BOLD}  Полная диагностика системы${NC}"
    echo ""
    echo -e "  ${CYAN}Собираю данные, подождите...${NC}"
    echo ""

    local TS FILE
    TS=$(date +"%Y%m%d_%H%M%S")
    FILE="${DIAG_DIR}/diag_${TS}.txt"

    {
        echo "════════════════════════════════════════════════════════════════"
        echo "  AmneziaWG — Диагностический отчёт"
        echo "  Дата: $(date '+%d.%m.%Y %H:%M:%S')"
        echo "════════════════════════════════════════════════════════════════"
        echo ""

        # ── СИСТЕМА ──────────────────────────────────────────────────────────
        echo "━━━ СИСТЕМА ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "OS:           $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '"')"
        echo "Ядро:         $(uname -r)"
        echo "Uptime:       $(uptime -p 2>/dev/null || uptime)"
        echo "Load average: $(cut -d' ' -f1-3 /proc/loadavg)"
        echo "RAM:          $(free -m | awk 'NR==2{printf "%s MB использовано / %s MB всего (%.0f%%)",$3,$2,$3*100/$2}')"
        echo "Диск (/):     $(df -h / | awk 'NR==2{printf "%s использовано / %s всего (%s)",$3,$2,$5}')"
        echo "CPU:          $(nproc) ядер / $(top -bn1 | grep '%Cpu' | awk '{print $2}')% использование"
        echo "Время сервера: $(date)"
        echo "Часовой пояс: $(cat /etc/timezone 2>/dev/null || timedatectl | grep 'Time zone' | awk '{print $3}')"
        echo ""

        # ── СЕТЬ ─────────────────────────────────────────────────────────────
        echo "━━━ СЕТЬ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        local REAL_IP
        REAL_IP=$(get_real_ip)
        echo "IP в конфиге (SERVER_IP):   ${SERVER_IP}"
        echo "Реальный внешний IP:        ${REAL_IP:-не удалось определить}"
        if [[ -n "$REAL_IP" && "$REAL_IP" != "$SERVER_IP" ]]; then
            echo "⚠️  РАСХОЖДЕНИЕ IP! Требуется обновление server.env"
        else
            echo "✅ IP актуален"
        fi
        echo "Основной endpoint:          ${SERVER_ENDPOINT}"
        echo "Резервный endpoint:         ${SERVER_ENDPOINT_BACKUP:-не задан}"
        echo "Порт AWG:                   ${SERVER_PORT}/UDP"
        echo ""
        echo "--- Порт слушается ---"
        ss -ulnp | grep ":${SERVER_PORT}" || echo "⚠️  Порт ${SERVER_PORT} не слушается!"
        echo ""
        echo "--- Проверка связности ---"
        echo -n "Ping 8.8.8.8:  "
        PING_RES=$(ping -c3 -W2 8.8.8.8 2>/dev/null | tail -1)
        echo "$PING_RES" | grep -q "rtt" && echo "✅ OK  ${PING_RES}" || echo "⚠️  НЕДОСТУПЕН"
        echo -n "Ping 1.1.1.1:  "
        PING_RES2=$(ping -c3 -W2 1.1.1.1 2>/dev/null | tail -1)
        echo "$PING_RES2" | grep -q "rtt" && echo "✅ OK  ${PING_RES2}" || echo "⚠️  НЕДОСТУПЕН"
        if [[ -n "$SERVER_ENDPOINT" && "$SERVER_ENDPOINT" != "$SERVER_IP" ]]; then
            echo -n "Резолв домена ${SERVER_ENDPOINT}: "
            local RESOLVED
            RESOLVED=$(getent hosts "$SERVER_ENDPOINT" 2>/dev/null | awk '{print $1}' | head -1)
            if [[ -n "$RESOLVED" ]]; then
                echo "✅ → ${RESOLVED}"
                [[ "$RESOLVED" != "$REAL_IP" ]] && echo "  ⚠️  Домен резолвится не в текущий IP сервера!"
            else
                echo "⚠️  НЕ РЕЗОЛВИТСЯ"
            fi
        fi
        echo ""
        echo "--- Traceroute до 8.8.8.8 (маршрут, макс 15 хопов) ---"
        if command -v traceroute &>/dev/null; then
            traceroute -n -m 15 -w 2 8.8.8.8 2>/dev/null || echo "  ошибка"
        elif command -v tracepath &>/dev/null; then
            tracepath -n -m 15 8.8.8.8 2>/dev/null || echo "  ошибка"
        else
            echo "  не установлен — apt install traceroute"
        fi
        echo ""
        echo "--- MTR до 8.8.8.8 (потери пакетов по хопам, 5 циклов) ---"
        if command -v mtr &>/dev/null; then
            mtr --report --report-cycles 5 --no-dns 8.8.8.8 2>/dev/null || echo "  ошибка mtr"
        else
            echo "  не установлен — apt install mtr-tiny"
        fi
        echo ""
        echo "--- Основной сетевой интерфейс ---"
        local HOST_IFACE
        HOST_IFACE=$(ip route get 8.8.8.8 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
        echo "Интерфейс: ${HOST_IFACE:-eth0}"
        ip addr show "${HOST_IFACE:-eth0}" 2>/dev/null | grep "inet "
        echo ""
        echo "--- Таблица маршрутов ---"
        ip route show
        echo ""
        echo "--- iptables (FORWARD и NAT для AWG) ---"
        iptables -L FORWARD --line-numbers -n 2>/dev/null | grep -E "awg|ACCEPT|DROP" | head -20 || echo "нет правил"
        echo ""
        iptables -t nat -L POSTROUTING --line-numbers -n 2>/dev/null | grep -E "awg|MASQUERADE" | head -10 || echo "нет правил NAT"
        echo ""

        # ── AWG ──────────────────────────────────────────────────────────────
        echo "━━━ AWG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "Интерфейс: ${VPN_IFACE}"
        echo "Статус сервиса: $(systemctl is-active awg-quick@${VPN_IFACE})"
        echo ""
        echo "--- awg show ---"
        awg show "$VPN_IFACE" 2>/dev/null || echo "⚠️  AWG не отвечает"
        echo ""

        local AWG_DUMP
        AWG_DUMP=$(awg show "$VPN_IFACE" dump 2>/dev/null)
        local PEER_COUNT_AWG
        PEER_COUNT_AWG=$(echo "$AWG_DUMP" | tail -n +2 | grep -c "." || echo 0)
        local FILE_COUNT
        FILE_COUNT=$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)

        echo "--- Сводка клиентов ---"
        echo "Пиров в AWG:          ${PEER_COUNT_AWG}"
        echo "Файлов .conf:         ${FILE_COUNT}"
        if [[ "$PEER_COUNT_AWG" != "$FILE_COUNT" ]]; then
            echo "⚠️  РАСХОЖДЕНИЕ! Возможны висячие пиры или файлы без пира."
        else
            echo "✅ Количество совпадает"
        fi
        echo ""
        echo "--- Детали клиентов (имя / IP / хендшейк / трафик) ---"
        printf "%-30s %-16s %-20s %s\n" "ИМЯ" "IP" "ХЕНДШЕЙК" "ТРАФИК"
        echo "────────────────────────────────────────────────────────────────────"
        local NOW
        NOW=$(date +%s)
        for CONF in "$CLIENTS_DIR"/*.conf; do
            [[ ! -f "$CONF" ]] && continue
            local CNAME CLIENT_IP CLIENT_PUB HS RX TX
            CNAME=$(basename "$CONF" .conf)
            CLIENT_IP=$(awk '/^Address/{print $3}' "$CONF" | cut -d'/' -f1)
            if [[ -f "$CLIENTS_DIR/${CNAME}.pub" ]]; then
                CLIENT_PUB=$(cat "$CLIENTS_DIR/${CNAME}.pub")
            else
                local CPRIV
                CPRIV=$(awk '/^PrivateKey/{print $3; exit}' "$CONF")
                CLIENT_PUB=$(echo "$CPRIV" | awg pubkey 2>/dev/null)
            fi
            local PEER_LINE
            PEER_LINE=$(echo "$AWG_DUMP" | grep "^${CLIENT_PUB}")
            HS=$(echo "$PEER_LINE" | awk '{print $5}')
            RX=$(echo "$PEER_LINE" | awk '{print $6}')
            TX=$(echo "$PEER_LINE" | awk '{print $7}')
            local HS_FMT TRAFFIC STATUS
            HS_FMT=$(fmt_handshake "$HS")
            [[ -n "$RX" ]] && TRAFFIC="↓$(numfmt --to=iec $RX 2>/dev/null) ↑$(numfmt --to=iec $TX 2>/dev/null)" || TRAFFIC="нет данных"
            if [[ -z "$HS" || "$HS" == "0" ]]; then
                STATUS="❌ никогда"
            elif (( NOW - HS < 180 )); then
                STATUS="🟢 онлайн"
            else
                STATUS="⚫ оффлайн"
            fi
            printf "%-30s %-16s %-20s %-20s %s\n" "$CNAME" "$CLIENT_IP" "$HS_FMT" "$TRAFFIC" "$STATUS"
        done
        echo ""

        # ── ЦЕЛОСТНОСТЬ ДАННЫХ ────────────────────────────────────────────────
        echo "━━━ ЦЕЛОСТНОСТЬ ДАННЫХ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "--- Проверка .pub файлов ---"
        local PUB_MISSING=0
        for CONF in "$CLIENTS_DIR"/*.conf; do
            [[ ! -f "$CONF" ]] && continue
            local CNAME
            CNAME=$(basename "$CONF" .conf)
            if [[ ! -f "$CLIENTS_DIR/${CNAME}.pub" ]]; then
                echo "  ⚠️  Нет .pub файла для: ${CNAME}"
                PUB_MISSING=$((PUB_MISSING+1))
            fi
        done
        [[ "$PUB_MISSING" == "0" ]] && echo "  ✅ Все .pub файлы на месте"
        echo ""

        echo "--- Проверка пиров в awg.conf ---"
        local ORPHAN=0
        for CONF in "$CLIENTS_DIR"/*.conf; do
            [[ ! -f "$CONF" ]] && continue
            local CNAME
            CNAME=$(basename "$CONF" .conf)
            if ! grep -q "# Client: ${CNAME}" "$AWG_CONF" 2>/dev/null; then
                echo "  ⚠️  Нет пира в awg.conf для: ${CNAME}"
                ORPHAN=$((ORPHAN+1))
            fi
        done
        [[ "$ORPHAN" == "0" ]] && echo "  ✅ Все клиенты есть в awg.conf"
        echo ""

        echo "--- Проверка SERVER_PUBLIC ---"
        local CONF_PUB
        CONF_PUB=$(grep "^PrivateKey" "$AWG_CONF" | awk '{print $3}' | awg pubkey 2>/dev/null)
        if [[ "$CONF_PUB" == "$SERVER_PUBLIC" ]]; then
            echo "  ✅ SERVER_PUBLIC совпадает с ключом в awg.conf"
        else
            echo "  ⚠️  РАСХОЖДЕНИЕ SERVER_PUBLIC!"
            echo "  В server.env: ${SERVER_PUBLIC}"
            echo "  В awg.conf:   ${CONF_PUB}"
        fi
        echo ""

        echo "--- users.json ---"
        if [[ -f "$USERS_FILE" ]]; then
            local APPROVED PENDING
            APPROVED=$(python3 -c "import json; d=json.load(open('$USERS_FILE')); print(len(d.get('approved',{})))" 2>/dev/null)
            PENDING=$(python3 -c "import json; d=json.load(open('$USERS_FILE')); print(len(d.get('pending',{})))" 2>/dev/null)
            echo "  ✅ users.json читается. Одобрено: ${APPROVED}, Ожидают: ${PENDING}"
        else
            echo "  ⚠️  users.json не найден"
        fi
        echo ""

        # ── БОТ ──────────────────────────────────────────────────────────────
        echo "━━━ TELEGRAM БОТ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "Статус сервиса: $(systemctl is-active awg-bot)"
        echo "python-telegram-bot: $(pip3 show python-telegram-bot 2>/dev/null | grep Version | awk '{print $2}')"
        echo "Python: $(python3 --version 2>&1)"
        echo ""
        echo "--- Ошибки за последние 24 часа ---"
        journalctl -u awg-bot -p err --since "24 hours ago" --no-pager 2>/dev/null | tail -20 || echo "нет ошибок"
        echo ""
        echo "--- Последние 100 строк лога бота ---"
        journalctl -u awg-bot -n 100 --no-pager 2>/dev/null
        echo ""
        echo "--- Последние 20 строк лога AWG сервиса ---"
        journalctl -u "awg-quick@${VPN_IFACE}" -n 20 --no-pager 2>/dev/null
        echo ""

        # ── ПРОИЗВОДИТЕЛЬНОСТЬ ────────────────────────────────────────────────
        echo "━━━ ПРОИЗВОДИТЕЛЬНОСТЬ И ТРАФИК ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        echo "--- Пики из bw_peak.json ---"
        if [[ -f "$BW_PEAK_FILE" ]]; then
            python3 -c "
import json
d = json.load(open('$BW_PEAK_FILE'))
day = d.get('day', {})
allp = d.get('all', {})
last = d.get('last', {})
print(f'  Пик сегодня: {day.get(\"load\",0)} Mbit/s  ↓{day.get(\"rx\",0)} ↑{day.get(\"tx\",0)}  ({day.get(\"date\",\"—\")})')
print(f'  Абс. пик:    {allp.get(\"load\",0)} Mbit/s  ↓{allp.get(\"rx\",0)} ↑{allp.get(\"tx\",0)}')
" 2>/dev/null || echo "  нет данных"
        else
            echo "  bw_peak.json не найден"
        fi
        echo ""
        echo "--- Топ-5 минут по нагрузке ---"
        if [[ -f "$BW_LOG_FILE" ]]; then
            awk '{print $3+$4, $0}' "$BW_LOG_FILE" 2>/dev/null | \
                sort -rn | head -5 | awk '{print "  "$2, $3, $4, $5}' || echo "  нет данных"
        else
            echo "  лог не найден"
        fi
        echo ""
        echo "--- vnstat (текущий месяц) ---"
        local HOST_IFACE
        HOST_IFACE=$(ip route get 8.8.8.8 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
        vnstat -i "${HOST_IFACE:-eth0}" --months 2>/dev/null | tail -10 || echo "  vnstat недоступен"
        echo ""

        # ── БЭКАПЫ ───────────────────────────────────────────────────────────
        echo "━━━ БЭКАПЫ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        local BFILES=( "$BACKUP_DIR"/*.tar.gz )
        if [[ -f "${BFILES[0]}" ]]; then
            for F in "${BFILES[@]}"; do
                echo "  $(basename $F)  $(du -sh $F | cut -f1)  $(date -r $F '+%d.%m.%Y %H:%M')"
            done
        else
            echo "  Бэкапов нет"
        fi
        echo ""

        echo "════════════════════════════════════════════════════════════════"
        echo "  Конец отчёта. Дата: $(date '+%d.%m.%Y %H:%M:%S')"
        echo "════════════════════════════════════════════════════════════════"

    } > "$FILE" 2>&1

    echo -e "${GREEN}  ✓ Отчёт сохранён: ${FILE}${NC}"
    echo ""
    echo -e "  Скачать: ${YELLOW}scp root@${SERVER_IP}:${FILE} ./$(basename $FILE)${NC}"
    echo ""
    read -p "  Открыть для просмотра? (y/N): " VIEW
    if [[ "$VIEW" == "y" || "$VIEW" == "Y" ]]; then
        nano -v "$FILE" 2>/dev/null || less "$FILE" || cat "$FILE"
    fi
}

view_old_diagnostics() {
    show_header
    echo -e "${BOLD}  Предыдущие диагностики${NC}"
    echo ""

    local FILES=( "$DIAG_DIR"/diag_*.txt )
    if [[ ! -f "${FILES[0]}" ]]; then
        echo -e "  ${YELLOW}Предыдущих диагностик нет.${NC}"
        press_enter; return
    fi

    local i=1
    local DNAMES=()
    for F in "${FILES[@]}"; do
        local FDATE
        FDATE=$(date -r "$F" '+%d.%m.%Y %H:%M' 2>/dev/null || basename "$F")
        echo -e "  $i) ${FDATE}  —  $(basename $F)  ($(du -sh $F | cut -f1))"
        DNAMES+=("$F")
        ((i++))
    done

    echo ""
    read -p "  Открыть (номер) или Enter для выхода: " NUM
    [[ -z "$NUM" ]] && return
    [[ "$NUM" -lt 1 || "$NUM" -gt "${#DNAMES[@]}" ]] 2>/dev/null && return

    local SEL="${DNAMES[$((NUM-1))]}"
    nano -v "$SEL" 2>/dev/null || less "$SEL" || cat "$SEL"
}

# =============================================================================
# ДИАГНОСТИКА — АНАЛИЗ КОНФИГОВ
# =============================================================================

analyze_configs() {
    show_header
    echo -e "${BOLD}  Анализ конфигов клиентов${NC}"
    echo ""

    local CONFS=( "$CLIENTS_DIR"/*.conf )
    if [[ ! -f "${CONFS[0]}" ]]; then
        echo -e "  ${YELLOW}Клиентов нет.${NC}"
        press_enter; return
    fi

    # Читаем параметры сервера из awg.conf
    local SRV_JC SRV_JMIN SRV_JMAX SRV_S1 SRV_S2 SRV_H1 SRV_H2 SRV_H3 SRV_H4 SRV_PORT SRV_PUB
    SRV_JC=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^Jc "   | awk '{print $3}')
    SRV_JMIN=$(awk '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^Jmin " | awk '{print $3}')
    SRV_JMAX=$(awk '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^Jmax " | awk '{print $3}')
    SRV_S1=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^S1 "   | awk '{print $3}')
    SRV_S2=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^S2 "   | awk '{print $3}')
    SRV_H1=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^H1 "   | awk '{print $3}')
    SRV_H2=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^H2 "   | awk '{print $3}')
    SRV_H3=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^H3 "   | awk '{print $3}')
    SRV_H4=$(awk   '/^\[Interface\]/,/^\[Peer\]/' "$AWG_CONF" | grep "^H4 "   | awk '{print $3}')
    SRV_PORT=$(grep "^ListenPort" "$AWG_CONF" | awk '{print $3}')
    SRV_PUB="$SERVER_PUBLIC"

    echo "  Выберите клиентов для анализа:"
    echo ""
    local i=1
    local CLIST=()
    for CONF in "${CONFS[@]}"; do
        local CNAME
        CNAME=$(basename "$CONF" .conf)
        echo -e "  $i) ${CNAME}"
        CLIST+=("$CNAME")
        ((i++))
    done
    echo ""
    echo -e "  0) Все клиенты"
    echo ""
    read -p "  Введите номер или 0 для всех: " SEL_NUM

    local SELECTED=()
    if [[ "$SEL_NUM" == "0" ]]; then
        SELECTED=("${CLIST[@]}")
    elif [[ "$SEL_NUM" -ge 1 && "$SEL_NUM" -le "${#CLIST[@]}" ]] 2>/dev/null; then
        SELECTED=("${CLIST[$((SEL_NUM-1))]}")
    else
        echo -e "${RED}  Неверный выбор${NC}"; sleep 2; return
    fi

    local TS FILE
    TS=$(date +"%Y%m%d_%H%M%S")
    FILE="${DIAG_DIR}/configs_${TS}.txt"

    {
        echo "════════════════════════════════════════════════════════════════"
        echo "  Анализ конфигов — $(date '+%d.%m.%Y %H:%M:%S')"
        echo "════════════════════════════════════════════════════════════════"
        echo ""
        echo "━━━ СЕРВЕР (${VPN_IFACE}.conf) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  PublicKey:   ${SRV_PUB}"
        echo "  ListenPort:  ${SRV_PORT}"
        echo "  Jc:          ${SRV_JC}"
        echo "  Jmin:        ${SRV_JMIN}"
        echo "  Jmax:        ${SRV_JMAX}"
        echo "  S1:          ${SRV_S1}"
        echo "  S2:          ${SRV_S2}"
        echo "  H1:          ${SRV_H1}"
        echo "  H2:          ${SRV_H2}"
        echo "  H3:          ${SRV_H3}"
        echo "  H4:          ${SRV_H4}"
        echo "  Endpoint(s): ${SERVER_ENDPOINT}:${SERVER_PORT}"
        [[ -n "$SERVER_ENDPOINT_BACKUP" ]] && echo "  Резерв:      ${SERVER_ENDPOINT_BACKUP}:${SERVER_PORT}"
        echo ""

        for CNAME in "${SELECTED[@]}"; do
            local CONF="$CLIENTS_DIR/${CNAME}.conf"
            [[ ! -f "$CONF" ]] && continue

            echo "━━━ КЛИЕНТ: ${CNAME} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

            local C_IP C_DNS C_JC C_JMIN C_JMAX C_S1 C_S2 C_H1 C_H2 C_H3 C_H4
            local C_SRVPUB C_EP C_AIPS C_KEEPALIVE
            C_IP=$(awk       '/^Address/{print $3}'        "$CONF")
            C_DNS=$(awk      '/^DNS/{print $3,$4}'         "$CONF")
            C_JC=$(awk       '/^Jc /{print $3}'            "$CONF")
            C_JMIN=$(awk     '/^Jmin /{print $3}'          "$CONF")
            C_JMAX=$(awk     '/^Jmax /{print $3}'          "$CONF")
            C_S1=$(awk       '/^S1 /{print $3}'            "$CONF")
            C_S2=$(awk       '/^S2 /{print $3}'            "$CONF")
            C_H1=$(awk       '/^H1 /{print $3}'            "$CONF")
            C_H2=$(awk       '/^H2 /{print $3}'            "$CONF")
            C_H3=$(awk       '/^H3 /{print $3}'            "$CONF")
            C_H4=$(awk       '/^H4 /{print $3}'            "$CONF")
            C_SRVPUB=$(awk   '/^PublicKey/{print $3}'      "$CONF")
            C_EP=$(awk       '/^Endpoint/{print $3}'       "$CONF")
            C_AIPS=$(awk     '/^AllowedIPs/{print $3}'     "$CONF")
            C_KEEPALIVE=$(awk '/^PersistentKeepalive/{print $3}' "$CONF")

            # Функция сравнения
            cmp_val() {
                local LABEL="$1" VAL="$2" REF="$3"
                local STATUS="✅"
                [[ "$VAL" != "$REF" && -n "$REF" ]] && STATUS="⚠️ "
                printf "  %-18s %-35s %s\n" "${LABEL}:" "${VAL}" "${STATUS}(сервер: ${REF})"
            }
            cmp_val_noref() {
                printf "  %-18s %s\n" "${1}:" "${2}"
            }

            cmp_val_noref "IP адрес"       "$C_IP"
            cmp_val_noref "DNS"            "$C_DNS"
            cmp_val       "Jc"             "$C_JC"       "$SRV_JC"
            cmp_val       "Jmin"           "$C_JMIN"     "$SRV_JMIN"
            cmp_val       "Jmax"           "$C_JMAX"     "$SRV_JMAX"
            cmp_val       "S1"             "$C_S1"       "$SRV_S1"
            cmp_val       "S2"             "$C_S2"       "$SRV_S2"
            cmp_val       "H1"             "$C_H1"       "$SRV_H1"
            cmp_val       "H2"             "$C_H2"       "$SRV_H2"
            cmp_val       "H3"             "$C_H3"       "$SRV_H3"
            cmp_val       "H4"             "$C_H4"       "$SRV_H4"
            cmp_val       "ServerPublicKey" "$C_SRVPUB"  "$SRV_PUB"
            cmp_val_noref "Endpoint"       "$C_EP"
            cmp_val_noref "AllowedIPs"     "$C_AIPS"
            cmp_val_noref "Keepalive"      "$C_KEEPALIVE"

            # Проверка endpoint
            local EP_HOST EP_PORT
            EP_HOST=$(echo "$C_EP" | cut -d: -f1)
            EP_PORT=$(echo "$C_EP" | cut -d: -f2)
            if [[ "$EP_PORT" != "$SERVER_PORT" ]]; then
                echo "  ⚠️  ПОРТ КЛИЕНТА (${EP_PORT}) НЕ СОВПАДАЕТ С СЕРВЕРОМ (${SERVER_PORT})"
            fi
            if [[ "$EP_HOST" != "$SERVER_ENDPOINT" && "$EP_HOST" != "$SERVER_IP" && "$EP_HOST" != "$SERVER_ENDPOINT_BACKUP" ]]; then
                echo "  ⚠️  ENDPOINT КЛИЕНТА (${EP_HOST}) НЕ СОВПАДАЕТ НИ С ОДНИМ ИЗВЕСТНЫМ ENDPOINT"
            fi

            echo ""
        done

        echo "════════════════════════════════════════════════════════════════"
        echo "  Конец анализа. Дата: $(date '+%d.%m.%Y %H:%M:%S')"
        echo "════════════════════════════════════════════════════════════════"

    } > "$FILE" 2>&1

    echo ""
    echo -e "${GREEN}  ✓ Анализ сохранён: ${FILE}${NC}"
    echo ""
    read -p "  Открыть для просмотра? (y/N): " VIEW
    if [[ "$VIEW" == "y" || "$VIEW" == "Y" ]]; then
        nano -v "$FILE" 2>/dev/null || less "$FILE" || cat "$FILE"
    fi
}

# =============================================================================
# ГЛАВНОЕ МЕНЮ
# =============================================================================

main_menu() {
    while true; do
        show_header
        local CLIENTS_COUNT AWG_ST BOT_ST
        CLIENTS_COUNT=$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)
        systemctl is-active --quiet "awg-quick@${VPN_IFACE}" \
            && AWG_ST="${GREEN}●${NC}" || AWG_ST="${RED}●${NC}"
        systemctl is-active --quiet awg-bot \
            && BOT_ST="${GREEN}●${NC}" || BOT_ST="${RED}●${NC}"

        echo -e "  ${BOLD}Endpoint:${NC} ${CYAN}${SERVER_ENDPOINT}:${SERVER_PORT}${NC}  |  Клиентов: ${CYAN}${CLIENTS_COUNT}${NC}  |  AWG: ${AWG_ST}  Бот: ${BOT_ST}"
        echo ""
        echo "  ── Клиенты ──────────────────────────"
        echo "  1) Добавить клиента"
        echo "  2) Список клиентов"
        echo "  3) Удалить клиента"
        echo ""
        echo "  ── Система ──────────────────────────"
        echo "  4) Статус сервера"
        echo "  5) Сервисы (запуск / стоп / логи)"
        echo "  6) Настройки (токен, IP, DNS...)"
        echo "  7) Бэкапы"
        echo "  8) Обновление"
        echo ""
        echo "  ── Диагностика ──────────────────────"
        echo "  9) Полный диагностический отчёт"
        echo " 10) Анализ конфигов клиентов"
        echo " 11) Просмотр предыдущих диагностик"
        echo ""
        echo "  0) Выход"
        echo ""
        read -p "  Выбор: " CHOICE

        case $CHOICE in
            1)  add_client ;;
            2)  list_clients ;;
            3)  delete_client ;;
            4)  show_status ;;
            5)  manage_services ;;
            6)  manage_settings ;;
            7)  manage_backups ;;
            8)  manage_updates ;;
            9)  run_diagnostics ;;
            10) analyze_configs ;;
            11) view_old_diagnostics ;;
            0)  echo ""; exit 0 ;;
            *)  echo -e "  ${RED}Неверный выбор${NC}"; sleep 1 ;;
        esac
    done
}

main_menu