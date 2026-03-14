#!/bin/bash
# Version: 1.3
# =============================================================================
# AmneziaWG — управление клиентами
# Запускать: bash vpn.sh
# =============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

CLIENTS_DIR="/etc/amnezia/amneziawg/clients"
ENV_FILE="/etc/amnezia/amneziawg/server.env"

[[ $EUID -ne 0 ]] && echo -e "${RED}Запускать от root: sudo bash vpn.sh${NC}" && exit 1
[[ ! -f "$ENV_FILE" ]] && echo -e "${RED}Сначала запустите setup.sh${NC}" && exit 1

source "$ENV_FILE"

# Проверка: SERVER_IP не должен быть IPv6
if [[ "$SERVER_IP" == *":"* ]]; then
    echo -e "${YELLOW}[!] В server.env записан IPv6 адрес: ${SERVER_IP}${NC}"
    echo -e "${YELLOW}    Клиентские конфиги будут генерироваться с IPv6 Endpoint — это не работает в большинстве случаев.${NC}"
    echo ""
    read -p "  Введите правильный IPv4 сервера (или Enter чтобы продолжить с IPv6): " FIX_IP
    if [[ -n "$FIX_IP" ]]; then
        SERVER_IP="$FIX_IP"
        sed -i "s/^SERVER_IP=.*/SERVER_IP=${FIX_IP}/" "$ENV_FILE"
        echo -e "${GREEN}[+] SERVER_IP исправлен на ${FIX_IP} в server.env${NC}"
        sleep 1
    fi
fi

# Фолбэк на awg0 если VPN_IFACE отсутствует в server.env (старые установки)
VPN_IFACE="${VPN_IFACE:-awg0}"
AWG_CONF="/etc/amnezia/amneziawg/${VPN_IFACE}.conf"
mkdir -p "$CLIENTS_DIR"

# DNS из server.env с дефолтами
PRIMARY_DNS="${PRIMARY_DNS:-1.1.1.1}"
SECONDARY_DNS="${SECONDARY_DNS:-1.0.0.1}"

next_ip() {
    local i=2
    while grep -q "AllowedIPs = ${VPN_SUBNET}.${i}/32" "$AWG_CONF" 2>/dev/null; do
        ((i++))
    done
    echo "$i"
}

show_header() {
    clear
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════╗"
    echo "  ║      AmneziaWG — Управление      ║"
    echo "  ╚══════════════════════════════════╝"
    echo -e "${NC}"
}

show_qr() {
    local NAME="$1"
    local VPN_FILE="$CLIENTS_DIR/${NAME}.vpn"
    if [[ ! -f "$VPN_FILE" ]]; then
        echo -e "${RED}vpn:// ссылка не найдена для ${NAME}${NC}"
        return
    fi
    if command -v qrencode &>/dev/null; then
        echo -e "${CYAN}QR-код (сканировать через AmneziaVPN):${NC}"
        echo ""
        qrencode -t ansiutf8 -l L -r "$VPN_FILE"
        echo ""
    fi
}

