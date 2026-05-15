#!/bin/bash
# ── Управление модулями ───────────────────────────────────────────────────────
# Используется из setup.sh. Переменные REPO_RAW, SLAVE_MODE — из родительского скрипта.

declare -A _MOD_DESC=(
    ["tma"]="Веб-панель TMA (Telegram Mini App)"
    ["mtproxy"]="Прокси Telegram (MTProxy)"
    ["socks5"]="SOCKS5 маршрутизация клиентов"
    ["slave_servers"]="Управление slave-серверами (добавление secondary VPS через SSH)"
)
_BASE_MODS=("bot")

_is_base_module() {
    local name="$1"
    for b in "${_BASE_MODS[@]}"; do [[ "$b" == "$name" ]] && return 0; done
    return 1
}

_modules_conf_set() {
    local name="$1" state="$2"
    if grep -q "^${name}=" /root/modules.conf 2>/dev/null; then
        sed -i "s|^${name}=.*|${name}=${state}|" /root/modules.conf
    else
        echo "${name}=${state}" >> /root/modules.conf
    fi
}

_mod_is_installed() {
    [[ -f "/root/modules/${1}/.installed" ]]
}

# Установить выбранные модули
_install_modules() {
    local -n _to_install=$1   # indexed array of module names to install

    local changed=0
    for name in "${_to_install[@]}"; do
        if _mod_is_installed "$name"; then
            info "Модуль ${name} уже установлен — пропускаю."
            continue
        fi

        log "Установка модуля: ${name}..."
        mkdir -p "/root/modules/${name}"

        if curl -fsSL "${REPO_RAW}/modules/${name}/__init__.py" \
                -o "/root/modules/${name}/__init__.py.new" 2>/dev/null; then
            mv "/root/modules/${name}/__init__.py.new" "/root/modules/${name}/__init__.py"
            ok "Файлы модуля ${name} загружены"
        else
            warn "Не удалось скачать модуль ${name} — пропуск."
            continue
        fi

        # Скачать strings.py если есть
        curl -fsSL "${REPO_RAW}/modules/${name}/strings.py" \
                -o "/root/modules/${name}/strings.py.new" 2>/dev/null \
            && mv "/root/modules/${name}/strings.py.new" "/root/modules/${name}/strings.py" \
            || rm -f "/root/modules/${name}/strings.py.new"

        # Скачать install.sh если есть
        curl -fsSL "${REPO_RAW}/modules/${name}/install.sh" \
                -o "/root/modules/${name}/install.sh.new" 2>/dev/null \
            && mv "/root/modules/${name}/install.sh.new" "/root/modules/${name}/install.sh" \
            && chmod +x "/root/modules/${name}/install.sh" \
            || rm -f "/root/modules/${name}/install.sh.new"

        # Скачать uninstall.sh если есть
        curl -fsSL "${REPO_RAW}/modules/${name}/uninstall.sh" \
                -o "/root/modules/${name}/uninstall.sh.new" 2>/dev/null \
            && mv "/root/modules/${name}/uninstall.sh.new" "/root/modules/${name}/uninstall.sh" \
            && chmod +x "/root/modules/${name}/uninstall.sh" \
            || rm -f "/root/modules/${name}/uninstall.sh.new"

        if [[ -f "/root/modules/${name}/install.sh" ]]; then
            log "Запуск установщика модуля ${name}..."
            bash "/root/modules/${name}/install.sh" || {
                warn "Установщик модуля ${name} завершился с ошибкой."
            }
        fi

        _modules_conf_set "$name" "on"
        changed=1
    done

    if [[ $changed -eq 1 ]]; then
        log "Перезапускаю awg-bot..."
        systemctl restart awg-bot 2>/dev/null && ok "awg-bot перезапущен" || warn "awg-bot не запущен"
        echo ""
        ok "Готово."
    else
        info "Изменений нет."
    fi
}

