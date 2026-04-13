#!/usr/bin/env bash
# AWG → SOCKS5 Installer v3.1 (redsocks2 edition)
# Ubuntu 24. Трафик выбранного AWG-клиента → SOCKS5-прокси.
# redsocks2 + dnscrypt-proxy + iptables

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET} $*"; }
success() { echo -e "${GREEN}[OK]${RESET}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $*"; }
error()   { echo -e "${RED}[ERR]${RESET}  $*" >&2; }
die()     { error "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите скрипт от root: sudo $0"

AWG_CONF_DIR="/etc/amnezia/amneziawg"
REDSOCKS2_BIN="/usr/local/bin/redsocks2"
REDSOCKS2_CONF="/etc/redsocks2/redsocks2.conf"
REDSOCKS2_PORT=12345
STATE_FILE="/etc/awg-socks5/state.conf"
IPTABLES_RULES_FILE="/etc/iptables/rules.v4"
SYSTEMD_REDSOCKS2="/etc/systemd/system/redsocks2.service"
DNSCRYPT_CONF="/etc/dnscrypt-proxy/dnscrypt-proxy.toml"
DNS_LOCAL_PORT=5300

mkdir -p /etc/awg-socks5 /etc/redsocks2

find_awg_config() {
    info "Поиск серверного AWG конфига в $AWG_CONF_DIR ..."
    [[ -d "$AWG_CONF_DIR" ]] || die "Директория $AWG_CONF_DIR не найдена."

    mapfile -t ALL_CONF < <(find "$AWG_CONF_DIR" -maxdepth 1 -name "*.conf" 2>/dev/null)

    CONF_FILES=()
    for f in "${ALL_CONF[@]}"; do
        grep -q '^\[Peer\]' "$f" 2>/dev/null && CONF_FILES+=("$f")
    done

    if [[ ${#CONF_FILES[@]} -eq 0 ]]; then
        mapfile -t ALL_CONF < <(find "$AWG_CONF_DIR" -maxdepth 3 -name "*.conf" 2>/dev/null)
        for f in "${ALL_CONF[@]}"; do
            grep -q '^\[Peer\]' "$f" 2>/dev/null && CONF_FILES+=("$f")
        done
    fi

    [[ ${#CONF_FILES[@]} -eq 0 ]] && die "Серверные конфиги AWG не найдены."

    if [[ ${#CONF_FILES[@]} -eq 1 ]]; then
        AWG_CONF="${CONF_FILES[0]}"
        info "Найден серверный конфиг: $AWG_CONF"
    else
        echo -e "\n${BOLD}Найдено несколько серверных конфигов:${RESET}"
        for i in "${!CONF_FILES[@]}"; do
            local pc
            pc=$(grep -c '^\[Peer\]' "${CONF_FILES[$i]}" 2>/dev/null || echo 0)
            echo "  [$((i+1))] ${CONF_FILES[$i]}  (пиров: $pc)"
        done
        read -rp "Выберите номер конфига: " CONF_CHOICE
        AWG_CONF="${CONF_FILES[$((CONF_CHOICE-1))]}"
    fi

    AWG_IFACE=$(basename "$AWG_CONF" .conf)
    success "Конфиг: $AWG_CONF  |  Интерфейс: $AWG_IFACE"

    ip link show "$AWG_IFACE" &>/dev/null \
        || warn "Интерфейс $AWG_IFACE не найден. Убедитесь что AWG запущен."
}

parse_clients() {
    info "Разбор клиентов из конфига..."

    declare -gA CLIENT_NAMES=()
    declare -gA CLIENT_IPS=()
    declare -ga CLIENT_ORDER=()

    local current_name="" current_ip="" idx=0

    while IFS= read -r line; do
        [[ "$line" =~ ^#[[:space:]]*(.+)$ ]] && current_name="${BASH_REMATCH[1]}"
        [[ "$line" =~ ^\[Peer\] ]]           && current_ip=""
        [[ "$line" =~ ^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]] \
            && current_ip="${BASH_REMATCH[1]}"

        if [[ ( -z "$line" || "$line" =~ ^\[Peer\] ) && -n "$current_ip" ]]; then
            idx=$((idx+1))
            local key="client${idx}"
            CLIENT_ORDER+=("$key")
            CLIENT_IPS["$key"]="$current_ip"
            CLIENT_NAMES["$key"]="${current_name:-Клиент $idx}"
            current_ip=""; current_name=""
        fi
    done < "$AWG_CONF"

    if [[ -n "$current_ip" ]]; then
        idx=$((idx+1)); local key="client${idx}"
        CLIENT_ORDER+=("$key")
        CLIENT_IPS["$key"]="$current_ip"
        CLIENT_NAMES["$key"]="${current_name:-Клиент $idx}"
    fi

    [[ ${#CLIENT_ORDER[@]} -eq 0 ]] && die "Клиенты не найдены в конфиге."
    success "Найдено клиентов: ${#CLIENT_ORDER[@]}"
}

select_client() {
    echo -e "\n${BOLD}Список AWG клиентов:${RESET}"
    for i in "${!CLIENT_ORDER[@]}"; do
        local key="${CLIENT_ORDER[$i]}"
        printf "  [%d] %-30s  IP: %s\n" "$((i+1))" "${CLIENT_NAMES[$key]}" "${CLIENT_IPS[$key]}"
    done
    echo ""
    read -rp "Выберите клиента для SOCKS5 маршрутизации (номер): " SEL
    [[ "$SEL" =~ ^[0-9]+$ && "$SEL" -ge 1 && "$SEL" -le "${#CLIENT_ORDER[@]}" ]] \
        || die "Неверный выбор."

    SELECTED_KEY="${CLIENT_ORDER[$((SEL-1))]}"
    SELECTED_NAME="${CLIENT_NAMES[$SELECTED_KEY]}"
    SELECTED_IP="${CLIENT_IPS[$SELECTED_KEY]}"
    success "Выбран клиент: ${SELECTED_NAME} (${SELECTED_IP})"
}

get_socks5_params() {
    echo -e "\n${BOLD}Параметры SOCKS5 прокси:${RESET}"
    read -rp  "  Хост (IP или домен): " SOCKS5_HOST
    read -rp  "  Порт:                " SOCKS5_PORT
    read -rp  "  Логин:               " SOCKS5_USER
    read -rsp "  Пароль:              " SOCKS5_PASS
    echo ""
    [[ -n "$SOCKS5_HOST" && -n "$SOCKS5_PORT" ]] || die "Хост и порт обязательны."
}

check_socks5() {
    info "Проверка SOCKS5 прокси (HTTPS)..."
    command -v curl &>/dev/null || apt-get install -y -q curl

    local result
    result=$(curl -s --max-time 15 \
        --socks5-hostname "${SOCKS5_HOST}:${SOCKS5_PORT}" \
        --proxy-user "${SOCKS5_USER}:${SOCKS5_PASS}" \
        https://ifconfig.me 2>/dev/null || true)

    if [[ -n "$result" ]]; then
        success "SOCKS5 HTTPS работает. Внешний IP через прокси: $result"
        PROXY_EXTERNAL_IP="$result"
    else
        die "SOCKS5 недоступен или не поддерживает HTTPS. Проверьте параметры прокси."
    fi
}

install_packages() {
    info "Установка пакетов..."
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
        curl wget git build-essential libevent-dev libssl-dev \
        iptables iptables-persistent dnscrypt-proxy dnsutils
    success "Базовые пакеты установлены."
    build_redsocks2
}

build_redsocks2() {
    info "Сборка redsocks2 из исходников..."

    rm -rf /tmp/redsocks2-build

    git clone --depth=1 https://github.com/semigodking/redsocks.git /tmp/redsocks2-build \
        || die "Не удалось клонировать redsocks2."

    cd /tmp/redsocks2-build
    make -j"$(nproc)" 2>&1 | tail -5
    [[ -f redsocks2 ]] || die "Сборка redsocks2 не удалась."

    cp redsocks2 "$REDSOCKS2_BIN"
    chmod +x "$REDSOCKS2_BIN"
    cd /
    rm -rf /tmp/redsocks2-build

    success "redsocks2 собран и установлен: $REDSOCKS2_BIN"
}

configure_redsocks2() {
    info "Настройка redsocks2 на порту $REDSOCKS2_PORT..."

    SOCKS5_HOST_IP=$(getent hosts "$SOCKS5_HOST" | awk '{print $1}' | head -1 || echo "$SOCKS5_HOST")
    info "IP прокси-сервера: $SOCKS5_HOST_IP"

    cat > "$REDSOCKS2_CONF" <<EOF
base {
    log_debug = on;
    log_info = on;
    log = "syslog:daemon";
    daemon = off;
    redirector = iptables;
}

redsocks {
    bind = "0.0.0.0:${REDSOCKS2_PORT}";
    relay = "${SOCKS5_HOST}:${SOCKS5_PORT}";
    type = socks5;
    autoproxy = 0;
    timeout = 10;
    login = "${SOCKS5_USER}";
    password = "${SOCKS5_PASS}";
}
EOF

    chmod 600 "$REDSOCKS2_CONF"
    success "Конфиг redsocks2 записан: $REDSOCKS2_CONF"
}

configure_redsocks2_service() {
    info "Настройка systemd сервиса redsocks2..."

    cat > "$SYSTEMD_REDSOCKS2" <<EOF
[Unit]
Description=Redsocks2 transparent SOCKS5 proxy
After=network.target

[Service]
Type=simple
ExecStart=${REDSOCKS2_BIN} -c ${REDSOCKS2_CONF}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable redsocks2
    systemctl restart redsocks2
    sleep 2

    systemctl is-active --quiet redsocks2 \
        && success "redsocks2 запущен." \
        || {
            error "redsocks2 не запустился. Лог:"
            journalctl -u redsocks2 -n 20 --no-pager
            die "Остановка установки."
        }
}

configure_dnscrypt() {
    info "Настройка dnscrypt-proxy (DoH) на порту $DNS_LOCAL_PORT..."

    mkdir -p /var/cache/dnscrypt-proxy

    cat > "$DNSCRYPT_CONF" <<'TOMLEOF'
listen_addresses = ['127.0.0.1:5300']
log_level = 2
max_clients = 250
ipv4_servers = true
ipv6_servers = false
dnscrypt_servers = true
doh_servers = true
require_dnssec = false
require_nolog = true
require_nofilter = true
force_tcp = false
timeout = 5000
keepalive = 30

[sources]
  [sources.'public-resolvers']
  urls = ['https://raw.githubusercontent.com/DNSCrypt/dnscrypt-resolvers/master/v3/public-resolvers.md',
          'https://download.dnscrypt.info/resolvers-list/v3/public-resolvers.md']
  cache_file = '/var/cache/dnscrypt-proxy/public-resolvers.md'
  minisign_key = 'RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3'
  refresh_delay = 72
  prefix = ''
TOMLEOF

    systemctl enable dnscrypt-proxy
    systemctl restart dnscrypt-proxy
    sleep 3

    if dig @127.0.0.1 -p "${DNS_LOCAL_PORT}" google.com +short +timeout=5 &>/dev/null; then
        success "dnscrypt-proxy отвечает на 127.0.0.1:${DNS_LOCAL_PORT}"
    else
        warn "dnscrypt-proxy не отвечает — проверьте: dig @127.0.0.1 -p ${DNS_LOCAL_PORT} google.com"
    fi
}

configure_iptables() {
    info "Настройка iptables для клиента ${SELECTED_NAME} (${SELECTED_IP})..."

    local CHAIN="SOCKS5_${SELECTED_IP//\./_}"

    # Чистим старые правила
    iptables -t nat -D PREROUTING -s "${SELECTED_IP}/32" -p udp --dport 53 \
        -j DNAT --to-destination "127.0.0.1:${DNS_LOCAL_PORT}" 2>/dev/null || true
    iptables -t nat -D PREROUTING -s "${SELECTED_IP}/32" -p tcp --dport 53 \
        -j DNAT --to-destination "127.0.0.1:${DNS_LOCAL_PORT}" 2>/dev/null || true
    iptables -t nat -D PREROUTING -s "${SELECTED_IP}/32" -j "$CHAIN" 2>/dev/null || true
    iptables -t nat -F "$CHAIN" 2>/dev/null || true
    iptables -t nat -X "$CHAIN" 2>/dev/null || true
    iptables -t filter -D FORWARD -s "${SELECTED_IP}/32" -p udp ! --dport 53 -j REJECT 2>/dev/null || true

    # DNS → dnscrypt-proxy
    iptables -t nat -A PREROUTING -s "${SELECTED_IP}/32" -p udp --dport 53 \
        -j DNAT --to-destination "127.0.0.1:${DNS_LOCAL_PORT}"
    iptables -t nat -A PREROUTING -s "${SELECTED_IP}/32" -p tcp --dport 53 \
        -j DNAT --to-destination "127.0.0.1:${DNS_LOCAL_PORT}"
    success "DNS → dnscrypt-proxy 127.0.0.1:${DNS_LOCAL_PORT}"

    # TCP → redsocks2
    iptables -t nat -N "$CHAIN"
    iptables -t nat -A "$CHAIN" -d "${SOCKS5_HOST_IP}" -j RETURN
    for net in 0.0.0.0/8 10.0.0.0/8 127.0.0.0/8 169.254.0.0/16 \
               172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
        iptables -t nat -A "$CHAIN" -d "$net" -j RETURN
    done
    iptables -t nat -A "$CHAIN" -p tcp -j REDIRECT --to-ports "${REDSOCKS2_PORT}"
    iptables -t nat -A PREROUTING -s "${SELECTED_IP}/32" -j "$CHAIN"
    success "TCP → redsocks2 порт ${REDSOCKS2_PORT}"

    # UDP блокировка — кроме DNS (защита от WebRTC и прочих UDP утечек)
    iptables -t filter -A FORWARD -s "${SELECTED_IP}/32" -p udp ! --dport 53 -j REJECT
    success "UDP заблокирован (кроме DNS)"

    # sysctl
    sysctl -w net.ipv4.conf.all.route_localnet=1 > /dev/null
    sysctl -w "net.ipv4.conf.${AWG_IFACE}.route_localnet=1" > /dev/null 2>&1 || true
    sysctl -w net.ipv4.ip_forward=1 > /dev/null

    for param in \
        "net.ipv4.conf.all.route_localnet=1" \
        "net.ipv4.conf.${AWG_IFACE}.route_localnet=1" \
        "net.ipv4.ip_forward=1"; do
        grep -qF "$param" /etc/sysctl.conf || echo "$param" >> /etc/sysctl.conf
    done

    success "sysctl настроен."
}

save_iptables() {
    info "Сохранение iptables правил..."
    mkdir -p /etc/iptables
    iptables-save > "$IPTABLES_RULES_FILE"
    systemctl enable netfilter-persistent 2>/dev/null || true
    success "Правила сохранены: $IPTABLES_RULES_FILE"
}

save_state() {
    cat > "$STATE_FILE" <<EOF
AWG_CONF=${AWG_CONF}
AWG_IFACE=${AWG_IFACE}
SELECTED_NAME=${SELECTED_NAME}
SELECTED_IP=${SELECTED_IP}
SOCKS5_HOST=${SOCKS5_HOST}
SOCKS5_HOST_IP=${SOCKS5_HOST_IP}
SOCKS5_PORT=${SOCKS5_PORT}
SOCKS5_USER=${SOCKS5_USER}
PROXY_EXTERNAL_IP=${PROXY_EXTERNAL_IP:-unknown}
REDSOCKS2_PORT=${REDSOCKS2_PORT}
DNS_LOCAL_PORT=${DNS_LOCAL_PORT}
EOF
    chmod 600 "$STATE_FILE"
    success "Состояние сохранено: $STATE_FILE"
}

verify_setup() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  Финальная проверка${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

    systemctl is-active --quiet redsocks2 \
        && echo -e "  redsocks2:        ${GREEN}работает${RESET}" \
        || echo -e "  redsocks2:        ${RED}НЕ работает${RESET}"

    systemctl is-active --quiet dnscrypt-proxy \
        && echo -e "  dnscrypt-proxy:   ${GREEN}работает${RESET}" \
        || echo -e "  dnscrypt-proxy:   ${RED}НЕ работает${RESET}"

    local rule_count
    rule_count=$(iptables -t nat -L PREROUTING -n 2>/dev/null | grep -c "${SELECTED_IP}" || true)
    [[ "$rule_count" -gt 0 ]] \
        && echo -e "  iptables:         ${GREEN}настроены (правил для ${SELECTED_IP}: ${rule_count})${RESET}" \
        || echo -e "  iptables:         ${RED}правила не найдены${RESET}"

    dig @127.0.0.1 -p "${DNS_LOCAL_PORT}" google.com +short +timeout=5 &>/dev/null \
        && echo -e "  DNS (dnscrypt):   ${GREEN}работает${RESET}" \
        || echo -e "  DNS (dnscrypt):   ${YELLOW}не отвечает${RESET}"

    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  Итог${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  Клиент:           ${BOLD}${SELECTED_NAME}${RESET} (${SELECTED_IP})"
    echo -e "  SOCKS5 прокси:    ${SOCKS5_HOST}:${SOCKS5_PORT}"
    echo -e "  Внешний IP:       ${PROXY_EXTERNAL_IP:-не определён}"
    echo -e "  DNS:              DoH через dnscrypt-proxy"
    echo -e "  Автостарт:        ${GREEN}включён${RESET}"
    echo ""
    echo -e "${GREEN}${BOLD}  Установка завершена!${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo -e "  Проверка с устройства:"
    echo -e "    http://ifconfig.me  → IP прокси (${PROXY_EXTERNAL_IP:-ожидается})"
    echo -e "    https://ifconfig.me → тот же IP"
    echo -e "    https://dnsleaktest.com → DNS не должен светить IP сервера"
    echo ""
    echo -e "  Диагностика:"
    echo -e "    journalctl -u redsocks2 -f"
    echo -e "    journalctl -u dnscrypt-proxy -f"
    echo -e "    iptables -t nat -L PREROUTING -n -v"
}

preflight_check() {
    echo -e "\n${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  Проверка окружения${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

    local warnings=0

    if [[ -f "$REDSOCKS2_BIN" ]]; then
        warn "redsocks2 уже установлен — будет перезаписан."
        warnings=$((warnings+1))
    fi

    if systemctl is-active --quiet redsocks2 2>/dev/null; then
        warn "Сервис redsocks2 уже запущен — будет перезапущен."
        warnings=$((warnings+1))
    fi

    if [[ -f "$REDSOCKS2_CONF" ]]; then
        warn "Конфиг redsocks2 уже существует — будет перезаписан."
        warnings=$((warnings+1))
    fi

    if ss -tlnp 2>/dev/null | grep -q ":${REDSOCKS2_PORT} "; then
        local owner
        owner=$(ss -tlnp | grep ":${REDSOCKS2_PORT} " | grep -oP 'users:\(\("\K[^"]+' || echo "неизвестно")
        warn "Порт ${REDSOCKS2_PORT} занят: ${owner}"
        warnings=$((warnings+1))
    fi

    if ss -ulnp 2>/dev/null | grep -q ":${DNS_LOCAL_PORT} "; then
        local owner
        owner=$(ss -ulnp | grep ":${DNS_LOCAL_PORT} " | grep -oP 'users:\(\("\K[^"]+' || echo "неизвестно")
        warn "Порт UDP ${DNS_LOCAL_PORT} занят: ${owner}"
        warnings=$((warnings+1))
    fi

    if iptables -t nat -L 2>/dev/null | grep -q "SOCKS5_"; then
        warn "В iptables nat уже есть цепочки SOCKS5_* — будут заменены."
        warnings=$((warnings+1))
    fi

    if [[ -f "$STATE_FILE" ]]; then
        warn "Найден state файл от прошлой установки: $STATE_FILE"
        source "$STATE_FILE" 2>/dev/null || true
        warn "  Прошлый клиент: ${SELECTED_IP:-неизвестно} (${SELECTED_NAME:-?})"
        warn "  Будет перезаписан."
        warnings=$((warnings+1))
        unset SELECTED_IP SELECTED_NAME SELECTED_KEY AWG_CONF AWG_IFACE
        unset SOCKS5_HOST SOCKS5_HOST_IP SOCKS5_PORT SOCKS5_USER SOCKS5_PASS
        unset PROXY_EXTERNAL_IP
    fi

    if [[ $warnings -eq 0 ]]; then
        success "Окружение чистое."
    else
        echo ""
        warn "Предупреждений: ${warnings}. Установка продолжится."
    fi

    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
}

main() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  AWG → SOCKS5 Installer v3.1 (redsocks2 edition)${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""

    preflight_check
    find_awg_config
    parse_clients
    select_client
    get_socks5_params
    check_socks5
    install_packages
    configure_redsocks2
    configure_redsocks2_service
    configure_dnscrypt
    configure_iptables
    save_iptables
    save_state
    verify_setup
}

main "$@"
