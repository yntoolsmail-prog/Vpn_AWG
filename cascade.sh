#!/bin/bash
# Каскад AmneziaWG: российский вход → зарубежные выходы. Подробно — docs/CASCADE.md.
#
# Вход — слейв-копия основного (awg0 с устройствами). Российские адреса он
# выпускает в интернет сам, остальное — через туннели awgxK к выходам. Выход —
# зарубежный сервер (обычно тоже слейв), который принимает туннели от входов и
# выпускает их трафик в интернет. Пользовательские ключи на выход не попадают.
#
# Куда пойдёт устройство, решает адрес в его конфиге (A.B.C — VPN_SUBNET, N — номер устройства):
#   A.B.C.N      «Авто»: устройства поровну между выходами, каждое закреплено за своим
#                (N по модулю числа выходов «Авто»), при отказе его выхода — на живой
#   A.B.(C+K).N  слот K: выход K, при его отказе — как «Авто»
# Нынешние конфиги — это A.B.C.N, то есть «Авто», перевыпускать их не нужно.
#
#   выход:     cascade.sh exit-up --slot K [--host АДРЕС] [--port P]   печатает токен выхода
#              cascade.sh exit-peer 'ТОКЕН_ВХОДА'                      пускает вход
#              cascade.sh exit-unpeer E
#   вход:      cascade.sh entry-add 'ТОКЕН_ВЫХОДА' [--entry E] [--auto N|--no-auto] [--ip АДРЕС]
#              cascade.sh entry-del K
#              cascade.sh auto K N                порядок выхода K в «Авто», 0 — не участвует
#              cascade.sh ru-update               список российских сетей (таймер — раз в сутки)
#   везде:     cascade.sh status | down
#   основной:  cascade.sh client-conf УСТРОЙСТВО АДРЕС_ВХОДА [K] [--qr]
#
# Токены содержат ключ защиты заголовков туннеля — пересылать их только между
# своими серверами.

set -o pipefail
umask 077

AWG_DIR="${CASCADE_AWG_DIR:-/etc/amnezia/amneziawg}"
STATE="$AWG_DIR/cascade"
RUN="${CASCADE_RUN:-/run/awg-cascade}"
VPN_IF="${CASCADE_VPN_IF:-awg0}"
EXIT_IF="${CASCADE_EXIT_IF:-awgx0}"
# Адреса туннелей «вход ↔ выход»: NET.K.1 — выход K, NET.K.(1+E) — вход E
NET="${CASCADE_NET:-10.249}"
# Решение о маршруте хранится в битах 16–23 метки пакета и соединения.
# Младшие биты не трогаем: там метка 51820 (0xca6c) awg-quick и чужих сервисов
MASK=0xff0000
MARK_DIRECT=0x10000     # российский адрес — напрямую
MARK_ALLDOWN=0xff0000   # напрямую, потому что все выходы мертвы: сбрасывается, когда выход оживёт
TABLE_BASE=2000
PRIO=10100
INTERVAL="${CASCADE_INTERVAL:-10}"
DOWN_AFTER=3            # подряд неудачных проверок до «выход упал» (~30 с)
UP_AFTER=2              # подряд удачных до «выход поднялся»
MIN_RU=2000             # меньше записей — список битый, старый не трогаем
# Все выходы мертвы: direct — выпускать с российского IP входа (интернет есть,
# зарубежное заблокированное не откроется), drop — без интернета
ALL_DOWN="${CASCADE_ALL_DOWN:-direct}"
SELF="$(readlink -f "$0")"

# Порядок как в AWG_PARAMS (awg_clients.py). I1–I5 туннелю не нужны: это
# маскировка первого пакета для мобильных сетей
OBFS_ORDER=(Jc Jmin Jmax S1 S2 S3 S4 H1 H2 H3 H4 HeaderProtectionKey ContentPaddingAddition
            RekeyAfterTime RekeyTimeout RejectAfterTime KeepaliveTimeout MaxHandshakeAttempts
            RandomTrailers DisableCookies)
PRIVATE_NETS=(0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16
              172.16.0.0/12 192.168.0.0/16 224.0.0.0/3)

say()  { echo "$*"; }
warn() { echo "⚠️  $*" >&2; }
die()  { echo "❌ $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
need_root() { [[ $EUID -eq 0 ]] || die "Запускать от root"; }
rnd() { shuf -i "$1-$2" -n 1; }
hexmark() { printf '0x%x' "$1"; }
mark_of() { hexmark $(( ($1 + 1) << 16 )); }

# Юниты systemd — только в боевом каталоге: awg-quick@ ищет конфиг в нём.
# Иначе (CASCADE_AWG_DIR, проверка в песочнице) — awg-quick с путём к файлу
use_systemd() {
    [[ -z "${CASCADE_NO_SYSTEMD:-}" && "$AWG_DIR" == /etc/amnezia/amneziawg && -d /run/systemd/system ]] && have systemctl
}

ensure_pkgs() {
    local miss=() p
    for p in "$@"; do have "$p" || miss+=("$p"); done
    [[ ${#miss[@]} -eq 0 ]] && return 0
    have apt-get || die "Нет ${miss[*]}, а apt-get недоступен"
    say "Ставлю: ${miss[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${miss[@]}" >/dev/null 2>&1 \
        || { apt-get update -qq >/dev/null 2>&1
             DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${miss[@]}" >/dev/null 2>&1; } \
        || die "Не удалось поставить: ${miss[*]}"
}

wan_if() {
    ip -4 route show default 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}'
}

# Внешний адрес — снаружи: в облаках (Яндекс, VK) у машины внутренний адрес,
# а наружу она ходит через 1:1 NAT, и выход увидит именно внешний
public_ip() {
    local ip
    ip=$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null)
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
    echo "$ip"
}

# Значение ключа из файла KEY=VALUE (без source: файл разбирается, а не исполняется)
env_get() { [[ -f "$1" ]] && awk -F= -v k="$2" '$1 == k { sub(/^[^=]*=/, ""); gsub(/^["'"'"']|["'"'"']$/, ""); print; exit }' "$1"; }

# Значение параметра из секции [Interface] конфига AWG
conf_get() {
    [[ -f "$1" ]] && awk -v k="$(tr '[:upper:]' '[:lower:]' <<<"$2")" '
        /^\[/ { sec = tolower($0); next }
        sec == "[interface]" && index($0, "=") {
            key = $0; sub(/[ \t]*=.*/, "", key); gsub(/^[ \t]+/, "", key)
            if (tolower(key) == k) { v = $0; sub(/^[^=]*=[ \t]*/, "", v); print v; exit }
        }' "$1"
}

# Подсеть устройств A.B.C. На слейве — из awg0.conf: это копия основного, а
# server.env там от собственной установки слейва
vpn_subnet() {
    local s=""
    s=$(conf_get "$AWG_DIR/$VPN_IF.conf" Address | cut -d/ -f1 | cut -d. -f1-3)
    [[ -z "$s" ]] && s=$(env_get "$AWG_DIR/server.env" VPN_SUBNET)
    [[ "$s" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    echo "$s"
}

# ── Обфускация туннеля ────────────────────────────────────────────────────────
# Свой набор, не клиентский: смена параметров клиентов не трогает туннели, и
# наоборот. Диапазоны — как в gen_awg31_env() (awg_clients.py)
awg_flavor() {
    if grep -qiE '^[[:space:]]*HeaderProtectionKey[[:space:]]*=' "$AWG_DIR/$VPN_IF.conf" 2>/dev/null; then
        echo 31
    elif [[ -f "$AWG_DIR/$VPN_IF.conf" ]]; then
        echo 20
    elif awg --version 2>/dev/null | grep -qE 'v3\.'; then
        echo 31
    else
        echo 20
    fi
}

gen_obfs() {
    local v=$1 i=1 h
    echo "Jc=$(rnd 4 6)"
    echo "Jmin=$(rnd 10 50)"
    echo "Jmax=$(rnd 51 100)"
    echo "S1=$(rnd 15 64)"
    echo "S2=$(rnd 15 64)"
    if [[ $v == 31 ]]; then
        echo "S3=$(rnd 12 32)"
        echo "S4=$(rnd 12 20)"
    fi
    for h in $(shuf -i 5-4294967295 -n 4); do echo "H$i=$h"; i=$((i + 1)); done
    if [[ $v == 31 ]]; then
        echo "HeaderProtectionKey=$(awg genkey)"
        echo "ContentPaddingAddition=10-100"
        echo "RekeyAfterTime=100-120"
        echo "RekeyTimeout=3-7"
        echo "RejectAfterTime=150-180"
        echo "KeepaliveTimeout=5-15"
        echo "MaxHandshakeAttempts=15-20"
        echo "RandomTrailers=on"
        echo "DisableCookies=on"
    fi
}

# ── Токены ────────────────────────────────────────────────────────────────────
# Строки KEY=VALUE в base64 одной строкой: копируется целиком, без переносов.
# При разборе каждое поле проверяется по своему шаблону — токен вставляет
# человек, и в конфиг из него не должно попасть ничего лишнего
# Последняя строка — контрольная сумма: обрезанный при копировании токен иначе
# разобрался бы без части параметров обфускации, и туннель молча не поднялся бы
token_sum() { printf '%s\n' "$@" | md5sum | cut -c1-8; }
token_encode() { printf 'AWGX1:%s\n' "$(printf '%s\n' "$@" "sum=$(token_sum "$@")" | base64 -w0)"; }

valid_kv() {
    case $1 in
        v)          [[ $2 == 1 ]] ;;
        role)       [[ $2 == exit || $2 == entry ]] ;;
        slot|entry) [[ $2 =~ ^[0-9]{1,3}$ ]] ;;
        port)       [[ $2 =~ ^[0-9]{1,5}$ ]] ;;
        host)       [[ $2 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,252})$ ]] ;;
        ip)         [[ $2 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] ;;
        net)        [[ $2 =~ ^[0-9]{1,3}\.[0-9]{1,3}$ ]] ;;
        pub|HeaderProtectionKey) [[ $2 =~ ^[A-Za-z0-9+/]{43}=$ ]] ;;
        RandomTrailers|DisableCookies) [[ $2 == on || $2 == off ]] ;;
        Jc|Jmin|Jmax|S1|S2|S3|S4|H1|H2|H3|H4|ContentPaddingAddition|RekeyAfterTime|RekeyTimeout|RejectAfterTime|KeepaliveTimeout|MaxHandshakeAttempts)
                    [[ $2 =~ ^[0-9]{1,10}(-[0-9]{1,10})?$ ]] ;;
        *)          return 1 ;;
    esac
}