# Удалить модуль: запустить uninstall.sh, убрать директорию и запись из modules.conf
_delete_module() {
    local name="$1"

    _is_base_module "$name" && { warn "Базовый модуль ${name} нельзя удалить."; return 1; }

    echo ""
    warn "Будет удалён модуль ${name} и все его системные компоненты."
    warn "Данные конфигурации (ключи, настройки) сохраняются."
    read -p "  Подтвердить удаление? [y/N]: " _confirm
    [[ "$_confirm" =~ ^[Yy]$ ]] || { info "Отмена."; return 0; }

    local _mod_dir="/root/modules/${name}"

    # Скачать актуальный uninstall.sh если нет
    if [[ ! -f "${_mod_dir}/uninstall.sh" ]]; then
        curl -fsSL "${REPO_RAW}/modules/${name}/uninstall.sh" \
            -o "${_mod_dir}/uninstall.sh.new" 2>/dev/null \
            && mv "${_mod_dir}/uninstall.sh.new" "${_mod_dir}/uninstall.sh" \
            && chmod +x "${_mod_dir}/uninstall.sh" \
            || rm -f "${_mod_dir}/uninstall.sh.new"
    fi

    if [[ -f "${_mod_dir}/uninstall.sh" ]]; then
        log "Запуск удалятора модуля ${name}..."
        bash "${_mod_dir}/uninstall.sh" || warn "Удалятор завершился с ошибкой — продолжаю."
    else
        warn "uninstall.sh не найден — удаляю только директорию модуля."
    fi

    rm -rf "$_mod_dir"
    sed -i "/^${name}=/d" /root/modules.conf
    ok "Модуль ${name} полностью удалён."
}

