#!/bin/bash
# ── Управление модулями ───────────────────────────────────────────────────────
# Используется из setup.sh. Переменные REPO_RAW, SLAVE_MODE — из родительского скрипта.

# Описания модулей (дублируем сюда комментарии из modules.conf для отображения в меню)
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

# Обновить или добавить строку в /root/modules.conf
_modules_conf_set() {
    local name="$1" state="$2"
    if grep -q "^${name}=" /root/modules.conf 2>/dev/null; then
        sed -i "s|^${name}=.*|${name}=${state}|" /root/modules.conf
    else
        echo "${name}=${state}" >> /root/modules.conf
    fi
}

# Проверить, установлен ли модуль (системные компоненты)
_mod_is_installed() {
    [[ -f "/root/modules/${1}/.installed" ]]
}

# Применить изменения: скачать файлы для включаемых модулей, запустить install.sh
_apply_modules() {
    local -n _desired=$1   # associative array name=on|off
    local -n _names=$2     # indexed array of module names

    local changed=0

    for name in "${_names[@]}"; do
        local want="${_desired[$name]:-off}"
        local have
        have=$(grep "^${name}=" /root/modules.conf 2>/dev/null | cut -d= -f2 || echo "off")

        if [[ "$want" == "on" ]]; then
            if [[ "$have" != "on" ]] || ! _mod_is_installed "$name"; then
                log "Включение модуля: ${name}..."
                mkdir -p "/root/modules/${name}"

                # Скачать __init__.py
                if curl -fsSL "${REPO_RAW}/modules/${name}/__init__.py" \
                        -o "/root/modules/${name}/__init__.py.new" 2>/dev/null; then
                    mv "/root/modules/${name}/__init__.py.new" "/root/modules/${name}/__init__.py"
                    ok "Файлы модуля ${name} загружены"
                else
                    warn "Не удалось скачать модуль ${name} — пропуск."
                    continue
                fi

                # Скачать install.sh (если есть в репо)
                curl -fsSL "${REPO_RAW}/modules/${name}/install.sh" \
                        -o "/root/modules/${name}/install.sh.new" 2>/dev/null \
                    && mv "/root/modules/${name}/install.sh.new" "/root/modules/${name}/install.sh" \
                    && chmod +x "/root/modules/${name}/install.sh" \
                    || rm -f "/root/modules/${name}/install.sh.new"

                # Запускать install.sh только если ещё не установлен (нет sentinel)
                if _mod_is_installed "$name"; then
                    info "Модуль ${name} уже установлен — пропускаю установщик."
                elif [[ -f "/root/modules/${name}/install.sh" ]]; then
                    log "Запуск установщика модуля ${name}..."
                    bash "/root/modules/${name}/install.sh" || {
                        warn "Установщик модуля ${name} завершился с ошибкой."
                        warn "Модуль будет включён в modules.conf — исправьте вручную при необходимости."
                    }
                fi

                _modules_conf_set "$name" "on"
                changed=1
            fi

        elif [[ "$want" == "off" && "$have" == "on" ]]; then
            log "Отключение модуля: ${name}..."
            _modules_conf_set "$name" "off"
            changed=1
            ok "Модуль ${name} отключён (системные компоненты сохранены, можно включить снова)"
        fi
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

# Удалить модуль: запустить uninstall.sh, убрать директорию, убрать из modules.conf
_delete_module() {
    local name="$1"

    # Безопасность: нельзя удалять базовые модули
    _is_base_module "$name" && { warn "Базовый модуль ${name} нельзя удалить."; return 1; }

    # Нельзя удалять включённый модуль
    local _have
    _have=$(grep "^${name}=" /root/modules.conf 2>/dev/null | cut -d= -f2 || echo "off")
    if [[ "$_have" == "on" ]]; then
        warn "Отключите модуль ${name} перед удалением (переключите в [ ] и примените)."
        return 1
    fi

    echo ""
    warn "Будет удалено: системные компоненты модуля ${name}."
    warn "Данные конфигурации сохраняются в /etc/proxy-bot/ или /etc/awg-socks5/ (зависит от модуля)."
    read -p "  Подтвердить удаление? [y/N]: " _confirm
    [[ "$_confirm" =~ ^[Yy]$ ]] || { info "Отмена."; return 0; }

    local _mod_dir="/root/modules/${name}"

    # Скачать актуальный uninstall.sh если его нет
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

    if [[ -d "$_mod_dir" ]]; then
        rm -rf "$_mod_dir"
        ok "Директория ${_mod_dir} удалена."
    fi

    sed -i "/^${name}=/d" /root/modules.conf

    ok "Модуль ${name} полностью удалён."
}

# Интерактивное меню выбора модулей
_modules_menu() {
    local _repo_conf
    _repo_conf=$(curl -fsSL "${REPO_RAW}/modules.conf" 2>/dev/null) || {
        warn "Не удалось загрузить список модулей из репозитория."
        return
    }

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

    # На slave-серверах показываем только релевантные модули
    if [[ "${SLAVE_MODE:-0}" -eq 1 ]]; then
        local -a _slave_only=("socks5" "mtproxy")
        local -a _filtered=()
        for _m in "${_avail[@]}"; do
            for _sm in "${_slave_only[@]}"; do
                [[ "$_m" == "$_sm" ]] && { _filtered+=("$_m"); break; }
            done
        done
        _avail=("${_filtered[@]}")
    fi

    if [[ ${#_avail[@]} -eq 0 ]]; then
        warn "Дополнительных модулей в репозитории не найдено."
        return
    fi

    declare -A _cur_state=()
    while IFS='=' read -r _n _s; do
        _n="${_n//[[:space:]]/}"; _s="${_s//[[:space:]]/}"
        [[ -z "$_n" ]] && continue
        _cur_state["$_n"]="$_s"
    done < <(grep -v '^#' /root/modules.conf 2>/dev/null | grep '=')

    # Рабочий массив (то, что пользователь ещё не применил)
    declare -A _new_state=()
    for _n in "${_avail[@]}"; do
        _new_state["$_n"]="${_cur_state[$_n]:-off}"
    done

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
        echo -e "  ${GREEN}[✓]${NC} tma  — Веб-панель"
        echo ""
        echo -e "  ${BOLD}Дополнительные:${NC}"
        echo ""
        local _i=1
        for _n in "${_avail[@]}"; do
            local _st="${_new_state[$_n]:-off}"
            local _desc="${_repo_desc[$_n]:-${_MOD_DESC[$_n]:-$_n}}"
            local _installed=false
            _mod_is_installed "$_n" && _installed=true

            if [[ "$_st" == "on" && "$_installed" == "true" ]]; then
                # Установлен И включён
                echo -e "  ${CYAN}${_i})${NC} ${GREEN}[✓]${NC} ${_n}  — ${_desc}"
            elif [[ "$_installed" == "true" ]]; then
                # Установлен, но выключен
                echo -e "  ${CYAN}${_i})${NC} ${YELLOW}[○]${NC} ${_n}  — ${_desc}  ${YELLOW}(установлен, выкл)${NC}"
            elif [[ "$_st" == "on" ]]; then
                # Включён в conf, но не установлен (sentinel отсутствует)
                echo -e "  ${CYAN}${_i})${NC} ${CYAN}[~]${NC} ${_n}  — ${_desc}  ${CYAN}(будет установлен)${NC}"
            else
                # Не установлен, выключен
                echo -e "  ${CYAN}${_i})${NC} [ ] ${_n}  — ${_desc}"
            fi
            ((_i++))
        done
        echo ""
        echo -e "  ${BOLD}Команды:${NC}"
        echo -e "  ${CYAN}<N>${NC}    — переключить модуль вкл/выкл"
        echo -e "  ${CYAN}d<N>${NC}  — удалить установленный модуль (только если выкл)"
        echo -e "  ${CYAN}a${NC}     — применить изменения"
        echo -e "  ${CYAN}0${NC}     — выход без изменений"
        echo ""
        read -p "  > " _ch

        case "$_ch" in
            0) return ;;
            a|A)
                echo ""
                _apply_modules _new_state _avail
                # Обновляем _cur_state после применения
                while IFS='=' read -r _n _s; do
                    _n="${_n//[[:space:]]/}"; _s="${_s//[[:space:]]/}"
                    [[ -z "$_n" ]] && continue
                    _cur_state["$_n"]="$_s"
                done < <(grep -v '^#' /root/modules.conf 2>/dev/null | grep '=')
                # Даём пользователю увидеть результат
                read -p "  Нажмите Enter для возврата в меню..." _dummy
                ;;
            d[0-9]*)
                # Команда удаления: d<N>
                local _dnum="${_ch#d}"
                local _didx=$((_dnum - 1))
                if [[ "$_dnum" =~ ^[0-9]+$ && $_didx -ge 0 && $_didx -lt ${#_avail[@]} ]]; then
                    local _dname="${_avail[$_didx]}"
                    # Принудительно применяем текущее off в рабочем массиве для этого модуля
                    _new_state["$_dname"]="off"
                    # Синхронизируем modules.conf если был on
                    if [[ "${_cur_state[$_dname]:-off}" == "on" ]]; then
                        _modules_conf_set "$_dname" "off"
                        systemctl restart awg-bot 2>/dev/null || true
                        _cur_state["$_dname"]="off"
                    fi
                    _delete_module "$_dname"
                    read -p "  Нажмите Enter для возврата в меню..." _dummy
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
                    if [[ "${_new_state[$_name]}" == "on" ]]; then
                        _new_state["$_name"]="off"
                    else
                        _new_state["$_name"]="on"
                    fi
                else
                    warn "Нет модуля с таким номером."
                    sleep 1
                fi
                ;;
        esac
    done
}