declare -A T
load_token() {
    local t="${1//[[:space:]]/}" line k v raw body=() sum=""
    T=()
    [[ "$t" == AWGX1:* ]] || die "Это не токен каскада: он начинается с AWGX1:"
    raw=$(printf '%s' "${t#AWGX1:}" | base64 -d 2>/dev/null) \
        || die "Токен повреждён — скопируйте строку целиком"
    while IFS= read -r line; do
        [[ "$line" == sum=* ]] && { sum=${line#sum=}; continue; }
        body+=("$line")
    done <<<"$raw"
    [[ -n "$sum" && "$sum" == "$(token_sum "${body[@]}")" ]] \
        || die "Токен неполный или повреждён — скопируйте строку целиком"
    for line in "${body[@]}"; do
        [[ "$line" == *=* ]] || continue
        k=${line%%=*}; v=${line#*=}
        valid_kv "$k" "$v" || die "В токене недопустимое поле: ${k:0:20}"
        T[$k]=$v
    done
    [[ -n "${T[role]:-}" && -n "${T[slot]:-}" && -n "${T[pub]:-}" ]] || die "Токен неполный"
}

# ── Интерфейсы ────────────────────────────────────────────────────────────────
iface_up() {
    if use_systemd; then
        # Как перезапуск awg0 в проекте: stop гасит поднятый службой, down —
        # поднятый вручную, иначе start падает на «already exists»
        systemctl stop "awg-quick@$1" >/dev/null 2>&1
        ip link show "$1" >/dev/null 2>&1 && awg-quick down "$1" >/dev/null 2>&1
        systemctl enable --now "awg-quick@$1" >/dev/null 2>&1 \
            || die "awg-quick@$1 не поднялся: journalctl -u awg-quick@$1 -n 20"
    else
        awg-quick down "$AWG_DIR/$1.conf" >/dev/null 2>&1
        awg-quick up "$AWG_DIR/$1.conf" >/dev/null 2>"$RUN/awg-quick.err" \
            || die "awg-quick up $1 не прошёл: $(tail -n 2 "$RUN/awg-quick.err")"
    fi
}

iface_down() {
    if use_systemd; then
        systemctl disable --now "awg-quick@$1" >/dev/null 2>&1
    fi
    ip link show "$1" >/dev/null 2>&1 && awg-quick down "$AWG_DIR/$1.conf" >/dev/null 2>&1
    ip link del "$1" >/dev/null 2>&1
    return 0
}

# Вырезает из конфига выхода блок входа E: от «# Entry: E [IP]» до его AllowedIPs
conf_drop_entry() {
    local f=$1 e=$2
    awk -v e="$e" '
        $1 == "#" && $2 == "Entry:" && $3 == e { skip = 1; next }
        skip { if ($0 ~ /^AllowedIPs/) skip = 0; next }
        { print }' "$f" | cat -s > "$f.tmp" && mv "$f.tmp" "$f" && chmod 600 "$f"
}

# ══ Выход ═════════════════════════════════════════════════════════════════════

exit_slot() { env_get "$STATE/exit.env" SLOT; }
exit_port() { conf_get "$AWG_DIR/$EXIT_IF.conf" ListenPort; }

free_port() {
    local p used own
    used=$(ss -Hlun 2>/dev/null | awk '{n = split($4, a, ":"); print a[n]}')
    own=$(conf_get "$AWG_DIR/$VPN_IF.conf" ListenPort)
    for _ in $(seq 100); do
        p=$(rnd 30000 49999)
        [[ "$p" == "$own" ]] && continue
        grep -qx "$p" <<<"$used" || { echo "$p"; return 0; }
    done
    die "Не нашёл свободный UDP-порт"
}

print_exit_token() {
    local conf="$AWG_DIR/$EXIT_IF.conf" host=$1 key val
    [[ -n "$host" ]] || host=$(env_get "$STATE/exit.env" HOST)
    local lines=(v=1 role=exit "slot=$(exit_slot)" "host=$host" "port=$(exit_port)"
                 "pub=$(awg pubkey <<<"$(conf_get "$conf" PrivateKey)")" "net=$NET")
    for key in "${OBFS_ORDER[@]}"; do
        val=$(conf_get "$conf" "$key")
        [[ -n "$val" ]] && lines+=("$key=$val")
    done
    echo
    echo "Выполните на ВХОДЕ эту команду (строку копировать целиком, токен в ней — ключ туннеля):"
    echo
    echo "bash /root/cascade.sh entry-add '$(token_encode "${lines[@]}")'"
    echo
}

cmd_exit_up() {
    local slot="" host="" port="" ver=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --slot)  slot=$2; shift 2 ;;
            --host)  host=$2; shift 2 ;;
            --port)  port=$2; shift 2 ;;
            --awg20) ver=20; shift ;;
            --awg31) ver=31; shift ;;
            *) die "Неизвестный параметр: $1" ;;
        esac
    done
    [[ "$slot" =~ ^[0-9]+$ && $slot -ge 1 && $slot -le 250 ]] \
        || die "Нужен --slot K (1–250): номер выхода, одинаковый для всех входов"
    [[ -z "$host" ]] || valid_kv host "$host" || die "Неверный --host"
    local conf="$AWG_DIR/$EXIT_IF.conf"
    mkdir -p "$STATE" "$RUN"

    if [[ -f "$conf" ]]; then
        [[ "$(exit_slot)" == "$slot" ]] \
            || die "Выход уже поднят со слотом $(exit_slot). Сменить: cascade.sh down, затем заново"
        say "Выход уже настроен — печатаю его токен заново."
        ip link show "$EXIT_IF" >/dev/null 2>&1 || iface_up "$EXIT_IF"
        [[ -n "$host" ]] && sed -i "s/^HOST=.*/HOST=$host/" "$STATE/exit.env"
        print_exit_token "$host"
        return 0
    fi

    ensure_pkgs ipset curl
    local wan; wan=$(wan_if)
    [[ -n "$wan" ]] || die "Не нашёл внешний интерфейс: нет маршрута по умолчанию"
    [[ -n "$port" ]] || port=$(free_port)
    [[ "$port" =~ ^[0-9]+$ ]] || die "Неверный --port"
    [[ -n "$ver" ]] || ver=$(awg_flavor)
    [[ -n "$host" ]] || host=$(public_ip)
    valid_kv host "$host" || die "Не определил внешний адрес — укажите --host"

    local priv net3="$NET.$slot" entries="$STATE/entries.ipset"
    priv=$(awg genkey)
    touch "$entries"
    # Порт туннеля — только для адресов входов (набор cascade_entries): правила
    # встают первыми в INPUT, поэтому работают и при ufw, и без него
    local up="sysctl -qw net.ipv4.ip_forward=1"
    up+="; iptables -I FORWARD 1 -i %i -o $wan -j ACCEPT"
    up+="; iptables -I FORWARD 1 -i $wan -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    up+="; iptables -t nat -I POSTROUTING 1 -s $net3.0/24 -o $wan -j MASQUERADE"
    up+="; ipset create -exist cascade_entries hash:ip; ipset restore -exist -file $entries"
    up+="; iptables -I INPUT 1 -p udp --dport $port -j DROP"
    up+="; iptables -I INPUT 1 -p udp --dport $port -m set --match-set cascade_entries src -j ACCEPT"
    local down="iptables -D FORWARD -i %i -o $wan -j ACCEPT"
    down+="; iptables -D FORWARD -i $wan -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    down+="; iptables -t nat -D POSTROUTING -s $net3.0/24 -o $wan -j MASQUERADE"
    down+="; iptables -D INPUT -p udp --dport $port -m set --match-set cascade_entries src -j ACCEPT"
    down+="; iptables -D INPUT -p udp --dport $port -j DROP"
    down+="; ipset destroy cascade_entries"
    {
        echo "[Interface]"
        echo "PrivateKey = $priv"
        echo "Address = $net3.1/24"
        echo "ListenPort = $port"
        echo "MTU = 1420"
        gen_obfs "$ver" | sed 's/=/ = /'
        echo "PostUp = $up"
        echo "PostDown = $down"
    } > "$conf"
    chmod 600 "$conf"
    printf 'SLOT=%s\nHOST=%s\n' "$slot" "$host" > "$STATE/exit.env"
    iface_up "$EXIT_IF"
    say "✅ Выход поднят: слот $slot, $host, UDP $port (AWG ${ver:0:1}.${ver:1})."
    print_exit_token "$host"
}