# Интерактивное меню модулей
_modules_menu() {
    local _repo_conf _offline=0
    _repo_conf=$(curl -fsSL --max-time 10 "${REPO_RAW}/modules.conf" 2>/dev/null) || true

    if [[ -z "$_repo_conf" ]]; then
        warn "GitHub недоступен — использую локальный список модулей."
        _repo_conf=$(cat /root/modules.conf 2>/dev/null) || {
            warn "Не удалось прочитать /root/modules.conf."
            return
        }
        _offline=1
    fi

    local -a _avail=()
    declare -A _repo_desc=()
    local _last_comment=""
    while IFS= read -r _line; do
        if [[ "$_line" =~ ^#[[:space:]]*(.*) ]]; then
            _last_comment="${BASH_REMATCH[1]}"
        elif [[ "$_line" =~ ^([a-zA-Z0-9_-]+)= ]]; then
            local _rn="${BASH_REMATCH[1]}"
            _is_base_module "$_rn" && { _last_comment=""; continue; }
            _avail+=("$_rn")
            [[ -n "$_last_comment" ]] && _repo_desc["$_rn"]="$_last_comment"
            _last_comment=""
        else
            _last_comment=""
        fi
    done <<< "$_repo_conf"

    # На slave-серверах только релевантные модули
    if [[ "${SLAVE_MODE:-0}" -eq 1 ]]; then
        local -a _filtered=()
        for _m in "${_avail[@]}"; do
            [[ "$_m" == "socks5" || "$_m" == "mtproxy" ]] && _filtered+=("$_m")
        done
        _avail=("${_filtered[@]}")
    fi

    if [[ ${#_avail[@]} -eq 0 ]]; then
        warn "Дополнительных модулей не найдено."
        return
    fi

    # Модули отмеченные для установки (pending)
    declare -A _pending=()

    while true; do
        clear
        echo -e "${CYAN}${BOLD}"
        echo "  ╔══════════════════════════════════════════╗"
        echo "  ║    AmneziaWG — Управление модулями      ║"
        echo "  ╚══════════════════════════════════════════╝"
        echo -e "${NC}"
        echo ""
        echo -e "  ${BOLD}Базовые (всегда активны):${NC}"
        echo -e "  ${GREEN}[✓]${NC} bot  — Telegram бот"
        echo ""
        [[ "$_offline" -eq 1 ]] && \
            echo -e "  ${YELLOW}[!] Офлайн-режим — установка недоступна${NC}\n"
        echo -e "  ${BOLD}Дополнительные:${NC}"
        echo ""
        local _i=1
        for _n in "${_avail[@]}"; do
            local _desc="${_repo_desc[$_n]:-${_MOD_DESC[$_n]:-$_n}}"
            if _mod_is_installed "$_n"; then
                echo -e "  ${CYAN}${_i})${NC} ${GREEN}[✓]${NC} ${_n}  — ${_desc}  ${YELLOW}d${_i} — удалить${NC}"
            elif [[ "${_pending[$_n]:-0}" -eq 1 ]]; then
                echo -e "  ${CYAN}${_i})${NC} ${CYAN}[+]${NC} ${_n}  — ${_desc}  ${CYAN}(к установке)${NC}"
            else
                echo -e "  ${CYAN}${_i})${NC} [ ] ${_n}  — ${_desc}"
            fi
            ((_i++))
        done
        echo ""
        echo -e "  ${BOLD}Команды:${NC}"
        echo -e "  ${CYAN}<N>${NC}    — отметить для установки"
        echo -e "  ${CYAN}d<N>${NC}  — удалить установленный модуль"
        echo -e "  ${CYAN}a${NC}     — установить отмеченные"
        echo -e "  ${CYAN}0${NC}     — выход"
        echo ""
        read -p "  > " _ch

        case "$_ch" in
            0) return ;;
            a|A)
                echo ""
                if [[ "$_offline" -eq 1 ]]; then
                    warn "Офлайн-режим: установка недоступна без интернета."
                    read -p "  Нажмите Enter..." _dummy
                    continue
                fi
                local -a _to_install=()
                for _n in "${_avail[@]}"; do
                    [[ "${_pending[$_n]:-0}" -eq 1 ]] && _to_install+=("$_n")
                done
                if [[ ${#_to_install[@]} -eq 0 ]]; then
                    info "Нет модулей для установки. Выберите <N>."
                    sleep 2
                    continue
                fi
                _install_modules _to_install
                # Сбрасываем pending после установки
                for _n in "${_avail[@]}"; do _pending["$_n"]=0; done
                read -p "  Нажмите Enter для возврата в меню..." _dummy
                ;;
            d[0-9]*)
                local _dnum="${_ch#d}"
                local _didx=$((_dnum - 1))
                if [[ "$_dnum" =~ ^[0-9]+$ && $_didx -ge 0 && $_didx -lt ${#_avail[@]} ]]; then
                    local _dname="${_avail[$_didx]}"
                    if ! _mod_is_installed "$_dname"; then
                        warn "Модуль ${_dname} не установлен."
                        sleep 1
                    else
                        _delete_module "$_dname"
                        read -p "  Нажмите Enter для возврата в меню..." _dummy
                    fi
                else
                    warn "Неверный номер модуля."
                    sleep 1
                fi
                ;;
            ''|*[!0-9]*)
                warn "Введите номер, 'd<N>', 'a' или '0'."
                sleep 1
                ;;
            *)
                local _idx=$((_ch - 1))
                if [[ $_idx -ge 0 && $_idx -lt ${#_avail[@]} ]]; then
                    local _name="${_avail[$_idx]}"
                    if _mod_is_installed "$_name"; then
                        warn "Модуль ${_name} уже установлен. Для удаления: d${_ch}"
                        sleep 2
                    else
                        if [[ "${_pending[$_name]:-0}" -eq 1 ]]; then
                            _pending["$_name"]=0
                        else
                            _pending["$_name"]=1
                        fi
                    fi
                else
                    warn "Нет модуля с таким номером."
                    sleep 1
                fi
                ;;
        esac
    done
}
