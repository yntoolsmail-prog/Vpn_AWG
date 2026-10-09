#!/bin/bash
# ── Облегчение слабого ВПС ────────────────────────────────────────────────────
# Используется из setup.sh: slim_offer — перед установкой, если сервер слабый
# (памяти < 900 МБ или на диске свободно < 4 ГБ); slim_system — по флагу --slim,
# в том числе на уже работающем основном или слейве. Цвета и log/ok/warn/info —
# из colors.sh/utils.sh. setup.sh работает под set -e, поэтому вызывать только
# как «slim_… || true»: в такой цепочке bash не обрывает функцию на ошибке.
#
# Порядок: проверки → опись (симуляция apt, ничего не меняет) → вопрос →
# журнал и своп → одним вызовом apt удаляется ровно тот список, что прошёл
# сверку → старые ядра → отчёт. Повторный запуск безопасен.
#
# Удаление метапакета (ubuntu-server и т.п.) делает «лишним» всё, что он тянул,
# и автоудаление унесло бы ssh, сеть, cron. Поэтому перед симуляцией нужное
# помечается установленным вручную, а список на удаление сверяется с
# _SLIM_KEEP_RE: при совпадении apt не вызывается вовсе.

_SLIM_LOG=/root/slim.log
_SLIM_APT=(-o DPkg::Lock::Timeout=600)
_SLIM_WEAK_RAM_MB=900
_SLIM_WEAK_DISK_MB=4096
_SLIM_SWAPFILE=/swapfile
_SLIM_FSTAB=/etc/fstab

# Никогда не удаляются. update-notifier-common создаёт /var/run/reboot-required
# при обновлении ядра — по нему бот сообщает «нужна перезагрузка».
# packagekit/polkitd — зависимости software-properties-common (add-apt-repository
# для репозитория Amnezia), cron — продление сертификата веб-панели, rsyslog —
# журнал входов для fail2ban, git — сборка SOCKS5-модуля, cloud-init — у части
# хостеров через него при загрузке настраиваются сеть и пароль.
_SLIM_KEEP_RE='^(openssh-|cloud-init|cloud-guest-utils|netplan|systemd|udev|dbus|sudo|curl|wget|ca-certificates|cron|rsyslog|logrotate|python3$|python3\.[0-9]+$|python3-minimal|python3-pip|python3-apt|python3-yaml|software-properties|packagekit|polkitd|unattended-upgrades|update-notifier-common|linux-image-[0-9]|linux-modules-[0-9]|linux-headers-[0-9]|linux-headers-virtual|linux-image-virtual|linux-virtual|dkms|gcc|make$|build-essential|qemu-guest-agent|grub|shim|initramfs|busybox|iptables|nftables|iproute2|ifupdown|isc-dhcp|apt$|apt-utils|dpkg$|libc6$|libc6-dev|openssl$|gnupg|gpg|tcpdump|strace|mtr|git$|nano|vim|tmux|screen|less$|amneziawg|qrencode|vnstat|fail2ban|nginx|certbot|python3-certbot|dnsutils|bind9-|e2fsprogs|util-linux|mount$|coreutils|bash$|tzdata|locales)'

# Пакеты конкретной версии ядра. В основном удалении не участвуют: работающее
# и самое новое трогать нельзя, остальные убирает _slim_old_kernels.
_SLIM_KVER_RE='^linux-(image|image-unsigned|modules|headers|tools|cloud-tools|buildinfo)-[0-9]'

# Группы для виртуальной машины: железа, которого в ней нет.
_SLIM_GRP_HW="modemmanager udisks2 upower multipath-tools kpartx open-iscsi thermald fwupd fwupd-signed ubuntu-drivers-common usb-modeswitch open-vm-tools"
# Отладка и телеметрия — проектом не используются.
_SLIM_GRP_DEBUG="bpftrace bpfcc-tools python3-bpfcc apport apport-symptoms whoopsie sosreport sysstat pollinate landscape-common popularity-contest ubuntu-report"
# Библиотека для Amazon (~100 МБ), на VPS не нужна.
_SLIM_GRP_CLOUD="python3-boto3 python3-botocore"