# --- Добавить клиента ---
add_client() {
    show_header
    echo -e "${BOLD}Добавление клиента${NC}"
    echo ""
    read -p "Имя клиента (только латиница): " NAME

    # Очищаем имя от кириллицы, пробелов и спецсимволов
    NAME=$(echo "$NAME" | tr -d '\r\xef\xbb\xbf' | sed 's/^[[:space:]]*//' | sed 's/[[:space:]]*$//' | tr -cd '[:alnum:]_-')

    [[ -z "$NAME" ]] && echo -e "${RED}Имя пустое. Используйте только латиницу, цифры, _ или -.${NC}" && sleep 3 && return
    [[ -f "$CLIENTS_DIR/${NAME}.conf" ]] && echo -e "${RED}Клиент '$NAME' уже существует${NC}" && sleep 2 && return

    CLIENT_PRIVATE=$(awg genkey)
    CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | awg pubkey)
    CLIENT_PSK=$(awg genpsk)
    CLIENT_IP="${VPN_SUBNET}.$(next_ip)"

    # Используем параметры обфускации с сервера
    JC_CLIENT="${JC}"
    JMIN_CLIENT="${JMIN}"
    JMAX_CLIENT="${JMAX}"
    S1_CLIENT="${S1}"
    S2_CLIENT="${S2}"
    H1_CLIENT="${H1}"
    H2_CLIENT="${H2}"
    H3_CLIENT="${H3}"
    H4_CLIENT="${H4}"

    printf "\n# Client: %s\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n" \
        "$NAME" "$CLIENT_PUBLIC" "$CLIENT_PSK" "$CLIENT_IP" >> "$AWG_CONF"

    echo "$CLIENT_PSK" | awg set "$VPN_IFACE" peer "$CLIENT_PUBLIC" \
        preshared-key /dev/stdin allowed-ips "${CLIENT_IP}/32"

    {
        printf "[Interface]\n"
        printf "PrivateKey = %s\n" "$CLIENT_PRIVATE"
        printf "Address = %s/32\n" "$CLIENT_IP"
        printf "DNS = %s, %s\n" "$PRIMARY_DNS" "$SECONDARY_DNS"
        printf "Jc = %s\nJmin = %s\nJmax = %s\n" "$JC_CLIENT" "$JMIN_CLIENT" "$JMAX_CLIENT"
        printf "S1 = %s\nS2 = %s\n" "$S1_CLIENT" "$S2_CLIENT"
        printf "H1 = %s\nH2 = %s\nH3 = %s\nH4 = %s\n" "$H1_CLIENT" "$H2_CLIENT" "$H3_CLIENT" "$H4_CLIENT"
        printf "\n[Peer]\n"
        printf "PublicKey = %s\n" "$SERVER_PUBLIC"
        printf "PresharedKey = %s\n" "$CLIENT_PSK"
        printf "Endpoint = %s:%s\n" "$SERVER_IP" "$SERVER_PORT"
        printf "AllowedIPs = 0.0.0.0/0\n"
        printf "PersistentKeepalive = 25\n"
    } > "$CLIENTS_DIR/${NAME}.conf"

    # Генерируем vpn:// ссылку для AmneziaVPN — сохраняем в .vpn
    python3 - "$NAME" "$CLIENT_PRIVATE" "$CLIENT_PUBLIC" "$CLIENT_IP" "$CLIENT_PSK" \
        "$JC_CLIENT" "$JMIN_CLIENT" "$JMAX_CLIENT" "$S1_CLIENT" "$S2_CLIENT" \
        "$H1_CLIENT" "$H2_CLIENT" "$H3_CLIENT" "$H4_CLIENT" \
        "$SERVER_PUBLIC" "$SERVER_IP" "$SERVER_PORT" \
        "$PRIMARY_DNS" "$SECONDARY_DNS" \
        "$CLIENTS_DIR/${NAME}.vpn" << 'PYEOF'
import sys, json, zlib, base64, struct
name,priv,pub,ip,psk,jc,jmin,jmax,s1,s2,h1,h2,h3,h4,srv_pub,srv_ip,srv_port,dns1,dns2,out = sys.argv[1:]
obfs = {"Jc":jc,"Jmin":jmin,"Jmax":jmax,"S1":s1,"S2":s2,"H1":h1,"H2":h2,"H3":h3,"H4":h4}
wg = (f"[Interface]\nAddress = {ip}/32\nDNS = {dns1}, {dns2}\n"
      f"PrivateKey = {priv}\nJc = {jc}\nJmin = {jmin}\nJmax = {jmax}\n"
      f"S1 = {s1}\nS2 = {s2}\nH1 = {h1}\nH2 = {h2}\nH3 = {h3}\nH4 = {h4}\n\n"
      f"[Peer]\nPublicKey = {srv_pub}\nPresharedKey = {psk}\n"
      f"AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = {srv_ip}:{srv_port}\nPersistentKeepalive = 25\n")
lc = {**obfs,"allowed_ips":["0.0.0.0/0","::/0"],"clientId":pub,"client_ip":ip,
      "client_priv_key":priv,"client_pub_key":pub,"config":wg,"hostName":srv_ip,
      "mtu":"1420","persistent_keep_alive":"25","port":int(srv_port),"psk_key":psk,"server_pub_key":srv_pub}
c = {"containers":[{"awg":{**obfs,"last_config":json.dumps(lc,indent=4),
     "port":srv_port,"subnet_address":".".join(ip.split(".")[:3])+".0","transport_proto":"udp"},
     "container":"amnezia-awg"}],"defaultContainer":"amnezia-awg","description":name,
     "dns1":dns1,"dns2":dns2,"hostName":srv_ip,"nameOverriddenByUser":True}
