#!/bin/bash
# modules/socks5/install.sh — установка инфраструктуры SOCKS5 (redsocks2 + dnscrypt-proxy)
# Вызывается из setup.sh --modules при активации модуля socks5.
#
# Что делает ЭТОТ скрипт: ставит системные компоненты (redsocks2, dnscrypt-proxy).
# Что делает БОТ:         выбирает клиента, вводит параметры прокси, применяет iptables.

set -e
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root"

REDSOCKS2_BIN="/usr/local/bin/redsocks2"
REDSOCKS2_CONF="/etc/redsocks2/redsocks2.conf"
REDSOCKS2_PORT=12345
DNS_LOCAL_PORT=5300
DNSCRYPT_CONF="/etc/dnscrypt-proxy/dnscrypt-proxy.toml"

echo ""
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   SOCKS5 (redsocks2) — Установка        ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Зависимости ───────────────────────────────────────────────────────────────
log "Установка пакетов..."
apt-get update -qq
apt-get install -y -qq \
    git build-essential libevent-dev libssl-dev \
    curl wget iptables iptables-persistent dnscrypt-proxy dnsutils

# ── Сборка redsocks2 ──────────────────────────────────────────────────────────
if [[ -f "$REDSOCKS2_BIN" ]]; then
    warn "redsocks2 уже установлен: $REDSOCKS2_BIN"
else
    log "Клонирование redsocks2 из исходников..."
    rm -rf /tmp/redsocks2-build
    git clone --depth=1 https://github.com/semigodking/redsocks.git /tmp/redsocks2-build \
        || err "Не удалось клонировать redsocks2."
    cd /tmp/redsocks2-build
    log "Сборка redsocks2 (~1-2 минуты)..."
    make -j"$(nproc)" > /dev/null
    [[ -f redsocks2 ]] || err "Сборка redsocks2 не удалась."
    cp redsocks2 "$REDSOCKS2_BIN"
    chmod +x "$REDSOCKS2_BIN"
    cd /
    rm -rf /tmp/redsocks2-build
    log "redsocks2 собран: $REDSOCKS2_BIN"
fi

# ── Директории ────────────────────────────────────────────────────────────────
mkdir -p /etc/awg-socks5 /etc/redsocks2 /etc/iptables

# ── Placeholder конфиг redsocks2 ─────────────────────────────────────────────
# Бот запишет реальные параметры (хост, порт, логин) при первой активации клиента.
if [[ ! -f "$REDSOCKS2_CONF" ]]; then
    cat > "$REDSOCKS2_CONF" << EOF
base {
    log_debug = off;
    log_info = on;
    log = "syslog:daemon";
    daemon = off;
    redirector = iptables;
}

redsocks {
    bind = "0.0.0.0:${REDSOCKS2_PORT}";
    relay = "127.0.0.1:1080";
    type = socks5;
    autoproxy = 0;
    timeout = 10;
}
EOF
    chmod 600 "$REDSOCKS2_CONF"
    info "Создан placeholder конфиг: $REDSOCKS2_CONF"
fi

# ── systemd сервис redsocks2 ──────────────────────────────────────────────────
cat > /etc/systemd/system/redsocks2.service << EOF
[Unit]
Description=Redsocks2 transparent SOCKS5 proxy
After=network.target

[Service]
Type=simple
ExecStart=${REDSOCKS2_BIN} -c ${REDSOCKS2_CONF}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable redsocks2
# Не запускаем — бот запустит при первой активации клиента через меню 🧦 SOCKS5
info "redsocks2 включён в автозагрузку (запустится при активации клиента через бота)"

# ── dnscrypt-proxy ────────────────────────────────────────────────────────────
log "Настройка dnscrypt-proxy (DNS-over-HTTPS)..."
mkdir -p /var/cache/dnscrypt-proxy

cat > "$DNSCRYPT_CONF" << 'TOMLEOF'
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
    log "dnscrypt-proxy отвечает на 127.0.0.1:${DNS_LOCAL_PORT} ✓"
else
    warn "dnscrypt-proxy не отвечает — проверьте: dig @127.0.0.1 -p ${DNS_LOCAL_PORT} google.com"
fi

# ── sysctl ────────────────────────────────────────────────────────────────────
sysctl -w net.ipv4.ip_forward=1 > /dev/null
sysctl -w net.ipv4.conf.all.route_localnet=1 > /dev/null
grep -qF "net.ipv4.ip_forward=1" /etc/sysctl.conf \
    || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
grep -qF "net.ipv4.conf.all.route_localnet=1" /etc/sysctl.conf \
    || echo "net.ipv4.conf.all.route_localnet=1" >> /etc/sysctl.conf

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}${BOLD}SOCKS5 инфраструктура установлена!${NC}"
echo ""
echo -e "  ${CYAN}Что установлено:${NC}"
echo -e "  • redsocks2        — прозрачный SOCKS5 прокси (${REDSOCKS2_BIN})"
echo -e "  • dnscrypt-proxy   — DNS-over-HTTPS на порту ${DNS_LOCAL_PORT}"
echo ""
echo -e "  ${CYAN}Следующий шаг:${NC}"
echo -e "  Откройте бота → 🧦 SOCKS5 прокси → выберите клиента"
echo -e "  Бот запросит адрес SOCKS5 сервера и применит маршрутизацию."
echo ""

# ── Sentinel: модуль установлен ───────────────────────────────────────────────
mkdir -p "/root/modules/socks5"
touch "/root/modules/socks5/.installed"
