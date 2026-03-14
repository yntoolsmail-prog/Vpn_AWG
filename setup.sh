#!/bin/bash
# =============================================================================
# AmneziaWG + Telegram Bot — Установщик
# Использование: bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/setup.sh)
# =============================================================================
# Version: 1.6

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root: sudo bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/setup.sh)"

# ── Проверка версии Ubuntu ────────────────────────────────────────────────────
UBUNTU_VERSION=$(lsb_release -rs 2>/dev/null || echo "0")
UBUNTU_MAJOR=$(echo "$UBUNTU_VERSION" | cut -d. -f1)

clear
echo -e "${CYAN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   AmneziaWG + Telegram Bot — Установка  ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

info "Ubuntu ${UBUNTU_VERSION}"

if [[ "$UBUNTU_MAJOR" -lt 22 ]]; then
    err "Требуется Ubuntu 22.04 или 24.04"
fi

if [[ "$UBUNTU_MAJOR" -gt 24 ]]; then
    warn "Ubuntu ${UBUNTU_VERSION} не тестировалась. Продолжаем..."
fi

# ── Проверка ядра ─────────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

CURRENT_KERNEL=$(uname -r)
apt-get update -qq

NEW_KERNEL=$(apt list --upgradable 2>/dev/null | grep "^linux-image-generic/" | awk -F'[ /]' '{print $2}' | head -1)

if [[ -n "$NEW_KERNEL" ]]; then
    echo ""
    echo -e "${YELLOW}${BOLD}  ┌─────────────────────────────────────────────────┐${NC}"
    echo -e "${YELLOW}${BOLD}  │              Требуется обновление ядра          │${NC}"
    echo -e "${YELLOW}${BOLD}  └─────────────────────────────────────────────────┘${NC}"
    echo ""
    echo -e "  Текущее ядро:  ${RED}${CURRENT_KERNEL}${NC}"
    echo -e "  Доступно:      ${GREEN}${NEW_KERNEL}${NC}"
    echo ""
    echo -e "  AmneziaWG компилируется как модуль ядра и должен"
    echo -e "  совпадать с загруженным ядром. Без обновления"
    echo -e "  установка завершится ошибкой."
    echo ""
    echo -e "  ${CYAN}1)${NC} Обновить ядро и перезагрузиться ${GREEN}(рекомендуется)${NC}"
    echo -e "  ${CYAN}2)${NC} Продолжить без обновления ${RED}(может не заработать)${NC}"
    echo -e "  ${CYAN}0)${NC} Выйти"
    echo ""
    while true; do
        read -p "  Ваш выбор [1]: " KERNEL_CHOICE
        KERNEL_CHOICE=${KERNEL_CHOICE:-1}
        [[ "$KERNEL_CHOICE" == "0" || "$KERNEL_CHOICE" == "1" || "$KERNEL_CHOICE" == "2" ]] && break
        warn "Введите 0, 1 или 2."
    done

    if [[ "$KERNEL_CHOICE" == "0" ]]; then
        echo ""
        info "Выход. Запустите установщик снова когда будете готовы."
        exit 0
    elif [[ "$KERNEL_CHOICE" == "1" ]]; then
        echo ""
        log "Обновление ядра..."
        apt-get install -y -qq linux-image-generic linux-headers-generic
        echo ""
        echo -e "${GREEN}${BOLD}  Ядро обновлено. Сервер перезагрузится через 5 секунд.${NC}"
        echo -e "${GREEN}  После перезагрузки запустите установщик снова.${NC}"
        echo ""
        sleep 5
        reboot
        exit 0
    else
        warn "Продолжаем без обновления ядра. Если установка упадёт — вернитесь и выберите вариант 1."
        echo ""
    fi
else
    info "Ядро актуально (${CURRENT_KERNEL})"
fi

# ══════════════════════════════════════════════════════════════════════════════
# ── Автодетект существующего AmneziaWG ───────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