cmd_exit_peer() {
    [[ -n "${1:-}" ]] || die "Нужен токен входа: cascade.sh exit-peer 'ТОКЕН'"
    load_token "$1"
    [[ ${T[role]} == entry ]] || die "Это токен выхода, а нужен токен ВХОДА (его печатает entry-add)"
    local conf="$AWG_DIR/$EXIT_IF.conf" slot
    [[ -f "$conf" ]] || die "Выход не поднят: сначала cascade.sh exit-up"
    slot=$(exit_slot)
    [[ "${T[slot]}" == "$slot" ]] || die "Токен для слота ${T[slot]}, а этот выход — слот $slot"
    local e=${T[entry]} ip=${T[ip]:-}
    [[ $e -ge 1 && $e -le 200 ]] || die "Неверный номер входа: $e"
    local addr="$NET.$slot.$((1 + e))/32"

    # Повторный exit-peer того же входа (новый ключ или адрес) заменяет его блок
    local oldip
    oldip=$(awk -v e="$e" '$1 == "#" && $2 == "Entry:" && $3 == e {print $4; exit}' "$conf")
    conf_drop_entry "$conf" "$e"
    printf '\n# Entry: %s %s\n[Peer]\nPublicKey = %s\nAllowedIPs = %s\n' "$e" "$ip" "${T[pub]}" "$addr" >> "$conf"

    local old
    old=$(awg show "$EXIT_IF" allowed-ips 2>/dev/null | awk -v a="$addr" '$2 == a {print $1}')
    [[ -n "$old" && "$old" != "${T[pub]}" ]] && awg set "$EXIT_IF" peer "$old" remove
    awg set "$EXIT_IF" peer "${T[pub]}" allowed-ips "$addr" || die "Не удалось добавить вход на интерфейс"

    local entries="$STATE/entries.ipset"
    if [[ -n "$oldip" && "$oldip" != "$ip" ]]; then
        sed -i "/^add cascade_entries ${oldip//./\\.}$/d" "$entries"
        ipset del -exist cascade_entries "$oldip" 2>/dev/null
    fi
    if [[ -n "$ip" ]]; then
        { grep -vxF "add cascade_entries $ip" "$entries" 2>/dev/null; echo "add cascade_entries $ip"; } > "$entries.tmp"
        mv "$entries.tmp" "$entries"
        ipset add -exist cascade_entries "$ip"
    else
        warn "В токене нет адреса входа — порт туннеля для него закрыт. Перевыпустите токен на входе с --ip"
    fi
    say "✅ Вход $e ($ip) пущен на выход (слот $slot)."
}

cmd_exit_unpeer() {
    local e=${1:-} conf="$AWG_DIR/$EXIT_IF.conf" slot pub ip
    [[ "$e" =~ ^[0-9]+$ ]] || die "Нужен номер входа: cascade.sh exit-unpeer E"
    [[ -f "$conf" ]] || die "Выход не поднят"
    slot=$(exit_slot)
    ip=$(awk -v e="$e" '$1 == "#" && $2 == "Entry:" && $3 == e {print $4; exit}' "$conf")
    pub=$(awg show "$EXIT_IF" allowed-ips 2>/dev/null | awk -v a="$NET.$slot.$((1 + e))/32" '$2 == a {print $1}')
    [[ -n "$pub" ]] && awg set "$EXIT_IF" peer "$pub" remove
    conf_drop_entry "$conf" "$e"
    if [[ -n "$ip" ]]; then
        sed -i "/^add cascade_entries ${ip//./\\.}$/d" "$STATE/entries.ipset"
        ipset del -exist cascade_entries "$ip" 2>/dev/null
    fi
    say "✅ Вход $e убран с выхода."
}

# ══ Вход ══════════════════════════════════════════════════════════════════════

declare -a EXITS=() AUTO_LIST=() SLOTS=()
declare -A AUTO=()
SA="" SB="" SC=""

