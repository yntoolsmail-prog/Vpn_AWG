#!/bin/bash
# =============================================================================
# ДИАГНОСТИКА — ПОЛНЫЙ ОТЧЁТ
# =============================================================================
# Используется из vpn.sh. Переменные (SERVER_IP, VPN_IFACE, и др.) — из родительского скрипта.

# Диапазон «5-5» и число «5» — одно значение: awg 3.x печатает H1–H4
# диапазоном, старые утилиты — числом
_diag_norm_range() {
    local v="$1"
    if [[ "$v" =~ ^([0-9]+)-([0-9]+)$ && "${BASH_REMATCH[1]}" == "${BASH_REMATCH[2]}" ]]; then
        v="${BASH_REMATCH[1]}"
    fi
    echo "$v"
}

run_diagnostics() {
    show_header
    echo -e "${BOLD}  Полная диагностика системы${NC}"
    echo ""
    echo -e "  ${CYAN}Собираю данные, подождите...${NC}"
    echo ""

    local TS FILE IS_SLAVE ROLE
    TS=$(date +"%Y%m%d_%H%M%S")
    FILE="${DIAG_DIR}/diag_${TS}.txt"

    # На слейве нет ни бота, ни users.json, ни конфигов клиентов — общий набор
    # проверок давал бы там ложные тревоги, поэтому определяем роль.
    #
    # Маркер /etc/awg-slave создаёт только `setup.sh --slave`. Слейв, добавленный
    # через бота (клонированием конфига по SSH), и слейв, поставленный до
    # появления маркера, его не имеют — поэтому есть запасной признак: bot.env и
    # users.json существуют исключительно на основном сервере. Определив роль по
    # нему, ставим маркер, чтобы дальше он определялся сразу.
    # bot.py признаком не является: setup.sh скачивает его в /root/modules/bot/
    # в обеих ролях, и с ним слейв без маркера всегда считал себя основным.
    IS_SLAVE=0
    if [[ -f /etc/awg-slave ]]; then
        IS_SLAVE=1
    elif [[ ! -f "$BOT_ENV" && ! -f "$USERS_FILE" ]]; then
        IS_SLAVE=1
        touch /etc/awg-slave 2>/dev/null || true
    fi
    if [[ "$IS_SLAVE" -eq 1 ]]; then
        ROLE="СЛЕЙВ (копия основного сервера)"
    else
        ROLE="ОСНОВНОЙ"
    fi

    {
        echo "════════════════════════════════════════════════════════════════"
        echo "  AmneziaWG — Диагностический отчёт"
        echo "  Роль сервера: ${ROLE}"
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
        # -S, а не -L -n: без -v список не показывает интерфейсы, и по нему не
        # понять, к awg0/какому внешнему интерфейсу относится правило
        echo "--- iptables (FORWARD и NAT для AWG) ---"
        iptables -S FORWARD 2>/dev/null | head -20 || echo "нет правил"
        echo ""
        iptables -t nat -S POSTROUTING 2>/dev/null | head -10 || echo "нет правил NAT"
        local IPT_DUPS
        IPT_DUPS=$( { iptables -S FORWARD; iptables -t nat -S POSTROUTING; } 2>/dev/null \
            | grep -v '^-P' | sort | uniq -d | wc -l)
        if [[ "$IPT_DUPS" -gt 0 ]]; then
            echo "  ⚠️  Повторяющихся правил: ${IPT_DUPS} — PostUp срабатывал без PostDown"
        fi
        echo ""
        echo "--- Входящий трафик (INPUT / ufw) ---"
        iptables -S INPUT 2>/dev/null | head -25
        if command -v ufw &>/dev/null; then
            ufw status 2>/dev/null | head -15
        fi
        echo ""

        # ── AWG ──────────────────────────────────────────────────────────────
        echo "━━━ AWG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "Интерфейс: ${VPN_IFACE}"
        echo "Статус сервиса: $(systemctl is-active awg-quick@${VPN_IFACE})"
        echo ""
        echo "--- awg show ---"
        awg show "$VPN_IFACE" 2>/dev/null || echo "⚠️  AWG не отвечает"
        echo ""

        # ── Версии: утилиты ↔ модуль ядра ─────────────────────────────────────
        # AWG 3 передаёт H1–H4 в netlink другим типом (u64 вместо строки), и
        # утилиты 1.0.x/2.0 с модулем 3.x не могут их ни задать, ни прочитать.
        # Модуль из DKMS пересобирается под новое ядро и подхватывается только
        # при перезагрузке — поэтому такая поломка обычно «случается» после ребута.
        echo "--- Версии AmneziaWG ---"
        local TOOLS_VER MOD_LOADED MOD_DISK
        TOOLS_VER=$(awg --version 2>/dev/null | grep -oE 'v[0-9][0-9.]*' | head -1)
        MOD_LOADED=$(cat /sys/module/amneziawg/version 2>/dev/null)
        MOD_DISK=$(modinfo -F version amneziawg 2>/dev/null)
        echo "Утилиты awg:               ${TOOLS_VER:-не определить}"
        echo "Модуль ядра (загружен):    ${MOD_LOADED:-не загружен}"
        echo "Модуль ядра (на диске):    ${MOD_DISK:-нет для ядра $(uname -r)}"
        dpkg -l 'amneziawg*' 2>/dev/null | awk '/^ii/{printf "Пакет %-20s %s\n", $2, $3}'
        if [[ -n "$MOD_LOADED" && -n "$MOD_DISK" && "$MOD_LOADED" != "$MOD_DISK" ]]; then
            echo "  ⚠️  Загружен не тот модуль, что установлен: новый подхватится при"
            echo "      следующей перезагрузке, и поведение AWG может смениться вместе с ним."
        fi
        if [[ "${TOOLS_VER#v}" =~ ^[12]\. && "$MOD_LOADED" =~ ^[3-9]\. ]]; then
            echo "  ⚠️  Утилиты awg старше модуля ядра (AWG 3): H1–H4 не применятся."
            echo "      Обновите утилиты: apt install --only-upgrade amneziawg-tools"
        fi
        echo ""

        # ── Обфускация: что реально применено к интерфейсу ────────────────────
        # Пакет клиента с заголовком H1, которого сервер не ждёт, отбрасывается
        # молча: ни ошибки, ни строки в логе — просто нет хендшейка. Поэтому
        # сверяем интерфейс с awg0.conf, а на основном — ещё и с server.env:
        # из него gen_obfs() собирает конфиги клиентов. На слейве server.env
        # остаётся от его собственной установки и эталоном не является.
        echo "--- Обфускация: awg0.conf ↔ интерфейс ---"
        local OBF_KEY OBF_CONF OBF_LIVE OBF_DEF OBF_ENV OBF_ENV_NAME
        local OBF_MISMATCH=0 OBF_ENV_MISMATCH=0 OBF_DEFAULT_H=1 OBF_ABSENT=0
        if [[ "$IS_SLAVE" -eq 0 ]]; then
            printf "  %-6s %-22s %-22s %s\n" "ПАРАМ" "awg0.conf" "server.env" "ИНТЕРФЕЙС"
        else
            printf "  %-6s %-22s %s\n" "ПАРАМ" "awg0.conf" "ИНТЕРФЕЙС"
        fi
        for OBF_KEY in Jc Jmin Jmax S1 S2 H1 H2 H3 H4; do
            case "$OBF_KEY" in
                H1) OBF_DEF=1 ;; H2) OBF_DEF=2 ;; H3) OBF_DEF=3 ;; H4) OBF_DEF=4 ;;
                *)  OBF_DEF=0 ;;
            esac
            OBF_CONF=$(awk -v k="$OBF_KEY" '
                /^\[Peer\]/ { exit }
                index($0, "=") {
                    key = substr($0, 1, index($0, "=") - 1); gsub(/[ \t]/, "", key)
                    if (tolower(key) == tolower(k)) {
                        v = substr($0, index($0, "=") + 1); gsub(/[ \t\r]/, "", v)
                        print v; exit
                    }
                }' "$AWG_CONF" 2>/dev/null)
            OBF_LIVE=$(_diag_norm_range "$(awg show "$VPN_IFACE" "${OBF_KEY,,}" 2>/dev/null)")
            local OBF_MARK=""
            if [[ "$OBF_LIVE" != "$(_diag_norm_range "${OBF_CONF:-$OBF_DEF}")" ]]; then
                OBF_MISMATCH=1
                OBF_MARK="  ⚠️"
            fi
            if [[ "$OBF_KEY" == H* && "$OBF_LIVE" != "$OBF_DEF" ]]; then
                OBF_DEFAULT_H=0
            fi
            [[ -z "$OBF_CONF" ]] && OBF_ABSENT=1
            # printf выравнивает по байтам, поэтому в колонках только ASCII
            if [[ "$IS_SLAVE" -eq 0 ]]; then
                # JC/JMIN/…/H4 — переменные из server.env, его подключает vpn.sh
                OBF_ENV_NAME="${OBF_KEY^^}"
                OBF_ENV=$(_diag_norm_range "${!OBF_ENV_NAME}")
                if [[ -n "$OBF_ENV" && "$OBF_ENV" != "$OBF_LIVE" ]]; then
                    OBF_ENV_MISMATCH=1
                    OBF_MARK="  ⚠️"
                fi
                printf "  %-6s %-22s %-22s %s%s\n" "$OBF_KEY" "${OBF_CONF:--}" "${OBF_ENV:--}" \
                    "${OBF_LIVE:-?}" "$OBF_MARK"
            else
                printf "  %-6s %-22s %s%s\n" "$OBF_KEY" "${OBF_CONF:--}" "${OBF_LIVE:-?}" "$OBF_MARK"
            fi
        done
        if [[ "$OBF_ABSENT" -eq 1 ]]; then
            echo "  (- = не задан в awg0.conf: действует значение по умолчанию —"
            echo "   0 для Jc/Jmin/Jmax/S1/S2, 1/2/3/4 для H1–H4)"
        fi
        if [[ "$OBF_MISMATCH" -eq 1 ]]; then
            echo "  ⚠️  Интерфейс работает НЕ с теми параметрами, что записаны в awg0.conf."
            echo "      Клиенты с параметрами из конфига не пройдут хендшейк. Частая"
            echo "      причина — утилиты и модуль разных версий (см. «Версии AmneziaWG»)."
        fi
        if [[ "$OBF_ENV_MISMATCH" -eq 1 ]]; then
            echo "  ⚠️  server.env расходится с интерфейсом: конфиги клиентов собираются"
            echo "      из server.env, поэтому новые и перевыпущенные не пройдут хендшейк."
        fi
        if [[ "$OBF_MISMATCH" -eq 0 && "$OBF_ENV_MISMATCH" -eq 0 ]]; then
            if [[ "$IS_SLAVE" -eq 0 ]]; then
                echo "  ✅ Интерфейс работает с параметрами из awg0.conf и server.env"
            else
                echo "  ✅ Интерфейс работает с параметрами из awg0.conf"
                echo "     (сверьте с основным: там должны быть те же значения)"
            fi
        fi
        # 1–4 — не поломка: так ставил setup.sh 13 апреля, и клиенты тех установок
        # выпущены с ними же. Но хендшейк тогда — обычный WireGuard-пакет, и
        # маскируют его только junk-пакеты (Jc), что DPI распознаёт легче.
        if [[ "$OBF_DEFAULT_H" -eq 1 ]]; then
            echo "  ℹ️  H1–H4 = 1–4 — стандартные заголовки WireGuard: хендшейк маскируют"
            echo "      только junk-пакеты (Jc). Работает, если у клиентов тоже 1–4,"
            echo "      но DPI такой трафик распознаёт легче, чем со случайными H1–H4."
        fi
        echo ""

        local AWG_DUMP
        AWG_DUMP=$(awg show "$VPN_IFACE" dump 2>/dev/null)
        # Счётчики только через awk: grep -c при нуле совпадений печатает 0 И
        # возвращает код 1, из-за чего "|| echo 0" дописывал второй ноль
        local PEER_COUNT_AWG
        PEER_COUNT_AWG=$(echo "$AWG_DUMP" | tail -n +2 | awk 'NF{n++} END{print n+0}')
        local FILE_COUNT
        FILE_COUNT=$(ls "$CLIENTS_DIR"/*.conf 2>/dev/null | wc -l)

        # Пиры, записанные в awg0.conf: "имя<TAB>публичный ключ<TAB>allowed ip"
        local CONF_PEERS PEER_COUNT_CONF NAMED_COUNT
        CONF_PEERS=$(awk '
            /^# Client:/  { name = substr($0, index($0, ":") + 2); next }
            /^PublicKey/  { pub = $3 }
            /^AllowedIPs/ { printf "%s\t%s\t%s\n", (name == "" ? "—" : name), pub, $3
                            name = ""; pub = "" }
        ' "$AWG_CONF" 2>/dev/null)
        PEER_COUNT_CONF=$(awk '/^\[Peer\]/{n++}   END{print n+0}' "$AWG_CONF" 2>/dev/null)
        NAMED_COUNT=$(awk    '/^# Client:/{n++}   END{print n+0}' "$AWG_CONF" 2>/dev/null)
        PEER_COUNT_CONF=${PEER_COUNT_CONF:-0}
        NAMED_COUNT=${NAMED_COUNT:-0}

        echo "--- Сводка клиентов ---"
        echo "Пиров на интерфейсе (awg show):   ${PEER_COUNT_AWG}"
        echo "Пиров в конфиге (awg0.conf):      ${PEER_COUNT_CONF}"
        echo "Из них с именем (# Client:):      ${NAMED_COUNT}"
        if [[ "$IS_SLAVE" -eq 0 ]]; then
            echo "Файлов .conf:                     ${FILE_COUNT}"
            if [[ "$PEER_COUNT_AWG" != "$FILE_COUNT" ]]; then
                echo "⚠️  РАСХОЖДЕНИЕ! Возможны висячие пиры или файлы без пира."
            else
                echo "✅ Количество совпадает"
            fi
        else
            echo "Файлов .conf:                     нет (норма для слейва — они только на основном)"
        fi
        echo ""

        # ── Хендшейки ─────────────────────────────────────────────────────────
        # Ни одного хендшейка у всех пиров за долгую работу интерфейса — это не
        # «никто не пришёл», а почти всегда «пакеты не доходят или отбрасываются».
        echo "--- Хендшейки ---"
        local HS_NOW HS_EVER HS_RECENT AWG_MONO AWG_UP_MIN=""
        HS_NOW=$(date +%s)
        HS_EVER=$(echo "$AWG_DUMP" | tail -n +2 | awk 'NF && $5 > 0 {n++} END{print n+0}')
        HS_RECENT=$(echo "$AWG_DUMP" | tail -n +2 \
            | awk -v now="$HS_NOW" 'NF && $5 > 0 && now - $5 < 180 {n++} END{print n+0}')
        # Монотонное время, а не ActiveEnterTimestamp: тот печатается с
        # аббревиатурой пояса (MSK), которую date -d не разбирает
        AWG_MONO=$(systemctl show -p ActiveEnterTimestampMonotonic --value "awg-quick@${VPN_IFACE}" 2>/dev/null)
        if [[ "$AWG_MONO" =~ ^[0-9]+$ && "$AWG_MONO" -gt 0 ]]; then
            AWG_UP_MIN=$(awk -v m="$AWG_MONO" '{printf "%d", ($1 - m / 1000000) / 60}' /proc/uptime)
        fi
        echo "Онлайн (хендшейк < 3 мин):        ${HS_RECENT}"
        echo "Был хендшейк с запуска AWG:       ${HS_EVER} из ${PEER_COUNT_AWG}"
        [[ -n "$AWG_UP_MIN" ]] && echo "AWG поднят:                       ${AWG_UP_MIN} мин назад"
        if [[ "$PEER_COUNT_AWG" -gt 0 && "$HS_EVER" -eq 0 && "${AWG_UP_MIN:-0}" -ge 30 ]]; then
            echo "  ⚠️  Ни один клиент не прошёл хендшейк за ${AWG_UP_MIN} мин."
            echo "      Если клиенты выбирали этот сервер, их пакеты либо не доходят"
            echo "      (блокировка, файрвол провайдера), либо отбрасываются (ключ сервера"
            echo "      или обфускация не совпали с конфигами клиентов — см. выше)."
            echo "      Отличить: tcpdump -ni ${HOST_IFACE:-eth0} -c 20 udp port ${SERVER_PORT}"
            echo "      во время попытки подключения."
        fi
        echo ""

        # ── Сверка «живой интерфейс ↔ конфиг на диске» ────────────────────────
        # Главная проверка удаления устройства: снять пир командой мало, надо
        # ещё вырезать блок из awg0.conf — иначе он вернётся при перезапуске AWG.
        echo "--- Сверка: интерфейс ↔ awg0.conf ---"
        local LIVE_PUBS CONF_PUBS ONLY_LIVE ONLY_CONF
        LIVE_PUBS=$(echo "$AWG_DUMP" | tail -n +2 | awk 'NF{print $1}' | sort -u)
        CONF_PUBS=$(echo "$CONF_PEERS" | awk -F'\t' 'NF{print $2}' | sort -u)
        ONLY_CONF=$(comm -13 <(echo "$LIVE_PUBS") <(echo "$CONF_PUBS") | awk 'NF{n++} END{print n+0}')
        ONLY_LIVE=$(comm -23 <(echo "$LIVE_PUBS") <(echo "$CONF_PUBS") | awk 'NF{n++} END{print n+0}')
        if [[ "$ONLY_CONF" == "0" && "$ONLY_LIVE" == "0" ]]; then
            echo "  ✅ Полное совпадение — после перезапуска AWG состав пиров не изменится"
        fi
        if [[ "$ONLY_CONF" != "0" ]]; then
            echo "  ⚠️  В конфиге есть ${ONLY_CONF} пир(ов), которых нет на интерфейсе."
            echo "      ВЕРНУТСЯ при перезапуске AWG. Если устройство удалялось —"
            echo "      удаление доехало не полностью:"
            comm -13 <(echo "$LIVE_PUBS") <(echo "$CONF_PUBS") | while read -r P; do
                [[ -z "$P" ]] && continue
                echo "        $(echo "$CONF_PEERS" | awk -F'\t' -v k="$P" '$2==k{print $1" ("$3")"; exit}')"
            done
        fi
        if [[ "$ONLY_LIVE" != "0" ]]; then
            echo "  ⚠️  На интерфейсе есть ${ONLY_LIVE} пир(ов), которых нет в конфиге."
            echo "      ИСЧЕЗНУТ при перезапуске AWG:"
            comm -23 <(echo "$LIVE_PUBS") <(echo "$CONF_PUBS") | while read -r P; do
                [[ -z "$P" ]] && continue
                echo "        ${P}  $(echo "$AWG_DUMP" | awk -v k="$P" '$1==k{print $4}')"
            done
        fi
        echo ""

        echo "--- Проверка дублей IP ---"
        local DUP_IPS
        DUP_IPS=$(echo "$CONF_PEERS" | awk -F'\t' 'NF{print $3}' | sort | uniq -d)
        if [[ -z "$DUP_IPS" ]]; then
            echo "  ✅ Дублей AllowedIPs нет"
        else
            echo "  ⚠️  Один адрес выдан нескольким клиентам:"
            echo "$DUP_IPS" | while read -r D; do
                [[ -z "$D" ]] && continue
                echo "        ${D} → $(echo "$CONF_PEERS" | awk -F'\t' -v ip="$D" '$3==ip{printf "%s ", $1}')"
            done
        fi
        echo ""

        if [[ "$IS_SLAVE" -eq 1 ]]; then
            # На слейве нет файлов клиентов — таблицу строим из awg0.conf
            echo "--- Клиенты слейва (из awg0.conf + awg show) ---"
            printf "%-30s %-16s %-22s %s\n" "ИМЯ" "IP" "ХЕНДШЕЙК" "ТРАФИК"
            echo "────────────────────────────────────────────────────────────────────"
            local NOW_S
            NOW_S=$(date +%s)
            echo "$CONF_PEERS" | while IFS=$'\t' read -r CNAME CPUB CIP; do
                [[ -z "$CPUB" ]] && continue
                local PLINE HS RX TX HS_FMT TRAFFIC STATUS
                PLINE=$(echo "$AWG_DUMP" | awk -v k="$CPUB" '$1==k')
                HS=$(echo "$PLINE" | awk '{print $5}')
                RX=$(echo "$PLINE" | awk '{print $6}')
                TX=$(echo "$PLINE" | awk '{print $7}')
                HS_FMT=$(fmt_handshake "${HS:-0}")
                [[ -n "$RX" ]] && TRAFFIC="↓$(numfmt --to=iec ${RX:-0} 2>/dev/null) ↑$(numfmt --to=iec ${TX:-0} 2>/dev/null)" || TRAFFIC="нет данных"
                if [[ -z "$HS" || "$HS" == "0" ]]; then
                    STATUS="⚫ не подключался к этому серверу"
                elif (( NOW_S - HS < 180 )); then
                    STATUS="🟢 онлайн"
                else
                    STATUS="⚫ оффлайн"
                fi
                printf "%-30s %-16s %-22s %-20s %s\n" "$CNAME" "${CIP%%/*}" "$HS_FMT" "$TRAFFIC" "$STATUS"
            done
            echo ""
            echo "  Примечание: пир без хендшейка — норма. Клиент подключается к тому"
            echo "  серверу, чей эндпоинт выбран в его конфиге; остальные его просто ждут."
            echo ""
        fi

        if [[ "$IS_SLAVE" -eq 0 ]]; then
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
        fi   # конец блока «только основной сервер»

        # ── ЦЕЛОСТНОСТЬ ДАННЫХ ────────────────────────────────────────────────
        echo "━━━ ЦЕЛОСТНОСТЬ ДАННЫХ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        if [[ "$IS_SLAVE" -eq 0 ]]; then
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
        fi   # конец блока «только основной сервер»

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

        if [[ "$IS_SLAVE" -eq 1 ]]; then
            echo "--- Обфускация (должна совпадать с основным сервером) ---"
            grep -E '^(Jc|Jmin|Jmax|S1|S2|H1|H2|H3|H4|i1|ListenPort)\s*=' "$AWG_CONF" 2>/dev/null \
                | sed 's/^/  /' || echo "  не найдена"
            echo ""
            echo "  Если эти значения или SERVER_PUBLIC выше разошлись с основным —"
            echo "  конфиги клиентов на этом сервере работать не будут."
            echo "  Лечится кнопкой «Синхронизировать» в карточке сервера в боте."
            echo ""
        else
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
        fi

        # ── БОТ ──────────────────────────────────────────────────────────────
        if [[ "$IS_SLAVE" -eq 1 ]]; then
        echo "━━━ TELEGRAM БОТ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Не применимо: бот ставится только на основной сервер."
        echo ""
        echo "--- Последние 20 строк лога AWG сервиса ---"
        journalctl -u "awg-quick@${VPN_IFACE}" -n 20 --no-pager 2>/dev/null
        echo ""
        else
        echo "━━━ TELEGRAM БОТ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "Статус сервиса: $(systemctl is-active ${BOT_SERVICE})"
        echo "python-telegram-bot: $(pip3 show python-telegram-bot 2>/dev/null | grep Version | awk '{print $2}')"
        echo "Python: $(python3 --version 2>&1)"
        echo ""
        echo "--- Ошибки за последние 24 часа ---"
        journalctl -u ${BOT_SERVICE} -p err --since "24 hours ago" --no-pager 2>/dev/null | tail -20 || echo "нет ошибок"
        echo ""
        echo "--- Последние 100 строк лога бота ---"
        journalctl -u ${BOT_SERVICE} -n 100 --no-pager 2>/dev/null
        echo ""
        echo "--- Последние 20 строк лога AWG сервиса ---"
        journalctl -u "awg-quick@${VPN_IFACE}" -n 20 --no-pager 2>/dev/null
        echo ""
        fi   # конец блока «только основной сервер»

        # ── ПРОИЗВОДИТЕЛЬНОСТЬ ────────────────────────────────────────────────
        echo "━━━ ПРОИЗВОДИТЕЛЬНОСТЬ И ТРАФИК ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
        if [[ "$IS_SLAVE" -eq 0 ]]; then
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
        else
            echo "  Пики и поминутный лог ведёт бот на основном сервере —"
            echo "  на слейве их нет. Трафик этого сервера — в vnstat ниже."
            echo ""
        fi
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
        elif [[ "$IS_SLAVE" -eq 1 ]]; then
            echo "  Бэкапов нет — норма для слейва."
            echo "  Бэкап делается на основном сервере и содержит всю инфраструктуру."
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

    local FILES=( "$DIAG_DIR"/*.txt )
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