b = json.dumps(c,ensure_ascii=False).encode()
p = struct.pack('>I',len(b)) + zlib.compress(b)
open(out,'w').write('vpn://' + base64.urlsafe_b64encode(p).decode().rstrip('='))
PYEOF

    echo ""
    echo -e "${GREEN}✓ Клиент '${NAME}' добавлен — IP: ${CLIENT_IP}${NC}"
    echo -e "${GREEN}  DNS: ${PRIMARY_DNS}, ${SECONDARY_DNS}${NC}"
    echo ""
    show_qr "$NAME"
    echo -e "${CYAN}Конфиг:${NC} ${CLIENTS_DIR}/${NAME}.conf"
    echo ""
    echo "Скачать на Windows (выполнить в cmd локально):"
    echo -e "${YELLOW}  scp root@${SERVER_IP}:${CLIENTS_DIR}/${NAME}.conf .\\${NAME}.conf${NC}"
    echo ""
    read -p "Показать текст конфига? (y/N): " SHOW
    if [[ "$SHOW" == "y" || "$SHOW" == "Y" ]]; then
        echo ""
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━${NC}"
        cat "$CLIENTS_DIR/${NAME}.conf"
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
    fi
    read -p "Нажмите Enter..."
}

# --- Список клиентов ---
list_clients() {
    show_header
    echo -e "${BOLD}Список клиентов${NC}"
    echo ""

    AWG_OUTPUT=$(awg show "$VPN_IFACE" 2>/dev/null)

    if [[ -z $(ls "$CLIENTS_DIR"/*.conf 2>/dev/null) ]]; then
        echo -e "${YELLOW}Клиентов нет. Добавьте первого через меню.${NC}"
        echo ""
        read -p "Нажмите Enter..."
        return
    fi

    printf "  %-4s %-20s %-16s %-22s %s\n" "N" "ИМЯ" "IP" "ПОСЛЕДНЕЕ СОЕДИНЕНИЕ" "ТРАФИК"
    echo "  ──────────────────────────────────────────────────────────────────────"

    local i=1
    local NAMES=()
    for CONF in "$CLIENTS_DIR"/*.conf; do
        NAME=$(basename "$CONF" .conf)
        CLIENT_IP=$(grep "^Address" "$CONF" | awk '{print $3}' | cut -d'/' -f1)
        # PublicKey вычисляем из PrivateKey клиента
        CLIENT_PRIVATE=$(awk '/^PrivateKey/{print $3; exit}' "$CONF")
        CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | awg pubkey)

        HANDSHAKE=$(echo "$AWG_OUTPUT" | grep -A5 "$CLIENT_PUBLIC" | grep "latest handshake" | sed 's/.*latest handshake: //' || true)
        TRANSFER=$(echo "$AWG_OUTPUT" | grep -A5 "$CLIENT_PUBLIC" | grep "transfer" | sed 's/.*transfer: //' || true)

        [[ -z "$HANDSHAKE" ]] && HANDSHAKE="никогда"
        [[ -z "$TRANSFER" ]] && TRANSFER="—"

        printf "  %-4s %-20s %-16s %-22s %s\n" "$i)" "$NAME" "$CLIENT_IP" "$HANDSHAKE" "$TRANSFER"
        NAMES+=("$NAME")
        ((i++))
    done

    echo ""
    read -p "Открыть клиента (введите номер) или Enter для выхода: " NUM
    [[ -z "$NUM" ]] && return
    [[ "$NUM" -lt 1 || "$NUM" -gt "${#NAMES[@]}" ]] 2>/dev/null && return

    NAME="${NAMES[$((NUM-1))]}"

    while true; do
        show_header
        echo -e "${BOLD}Клиент: ${CYAN}${NAME}${NC}"
        echo ""
        echo "  1) Показать QR-код"
        echo "  2) Показать текст конфига"
        echo "  3) Команда для скачивания"
        echo "  0) Назад"
        echo ""
        read -p "  Выбор: " ACTION
        case $ACTION in
            1)
                echo ""
                show_qr "$NAME"
                read -p "Нажмите Enter..."
                ;;
            2)
                echo ""
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━${NC}"
                cat "$CLIENTS_DIR/${NAME}.conf"
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo ""
                read -p "Нажмите Enter..."
                ;;
            3)
                echo ""
                echo "Windows (cmd):"
                echo -e "${YELLOW}  scp root@${SERVER_IP}:${CLIENTS_DIR}/${NAME}.conf .\\${NAME}.conf${NC}"
                echo ""
                echo "Linux/Mac:"
                echo -e "${YELLOW}  scp root@${SERVER_IP}:${CLIENTS_DIR}/${NAME}.conf ./${NAME}.conf${NC}"
                echo ""
                read -p "Нажмите Enter..."
                ;;
            0) return ;;
        esac
    done
}

# --- Удалить клиента ---
delete_client() {
    show_header
    echo -e "${BOLD}Удаление клиента${NC}"
    echo ""

    if [[ -z $(ls "$CLIENTS_DIR"/*.conf 2>/dev/null) ]]; then
        echo -e "${YELLOW}Клиентов нет.${NC}"
        sleep 2; return
    fi

    echo "Текущие клиенты:"
    local i=1
    local NAMES=()
    for CONF in "$CLIENTS_DIR"/*.conf; do
        NAME=$(basename "$CONF" .conf)
        IP=$(grep "^Address" "$CONF" | awk '{print $3}')
        echo "  $i) $NAME ($IP)"
        NAMES+=("$NAME")
        ((i++))
    done

    echo ""
    read -p "Номер клиента для удаления (0 — отмена): " NUM

    [[ "$NUM" == "0" || -z "$NUM" ]] && return
    [[ "$NUM" -lt 1 || "$NUM" -gt "${#NAMES[@]}" ]] && echo -e "${RED}Неверный номер${NC}" && sleep 2 && return

    NAME="${NAMES[$((NUM-1))]}"
    CONF="$CLIENTS_DIR/${NAME}.conf"

    # PublicKey вычисляем из PrivateKey клиента
    CLIENT_PRIVATE=$(awk '/^PrivateKey/{print $3; exit}' "$CONF")
    CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | awg pubkey)

    echo ""
    read -p "Удалить '$NAME'? (y/N): " CONFIRM
    [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]] && return

    # Удаляем пир из живого интерфейса
    [[ -n "$CLIENT_PUBLIC" ]] && awg set "$VPN_IFACE" peer "$CLIENT_PUBLIC" remove

    # Удаляем блок из конфига интерфейса
    python3 - "$AWG_CONF" "$NAME" << 'PYEOF'
import sys
conf_path, name = sys.argv[1], sys.argv[2]
with open(conf_path, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()
lines = content.split('\n')
new_lines = []
skip = False
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

    # Удаляем все файлы клиента
    rm -f "$CLIENTS_DIR/${NAME}.conf"
    rm -f "$CLIENTS_DIR/${NAME}.vpn"
    rm -f "$CLIENTS_DIR/${NAME}.vpnlink"  # на случай старых файлов

    echo -e "${GREEN}✓ Клиент '$NAME' удалён${NC}"
    sleep 2
}

# --- Статус сервера ---
show_status() {
    show_header
    echo -e "${BOLD}Статус сервера${NC}"
    echo ""

    if awg show "$VPN_IFACE" > /dev/null 2>&1; then
        echo -e "  AWG интерфейс:  ${GREEN}● работает${NC}"
    else
        echo -e "  AWG интерфейс:  ${RED}● остановлен${NC}"
    fi

    UPTIME=$(uptime -p 2>/dev/null || uptime)
    echo -e "  Сервер uptime:  ${CYAN}${UPTIME}${NC}"
    CPU=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d. -f1)
    echo -e "  CPU:            ${CYAN}${CPU}%${NC}"
    RAM=$(free -m | awk 'NR==2{printf "%s MB / %s MB (%.0f%%)", $3, $2, $3*100/$2}')
    echo -e "  RAM:            ${CYAN}${RAM}${NC}"
    CLIENTS=$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)
    echo -e "  Клиентов:       ${CYAN}${CLIENTS}${NC}"
    echo -e "  DNS:            ${CYAN}${PRIMARY_DNS}, ${SECONDARY_DNS}${NC}"
    echo ""
    echo -e "${BOLD}awg show:${NC}"
    awg show "$VPN_IFACE" 2>/dev/null
    echo ""
    read -p "Нажмите Enter..."
}

# --- Главное меню ---
main_menu() {
    while true; do
        show_header
        CLIENTS_COUNT=$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)
        echo -e "  IP сервера: ${CYAN}${SERVER_IP}:${SERVER_PORT}${NC}  |  Клиентов: ${CYAN}${CLIENTS_COUNT}${NC}"
        echo ""
        echo "  1) Добавить клиента"
        echo "  2) Список клиентов"
        echo "  3) Удалить клиента"
        echo "  4) Статус сервера"
        echo "  0) Выход"
        echo ""
        read -p "  Выбор: " CHOICE

        case $CHOICE in
            1) add_client ;;
            2) list_clients ;;
            3) delete_client ;;
            4) show_status ;;
            0) echo ""; exit 0 ;;
            *) echo -e "${RED}Неверный выбор${NC}"; sleep 1 ;;
        esac
    done
}

main_menu