# SLOTS — все слоты, которые вход когда-либо обслуживал (файл slots) плюс
# нынешние выходы. Адреса слотов остаются у устройств и после entry-del: иначе
# AWG отбрасывал бы пакеты конфигов этого слота, а так они уходят в «Авто»
load_exits() {
    EXITS=(); AUTO_LIST=(); AUTO=(); SLOTS=()
    local f k
    for f in "$STATE"/exits/*.env; do
        [[ -f "$f" ]] || continue
        k=$(env_get "$f" SLOT)
        [[ "$k" =~ ^[0-9]+$ ]] || continue
        EXITS+=("$k")
        AUTO[$k]=$(env_get "$f" AUTO)
        [[ "${AUTO[$k]}" =~ ^[0-9]+$ ]] || AUTO[$k]=0
    done
    mapfile -t SLOTS < <({ printf '%s\n' "${EXITS[@]}"; cat "$STATE/slots" 2>/dev/null; } \
        | grep -E '^[0-9]+$' | sort -nu)
    [[ ${#EXITS[@]} -gt 0 ]] || return 0
    mapfile -t EXITS < <(printf '%s\n' "${EXITS[@]}" | sort -n)
    mapfile -t AUTO_LIST < <(for k in "${EXITS[@]}"; do
        [[ ${AUTO[$k]} -gt 0 ]] && echo "${AUTO[$k]} $k"
    done | sort -n -k1,1 -k2,2 | awk '{print $2}')
}

load_subnet() {
    local base
    base=$(vpn_subnet) || return 1
    IFS=. read -r SA SB SC <<<"$base"
}

entry_conf() { echo "$AWG_DIR/awgx$1.conf"; }

cmd_entry_add() {
    local tok="" e=1 auto="" myip=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --entry)   e=$2; shift 2 ;;
            --auto)    auto=$2; shift 2 ;;
            --no-auto) auto=0; shift ;;
            --ip)      myip=$2; shift 2 ;;
            AWGX1:*)   tok=$1; shift ;;
            *) die "Неизвестный параметр: ${1:0:30}" ;;
        esac
    done
    [[ -n "$tok" ]] || die "Нужен токен выхода: cascade.sh entry-add 'ТОКЕН'"
    load_token "$tok"
    [[ ${T[role]} == exit ]] || die "Это токен входа, а нужен токен ВЫХОДА (его печатает exit-up)"
    [[ "$e" =~ ^[0-9]+$ && $e -ge 1 && $e -le 200 ]] || die "Неверный --entry (1–200)"
    [[ -z "$auto" || "$auto" =~ ^[0-9]+$ ]] || die "Неверный --auto"
    [[ -z "$myip" ]] || valid_kv ip "$myip" || die "Неверный --ip"
    ip link show "$VPN_IF" >/dev/null 2>&1 \
        || die "Нет интерфейса $VPN_IF. Вход — это слейв с устройствами: сначала добавьте сервер в боте"
    [[ "${T[net]}" == "$NET" ]] || die "Сеть туннелей у выхода ${T[net]}, а здесь $NET"
    load_subnet || die "Не нашёл подсеть устройств (Address в $VPN_IF.conf)"
    [[ "$SA.$SB" != "$NET" ]] || die "Подсеть устройств пересекается с сетью туннелей $NET"

    local k=${T[slot]}
    [[ $((SC + k)) -le 255 ]] || die "Слот $k не помещается в подсеть $SA.$SB.$SC (третий октет > 255)"
    # Повторное добавление без --auto сохраняет прежний порядок (cascade.sh auto)
    [[ -n "$auto" ]] || auto=$(env_get "$STATE/exits/$k.env" AUTO)
    [[ "$auto" =~ ^[0-9]+$ ]] || auto=$k
    mkdir -p "$STATE/exits" "$RUN"
    ensure_pkgs ipset conntrack curl

    local ifc="awgx$k" conf priv
    conf=$(entry_conf "$k")
    # Ключ входа сохраняется при повторном добавлении — выход уже его знает
    priv=$(conf_get "$conf" PrivateKey)
    [[ -n "$priv" ]] || priv=$(awg genkey)
    {
        echo "[Interface]"
        echo "PrivateKey = $priv"
        echo "Address = $NET.$k.$((1 + e))/24"
        echo "MTU = 1420"
        # Своя таблица: awg-quick кладёт туда маршрут по умолчанию через туннель,
        # а основная таблица входа не меняется
        echo "Table = $((TABLE_BASE + k))"
        local key
        for key in "${OBFS_ORDER[@]}"; do
            [[ -n "${T[$key]:-}" ]] && echo "$key = ${T[$key]}"
        done
        # Ответы из интернета приходят через туннель, а маршрут к их адресам —
        # через внешний интерфейс: строгий rp_filter их бы отбрасывал
        echo "PostUp = sysctl -qw net.ipv4.conf.%i.rp_filter=0"
        echo
        echo "[Peer]"
        echo "PublicKey = ${T[pub]}"
        echo "Endpoint = ${T[host]}:${T[port]}"
        echo "AllowedIPs = 0.0.0.0/0"
        echo "PersistentKeepalive = 25"
    } > "$conf"
    chmod 600 "$conf"
    printf 'SLOT=%s\nAUTO=%s\nENTRY=%s\nHOST=%s\n' "$k" "$auto" "$e" "${T[host]}" > "$STATE/exits/$k.env"
    grep -qx "$k" "$STATE/slots" 2>/dev/null || echo "$k" >> "$STATE/slots"

    sysctl -qw net.ipv4.ip_forward=1
    if [[ "$(sysctl -n net.ipv4.conf.all.rp_filter 2>/dev/null)" == 1 ]]; then
        # Действует максимум из all и интерфейса: при all=1 строгий режим не выключить
        sysctl -qw net.ipv4.conf.all.rp_filter=2
        echo "net.ipv4.conf.all.rp_filter = 2" > /etc/sysctl.d/90-awg-cascade.conf 2>/dev/null
    fi
    iface_up "$ifc"
    [[ -s "$STATE/ru.zone" ]] || cmd_ru_update || warn "Список российских сетей не скачался — пока всё пойдёт через выходы"
    install_units

    [[ -n "$myip" ]] || myip=$(public_ip)
    valid_kv ip "$myip" || die "Не определил внешний адрес входа — укажите --ip"
    say "✅ Туннель к выходу $k (${T[host]}) поднят. «Авто»: $([[ $auto -gt 0 ]] && echo "порядок $auto" || echo "не участвует")."
    echo
    echo "Выполните на ВЫХОДЕ $k эту команду (строку копировать целиком):"
    echo
    echo "bash /root/cascade.sh exit-peer '$(token_encode v=1 role=entry "slot=$k" "entry=$e" "pub=$(awg pubkey <<<"$priv")" "ip=$myip")'"
    echo
}

cmd_entry_del() {
    local k=${1:-} forget=0
    [[ "${2:-}" == --forget ]] && forget=1
    [[ "$k" =~ ^[0-9]+$ ]] || die "Нужен номер выхода: cascade.sh entry-del K [--forget]"
    if [[ -f "$(entry_conf "$k")" ]]; then
        local e; e=$(env_get "$STATE/exits/$k.env" ENTRY)
        iface_down "awgx$k"
        rm -f "$(entry_conf "$k")" "$STATE/exits/$k.env"
        say "✅ Выход $k убран со входа, маршруты обновятся в течение ${INTERVAL} с."
        say "   На самом выходе: bash /root/cascade.sh exit-unpeer ${e:-1}"
    elif [[ $forget == 0 ]]; then
        die "Выхода $k на этом входе нет"
    fi
    if [[ $forget == 1 ]]; then
        sed -i "/^$k\$/d" "$STATE/slots" 2>/dev/null
        say "   Слот $k забыт: конфиги этого слота на входе больше не работают."
    else
        say "   Конфиги слота $k продолжают работать через «Авто». Поставить в слот другой"
        say "   сервер — exit-up --slot $k на нём и entry-add с его токеном здесь."
    fi
}

cmd_auto() {
    local k=${1:-} n=${2:-}
    [[ "$k" =~ ^[0-9]+$ && "$n" =~ ^[0-9]+$ ]] || die "cascade.sh auto K N  (N — порядок в «Авто», 0 — не участвует)"
    local f="$STATE/exits/$k.env"
    [[ -f "$f" ]] || die "Выхода $k на этом входе нет"
    sed -i "s/^AUTO=.*/AUTO=$n/" "$f"
    say "✅ Выход $k: $([[ $n -gt 0 ]] && echo "порядок в «Авто» $n" || echo "в «Авто» не участвует"). Применится в течение ${INTERVAL} с."
}