# Модули, без которых не работают AWG (udp_tunnel), NAT и правила проекта
# (MASQUERADE, DNAT, REDIRECT, comment), fail2ban (multiport, nftables).
# Перед удалением linux-modules-extra проверяется, что их там нет.
_SLIM_NEED_MODS="tun udp_tunnel ip6_udp_tunnel nf_conntrack nf_nat nf_tables nft_compat nft_chain_nat iptable_nat iptable_filter xt_MASQUERADE xt_nat xt_REDIRECT xt_comment xt_tcpudp xt_conntrack xt_multiport"

# Пакет → процесс: сколько памяти освободится.
declare -A _SLIM_DAEMON=(
    [snapd]=snapd [modemmanager]=ModemManager [udisks2]=udisksd [upower]=upowerd
    [multipath-tools]=multipathd [fwupd]=fwupd [thermald]=thermald
    [open-vm-tools]=vmtoolsd [open-iscsi]=iscsid [whoopsie]=whoopsie
)

_slim_meminfo_mb() { awk -v k="$1:" '$1 == k { printf "%d", $2 / 1024 }' /proc/meminfo; }
_slim_disk_mb()    { df -Pm / 2>/dev/null | awk -v c="$1" 'NR == 2 { print $c }'; }  # 3 — занято, 4 — свободно
_slim_gb()         { awk -v m="$1" 'BEGIN { printf "%.1f", m / 1024 }'; }