EXISTING_IFACES=()       # список найденных awg-интерфейсов
EXISTING_DOCKER=0        # флаг: обнаружен Docker-контейнер amnezia-awg
EXISTING_NATIVE=0        # флаг: обнаружен нативный модуль ядра
EXISTING_PORTS=()        # занятые UDP-порты
EXISTING_SUBNETS=()      # занятые подсети

# Проверяем работающие awg* интерфейсы
while IFS= read -r line; do
    IFACE_NAME=$(echo "$line" | grep -oP 'awg\d+' | head -1)
    [[ -n "$IFACE_NAME" ]] && EXISTING_IFACES+=("$IFACE_NAME")
done < <(ip link show 2>/dev/null | grep awg || true)

# Проверяем Docker-контейнер от официального AmneziaVPN
if command -v docker &>/dev/null; then
    if docker ps 2>/dev/null | grep -q "amnezia-awg"; then
        EXISTING_DOCKER=1
    fi
fi

# Проверяем нативный модуль ядра
if lsmod 2>/dev/null | grep -q "amneziawg"; then
    EXISTING_NATIVE=1
fi

# Проверяем systemd-сервисы awg-quick@*
while IFS= read -r line; do
    SVC_IFACE=$(echo "$line" | grep -oP 'awg-quick@\K[^\s.]+')
    [[ -n "$SVC_IFACE" ]] && EXISTING_IFACES+=("$SVC_IFACE")
done < <(systemctl list-units --type=service 2>/dev/null | grep "awg-quick@" || true)

# Дедупликация интерфейсов
EXISTING_IFACES=($(printf "%s\n" "${EXISTING_IFACES[@]}" | sort -u))

# Собираем занятые порты и подсети из существующих конфигов
for IFACE_NAME in "${EXISTING_IFACES[@]}"; do
    CONF_PATH="/etc/amnezia/amneziawg/${IFACE_NAME}.conf"
    if [[ -f "$CONF_PATH" ]]; then
        PORT=$(grep "^ListenPort" "$CONF_PATH" 2>/dev/null | awk '{print $3}')
        [[ -n "$PORT" ]] && EXISTING_PORTS+=("$PORT")
        SUBNET=$(grep "^Address" "$CONF_PATH" 2>/dev/null | awk '{print $3}' | grep -oP '^\d+\.\d+\.\d+' | head -1)
        [[ -n "$SUBNET" ]] && EXISTING_SUBNETS+=("$SUBNET")
    fi
done