# ── Список российских сетей ──────────────────────────────────────────────────
# Основной источник — делегирование RIPE (Россия только у RIPE), с проверкой md5.
# Запасной — ipdeny. Неполный список не заменяет прежний
ripe_to_cidr() {
    awk -F'|' '
        function ip2n(s,  a) { split(s, a, "."); return ((a[1] * 256 + a[2]) * 256 + a[3]) * 256 + a[4] }
        function n2ip(n) { return int(n / 16777216) % 256 "." int(n / 65536) % 256 "." int(n / 256) % 256 "." n % 256 }
        $2 == "RU" && $3 == "ipv4" && ($7 == "allocated" || $7 == "assigned") {
            start = ip2n($4); cnt = $5 + 0
            while (cnt > 0) {
                b = 1
                while (b * 2 <= cnt && start % (b * 2) == 0) b *= 2
                pl = 32; t = b; while (t > 1) { t /= 2; pl-- }
                print n2ip(start) "/" pl
                start += b; cnt -= b
            }
        }'
}

cmd_ru_update() {
    mkdir -p "$STATE" "$RUN"
    local tmp="$RUN/ru.new" raw="$RUN/ru.raw" src="" n=0 sum want
    : > "$tmp"
    if curl -fsS --max-time 180 -o "$raw" https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest 2>/dev/null; then
        want=$(curl -fsS --max-time 30 https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest.md5 2>/dev/null | grep -oE '[0-9a-f]{32}' | head -1)
        sum=$(md5sum < "$raw" | cut -c1-32)
        if [[ -n "$want" && "$sum" != "$want" ]]; then
            warn "RIPE: файл скачался не целиком (md5 не сошёлся)"
        else
            ripe_to_cidr < "$raw" > "$tmp"; src="RIPE"
        fi
    fi
    rm -f "$raw"
    n=$(awk 'END{print NR}' "$tmp")
    if [[ $n -lt $MIN_RU ]]; then
        curl -fsS --max-time 120 https://www.ipdeny.com/ipblocks/data/aggregated/ru-aggregated.zone 2>/dev/null \
            | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' > "$tmp"
        src="ipdeny"
        n=$(awk 'END{print NR}' "$tmp")
    fi
    if [[ $n -lt $MIN_RU ]]; then
        rm -f "$tmp"
        warn "Список российских сетей не обновлён: получено $n записей (нужно от $MIN_RU)"
        return 1
    fi
    chmod 644 "$tmp"
    mv "$tmp" "$STATE/ru.zone"
    say "✅ Список российских сетей: $n записей ($src). Служба подхватит его в течение ${INTERVAL} с."
}

# ── Применение правил (вызывает служба) ──────────────────────────────────────

load_set_from_file() {   # $1 набор, $2 файл со списком сетей (# — комментарии)
    local set=$1 file=$2
    # Проверка формата — grep, не awk: mawk (awk по умолчанию в Ubuntu) не знает {n,m}
    {
        echo "create ${set}_new hash:net family inet hashsize 16384 maxelem 262144 -exist"
        echo "flush ${set}_new"
        [[ -f "$file" ]] && sed 's/#.*//; s/[[:space:]]//g' "$file" \
            | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$' | sed "s/^/add ${set}_new /"
    } | ipset restore -exist && ipset swap "${set}_new" "$set" && ipset destroy "${set}_new"
}