# Установленные пакеты из списка (имена или шаблоны dpkg)
_slim_installed() {
    [[ $# -eq 0 ]] && return 0
    dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' "$@" 2>/dev/null | awk '$1 == "ii" { print $2 }'
}

_slim_all_installed() {
    dpkg-query -W -f '${db:Status-Abbrev} ${Package}\n' 2>/dev/null | awk '$1 == "ii" { print $2 }'
}

# Суммарный размер пакетов, МБ
_slim_size_mb() {
    [[ $# -eq 0 ]] && { echo 0; return 0; }
    dpkg-query -W -f '${Installed-Size}\n' "$@" 2>/dev/null | awk '{ s += $1 } END { printf "%d", s / 1024 }'
}

_slim_is_weak() {
    local ram disk
    ram=$(_slim_meminfo_mb MemTotal); disk=$(_slim_disk_mb 4)
    [[ "${ram:-0}" -lt $_SLIM_WEAK_RAM_MB || "${disk:-0}" -lt $_SLIM_WEAK_DISK_MB ]]
}

# Версия ядра без варианта: 6.8.0-146-generic → 6.8.0-146
_slim_kbase() { echo "${1%-*}"; }

_slim_kernels() {
    _slim_installed 'linux-image-[0-9]*' | sed 's/^linux-image-//' | sort -V
}

# Модули, которые для ядра $1 берутся из linux-modules-extra: нужные проекту,
# корневой ФС и — для работающего ядра — уже загруженные (драйверы сети и диска
# виртуалки: virtio, hv_*, xen-*). Пусто — без extra это ядро обходится.
_slim_extra_needed() {
    local k="$1" extra m f mods out=""
    extra=$(dpkg -L "linux-modules-extra-$k" 2>/dev/null | grep '\.ko')
    [[ -z "$extra" ]] && return 0
    mods="$_SLIM_NEED_MODS $(findmnt -no FSTYPE / 2>/dev/null)"
    [[ "$k" == "$(uname -r)" ]] && mods+=" $(lsmod 2>/dev/null | awk 'NR > 1 { print $1 }')"
    for m in $(tr ' ' '\n' <<< "$mods" | sort -u); do
        f=$(modinfo -k "$k" -F filename "$m" 2>/dev/null | head -1)
        [[ -n "$f" ]] && grep -qxF "$f" <<< "$extra" && out+=" $m"
    done
    echo "${out# }"
}

# Каких модулей из списка $2 нет у ядра $1
_slim_missing_mods() {
    local k="$1" m out=""
    for m in $2; do
        modinfo -k "$k" -F filename "$m" > /dev/null 2>&1 || out+=" $m"
    done
    echo "${out# }"
}

# Пакеты старых ядер: всё, кроме работающего. Только когда работает самое
# новое — иначе после перезагрузки загрузилось бы ядро, которого уже нет.
_slim_old_kernel_pkgs() {
    local run newest v re=""
    run=$(uname -r)
    newest=$(_slim_kernels | tail -1)
    [[ -z "$newest" || "$run" != "$newest" ]] && return 0
    for v in $(_slim_kernels); do
        [[ "$v" == "$run" ]] && continue
        v=$(_slim_kbase "$v")
        re+="${re:+|}${v//./\\.}"
    done
    [[ -z "$re" ]] && return 0
    _slim_installed 'linux-*' | grep -E -- "-(${re})(-|$)"
}

# Помечает нужное установленным вручную — иначе удаление метапакета
# отдаст его автоудалению. Пакеты версий ядра не помечаем: ручные ядра apt
# не удаляет никогда, и на маленьком диске они копились бы с каждым обновлением.
# Если проект уже стоит, защищаем и все python3-* из apt: pip не ставит то, что
# уже есть в системе (click для flask, cryptography для paramiko), и их удаление
# сломало бы бота и веб-панель. Аргументы — явные цели, их не защищаем.
_slim_protect() {
    local m targets=" $* "
    {
        for m in ubuntu-minimal ubuntu-standard ubuntu-server-minimal ubuntu-server; do
            apt-cache depends --installed "$m" 2>/dev/null \
                | awk '/(Depends|Recommends):/ && $2 !~ /^</ { print $2 }'
        done
        _slim_all_installed | grep -E "$_SLIM_KEEP_RE"
        [[ -f /root/awg_core.py ]] && _slim_all_installed | grep '^python3-'
    } | grep -vE "$_SLIM_KVER_RE" | sort -u \
      | while read -r m; do [[ "$targets" == *" $m "* ]] || echo "$m"; done \
      | xargs -r apt-mark manual > /dev/null 2>&1 || true
}

# Симуляция: apt-get -s с аргументами → глобальные _SLIM_SIM_RM / _SLIM_SIM_IN /
# _SLIM_SIM_ERR. Возвращает 1, если apt отказал.
_slim_simulate() {
    local out
    out=$(apt-get -s "$@" 2>&1)
    _SLIM_SIM_ERR=$(grep '^E:' <<< "$out")
    _SLIM_SIM_RM=$(awk '/^(Purg|Remv) / { print $2 }' <<< "$out" | sort -u)
    _SLIM_SIM_IN=$(awk '/^Inst / { print $2 }' <<< "$out" | sort -u)
    [[ -z "$_SLIM_SIM_ERR" ]]
}

# ── Опись: решает, что делать, ничего не меняя ───────────────────────────────
_slim_inventory() {
    local virt snaps p m blockers k gmeta suffix vmeta
    local -a targets=()
    _SLIM_NOTES=(); _SLIM_INSTALL=""; _SLIM_KERNEL=0; _SLIM_NEED_FOUND=""
    _SLIM_RM=""; _SLIM_BLOCK=""; _SLIM_OLDK=""; _SLIM_SWAP_MB=0; _SLIM_JOURNAL=0
    _SLIM_GROUP_LINES=()

    virt=$(systemd-detect-virt 2>/dev/null) || true
    _SLIM_VIRT="${virt:-none}"

    # Snap: только если ни одного снапа не установлено
    if [[ -n "$(_slim_installed snapd)" ]]; then
        # По файлам, а не «snap list»: при остановленном snapd тот молчит
        snaps=$(find /var/lib/snapd/snaps -maxdepth 1 -name '*.snap' -printf '%f\n' 2>/dev/null \
                | sed 's/_[^_]*\.snap$//' | sort -u | paste -sd' ')
        if [[ -z "$snaps" ]]; then
            targets+=(snapd)
            _SLIM_GROUP_LINES+=("Snap (снапов нет): snapd")
        else
            _SLIM_NOTES+=("snapd оставлен: установлены снапы ($snaps). Если не нужны: snap remove <имя>, затем setup.sh --slim")
        fi
    fi

    local hw debug cloud
    if [[ "$_SLIM_VIRT" != "none" ]]; then
        # shellcheck disable=SC2086
        hw=$(_slim_installed $_SLIM_GRP_HW)
        [[ "$_SLIM_VIRT" == "vmware" ]] && hw=$(grep -vx open-vm-tools <<< "$hw")
    else
        _SLIM_NOTES+=("Физический сервер: драйверы, прошивки и ядро не трогаю — они нужны железу")
    fi
    # shellcheck disable=SC2086
    debug=$(_slim_installed $_SLIM_GRP_DEBUG)
    # shellcheck disable=SC2086
    cloud=$(_slim_installed $_SLIM_GRP_CLOUD)
    [[ -n "$hw" ]]    && { targets+=($hw);    _SLIM_GROUP_LINES+=("Железо, которого у виртуалки нет: ${hw//$'\n'/ }"); }
    [[ -n "$debug" ]] && { targets+=($debug); _SLIM_GROUP_LINES+=("Отладка и телеметрия: ${debug//$'\n'/ }"); }
    [[ -n "$cloud" ]] && { targets+=($cloud); _SLIM_GROUP_LINES+=("Библиотека Amazon: ${cloud//$'\n'/ }"); }

    # Ядро для виртуалок: тот же образ ядра без прошивок и драйверов для железа
    if [[ "$_SLIM_VIRT" != "none" && "$(uname -r)" == *-generic ]]; then
        local fw extra
        fw=$(_slim_installed 'linux-firmware*' firmware-sof-signed intel-microcode amd64-microcode)
        extra=$(_slim_installed 'linux-modules-extra-*')
        gmeta=$(_slim_installed 'linux-image-generic*' | grep -E '^linux-image-generic(-hwe-[0-9.]+)?$' | sort | tail -1)
        if [[ -n "$fw$extra$gmeta" ]]; then
            blockers=""
            for p in $extra; do
                k="${p#linux-modules-extra-}"
                m=$(_slim_extra_needed "$k")
                [[ -n "$m" ]] && blockers+=" $k: $m;"
            done
            if [[ -n "$gmeta" ]]; then
                # HWE-ядро (22.04 с 6.x) меняем на virtual той же серии, а не на старое GA
                suffix="${gmeta#linux-image-generic}"
                vmeta="linux-virtual${suffix}"
                if [[ -z "$(_slim_installed "linux-image-virtual${suffix}")" ]] \
                   && ! apt-cache show "$vmeta" > /dev/null 2>&1; then
                    blockers+=" нет пакета ${vmeta} в списках apt;"
                fi
            fi
            if [[ -n "$blockers" ]]; then
                _SLIM_NOTES+=("Ядро не трогаю:${blockers%;}")
            else
                _SLIM_KERNEL=1
                if [[ -n "$gmeta" ]]; then
                    [[ -z "$(_slim_installed "linux-image-virtual${suffix}")" ]] && _SLIM_INSTALL="$vmeta"
                    targets+=($(_slim_installed "linux-generic${suffix}" "linux-image-generic${suffix}"))
                fi
                targets+=($fw $extra)
                _SLIM_GROUP_LINES+=("Ядро: прошивки и драйверы для железа ($(echo $fw $extra | wc -w) пакетов)${_SLIM_INSTALL:+ → $_SLIM_INSTALL}")
                for m in $_SLIM_NEED_MODS $(findmnt -no FSTYPE / 2>/dev/null); do
                    modinfo -F filename "$m" > /dev/null 2>&1 && _SLIM_NEED_FOUND+=" $m"
                done
            fi
        fi
    fi

    # Симуляция удаления. Не прошла из-за ядра — пробуем без него.
    if [[ ${#targets[@]} -gt 0 || -n "$_SLIM_INSTALL" ]]; then
        _slim_protect "${targets[@]}"
        local -a args=(--purge --no-install-recommends --autoremove install)
        [[ -n "$_SLIM_INSTALL" ]] && args+=("$_SLIM_INSTALL")
        for p in "${targets[@]}"; do args+=("$p-"); done
        if ! _slim_simulate "${args[@]}" && [[ "$_SLIM_KERNEL" -eq 1 ]]; then
            _SLIM_NOTES+=("Ядро не трогаю: apt отказал — ${_SLIM_SIM_ERR//$'\n'/ }")
            _SLIM_KERNEL=0; _SLIM_INSTALL=""
            unset '_SLIM_GROUP_LINES[-1]'
            args=(--purge --no-install-recommends --autoremove install)
            for p in "${targets[@]}"; do
                [[ "$p" =~ ^(linux-|firmware-sof|intel-microcode|amd64-microcode) ]] || args+=("$p-")
            done
            _slim_simulate "${args[@]}" || true
        fi
        if [[ -n "$_SLIM_SIM_ERR" ]]; then
            _SLIM_NOTES+=("Пакеты не трогаю: apt отказал — ${_SLIM_SIM_ERR//$'\n'/ }")
        else
            local run newest newk
            run=$(_slim_kbase "$(uname -r)"); newest=$(_slim_kbase "$(_slim_kernels | tail -1)")
            for p in $_SLIM_SIM_RM; do
                if [[ "$p" =~ ^linux-modules-extra- ]]; then
                    _SLIM_RM+=" $p"
                elif [[ "$p" =~ $_SLIM_KVER_RE ]]; then
                    # Старое ядро — в _slim_old_kernels; работающее и новейшее — нельзя
                    [[ "$p" == *"-${run}"* || ( -n "$newest" && "$p" == *"-${newest}"* ) ]] && _SLIM_BLOCK+=" $p"
                elif [[ "$p" =~ $_SLIM_KEEP_RE ]]; then
                    _SLIM_BLOCK+=" $p"
                else
                    _SLIM_RM+=" $p"
                fi
            done
            _SLIM_RM="${_SLIM_RM# }"; _SLIM_BLOCK="${_SLIM_BLOCK# }"
            # linux-virtual новее установленного ядра тянет и само ядро
            newk=$(grep -E '^linux-image-[0-9]' <<< "$_SLIM_SIM_IN" | sed 's/^linux-image-//' | paste -sd' ')
            [[ -n "$newk" ]] && _SLIM_NOTES+=("Вместе с $_SLIM_INSTALL поставится ядро $newk — после облегчения нужна перезагрузка")
        fi
    fi

    # Без того, что уже уходит в основном списке (extra старого ядра)
    _SLIM_OLDK=$(_slim_old_kernel_pkgs | grep -vxF -f <(tr ' ' '\n' <<< "$_SLIM_RM") | paste -sd' ')
    if [[ -z "$_SLIM_OLDK" ]]; then
        local run newest
        run=$(uname -r); newest=$(_slim_kernels | tail -1)
        [[ -n "$newest" && "$run" != "$newest" ]] && \
            _SLIM_NOTES+=("Работает ядро $run, а установлено новее — $newest. Перезагрузитесь и запустите bash /root/setup.sh --slim ещё раз: уберу старые ядра")
    fi

    # Своп: при памяти < 1 ГБ без него сборка модуля AWG и pip падают от нехватки памяти
    local ram swap free fstype
    ram=$(_slim_meminfo_mb MemTotal); swap=$(_slim_meminfo_mb SwapTotal); free=$(_slim_disk_mb 4)
    if [[ "${ram:-0}" -lt 1024 && "${swap:-0}" -lt 512 ]]; then
        fstype=$(findmnt -no FSTYPE / 2>/dev/null)
        if [[ "$fstype" != "ext4" && "$fstype" != "xfs" ]]; then
            _SLIM_NOTES+=("Своп не создаю: файловая система ${fstype:-?} — swap-файл на ней создаётся иначе")
        elif [[ -e "$_SLIM_SWAPFILE" ]]; then
            # Подключённый не пересоздаём (fallocate по живому свопу), отключённый — подключим
            if swapon --show=NAME --noheadings 2>/dev/null | grep -qxF "$_SLIM_SWAPFILE"; then
                _SLIM_NOTES+=("Своп $_SLIM_SWAPFILE уже подключён (${swap} МБ) — не трогаю")
            else
                _SLIM_SWAP_MB=-1
            fi
        elif [[ "${free:-0}" -gt 3072 ]]; then
            _SLIM_SWAP_MB=1024
        elif [[ "${free:-0}" -gt 1024 ]]; then
            _SLIM_SWAP_MB=512
        else
            _SLIM_NOTES+=("Своп не создаю: на диске свободно меньше 1 ГБ")
        fi
    fi

    [[ -z "$(_slim_installed update-notifier-common)" ]] && \
        _SLIM_NOTES+=("Нет update-notifier-common: без него бот не узнает, что после обновления ядра нужна перезагрузка. Вернуть: apt install update-notifier-common")

    # Журнал: по умолчанию до 10% диска
    grep -qs '^SystemMaxUse=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf || _SLIM_JOURNAL=1
}

_slim_print_plan() {
    local line p mem=0 r size_rm size_old
    echo ""
    echo -e "  ${BOLD}Что будет сделано:${NC}"
    echo ""
    if [[ -n "$_SLIM_BLOCK" ]]; then
        warn "Пакеты не трогаю: apt удалил бы вместе с ними нужные — $_SLIM_BLOCK"
        echo -e "     От чего они зависят: apt-cache depends <пакет>"
    elif [[ -n "$_SLIM_RM" ]]; then
        for line in "${_SLIM_GROUP_LINES[@]}"; do echo -e "  • $line"; done
        # shellcheck disable=SC2086
        size_rm=$(_slim_size_mb $_SLIM_RM)
        echo -e "  • Всего пакетов на удаление вместе с ненужными зависимостями: $(wc -w <<< "$_SLIM_RM"), ${CYAN}~${size_rm} МБ${NC}"
        for p in $_SLIM_RM; do
            [[ -n "${_SLIM_DAEMON[$p]:-}" ]] || continue
            r=$(ps -C "${_SLIM_DAEMON[$p]}" -o rss= 2>/dev/null | awk '{ s += $1 } END { printf "%d", s / 1024 }')
            [[ "${r:-0}" -gt 0 ]] && { echo -e "      остановится ${_SLIM_DAEMON[$p]}: ${r} МБ памяти"; mem=$((mem + r)); }
        done
    fi
    if [[ -n "$_SLIM_OLDK" ]]; then
        # shellcheck disable=SC2086
        size_old=$(_slim_size_mb $_SLIM_OLDK)
        echo -e "  • Старые ядра (работает $(uname -r)): $(wc -w <<< "$_SLIM_OLDK") пакетов, ${CYAN}~${size_old} МБ${NC}"
    fi
    [[ "$_SLIM_SWAP_MB" -gt 0 ]]  && echo -e "  • Своп: создать $_SLIM_SWAPFILE на ${_SLIM_SWAP_MB} МБ (памяти $(_slim_meminfo_mb MemTotal) МБ, свопа $(_slim_meminfo_mb SwapTotal) МБ)"
    [[ "$_SLIM_SWAP_MB" -eq -1 ]] && echo -e "  • Своп: подключить существующий $_SLIM_SWAPFILE"
    [[ "$_SLIM_JOURNAL" -eq 1 ]]  && echo -e "  • Журнал systemd: не больше 100 МБ (сейчас может занять до 10% диска)"
    [[ "$mem" -gt 0 ]] && echo -e "  • Освободится памяти: ${CYAN}~${mem} МБ${NC}"
    for line in "${_SLIM_NOTES[@]}"; do info "$line"; done
    echo ""
}

_slim_do_journal() {
    [[ "$_SLIM_JOURNAL" -eq 1 ]] || return 0
    mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/awg-slim.conf
    systemctl restart systemd-journald 2> /dev/null || true
    journalctl --vacuum-size=100M > /dev/null 2>&1 || true
    ok "Журнал ограничен 100 МБ"
}

_slim_do_swap() {
    local mb="$_SLIM_SWAP_MB" f="$_SLIM_SWAPFILE"
    [[ "$mb" -ne 0 ]] || return 0
    if [[ "$mb" -gt 0 ]]; then
        log "Создаю своп ${mb} МБ..."
        if ! fallocate -l "${mb}M" "$f" 2> /dev/null; then
            dd if=/dev/zero of="$f" bs=1M count="$mb" status=none \
                || { warn "Не удалось создать $f"; rm -f -- "$f"; return 0; }
        fi
        chmod 600 "$f"
        mkswap "$f" > /dev/null || { warn "mkswap не сработал"; rm -f -- "$f"; return 0; }
    fi
    swapon "$f" 2> /dev/null || { warn "Не удалось подключить $f (хостер может запрещать своп)"; return 0; }
    grep -qE "^${f}[[:space:]]" "$_SLIM_FSTAB" || echo "$f none swap sw 0 0" >> "$_SLIM_FSTAB"
    ok "Своп подключён: $(_slim_meminfo_mb SwapTotal) МБ"
}

_slim_do_packages() {
    [[ -n "$_SLIM_RM" && -z "$_SLIM_BLOCK" ]] || return 0
    local -a args=(--purge --no-install-recommends install)
    local p
    [[ -n "$_SLIM_INSTALL" ]] && args+=("$_SLIM_INSTALL")
    for p in $_SLIM_RM; do args+=("$p-"); done
    # Ровно этот список: сверяем ещё раз без автоудаления — apt не должен
    # захотеть удалить что-то сверх проверенного
    if ! _slim_simulate "${args[@]}" || [[ "$_SLIM_SIM_RM" != "$(tr ' ' '\n' <<< "$_SLIM_RM" | sort -u)" ]]; then
        warn "Повторная сверка не совпала с описью — пакеты не трогаю. Подробности: $_SLIM_LOG"
        { echo "== сверка: ожидалось"; echo "$_SLIM_RM"; echo "== apt хочет удалить"; echo "$_SLIM_SIM_RM"; echo "$_SLIM_SIM_ERR"; } >> "$_SLIM_LOG"
        return 0
    fi
    log "Удаляю пакеты (на одном ядре 2–5 минут: пересобирается загрузочный образ)..."
    echo "== $(date '+%F %T') apt-get ${args[*]}" >> "$_SLIM_LOG"
    if DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get "${_SLIM_APT[@]}" -y "${args[@]}" >> "$_SLIM_LOG" 2>&1; then
        ok "Пакеты удалены"
    else
        warn "apt завершился с ошибкой — хвост $_SLIM_LOG:"
        tail -15 "$_SLIM_LOG"
    fi
}

# После смены ядра: у самого нового ядра должны быть все нужные модули и initrd.
# Если apt поставил ядро новее и ему чего-то не хватает — возвращаем extra.
_slim_check_kernel() {
    [[ "$_SLIM_KERNEL" -eq 1 ]] || return 0
    local newest miss
    newest=$(_slim_kernels | tail -1)
    [[ -n "$newest" ]] || return 0
    miss=$(_slim_missing_mods "$newest" "$_SLIM_NEED_FOUND")
    if [[ -n "$miss" ]]; then
        warn "У ядра $newest нет модулей: $miss — возвращаю linux-modules-extra-$newest"
        DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get "${_SLIM_APT[@]}" install -y "linux-modules-extra-$newest" >> "$_SLIM_LOG" 2>&1 \
            || warn "Не получилось. НЕ перезагружайтесь: apt-get install linux-modules-extra-$newest"
    fi
    [[ -s "/boot/initrd.img-$newest" ]] || \
        warn "Нет /boot/initrd.img-$newest. НЕ перезагружайтесь: update-initramfs -c -k $newest"
}

_slim_do_old_kernels() {
    local pkgs
    pkgs=$(_slim_old_kernel_pkgs | paste -sd' ')
    [[ -n "$pkgs" ]] || return 0
    local run re v rm_list
    run=$(uname -r)
    re=$(_slim_kernels | grep -vx "$run" | while read -r v; do _slim_kbase "$v"; done | sed 's/\./\\./g' | paste -sd'|')
    # shellcheck disable=SC2086
    if ! _slim_simulate purge $pkgs; then
        warn "Старые ядра не трогаю: apt отказал — $_SLIM_SIM_ERR"; return 0
    fi
    rm_list=$(grep -vE -- "-(${re})(-|$)" <<< "$_SLIM_SIM_RM")
    if [[ -n "$rm_list" || "$_SLIM_SIM_RM" == *"$(_slim_kbase "$run")"* ]]; then
        warn "Старые ядра не трогаю: apt удалил бы и другое — ${rm_list//$'\n'/ }"; return 0
    fi
    log "Удаляю старые ядра..."
    echo "== $(date '+%F %T') apt-get purge $pkgs" >> "$_SLIM_LOG"
    # shellcheck disable=SC2086
    if DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get "${_SLIM_APT[@]}" -y purge $pkgs >> "$_SLIM_LOG" 2>&1; then
        ok "Старые ядра удалены"
    else
        warn "apt завершился с ошибкой — хвост $_SLIM_LOG:"; tail -15 "$_SLIM_LOG"
    fi
}

# ── Главная ───────────────────────────────────────────────────────────────────
# $1 = "offer" — вызвано из установки (другой текст вопроса)
slim_system() {
    local mode="$1" ans
    local ram0 avail0 used0 free0 swap0

    echo ""
    echo -e "${CYAN}${BOLD}  Облегчение системы${NC}"
    echo ""
    if systemd-detect-virt --container --quiet 2> /dev/null; then
        warn "Это контейнер ($(systemd-detect-virt 2> /dev/null)): модуль ядра AWG в нём не загрузится — облегчение не поможет."
        return 1
    fi
    if ! grep -qs '^ID=ubuntu' /etc/os-release; then
        warn "Облегчение рассчитано на Ubuntu — здесь пропускаю."
        return 1
    fi
    if [[ -n "$(dpkg --audit 2> /dev/null)" ]]; then
        warn "Система пакетов в незавершённом состоянии. Сначала: dpkg --configure -a"
        return 1
    fi

    ram0=$(_slim_meminfo_mb MemTotal); swap0=$(_slim_meminfo_mb SwapTotal)
    avail0=$(_slim_meminfo_mb MemAvailable)
    used0=$(_slim_disk_mb 3); free0=$(_slim_disk_mb 4)
    info "Память ${ram0} МБ (доступно ${avail0}), своп ${swap0} МБ, диск: занято $(_slim_gb "$used0") ГБ, свободно $(_slim_gb "$free0") ГБ"
    local virt; virt=$(systemd-detect-virt 2> /dev/null) || true
    [[ -z "$virt" || "$virt" == "none" ]] && virt="нет (физический сервер)"
    info "Виртуализация: ${virt}, ядро $(uname -r)"

    # Списки пакетов нужны, чтобы поставить linux-virtual
    log "Смотрю, что установлено..."
    apt-get "${_SLIM_APT[@]}" update -qq >> "$_SLIM_LOG" 2>&1 \
        || warn "Списки пакетов не обновились — работаю с имеющимися."
    _slim_inventory

    if [[ -z "$_SLIM_RM$_SLIM_OLDK$_SLIM_BLOCK" && "$_SLIM_SWAP_MB" -eq 0 && "$_SLIM_JOURNAL" -eq 0 ]]; then
        local n; for n in "${_SLIM_NOTES[@]}"; do info "$n"; done
        ok "Система уже облегчена — удалять нечего."
        return 0
    fi

    _slim_print_plan
    if [[ "$mode" == "offer" ]]; then
        read -r -p "  Облегчить систему перед установкой? [Y/n]: " ans
    else
        read -r -p "  Выполнить? [Y/n]: " ans
    fi
    [[ "${ans,,}" == "n" ]] && { info "Пропущено."; return 1; }

    echo "== $(date '+%F %T') облегчение: $(uname -r), память ${ram0} МБ, свободно ${free0} МБ" >> "$_SLIM_LOG"
    _slim_do_journal
    _slim_do_swap
    _slim_do_packages
    _slim_check_kernel
    _slim_do_old_kernels
    apt-get clean > /dev/null 2>&1 || true

    local used1 free1 avail1 newest
    used1=$(_slim_disk_mb 3); free1=$(_slim_disk_mb 4); avail1=$(_slim_meminfo_mb MemAvailable)
    echo ""
    echo -e "  ${BOLD}Итог:${NC}"
    echo -e "  Диск:   занято $(_slim_gb "$used0") → ${GREEN}$(_slim_gb "$used1") ГБ${NC}, свободно $(_slim_gb "$free0") → ${GREEN}$(_slim_gb "$free1") ГБ${NC}"
    echo -e "  Память: доступно ${avail0} → ${GREEN}${avail1} МБ${NC}, своп ${swap0} → $(_slim_meminfo_mb SwapTotal) МБ"
    echo -e "  Полный вывод apt: $_SLIM_LOG"
    newest=$(_slim_kernels | tail -1)
    # При установке перезагрузку предложит проверка ядра setup.sh сразу после этого
    if [[ "$mode" != "offer" && -n "$newest" && "$newest" != "$(uname -r)" ]]; then
        warn "Установлено ядро новее ($newest) — перезагрузитесь (reboot), затем: bash /root/setup.sh --slim — уберу старое."
    fi
    if [[ "$ram0" -lt 1024 ]]; then
        if systemctl is-active --quiet fail2ban 2> /dev/null; then
            info "fail2ban занимает $(ps -C fail2ban-server -o rss= | awk '{ s += $1 } END { printf "%d", s / 1024 }') МБ. Если вход по SSH только по ключу, он не нужен: apt purge fail2ban"
        else
            info "При памяти меньше 1 ГБ вместо fail2ban (~70 МБ) лучше вход только по ключу: setup.sh --ssh → 2"
        fi
    fi
    echo ""
    return 0
}

# Перед установкой: только на слабом сервере
slim_offer() {
    _slim_is_weak || return 0
    echo ""
    warn "Слабый сервер: памяти $(_slim_meminfo_mb MemTotal) МБ, на диске свободно $(_slim_gb "$(_slim_disk_mb 4)") ГБ."
    echo -e "     Перед установкой лучше убрать лишнее из системы."
    slim_system offer
}