# Если нашли Docker-контейнер — пробуем вытащить порт из него
if [[ "$EXISTING_DOCKER" -eq 1 ]]; then
    DOCKER_PORT=$(docker inspect amnezia-awg 2>/dev/null | python3 -c "
import sys,json
try:
    data=json.load(sys.stdin)
    ports=data[0].get('HostConfig',{}).get('PortBindings',{})
    for k,v in ports.items():
        if v: print(v[0].get('HostPort',''))
except: pass
" 2>/dev/null | head -1)
    [[ -n "$DOCKER_PORT" ]] && EXISTING_PORTS+=("$DOCKER_PORT")
fi

EXISTING_PORTS=($(printf "%s\n" "${EXISTING_PORTS[@]}" | sort -u))
EXISTING_SUBNETS=($(printf "%s\n" "${EXISTING_SUBNETS[@]}" | sort -u))

# ── Если что-то обнаружено — показываем детальное объяснение ─────────────────
AWG_ALREADY_EXISTS=0
if [[ ${#EXISTING_IFACES[@]} -gt 0 || "$EXISTING_DOCKER" -eq 1 || "$EXISTING_NATIVE" -eq 1 ]]; then
    AWG_ALREADY_EXISTS=1
fi

if [[ "$AWG_ALREADY_EXISTS" -eq 1 ]]; then
    echo ""
    echo -e "${YELLOW}${BOLD}  ╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}${BOLD}  ║          ⚠  AmneziaWG уже установлен на сервере  ⚠       ║${NC}"
    echo -e "${YELLOW}${BOLD}  ╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}Что обнаружено:${NC}"
    echo ""

    # Выводим найденные интерфейсы
    if [[ ${#EXISTING_IFACES[@]} -gt 0 ]]; then
        for IFACE_NAME in "${EXISTING_IFACES[@]}"; do
            CONF_PATH="/etc/amnezia/amneziawg/${IFACE_NAME}.conf"
            PORT=$(grep "^ListenPort" "$CONF_PATH" 2>/dev/null | awk '{print $3}' || echo "неизвестен")
            ADDR=$(grep "^Address"    "$CONF_PATH" 2>/dev/null | awk '{print $3}' || echo "неизвестен")
            echo -e "    ${GREEN}●${NC} Интерфейс: ${CYAN}${IFACE_NAME}${NC}  |  Порт: ${CYAN}${PORT}/UDP${NC}  |  Адрес: ${CYAN}${ADDR}${NC}"
        done
    fi

    if [[ "$EXISTING_DOCKER" -eq 1 ]]; then
        echo -e "    ${YELLOW}●${NC} Тип: ${YELLOW}Docker-контейнер${NC} (официальное приложение AmneziaVPN)"
        [[ -n "$DOCKER_PORT" ]] && echo -e "       Порт контейнера: ${CYAN}${DOCKER_PORT}/UDP${NC}"
    fi

    if [[ "$EXISTING_NATIVE" -eq 1 ]]; then
        echo -e "    ${GREEN}●${NC} Тип: ${GREEN}нативный модуль ядра${NC} (amneziawg загружен в kernelspace)"
    fi

    # Показываем занятые порты сводно
    if [[ ${#EXISTING_PORTS[@]} -gt 0 ]]; then
        echo ""
        echo -e "    ${RED}Занятые UDP-порты:${NC} ${EXISTING_PORTS[*]}"
        echo -e "    ${YELLOW}→ При вводе порта ниже выберите другой номер!${NC}"
    fi

    echo ""
    echo -e "  ${BOLD}Что это значит и чем грозит каждый вариант:${NC}"
    echo ""
    echo -e "  ┌──────────────────────────────────────────────────────────────────┐"
    echo -e "  │  ${CYAN}Вариант 1 — Установить параллельно (РЕКОМЕНДУЕТСЯ)${NC}            │"
    echo -e "  │                                                                  │"
    echo -e "  │  Существующий VPN продолжает работать в штатном режиме.          │"
    echo -e "  │  Все текущие клиенты остаются подключёнными — никто не отвалится.│"
    echo -e "  │                                                                  │"
    echo -e "  │  Установщик автоматически выберет следующий свободный            │"
    echo -e "  │  интерфейс (awg1, awg2...) и незанятую подсеть (10.9.0.x...).   │"
    echo -e "  │  Вы управляете двумя независимыми VPN-серверами.                │"
    echo -e "  │                                                                  │"
    echo -e "  │  Единственное требование: выбрать другой порт UDP.               │"
    echo -e "  └──────────────────────────────────────────────────────────────────┘"
    echo ""
    echo -e "  ┌──────────────────────────────────────────────────────────────────┐"
    echo -e "  │  ${RED}Вариант 2 — Заменить существующий (ОПАСНО)${NC}                    │"
    echo -e "  │                                                                  │"
    echo -e "  │  Старый интерфейс будет остановлен и удалён.                     │"
    echo -e "  │  Все подключённые клиенты потеряют связь немедленно.             │"
    echo -e "  │  Конфиги старых клиентов удаляются и восстановлению              │"
    echo -e "  │  не подлежат (если нет бэкапа).                                  │"
    echo -e "  │                                                                  │"
    echo -e "  │  Выбирайте только если вы точно хотите начать с нуля и           │"
    echo -e "  │  понимаете что старый VPN перестанет существовать.               │"
    echo -e "  └──────────────────────────────────────────────────────────────────┘"
    echo ""
    echo -e "  ┌──────────────────────────────────────────────────────────────────┐"
    echo -e "  │  ${YELLOW}Вариант 0 — Выйти${NC}                                             │"
    echo -e "  │                                                                  │"
    echo -e "  │  Ничего не изменится. Установщик завершится без каких-либо       │"
    echo -e "  │  изменений на сервере.                                           │"
    echo -e "  └──────────────────────────────────────────────────────────────────┘"
    echo ""

    while true; do
        read -p "  Ваш выбор [1]: " AWG_CONFLICT_CHOICE
        AWG_CONFLICT_CHOICE=${AWG_CONFLICT_CHOICE:-1}
        [[ "$AWG_CONFLICT_CHOICE" == "0" || "$AWG_CONFLICT_CHOICE" == "1" || "$AWG_CONFLICT_CHOICE" == "2" ]] && break
        warn "Введите 0, 1 или 2."
    done

    if [[ "$AWG_CONFLICT_CHOICE" == "0" ]]; then
        echo ""
        info "Выход без изменений. Существующий AWG не затронут."
        exit 0

    elif [[ "$AWG_CONFLICT_CHOICE" == "2" ]]; then
        echo ""
        echo -e "${RED}${BOLD}  !! ВНИМАНИЕ — ДЕСТРУКТИВНОЕ ДЕЙСТВИЕ !!${NC}"
        echo ""
        echo -e "  Вы выбрали замену существующего AWG."
        echo -e "  ${RED}Все текущие клиенты будут отключены, их конфиги удалены.${NC}"
        echo -e "  Это действие необратимо без бэкапа."
        echo ""
        read -p "  Для подтверждения введите YES (заглавными): " CONFIRM_REPLACE
        if [[ "$CONFIRM_REPLACE" != "YES" ]]; then
            echo ""
            info "Отменено. Выход без изменений."
            exit 0
        fi

        echo ""
        log "Остановка существующих AWG интерфейсов..."
        for IFACE_NAME in "${EXISTING_IFACES[@]}"; do
            systemctl stop "awg-quick@${IFACE_NAME}" 2>/dev/null || true
            systemctl disable "awg-quick@${IFACE_NAME}" 2>/dev/null || true
            awg-quick down "/etc/amnezia/amneziawg/${IFACE_NAME}.conf" 2>/dev/null || \
                ip link delete "$IFACE_NAME" 2>/dev/null || true
        done
        if [[ "$EXISTING_DOCKER" -eq 1 ]]; then
            log "Остановка Docker-контейнера amnezia-awg..."
            docker stop amnezia-awg 2>/dev/null || true
            docker rm   amnezia-awg 2>/dev/null || true
        fi

        # Используем дефолтный интерфейс awg0 и подсеть 10.8.0
        VPN_IFACE="awg0"
        VPN_SUBNET="10.8.0"
        log "Замена: будет использован интерфейс ${VPN_IFACE}, подсеть ${VPN_SUBNET}.x"

    else
        # ── Параллельная установка ────────────────────────────────────────────
        echo ""
        echo -e "${GREEN}${BOLD}  Параллельная установка — ищем свободный интерфейс...${NC}"
        echo ""

        # Находим следующий свободный интерфейс
        IDX=1
        while ip link show "awg${IDX}" &>/dev/null || systemctl is-active "awg-quick@awg${IDX}" &>/dev/null; do
            ((IDX++))
        done
        SUGGESTED_IFACE="awg${IDX}"

        # Находим следующую свободную подсеть из списка кандидатов
        SUBNET_CANDIDATES=("10.8.0" "10.9.0" "10.10.0" "10.11.0" "10.12.0" "10.13.0" "10.14.0" "10.15.0")
        SUGGESTED_SUBNET=""
        for CANDIDATE in "${SUBNET_CANDIDATES[@]}"; do
            OCCUPIED=0
            for USED in "${EXISTING_SUBNETS[@]}"; do
                [[ "$USED" == "$CANDIDATE" ]] && OCCUPIED=1 && break
            done
            # Дополнительно проверяем через ip route
            if ip route 2>/dev/null | grep -q "${CANDIDATE}.0/24"; then
                OCCUPIED=1
            fi
            if [[ "$OCCUPIED" -eq 0 ]]; then
                SUGGESTED_SUBNET="$CANDIDATE"
                break
            fi
        done
        [[ -z "$SUGGESTED_SUBNET" ]] && SUGGESTED_SUBNET="10.20.0"

        echo -e "  Предлагаемый интерфейс: ${CYAN}${SUGGESTED_IFACE}${NC}"
        echo -e "  Предлагаемая подсеть:   ${CYAN}${SUGGESTED_SUBNET}.x/24${NC}"
        echo ""

        if [[ ${#EXISTING_PORTS[@]} -gt 0 ]]; then
            echo -e "  ${RED}Уже занятые порты: ${EXISTING_PORTS[*]}${NC}"
            echo -e "  ${YELLOW}Выберите другой порт для нового VPN!${NC}"
            echo ""
        fi

        # Даём пользователю возможность поменять предложенные значения
        read -p "  Имя интерфейса [${SUGGESTED_IFACE}]: " USER_IFACE
        VPN_IFACE="${USER_IFACE:-$SUGGESTED_IFACE}"

        read -p "  Подсеть (первые три октета) [${SUGGESTED_SUBNET}]: " USER_SUBNET
        VPN_SUBNET="${USER_SUBNET:-$SUGGESTED_SUBNET}"

        echo ""
        log "Будет установлен новый AWG: интерфейс ${VPN_IFACE}, подсеть ${VPN_SUBNET}.x"
    fi

else
    # ── Чистая установка — дефолтные значения ────────────────────────────────
    VPN_IFACE="awg0"
    VPN_SUBNET="10.8.0"
fi

# ── Выбор режима установки ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Выберите режим установки:${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} Тихая     — только ключевые шаги (быстрее читать)"
echo -e "  ${CYAN}2)${NC} Подробная — показывать все процессы"
echo ""
while true; do
    read -p "  Ваш выбор [1]: " INSTALL_MODE
    INSTALL_MODE=${INSTALL_MODE:-1}
    [[ "$INSTALL_MODE" == "1" || "$INSTALL_MODE" == "2" ]] && break
    warn "Введите 1 или 2."
done

if [[ "$INSTALL_MODE" == "1" ]]; then
    APT_FLAGS="-qq"
    info "Режим: тихая установка"
else
    APT_FLAGS=""
    info "Режим: подробная установка"
fi

# Хелпер для выполнения команд с учётом режима
run() {
    if [[ "$INSTALL_MODE" == "1" ]]; then
        eval "$@" > /dev/null 2>&1
    else
        eval "$@"
    fi
}

echo ""

# ── Шаг 1: Зависимости ────────────────────────────────────────────────────────
log "Установка зависимостей..."
run "apt-get install -y $APT_FLAGS curl software-properties-common qrencode python3 python3-pip"

# ── Шаг 2: AmneziaWG ──────────────────────────────────────────────────────────
log "Добавление PPA Amnezia..."
run "add-apt-repository -y ppa:amnezia/ppa"
run "apt-get $APT_FLAGS update"

log "Установка AmneziaWG (компиляция ~3-5 мин)..."
run "apt-get install -y $APT_FLAGS amneziawg amneziawg-tools"

log "Загрузка модуля ядра..."
if ! modprobe amneziawg 2>/dev/null; then
    RUNNING_KERNEL=$(uname -r)
    INSTALLED_KERNEL=$(ls /lib/modules/ | grep -v "$RUNNING_KERNEL" | tail -1)
    echo ""
    echo -e "${YELLOW}${BOLD}  ┌─────────────────────────────────────────────────────┐${NC}"
    echo -e "${YELLOW}${BOLD}  │         Требуется перезагрузка сервера              │${NC}"
    echo -e "${YELLOW}${BOLD}  └─────────────────────────────────────────────────────┘${NC}"
    echo ""
    echo -e "  Модуль AWG скомпилирован под ядро: ${GREEN}${INSTALLED_KERNEL}${NC}"
    echo -e "  Сейчас загружено старое ядро:      ${RED}${RUNNING_KERNEL}${NC}"
    echo ""
    echo -e "  В процессе установки AWG автоматически обновил ядро."
    echo -e "  После перезагрузки запустите установщик повторно —"
    echo -e "  AWG уже скомпилирован и установится мгновенно."
    echo ""
    echo -e "${GREEN}${BOLD}  Перезагружаюсь через 5 секунд...${NC}"
    echo "amneziawg" > /etc/modules-load.d/amneziawg.conf
    sleep 5
    reboot
    exit 0
fi
echo "amneziawg" > /etc/modules-load.d/amneziawg.conf

# ── Шаг 3: Параметры сервера ──────────────────────────────────────────────────
echo ""
SERVER_IP=$(curl -4 -s ifconfig.me 2>/dev/null || curl -4 -s api.ipify.org 2>/dev/null || curl -4 -s ifconfig.co 2>/dev/null)

# Проверяем что получили именно IPv4 (не IPv6 и не пусто)
if [[ -z "$SERVER_IP" ]] || [[ "$SERVER_IP" == *":"* ]]; then
    warn "Не удалось автоматически определить внешний IPv4 (получено: '${SERVER_IP}')."
    warn "Это может случиться на серверах с только IPv6 на внешнем интерфейсе."
    echo ""
    while true; do
        read -p "  Введите внешний IPv4 вашего сервера вручную: " SERVER_IP
        # Простая проверка формата IPv4
        if [[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
            break
        fi
        warn "Неверный формат. Введите IPv4, например: 185.123.45.67"
    done
fi
IFACE=$(ip route | grep default | awk '{print $5}' | head -1)
info "Внешний IP: $SERVER_IP"
info "Интерфейс:  $IFACE"
info "AWG интерфейс: ${VPN_IFACE}  |  Подсеть: ${VPN_SUBNET}.x"
echo ""

# Показываем занятые порты при вводе, если есть
if [[ ${#EXISTING_PORTS[@]} -gt 0 ]]; then
    warn "Уже занятые порты: ${EXISTING_PORTS[*]} — выберите другой!"
fi

read -p "  Порт AWG [51820]: " AWG_PORT
AWG_PORT=${AWG_PORT:-51820}

# Проверяем что введённый порт не занят
for BUSY_PORT in "${EXISTING_PORTS[@]}"; do
    if [[ "$AWG_PORT" == "$BUSY_PORT" ]]; then
        echo ""
        warn "Порт ${AWG_PORT} уже используется другим AWG-интерфейсом."
        warn "Это вызовет конфликт. Поменяйте порт и запустите установщик снова."
        err "Конфликт порта ${AWG_PORT}"
    fi
done

# ── Шаг 4: Ключи сервера ──────────────────────────────────────────────────────
log "Генерация ключей сервера..."
mkdir -p /etc/amnezia/amneziawg/clients
chmod 700 /etc/amnezia/amneziawg
awg genkey | tee /etc/amnezia/amneziawg/server_private.key | awg pubkey > /etc/amnezia/amneziawg/server_public.key
chmod 600 /etc/amnezia/amneziawg/server_private.key
SERVER_PRIVATE=$(cat /etc/amnezia/amneziawg/server_private.key)
SERVER_PUBLIC=$(cat /etc/amnezia/amneziawg/server_public.key)

# ── Шаг 5: Генерация параметров обфускации ───────────────────────────────────
log "Генерация случайных параметров обфускации..."
read JC JMIN JMAX S1 S2 H1 H2 H3 H4 < <(python3 -c "
import random
print(
    random.randint(3,10),
    random.randint(10,50),
    random.randint(51,100),
    random.randint(20,100),
    random.randint(20,100),
    random.randint(100000000,2000000000),
    random.randint(100000000,2000000000),
    random.randint(100000000,2000000000),
    random.randint(100000000,2000000000),
)")
info "Jc=$JC Jmin=$JMIN Jmax=$JMAX S1=$S1 S2=$S2"

# ── Шаг 6: Конфиг AWG ────────────────────────────────────────────────────────
log "Создание конфига интерфейса ${VPN_IFACE}..."
{
    printf "[Interface]\n"
    printf "PrivateKey = %s\n" "$SERVER_PRIVATE"
    printf "Address = %s.1/24\n" "$VPN_SUBNET"
    printf "ListenPort = %s\n" "$AWG_PORT"
    printf "Jc = %s\nJmin = %s\nJmax = %s\n" "$JC" "$JMIN" "$JMAX"
    printf "S1 = %s\nS2 = %s\n" "$S1" "$S2"
    printf "H1 = %s\nH2 = %s\nH3 = %s\nH4 = %s\n" "$H1" "$H2" "$H3" "$H4"
    printf "\n"
    printf "PostUp = iptables -A FORWARD -i %s -j ACCEPT; iptables -A FORWARD -o %s -j ACCEPT; iptables -t nat -A POSTROUTING -o %s -j MASQUERADE\n" \
        "$VPN_IFACE" "$VPN_IFACE" "$IFACE"
    printf "PostDown = iptables -D FORWARD -i %s -j ACCEPT; iptables -D FORWARD -o %s -j ACCEPT; iptables -t nat -D POSTROUTING -o %s -j MASQUERADE\n" \
        "$VPN_IFACE" "$VPN_IFACE" "$IFACE"
} > "/etc/amnezia/amneziawg/${VPN_IFACE}.conf"
chmod 600 "/etc/amnezia/amneziawg/${VPN_IFACE}.conf"

# ── Шаг 7: IP форвардинг и запуск ────────────────────────────────────────────
log "IP форвардинг..."
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-awg-forward.conf
sysctl -p /etc/sysctl.d/99-awg-forward.conf

log "Запуск AWG интерфейса ${VPN_IFACE}..."
awg-quick up "/etc/amnezia/amneziawg/${VPN_IFACE}.conf"

log "Автозапуск AWG..."
cat > /etc/systemd/system/awg-quick@.service << 'EOF'
[Unit]
Description=AmneziaWG via awg-quick(8) for %I
After=network-online.target nss-lookup.target
Wants=network-online.target nss-lookup.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/awg-quick up /etc/amnezia/amneziawg/%i.conf
ExecStop=/usr/bin/awg-quick down /etc/amnezia/amneziawg/%i.conf

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "awg-quick@${VPN_IFACE}"

# ── Шаг 8: Сохраняем server.env ──────────────────────────────────────────────
printf "SERVER_IP=%s\nSERVER_PORT=%s\nSERVER_PUBLIC=%s\nVPN_IFACE=%s\nVPN_SUBNET=%s\n" \
    "$SERVER_IP" "$AWG_PORT" "$SERVER_PUBLIC" "$VPN_IFACE" "$VPN_SUBNET" \
    > /etc/amnezia/amneziawg/server.env
printf "JC=%s\nJMIN=%s\nJMAX=%s\nS1=%s\nS2=%s\nH1=%s\nH2=%s\nH3=%s\nH4=%s\n" \
    "$JC" "$JMIN" "$JMAX" "$S1" "$S2" "$H1" "$H2" "$H3" "$H4" \
    >> /etc/amnezia/amneziawg/server.env
printf "PRIMARY_DNS=1.1.1.1\nSECONDARY_DNS=1.0.0.1\n" \
    >> /etc/amnezia/amneziawg/server.env

# ── Шаг 9: Скачиваем скрипты ─────────────────────────────────────────────────
log "Загрузка скриптов управления..."
curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/vpn.sh -o /root/vpn.sh
curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/bot.py  -o /root/bot.py
chmod +x /root/vpn.sh

# ── Шаг 10: Python зависимости ───────────────────────────────────────────────
log "Установка python-telegram-bot..."
if [[ "$INSTALL_MODE" == "1" ]]; then
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages > /dev/null 2>&1 || \
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" > /dev/null 2>&1
else
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages || \
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23"
fi

# ── Шаг 11: Настройка бота ───────────────────────────────────────────────────
echo ""
echo -e "${CYAN}${BOLD}Настройка Telegram бота${NC}"
echo ""
echo -e "  1. Найдите ${YELLOW}@BotFather${NC} в Telegram"
echo -e "  2. Напишите ${YELLOW}/newbot${NC} и следуйте инструкциям"
echo -e "  3. Скопируйте токен вида ${YELLOW}1234567890:AAF...${NC}"
echo ""
while true; do
    read -p "  Вставьте токен бота: " BOT_TOKEN
    [[ "$BOT_TOKEN" == *":"* && ${#BOT_TOKEN} -gt 20 ]] && break
    warn "Неверный формат токена."
done
echo ""
echo -e "  1. Найдите ${YELLOW}@userinfobot${NC} в Telegram"
echo -e "  2. Напишите ему любое сообщение"
echo -e "  3. Скопируйте ваш ID — число"
echo ""
while true; do
    read -p "  Вставьте ваш Telegram ID: " ADMIN_ID
    [[ "$ADMIN_ID" =~ ^[0-9]+$ ]] && break
    warn "ID должен быть числом."
done

printf "BOT_TOKEN=%s\nADMIN_ID=%s\n" "$BOT_TOKEN" "$ADMIN_ID" > /etc/amnezia/amneziawg/bot.env
chmod 600 /etc/amnezia/amneziawg/bot.env

# ── Шаг 12: systemd сервис для бота ──────────────────────────────────────────
log "Настройка автозапуска бота..."
cat > /etc/systemd/system/awg-bot.service << 'EOF'
[Unit]
Description=AmneziaWG Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable awg-bot
systemctl start awg-bot

# ── Шаг 13: Автообновление ───────────────────────────────────────────────────
log "Настройка автообновления..."
cat > /root/update.sh << 'EOF'
#!/bin/bash
RAW="https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main"
CURRENT=$(cat /root/.bot_version 2>/dev/null || echo "none")
LATEST=$(curl -s "https://api.github.com/repos/yntoolsmail-prog/Vpn_AWG/commits/main" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'][:7])")
if [ "$CURRENT" != "$LATEST" ]; then
    curl -s $RAW/bot.py -o /root/bot.py
    curl -s $RAW/vpn.sh -o /root/vpn.sh
    echo $LATEST > /root/.bot_version
    systemctl restart awg-bot
    echo "$(date) — обновлено до $LATEST" >> /var/log/awg-update.log
fi
EOF
chmod +x /root/update.sh
(crontab -l 2>/dev/null; echo "*/5 * * * * /root/update.sh") | crontab -

# ── Готово ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}   Установка завершена!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo ""
info "AWG интерфейс: ${VPN_IFACE}  |  Подсеть: ${VPN_SUBNET}.x  |  Порт: ${AWG_PORT}/UDP"
info "AWG запущен с параметрами: Jc=$JC Jmin=$JMIN Jmax=$JMAX"
info "DNS: 1.1.1.1, 1.0.0.1 (можно изменить, см. README)"
info "Бот: systemctl status awg-bot"
echo ""
echo -e "  Терминал: ${CYAN}bash /root/vpn.sh${NC}"
echo -e "  Telegram: ${CYAN}напишите /start вашему боту${NC}"
echo -e "  Логи:     ${YELLOW}journalctl -u awg-bot -f${NC}"
echo ""