# Наборы «Авто»: в cascade_auto_i — адреса устройств с N mod n == i, во всех
# подсетях (основной и слотов): при отказе слота устройство уходит в свою корзину
build_auto_sets() {
    local n=${#AUTO_LIST[@]} i k ks="0"
    for k in "${SLOTS[@]}"; do ks+=" $k"; done
    for ((i = 0; i < n; i++)); do
        {
            echo "create cascade_auto_${i}_new hash:ip family inet -exist"
            echo "flush cascade_auto_${i}_new"
            awk -v a="$SA" -v b="$SB" -v c="$SC" -v ks="$ks" -v n="$n" -v i="$i" -v s="cascade_auto_${i}_new" '
                BEGIN { m = split(ks, K, " ")
                        for (x = 2; x <= 254; x++) if (x % n == i) for (j = 1; j <= m; j++)
                            print "add " s " " a "." b "." (c + K[j]) "." x }'
        } | ipset restore -exist
        ipset create -exist "cascade_auto_$i" hash:ip family inet
        ipset swap "cascade_auto_${i}_new" "cascade_auto_$i"
        ipset destroy "cascade_auto_${i}_new"
    done
}

drop_extra_auto_sets() {
    local n=${#AUTO_LIST[@]} s
    for s in $(ipset list -n 2>/dev/null | grep -E '^cascade_auto_[0-9]+$'); do
        [[ ${s#cascade_auto_} -ge $n ]] && ipset destroy "$s" 2>/dev/null
    done
}

ensure_chain() {   # $1 таблица, $2 цепочка, $3 базовая цепочка
    iptables -t "$1" -N "$2" 2>/dev/null
    iptables -t "$1" -C "$3" -j "$2" 2>/dev/null || iptables -t "$1" -I "$3" 1 -j "$2"
}

declare -A ST=() OKC=() FAILC=() RTT=() SINCE=() HS=()
alive() { [[ "${ST[$1]:-unknown}" != down ]]; }   # не проверенный ещё считается живым

# Решение для новых соединений: российское — напрямую; слот — в свой выход, если
# он жив; иначе корзина «Авто» устройства; корзина без живого выхода — на живой
# по порядку. Правило срабатывает, только пока метка не поставлена (mark 0)
write_decide() {
    local r=() k i t d alive_auto=() fallback=""
    for k in "${AUTO_LIST[@]}"; do alive "$k" && alive_auto+=("$k"); done
    if [[ ${#alive_auto[@]} -gt 0 ]]; then
        fallback=$(mark_of "${alive_auto[0]}")
    elif [[ $ALL_DOWN == direct ]]; then
        fallback=$MARK_ALLDOWN
    fi
    r+=("-F CASCADE_DECIDE")
    for d in "${PRIVATE_NETS[@]}"; do r+=("-A CASCADE_DECIDE -d $d -j RETURN"); done
    r+=("-A CASCADE_DECIDE -m set --match-set cascade_ru dst -m set ! --match-set cascade_force dst -j MARK --set-xmark $MARK_DIRECT/$MASK")
    for k in "${EXITS[@]}"; do
        alive "$k" && r+=("-A CASCADE_DECIDE -m mark --mark 0/$MASK -s $SA.$SB.$((SC + k)).0/24 -j MARK --set-xmark $(mark_of "$k")/$MASK")
    done
    for ((i = 0; i < ${#AUTO_LIST[@]}; i++)); do
        k=${AUTO_LIST[$i]}
        if alive "$k"; then
            t=$(mark_of "$k")
        elif [[ ${#alive_auto[@]} -gt 0 ]]; then
            t=$(mark_of "${alive_auto[$((i % ${#alive_auto[@]}))]}")
        else
            t=$fallback
        fi
        [[ -n "$t" ]] && r+=("-A CASCADE_DECIDE -m mark --mark 0/$MASK -m set --match-set cascade_auto_$i src -j MARK --set-xmark $t/$MASK")
    done
    if [[ -n "$fallback" ]]; then
        r+=("-A CASCADE_DECIDE -m mark --mark 0/$MASK -j MARK --set-xmark $fallback/$MASK")
    else
        r+=("-A CASCADE_DECIDE -m mark --mark 0/$MASK -j DROP")
    fi
    { echo "*mangle"; printf '%s\n' "${r[@]}"; echo "COMMIT"; } | iptables-restore --noflush
}

sync_ip_rules() {
    local want=() k prio line cur
    want+=("$PRIO|fwmark $MARK_DIRECT/$MASK lookup main")
    for k in "${EXITS[@]}"; do want+=("$((PRIO + k))|fwmark $(mark_of "$k")/$MASK lookup $((TABLE_BASE + k))"); done
    want+=("$((PRIO + 255))|fwmark $MARK_ALLDOWN/$MASK lookup main")
    cur=$(ip -4 rule show | awk -F':[ \t]*' -v lo="$PRIO" -v hi="$((PRIO + 255))" \
        '$1 + 0 >= lo && $1 + 0 <= hi { sub(/^from all /, "", $2); print $1 "|" $2 }')
    # Лишние и изменившиеся — убрать, недостающие — добавить; совпадающие не трогаем,
    # чтобы не было мгновения без правила
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        printf '%s\n' "${want[@]}" | grep -qxF "$line" || ip -4 rule del prio "${line%%|*}" 2>/dev/null
    done <<<"$cur"
    for line in "${want[@]}"; do
        grep -qxF "$line" <<<"$cur" && continue
        prio=${line%%|*}
        # shellcheck disable=SC2086
        ip -4 rule add prio "$prio" ${line#*|}
    done
}

# Ответы на адреса слотов (A.B.(C+K).N) должны уходить в awg0: пока у него
# адрес /24, добавляем маршрут на всю /16
ensure_route() {
    ip link show "$VPN_IF" >/dev/null 2>&1 || return 0
    [[ -n "$(ip -4 route show exact "$SA.$SB.0.0/16" 2>/dev/null)" ]] && return 0
    ip -4 route add "$SA.$SB.0.0/16" dev "$VPN_IF" 2>/dev/null
}

# Каждому устройству на awg0 — адреса всех слотов этого входа (SLOTS). Только на
# интерфейсе: awg0.conf — копия основного, его перезаписывает синхронизация, а
# служба сразу же возвращает адреса (бот при синке пира их сбрасывает)
ensure_peer_ips() {
    ip link show "$VPN_IF" >/dev/null 2>&1 || return 0
    local pub want
    while read -r pub want; do
        [[ -n "$pub" && -n "$want" ]] && awg set "$VPN_IF" peer "$pub" allowed-ips "$want"
    done < <(awg show "$VPN_IF" allowed-ips 2>/dev/null | awk -v a="$SA" -v b="$SB" -v c="$SC" -v ks="${SLOTS[*]}" '
        BEGIN { m = split(ks, K, " ") }
        {
            split("", have); n = ""; other = ""; cnt = 0
            for (i = 2; i <= NF; i++) {
                if ($i == "(none)") next
                have[$i] = 1; cnt++
                split($i, p, /[.\/]/)
                if (p[1] == a && p[2] == b && p[3] == c && p[5] == 32) n = p[4]
                else if (!(p[1] == a && p[2] == b)) other = other "," $i
            }
            if (n == "") next
            want = a "." b "." c "." n "/32"; wn = 1; need = 0
            for (j = 1; j <= m; j++) {
                x = a "." b "." (c + K[j]) "." n "/32"; want = want "," x; wn++
                if (!(x in have)) need = 1
            }
            if (other != "") wn += split(substr(other, 2), o, ",")
            if (need || cnt != wn) print $1, want other
        }')
}

apply_static() {
    load_exits
    load_subnet || { warn "Нет подсети устройств ($VPN_IF.conf) — жду"; return 1; }
    ipset create -exist cascade_ru hash:net family inet hashsize 16384 maxelem 262144
    ipset create -exist cascade_force hash:net family inet
    load_set_from_file cascade_ru "$STATE/ru.zone"
    [[ -s "$STATE/ru.zone" ]] || warn "Нет списка российских сетей — всё идёт через выходы (cascade.sh ru-update)"
    [[ -f "$STATE/force_exit.list" ]] || printf '%s\n' \
        "# Сети, которые всегда идут через выход, даже если они в российском списке:" \
        "# по одной на строку (1.2.3.0/24). Служба подхватывает изменения сама." > "$STATE/force_exit.list"
    load_set_from_file cascade_force "$STATE/force_exit.list"
    build_auto_sets

    ensure_chain mangle CASCADE_PRE PREROUTING
    iptables -t mangle -N CASCADE_DECIDE 2>/dev/null
    {
        echo "*mangle"
        echo "-F CASCADE_PRE"
        # Решение принимается на первом пакете соединения и запоминается в нём:
        # смена выхода не рвёт уже идущие соединения, если их выход жив
        echo "-A CASCADE_PRE -i $VPN_IF -j CONNMARK --restore-mark --nfmask $MASK --ctmask $MASK"
        echo "-A CASCADE_PRE -i $VPN_IF -m mark ! --mark 0/$MASK -j RETURN"
        echo "-A CASCADE_PRE -i $VPN_IF -j CASCADE_DECIDE"
        echo "-A CASCADE_PRE -i $VPN_IF -j CONNMARK --save-mark --nfmask $MASK --ctmask $MASK"
        echo "COMMIT"
    } | iptables-restore --noflush
    write_decide
    drop_extra_auto_sets

    ensure_chain mangle CASCADE_MSS FORWARD
    iptables -t mangle -F CASCADE_MSS
    iptables -t mangle -A CASCADE_MSS -o awgx+ -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
    ensure_chain filter CASCADE_FWD FORWARD
    iptables -F CASCADE_FWD
    iptables -A CASCADE_FWD -i "$VPN_IF" -o awgx+ -j ACCEPT
    iptables -A CASCADE_FWD -i awgx+ -o "$VPN_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
    ensure_chain nat CASCADE_NAT POSTROUTING
    iptables -t nat -F CASCADE_NAT
    iptables -t nat -A CASCADE_NAT -o awgx+ -j MASQUERADE

    sync_ip_rules
    ensure_route
    ensure_peer_ips
}

static_ok() {
    iptables -t mangle -C PREROUTING -j CASCADE_PRE 2>/dev/null \
        && iptables -t nat -C POSTROUTING -j CASCADE_NAT 2>/dev/null \
        && ipset list -n cascade_ru >/dev/null 2>&1 \
        && ip -4 rule show | grep -q "^$PRIO:"
}

config_sig() {
    stat -c '%n %Y %s' "$STATE"/exits/*.env "$STATE/slots" "$STATE/ru.zone" "$STATE/force_exit.list" \
        "$AWG_DIR/$VPN_IF.conf" 2>/dev/null | md5sum
}

# ── Проверка выходов ─────────────────────────────────────────────────────────
# Живой — если отвечает адрес выхода внутри туннеля. Одного хендшейка мало:
# «заморозка» пропускает хендшейк и режет данные, это и есть «подключено без интернета»
probe() {
    local out rx
    out=$(ping -q -n -c 2 -i 0.3 -W 2 -I "awgx$1" "$NET.$1.1" 2>/dev/null)
    rx=$(awk '/packets transmitted/ {print $4}' <<<"$out")
    if [[ ${rx:-0} -ge 1 ]]; then
        echo "ok $(awk -F/ '/^rtt|^round-trip/ {printf "%.0f", $5}' <<<"$out")"
    else
        echo "fail"
    fi
}

health_round() {
    local k res changed=0 up=0 down=() now hs
    now=$(date +%s)
    for k in "${EXITS[@]}"; do probe "$k" > "$RUN/probe.$k" & done
    wait
    for k in "${EXITS[@]}"; do
        res=$(<"$RUN/probe.$k")
        hs=$(awg show "awgx$k" latest-handshakes 2>/dev/null | awk '{print $2; exit}')
        HS[$k]=$(( ${hs:-0} > 0 ? now - hs : -1 ))
        if [[ $res == ok* ]]; then
            RTT[$k]=${res#ok }; FAILC[$k]=0; OKC[$k]=$(( ${OKC[$k]:-0} + 1 ))
            if [[ ${ST[$k]:-unknown} != up ]] && [[ ${ST[$k]:-unknown} == unknown || ${OKC[$k]} -ge $UP_AFTER ]]; then
                [[ ${ST[$k]:-unknown} == down ]] && echo "✅ Выход $k снова отвечает (${RTT[$k]} мс)"
                ST[$k]=up; SINCE[$k]=$now; changed=1
            fi
        else
            # «Упал» — только после DOWN_AFTER неудач подряд, даже при первой
            # проверке: сразу после запуска туннель ещё может не пройти хендшейк
            RTT[$k]=""; OKC[$k]=0; FAILC[$k]=$(( ${FAILC[$k]:-0} + 1 ))
            if [[ ${ST[$k]:-unknown} != down && ${FAILC[$k]} -ge $DOWN_AFTER ]]; then
                echo "⚠️  Выход $k не отвечает — его устройства уходят на живой выход"
                ST[$k]=down; SINCE[$k]=$now; changed=1; down+=("$k")
            fi
        fi
        [[ ${ST[$k]} == up && ${SINCE[$k]} == "$now" ]] && up=1
    done
    if [[ $changed == 1 ]]; then
        write_decide
        # Соединения через упавший выход висели бы до таймаута: сбрасываем их,
        # и следующий же пакет выберет живой выход. Ушедшие напрямую, пока все
        # выходы лежали, — сбрасываем, когда выход ожил: иначе зарубежное так и
        # шло бы с российского IP до конца соединения
        for k in "${down[@]}"; do conntrack -D -m "$(mark_of "$k")/$MASK" >/dev/null 2>&1; done
        [[ $up == 1 ]] && conntrack -D -m "$MARK_ALLDOWN/$MASK" >/dev/null 2>&1
    fi
    {
        for k in "${EXITS[@]}"; do
            echo "$k ${ST[$k]:-unknown} ${SINCE[$k]:-$now} ${RTT[$k]:--} ${HS[$k]}"
        done
    } > "$RUN/health.tmp" && mv "$RUN/health.tmp" "$RUN/health"
}

cmd_daemon() {
    need_root
    mkdir -p "$RUN" "$STATE/exits"
    echo $$ > "$RUN/daemon.pid"
    trap 'rm -f "$RUN/daemon.pid"; exit 0' TERM INT
    local sig="" cur
    while true; do
        cur=$(config_sig)
        if [[ "$cur" != "$sig" ]] || ! static_ok; then
            if apply_static; then
                # Подпись — после применения: оно само создаёт force_exit.list
                sig=$(config_sig)
                echo "Правила применены: выходов ${#EXITS[@]}, в «Авто» ${#AUTO_LIST[@]} ($(ipset list -t cascade_ru 2>/dev/null | awk '/Number of entries/ {print $4}') российских сетей)"
            fi
        else
            ensure_route
            ensure_peer_ips
        fi
        [[ ${#EXITS[@]} -gt 0 ]] && health_round
        # После неудачной проверки перепроверяем чаще: отказ выхода замечается
        # за ~20 с, а не за три полных интервала
        local pause=$INTERVAL k
        for k in "${EXITS[@]}"; do
            [[ ${FAILC[$k]:-0} -gt 0 && ${ST[$k]:-} != down ]] && pause=$(( INTERVAL < 3 ? INTERVAL : 3 ))
        done
        sleep "$pause" & wait $!
    done
}

install_units() {
    use_systemd || return 0
    cat > /etc/systemd/system/awg-cascade.service <<EOF
[Unit]
Description=AWG cascade: маршруты входа и проверка выходов
After=network-online.target awg-quick@$VPN_IF.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash $SELF daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    cat > /etc/systemd/system/awg-cascade-ru.service <<EOF
[Unit]
Description=AWG cascade: обновление списка российских сетей
After=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $SELF ru-update
EOF
    cat > /etc/systemd/system/awg-cascade-ru.timer <<EOF
[Unit]
Description=AWG cascade: список российских сетей раз в сутки

[Timer]
OnCalendar=daily
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now awg-cascade-ru.timer >/dev/null 2>&1
    systemctl enable --now awg-cascade.service >/dev/null 2>&1 || warn "Служба awg-cascade не стартовала: journalctl -u awg-cascade -n 20"
}

daemon_running() {
    if use_systemd; then
        systemctl is-active --quiet awg-cascade.service
    else
        [[ -f "$RUN/daemon.pid" ]] && kill -0 "$(<"$RUN/daemon.pid")" 2>/dev/null
    fi
}

# ══ Статус и снятие ═══════════════════════════════════════════════════════════

fmt_age() {
    local s=$1
    [[ "$s" =~ ^[0-9]+$ ]] || { echo "никогда"; return; }
    if   [[ $s -lt 120 ]];  then echo "$s с назад"
    elif [[ $s -lt 7200 ]]; then echo "$((s / 60)) мин назад"
    else echo "$((s / 3600)) ч назад"; fi
}

fmt_bytes() { awk -v b="${1:-0}" 'BEGIN { split("Б КБ МБ ГБ ТБ", u); i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ } printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i] }'; }

cmd_status() {
    local any=0 k now
    now=$(date +%s)
    if [[ -f "$AWG_DIR/$EXIT_IF.conf" ]]; then
        any=1
        echo "═══ Выход: слот $(exit_slot), $(env_get "$STATE/exit.env" HOST), UDP $(exit_port) ═══"
        ip link show "$EXIT_IF" >/dev/null 2>&1 || echo "  ⚠️  интерфейс $EXIT_IF не поднят"
        local e ip pub hs rx tx
        while read -r e ip; do
            pub=$(awg show "$EXIT_IF" allowed-ips 2>/dev/null | awk -v a="$NET.$(exit_slot).$((1 + e))/32" '$2 == a {print $1}')
            read -r hs rx tx < <(awg show "$EXIT_IF" dump 2>/dev/null | awk -v p="$pub" '$1 == p {print $5, $6, $7}')
            printf '  Вход %s (%s): хендшейк %s, принято %s, отдано %s\n' "$e" "${ip:-?}" \
                "$( [[ ${hs:-0} -gt 0 ]] && fmt_age $((now - hs)) || echo никогда)" "$(fmt_bytes "$rx")" "$(fmt_bytes "$tx")"
        done < <(awk '$1 == "#" && $2 == "Entry:" {print $3, $4}' "$AWG_DIR/$EXIT_IF.conf")
        echo
    fi
    load_exits 2>/dev/null
    if [[ ${#EXITS[@]} -gt 0 ]]; then
        any=1
        load_subnet
        local ru_n ru_age
        ru_n=$(ipset list -t cascade_ru 2>/dev/null | awk '/Number of entries/ {print $4}')
        ru_age=$([[ -f "$STATE/ru.zone" ]] && fmt_age $((now - $(stat -c %Y "$STATE/ru.zone"))) || echo "нет файла")
        echo "═══ Вход: выходов ${#EXITS[@]} ═══"
        daemon_running && echo "  Служба: работает" || echo "  ⚠️  Служба не запущена: маршруты не обновляются"
        echo "  Российские сети: ${ru_n:-0} записей (файл обновлён: $ru_age)"
        local st since rtt hsa line tr ep state
        for k in "${EXITS[@]}"; do
            line=$(awk -v k="$k" '$1 == k' "$RUN/health" 2>/dev/null)
            st="" since="" rtt="" hsa=""
            read -r _ st since rtt hsa <<<"$line"
            ep=$(awk -F'[ \t]*=[ \t]*' '$1 == "Endpoint" {print $2; exit}' "$(entry_conf "$k")")
            tr=$(awg show "awgx$k" transfer 2>/dev/null | awk '{print $2, $3; exit}')
            case $st in
                up)   state="✅ работает${rtt:+, $rtt мс}" ;;
                down) state="❌ не отвечает с $(date -d "@$since" '+%d.%m %H:%M' 2>/dev/null)" ;;
                *)    state="… проверяется" ;;
            esac
            printf '  Выход %s (%s): %s\n      хендшейк %s; «Авто»: %s; через туннель принято %s, отдано %s\n' \
                "$k" "$ep" "$state" "$(fmt_age "$hsa")" \
                "$([[ ${AUTO[$k]} -gt 0 ]] && echo "порядок ${AUTO[$k]}" || echo "не участвует")" \
                "$(fmt_bytes "${tr%% *}")" "$(fmt_bytes "${tr##* }")"
        done
        if [[ ${#AUTO_LIST[@]} -gt 0 ]] && ip link show "$VPN_IF" >/dev/null 2>&1; then
            echo "  Устройства «Авто» по выходам (закреплены за своим, при отказе — на живой):"
            awg show "$VPN_IF" allowed-ips 2>/dev/null | awk -v a="$SA" -v b="$SB" -v c="$SC" -v al="${AUTO_LIST[*]}" '
                BEGIN { n = split(al, L, " ") }
                { for (i = 2; i <= NF; i++) { split($i, p, /[.\/]/)
                    if (p[1] == a && p[2] == b && p[3] == c) { cnt[L[(p[4] % n) + 1]]++; break } } }
                END { for (j = 1; j <= n; j++) printf "    выход %s — %d\n", L[j], cnt[L[j]] + 0 }'
        fi
        echo
    fi
    [[ $any == 1 ]] || echo "Каскад на этом сервере не настроен."
}

cmd_down() {
    need_root
    local k f
    if use_systemd; then
        systemctl disable --now awg-cascade.service awg-cascade-ru.timer >/dev/null 2>&1
        rm -f /etc/systemd/system/awg-cascade.service /etc/systemd/system/awg-cascade-ru.service \
              /etc/systemd/system/awg-cascade-ru.timer
        systemctl daemon-reload
    elif [[ -f "$RUN/daemon.pid" ]]; then
        kill "$(<"$RUN/daemon.pid")" 2>/dev/null
        sleep 1
    fi
    load_exits 2>/dev/null
    for f in "$AWG_DIR"/awgx*.conf; do
        [[ -f "$f" ]] || continue
        k=$(basename "$f" .conf)
        [[ "$k" == "$EXIT_IF" ]] && continue
        iface_down "$k"; rm -f "$f"
        say "  туннель $k снят"
    done
    iptables -t mangle -D PREROUTING -j CASCADE_PRE 2>/dev/null
    iptables -t mangle -D FORWARD -j CASCADE_MSS 2>/dev/null
    iptables -D FORWARD -j CASCADE_FWD 2>/dev/null
    iptables -t nat -D POSTROUTING -j CASCADE_NAT 2>/dev/null
    local t c
    for t in "mangle CASCADE_PRE" "mangle CASCADE_DECIDE" "mangle CASCADE_MSS" "filter CASCADE_FWD" "nat CASCADE_NAT"; do
        read -r t c <<<"$t"
        iptables -t "$t" -F "$c" 2>/dev/null; iptables -t "$t" -X "$c" 2>/dev/null
    done
    ip -4 rule show | awk -F: -v lo="$PRIO" -v hi="$((PRIO + 255))" '$1 + 0 >= lo && $1 + 0 <= hi {print $1}' \
        | while read -r k; do ip -4 rule del prio "$k"; done
    for f in $(ipset list -n 2>/dev/null | grep -E '^cascade_(ru|force|auto_[0-9]+)(_new)?$'); do ipset destroy "$f" 2>/dev/null; done
    if load_subnet; then
        EXITS=(); SLOTS=()
        ensure_peer_ips   # без слотов — у устройств остаётся только основной адрес
        # Маршрут /16 добавлял каскад, если у awg0 адрес /24
        [[ "$(conf_get "$AWG_DIR/$VPN_IF.conf" Address)" == */16 ]] || ip -4 route del "$SA.$SB.0.0/16" dev "$VPN_IF" 2>/dev/null
    fi
    if [[ -f "$AWG_DIR/$EXIT_IF.conf" ]]; then
        iface_down "$EXIT_IF"; rm -f "$AWG_DIR/$EXIT_IF.conf"
        say "  выход $EXIT_IF снят"
    fi
    rm -rf "$STATE" "$RUN"
    rm -f /etc/sysctl.d/90-awg-cascade.conf
    say "✅ Каскад на этом сервере снят. awg0 и устройства не тронуты."
}

# ══ Конфиг устройства (на основном) ═══════════════════════════════════════════
# Берёт файл устройства из clients/, меняет адрес сервера на вход, а адрес
# устройства — на адрес слота K (0 — «Авто», адрес прежний)
cmd_client_conf() {
    local args=() qr=0 a
    for a in "$@"; do [[ "$a" == --qr ]] && qr=1 || args+=("$a"); done
    local name=${args[0]:-} host=${args[1]:-} k=${args[2]:-0}
    [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ && "$name" != *..* ]] || die "Неверное имя устройства"
    valid_kv host "$host" || die "Нужен адрес входа: cascade.sh client-conf УСТРОЙСТВО АДРЕС [K] [--qr]"
    [[ "$k" =~ ^[0-9]+$ ]] || die "K — номер слота (0 — «Авто»)"
    local f="$AWG_DIR/clients/$name.conf"
    [[ -f "$f" ]] || die "Нет $f — команда запускается на основном сервере"
    load_subnet || die "Нет подсети устройств"
    local out
    out=$(awk -v k="$k" -v host="$host" -v a="$SA" -v b="$SB" -v c="$SC" '
        /^[ \t]*Address[ \t]*=/ {
            v = $0; sub(/^[^=]*=[ \t]*/, "", v); split(v, p, /[.\/]/)
            if (k > 0 && p[1] == a && p[2] == b && p[3] == c) { print "Address = " a "." b "." (c + k) "." p[4] "/32"; next }
        }
        /^[ \t]*Endpoint[ \t]*=/ {
            v = $0; sub(/^[^=]*=[ \t]*/, "", v); n = split(v, q, ":")
            print "Endpoint = " host ":" q[n]; next
        }
        { print }' "$f")
    echo "$out"
    if [[ $qr == 1 ]]; then
        have qrencode || ensure_pkgs qrencode
        qrencode -t ansiutf8 <<<"$out"
    fi
}

usage() {
    sed -n '2,27p' "$SELF" | sed 's/^# \{0,1\}//'
}

case "${1:-}" in
    exit-up)     shift; need_root; cmd_exit_up "$@" ;;
    exit-peer)   shift; need_root; cmd_exit_peer "$@" ;;
    exit-unpeer) shift; need_root; cmd_exit_unpeer "$@" ;;
    entry-add)   shift; need_root; cmd_entry_add "$@" ;;
    entry-del)   shift; need_root; cmd_entry_del "$@" ;;
    auto)        shift; need_root; cmd_auto "$@" ;;
    ru-update)   shift; need_root; cmd_ru_update ;;
    daemon)      shift; cmd_daemon ;;
    status)      shift; cmd_status ;;
    down)        shift; cmd_down ;;
    client-conf) shift; need_root; cmd_client_conf "$@" ;;
    *)           usage ;;
esac
