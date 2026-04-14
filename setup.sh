#!/bin/bash
# =============================================================================
# AmneziaWG + Telegram Bot — Установщик
# Использование: bash <(curl -s https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/setup.sh)
# Режимы: setup.sh           — полная установка
#         setup.sh --tma     — доустановить TMA
#         setup.sh --update  — обновить все файлы проекта
# =============================================================================
# Version: 3.0

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[i]${NC} $1"; }

[[ $EUID -ne 0 ]] && err "Запускать от root: sudo bash setup.sh"

REPO_ORG="yntoolsmail-prog"
REPO_NAME="Vpn_AWG"
AWG_DIR="/etc/amnezia/amneziawg"

# ── Выбор ветки репозитория ───────────────────────────────────────────────────
# При --tma: берём сохранённую ветку молча (переключение ветки там не нужно).
# При установке и --update: показываем меню; дефолт — текущая ветка если есть.
REPO_BRANCH="main"
_saved_branch=$(grep "^REPO_BRANCH=" "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2)

if [[ -n "$_REEXEC_BRANCH" ]]; then
    # Уже перезапущены из нужной ветки — пропускаем выбор
    REPO_BRANCH="$_REEXEC_BRANCH"
elif [[ "${1}" == "--tma" ]]; then
    [[ -n "$_saved_branch" ]] && REPO_BRANCH="$_saved_branch"
else
    _api=$(curl -fsSL --max-time 8 \
        "https://api.github.com/repos/${REPO_ORG}/${REPO_NAME}/branches" 2>/dev/null) || true
    _branches=()
    while IFS= read -r _b; do
        [[ -n "$_b" ]] && _branches+=("$_b")
    done < <(printf '%s' "$_api" | grep '"name"' | sed 's/.*"name": "\(.*\)".*/\1/')

    # Текущая ветка (для подсказки дефолта при --update)
    _current="${_saved_branch:-main}"

    if [[ ${#_branches[@]} -gt 1 ]]; then
        # Находим индекс текущей ветки для дефолта
        _default_idx=1
        for _i in "${!_branches[@]}"; do
            if [[ "${_branches[$_i]}" == "$_current" ]]; then
                _default_idx=$((_i+1))
            fi
        done

        echo ""
        if [[ "${1}" == "--update" ]]; then
            echo -e "  ${BOLD}Обновить из ветки:${NC}  (сейчас: ${CYAN}${_current}${NC})"
        else
            echo -e "  ${BOLD}Выберите ветку репозитория:${NC}"
        fi
        for _i in "${!_branches[@]}"; do
            _label=""
            if [[ "${_branches[$_i]}" == "main" ]]; then
                _label="  ${GREEN}← стабильная${NC}"
            fi
            if [[ "${_branches[$_i]}" == "$_current" ]]; then
                _label+="  ${CYAN}← текущая${NC}"
            fi
            echo -e "  ${CYAN}$((_i+1)).${NC} ${_branches[$_i]}${_label}"
        done
        echo ""
        read -p "  Выбор [${_default_idx}]: " _CHOICE
        _CHOICE="${_CHOICE:-${_default_idx}}"
        if [[ "$_CHOICE" =~ ^[0-9]+$ ]] && (( _CHOICE >= 1 && _CHOICE <= ${#_branches[@]} )); then
            REPO_BRANCH="${_branches[$((_CHOICE-1))]}"
        else
            REPO_BRANCH="$_current"
        fi
        [[ "$REPO_BRANCH" != "main" ]] && warn "Используется ветка: ${REPO_BRANCH}"
    elif [[ ${#_branches[@]} -eq 1 ]]; then
        REPO_BRANCH="${_branches[0]}"
    else
        # API недоступен — берём сохранённую или main
        REPO_BRANCH="$_current"
    fi
fi

REPO_RAW="https://raw.githubusercontent.com/${REPO_ORG}/${REPO_NAME}/${REPO_BRANCH}"

# Если пользователь выбрал ветку отличную от main и мы ещё не перезапускались —
# скачиваем setup.sh из выбранной ветки и перезапускаемся из него.
# Это гарантирует что логика установщика соответствует выбранной ветке.
if [[ "$REPO_BRANCH" != "main" && -z "$_REEXEC_BRANCH" ]]; then
    info "Загружаю setup.sh из ветки ${REPO_BRANCH}..."
    _new_script=$(curl -fsSL --max-time 30 "${REPO_RAW}/setup.sh" 2>/dev/null)
    if [[ -n "$_new_script" ]]; then
        export _REEXEC_BRANCH="$REPO_BRANCH"
        exec bash <(printf '%s' "$_new_script") "$@"
    else
        warn "Не удалось загрузить setup.sh из ветки. Продолжаю с текущей версией."
    fi
fi

# Список всех файлов проекта — используется в --update
PROJECT_FILES=(
    "modules/bot/bot.py:/root/modules/bot/bot.py"
    "awg_core.py:/root/awg_core.py"
    "sites_data.py:/root/sites_data.py"
    "module_loader.py:/root/module_loader.py"
    "vpn.sh:/root/vpn.sh"
    "modules/tma/tma_server.py:/root/modules/tma/tma_server.py"
    "tma/index.html:${AWG_DIR}/tma/index.html"
)
# modules.conf намеренно исключён — это пользовательский конфиг.
# Управление модулями: bash /root/setup.sh --modules

# ── Управление модулями ───────────────────────────────────────────────────────

# Описания модулей (дублируем сюда комментарии из modules.conf для отображения в меню)
declare -A _MOD_DESC=(
    ["tma"]="Веб-панель TMA (Telegram Mini App)"
    ["mtproxy"]="Прокси Telegram (MTProxy)"
    ["socks5"]="SOCKS5 маршрутизация клиентов"
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

        if [[ "$want" == "on" && "$have" != "on" ]]; then
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

    # Удалить директорию модуля
    if [[ -d "$_mod_dir" ]]; then
        rm -rf "$_mod_dir"
        ok "Директория ${_mod_dir} удалена."
    fi

    # Убрать из modules.conf
    sed -i "/^${name}=/d" /root/modules.conf

    ok "Модуль ${name} полностью удалён."
}

# Интерактивное меню выбора модулей
_modules_menu() {
    # Получаем список доступных опциональных модулей из репо
    local _repo_conf
    _repo_conf=$(curl -fsSL "${REPO_RAW}/modules.conf" 2>/dev/null) || {
        warn "Не удалось загрузить список модулей из репозитория."
        return
    }

    # Парсим доступные опциональные модули (сохраняем описания из комментариев над строкой)
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

    if [[ ${#_avail[@]} -eq 0 ]]; then
        warn "Дополнительных модулей в репозитории не найдено."
        return
    fi

    # Текущее состояние из /root/modules.conf
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

    # Цикл меню
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

# ── Режим --update ────────────────────────────────────────────────────────────
if [[ "${1}" == "--update" ]]; then
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║       AmneziaWG — Обновление             ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"

    UPDATED=0
    FAILED=0
    for entry in "${PROJECT_FILES[@]}"; do
        src_file="${entry%%:*}"
        dst_file="${entry##*:}"
        # Пропускаем файлы которых нет на сервере (например TMA не установлена)
        [[ "$src_file" == "modules/tma/tma_server.py" && ! -f "$dst_file" ]] && continue
        [[ "$src_file" == "tma/index.html" && ! -f "$dst_file" ]] && continue
        log "Обновляю ${src_file}..."
        if curl -fsSL "${REPO_RAW}/${src_file}" -o "${dst_file}.new"; then
            mv "${dst_file}.new" "$dst_file"
            [[ "$dst_file" == *.sh ]] && chmod +x "$dst_file"
            UPDATED=$((UPDATED+1))
        else
            warn "Не удалось обновить ${src_file}"
            rm -f "${dst_file}.new"
            FAILED=$((FAILED+1))
        fi
    done

    # Обновляем манифесты базовых модулей (создаём папки если их ещё нет)
    mkdir -p /root/modules/bot /root/modules/tma
    for _mf in "modules/__init__.py" "modules/bot/__init__.py" "modules/tma/__init__.py"; do
        if curl -fsSL "${REPO_RAW}/${_mf}" -o "/root/${_mf}.new" 2>/dev/null; then
            mv "/root/${_mf}.new" "/root/${_mf}"
            UPDATED=$((UPDATED+1))
        else
            rm -f "/root/${_mf}.new"
        fi
    done

    # Обновляем файлы активных дополнительных модулей
    log "Обновление активных модулей..."
    while IFS='=' read -r _mod _state; do
        _mod="${_mod//[[:space:]]/}"; _state="${_state//[[:space:]]/}"
        [[ -z "$_mod" || "$_state" != "on" ]] && continue
        _is_base_module "$_mod" && continue
        _mod_dir="/root/modules/${_mod}"
        [[ ! -d "$_mod_dir" ]] && { warn "Директория модуля ${_mod} не найдена — пропуск."; continue; }
        if curl -fsSL "${REPO_RAW}/modules/${_mod}/__init__.py" \
                -o "${_mod_dir}/__init__.py.new" 2>/dev/null; then
            mv "${_mod_dir}/__init__.py.new" "${_mod_dir}/__init__.py"
            # Также обновляем install.sh и uninstall.sh (не запускаем — только файлы)
            for _sh in install.sh uninstall.sh; do
                curl -fsSL "${REPO_RAW}/modules/${_mod}/${_sh}" \
                    -o "${_mod_dir}/${_sh}.new" 2>/dev/null \
                    && mv "${_mod_dir}/${_sh}.new" "${_mod_dir}/${_sh}" \
                    && chmod +x "${_mod_dir}/${_sh}" \
                    || rm -f "${_mod_dir}/${_sh}.new"
            done
            ok "Модуль ${_mod} обновлён"
            UPDATED=$((UPDATED+1))
        else
            warn "Не удалось обновить модуль ${_mod}"
            FAILED=$((FAILED+1))
        fi
    done < <(grep -v '^#' /root/modules.conf 2>/dev/null | grep '=')

    # Обновляем setup.sh последним — нельзя перезаписывать запущенный скрипт раньше времени
    log "Обновляю setup.sh..."
    if curl -fsSL "${REPO_RAW}/setup.sh" -o /root/setup.sh.new; then
        # Атомарная замена: mv не ломает уже запущенный bash-процесс
        # (bash читает скрипт блоками — к этому моменту весь код уже в памяти)
        mv /root/setup.sh.new /root/setup.sh
        chmod +x /root/setup.sh
        UPDATED=$((UPDATED+1))
        ok "setup.sh обновлён"
    else
        warn "Не удалось обновить setup.sh"
        rm -f /root/setup.sh.new
        FAILED=$((FAILED+1))
    fi

    # Сохраняем выбранную ветку в server.env
    if grep -q "^REPO_BRANCH=" "${AWG_DIR}/server.env" 2>/dev/null; then
        sed -i "s|^REPO_BRANCH=.*|REPO_BRANCH=${REPO_BRANCH}|" "${AWG_DIR}/server.env"
    else
        printf "REPO_BRANCH=%s\n" "$REPO_BRANCH" >> "${AWG_DIR}/server.env"
    fi

    echo ""
    log "Перезапускаю сервисы..."
    systemctl restart awg-bot 2>/dev/null && ok "awg-bot перезапущен" || warn "awg-bot не запущен"
    systemctl is-active --quiet awg-tma 2>/dev/null && systemctl restart awg-tma && ok "awg-tma перезапущен" || true

    echo ""
    echo -e "  ${GREEN}${BOLD}✓ Обновление завершено${NC}  (обновлено: ${UPDATED}, ошибок: ${FAILED}, ветка: ${REPO_BRANCH})"
    echo ""
    exit 0
fi

# ── Режим --tma ───────────────────────────────────────────────────────────────
if [[ "${1}" == "--tma" ]]; then
    [[ ! -f "${AWG_DIR}/bot.env" ]]    && err "Не найден bot.env — сначала установите бота (setup.sh)"
    [[ ! -f "${AWG_DIR}/server.env" ]] && err "Не найден server.env — сначала установите AWG (setup.sh)"
    _TMA_INSTALLER="/root/modules/tma/install.sh"
    if [[ ! -f "$_TMA_INSTALLER" ]]; then
        log "Скачиваю установщик TMA..."
        mkdir -p /root/modules/tma
        curl -fsSL "${REPO_RAW}/modules/tma/install.sh" -o "${_TMA_INSTALLER}.new" \
            || err "Не удалось скачать modules/tma/install.sh"
        mv "${_TMA_INSTALLER}.new" "$_TMA_INSTALLER"
        chmod +x "$_TMA_INSTALLER"
    fi
    bash "$_TMA_INSTALLER"
    exit 0
fi

# ── Режим --modules ───────────────────────────────────────────────────────────
if [[ "${1}" == "--modules" ]]; then
    [[ ! -f "/root/modules.conf" ]] && \
        err "modules.conf не найден — сначала установите систему (setup.sh)"
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║    AmneziaWG — Управление модулями      ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
    _modules_menu
    exit 0
fi

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

# ── Автодетект существующего AmneziaWG ───────────────────────────────────────

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
# В тихом режиме прячем только stdout — stderr всегда видим чтобы ошибки не терялись
run() {
    if [[ "$INSTALL_MODE" == "1" ]]; then
        eval "$@" > /dev/null
    else
        eval "$@"
    fi
}

echo ""

# ── Шаг 1: Зависимости ────────────────────────────────────────────────────────
log "Установка зависимостей..."
run "apt-get install -y $APT_FLAGS curl software-properties-common qrencode python3 python3-pip" \
    || err "Не удалось установить зависимости. Проверьте подключение к интернету и повторите."

# ── Шаг 2: AmneziaWG ──────────────────────────────────────────────────────────
log "Добавление репозитория Amnezia..."

UBUNTU_CODENAME=$(lsb_release -cs 2>/dev/null || echo "noble")
AMNEZIA_GPG="/etc/apt/trusted.gpg.d/amnezia.gpg"
AMNEZIA_LIST="/etc/apt/sources.list.d/amnezia-ppa.list"
AMNEZIA_KEY_ID="57290828"
PPA_ADDED=0

# ── Способ 1: напрямую, без api.launchpad.net ─────────────────────────────────
# Пробуем получить GPG-ключ из нескольких источников по приоритету:
#   1. raw.githubusercontent.com — тот же хост что и установщик, почти всегда доступен
#   2. keyserver.ubuntu.com — стандартный путь
#   3. keys.openpgp.org — резервный keyserver
KEY_ADDED=0
for KEYSERVER_URL in \
    "https://raw.githubusercontent.com/yntoolsmail-prog/Vpn_AWG/main/amnezia.gpg.asc" \
    "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x${AMNEZIA_KEY_ID}" \
    "https://keys.openpgp.org/vks/v1/by-keyid/${AMNEZIA_KEY_ID}"
do
    KEY_TMP=$(mktemp)
    if curl -fsSL --max-time 15 "$KEYSERVER_URL" -o "$KEY_TMP" 2>/dev/null && [[ -s "$KEY_TMP" ]]; then
        # Файл может быть ASCII-armored или уже бинарным — пробуем оба варианта
        if gpg --dearmor < "$KEY_TMP" > "$AMNEZIA_GPG" 2>/dev/null && [[ -s "$AMNEZIA_GPG" ]]; then
            chmod 644 "$AMNEZIA_GPG"
            KEY_ADDED=1
            rm -f "$KEY_TMP"
            break
        elif cp "$KEY_TMP" "$AMNEZIA_GPG" 2>/dev/null && [[ -s "$AMNEZIA_GPG" ]]; then
            chmod 644 "$AMNEZIA_GPG"
            KEY_ADDED=1
            rm -f "$KEY_TMP"
            break
        fi
    fi
    rm -f "$KEY_TMP" "$AMNEZIA_GPG"
    warn "  → ${KEYSERVER_URL} недоступен, пробуем следующий..."
done

# Если ключ получен — пишем sources.list напрямую (ppa.launchpadcontent.net — CDN, не API)
if [[ "$KEY_ADDED" -eq 1 ]]; then
    echo "deb https://ppa.launchpadcontent.net/amnezia/ppa/ubuntu ${UBUNTU_CODENAME} main" \
        > "$AMNEZIA_LIST"
    # Проверяем что apt видит репозиторий
    if apt-get -qq update 2>/dev/null; then
        PPA_ADDED=1
        info "Репозиторий добавлен напрямую (без api.launchpad.net)"
    else
        warn "Репозиторий добавлен, но apt update вернул ошибку — пробуем через add-apt-repository"
        rm -f "$AMNEZIA_GPG" "$AMNEZIA_LIST"
    fi
else
    warn "Не удалось получить GPG-ключ напрямую — пробуем через add-apt-repository"
fi

# ── Способ 2: классический add-apt-repository (требует api.launchpad.net) ─────
if [[ "$PPA_ADDED" -eq 0 ]]; then
    if add-apt-repository -y --no-update ppa:amnezia/ppa 2>/dev/null; then
        run "apt-get $APT_FLAGS update" \
            || err "Не удалось обновить списки пакетов."
        PPA_ADDED=1
        info "Репозиторий добавлен через add-apt-repository"
    fi
fi

[[ "$PPA_ADDED" -eq 0 ]] && err "Не удалось добавить репозиторий Amnezia. Проверьте подключение к интернету."

log "Установка AmneziaWG (компиляция ~3-5 мин)..."
run "apt-get install -y $APT_FLAGS amneziawg amneziawg-tools" \
    || err "Не удалось установить AmneziaWG. Попробуйте подробный режим установки для диагностики."

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

# ── Домены / эндпоинты ────────────────────────────────────────────────────────
validate_endpoint() {
    local domain="$1"
    # Если это IP — принимаем без проверки DNS
    if [[ "$domain" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        return 0
    fi
    # Домен — проверяем что резолвится в SERVER_IP
    local resolved
    resolved=$(getent hosts "$domain" 2>/dev/null | awk '{print $1}' | head -1)
    if [[ -z "$resolved" ]]; then
        warn "Домен $domain не резолвится. Проверьте DNS-запись."
        return 1
    fi
    if [[ "$resolved" != "$SERVER_IP" ]]; then
        warn "Домен $domain резолвится в $resolved, а не в $SERVER_IP."
        warn "Убедитесь что A-запись домена указывает на этот сервер."
        read -p "  Всё равно использовать этот домен? [y/N]: " force
        [[ "$force" =~ ^[Yy]$ ]] && return 0 || return 1
    fi
    info "Домен $domain → $resolved ✓"
    return 0
}

echo "  Укажите домен или оставьте пустым чтобы использовать IP ($SERVER_IP)."
echo "  Домен должен быть настроен заранее (A-запись → $SERVER_IP)."
echo ""
SERVER_ENDPOINT=""
while true; do
    read -p "  Основной endpoint (домен или Enter для IP): " input_ep
    input_ep="${input_ep// /}"
    if [[ -z "$input_ep" ]]; then
        SERVER_ENDPOINT="$SERVER_IP"
        info "Основной endpoint: $SERVER_ENDPOINT (IP)"
        break
    fi
    if validate_endpoint "$input_ep"; then
        SERVER_ENDPOINT="$input_ep"
        info "Основной endpoint: $SERVER_ENDPOINT"
        break
    fi
done

SERVER_ENDPOINT_BACKUP=""
echo ""
echo "  Резервный endpoint — опционально. Пользователи получат второй конфиг."
read -p "  Резервный endpoint (домен или Enter чтобы пропустить): " input_backup
input_backup="${input_backup// /}"
if [[ -n "$input_backup" ]]; then
    if validate_endpoint "$input_backup"; then
        SERVER_ENDPOINT_BACKUP="$input_backup"
        info "Резервный endpoint: $SERVER_ENDPOINT_BACKUP"
    else
        warn "Резервный endpoint пропущен."
    fi
fi
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

# ── Шаг 5: Генерация параметров обфускации (AWG 2.0) ─────────────────────────
log "Генерация параметров обфускации AWG 2.0..."
read JC JMIN JMAX I1 H1 H2 H3 H4 < <(python3 -c "
import random
h = random.sample(range(5, 2**32), 4)
print(
    random.randint(3,10),
    random.randint(10,50),
    random.randint(51,100),
    random.randint(1, 2**64-1),
    *h,
)")
S1=0
S2=0
info "Jc=$JC Jmin=$JMIN Jmax=$JMAX H1=$H1 H2=$H2 H3=$H3 H4=$H4 i1=$I1"

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
    printf "i1 = %s\n" "$I1"
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

# ── Шаг 8: Часовой пояс ──────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}${BOLD}Часовой пояс${NC}"
echo ""
echo -e "  Используется для отображения времени в статистике трафика."
echo ""

# Читаем текущий системный пояс
SYS_TZ=$(cat /etc/timezone 2>/dev/null || timedatectl | grep "Time zone" | awk '{print $3}')
SYS_TZ=${SYS_TZ:-UTC}

echo -e "  Текущий часовой пояс сервера: ${CYAN}${SYS_TZ}${NC}"
echo ""
echo -e "  ${CYAN}1)${NC} Europe/Moscow      — Москва (UTC+3)"
echo -e "  ${CYAN}2)${NC} Asia/Yekaterinburg — Екатеринбург (UTC+5)"
echo -e "  ${CYAN}3)${NC} Asia/Novosibirsk   — Новосибирск (UTC+7)"
echo -e "  ${CYAN}4)${NC} Asia/Vladivostok   — Владивосток (UTC+10)"
echo -e "  ${CYAN}5)${NC} Europe/Kiev        — Киев (UTC+2)"
echo -e "  ${CYAN}6)${NC} Asia/Almaty        — Алматы (UTC+5)"
echo -e "  ${CYAN}7)${NC} Europe/Berlin      — Берлин (UTC+1/2)"
echo -e "  ${CYAN}8)${NC} UTC"
echo -e "  ${CYAN}9)${NC} Использовать пояс сервера ${CYAN}(${SYS_TZ})${NC}"
echo -e "  ${CYAN}0)${NC} Ввести вручную"
echo ""
while true; do
    read -p "  Ваш выбор [9]: " TZ_CHOICE
    TZ_CHOICE=${TZ_CHOICE:-9}
    [[ "$TZ_CHOICE" =~ ^[0-9]$ ]] && break
    warn "Введите число от 0 до 9."
done
case "$TZ_CHOICE" in
    1) TIMEZONE="Europe/Moscow" ;;
    2) TIMEZONE="Asia/Yekaterinburg" ;;
    3) TIMEZONE="Asia/Novosibirsk" ;;
    4) TIMEZONE="Asia/Vladivostok" ;;
    5) TIMEZONE="Europe/Kiev" ;;
    6) TIMEZONE="Asia/Almaty" ;;
    7) TIMEZONE="Europe/Berlin" ;;
    8) TIMEZONE="UTC" ;;
    9) TIMEZONE="$SYS_TZ" ;;
    0)
        echo ""
        echo -e "  Примеры: Europe/Moscow, Asia/Tokyo, America/New_York"
        echo -e "  Полный список: timedatectl list-timezones"
        echo ""
        while true; do
            read -p "  Введите часовой пояс: " TIMEZONE
            TIMEZONE="${TIMEZONE// /}"
            if [[ -z "$TIMEZONE" ]]; then
                warn "Пояс не может быть пустым."
                continue
            fi
            # Проверяем что пояс существует
            if timedatectl list-timezones 2>/dev/null | grep -qx "$TIMEZONE"; then
                break
            else
                warn "Пояс '${TIMEZONE}' не найден. Проверьте написание."
            fi
        done
        ;;
esac
timedatectl set-timezone "$TIMEZONE" 2>/dev/null || true
info "Часовой пояс: ${TIMEZONE}"

# ── Шаг 9: Сохраняем server.env ──────────────────────────────────────────────
printf "SERVER_IP=%s\nSERVER_PORT=%s\nSERVER_PUBLIC=%s\nVPN_IFACE=%s\nVPN_SUBNET=%s\n" \
    "$SERVER_IP" "$AWG_PORT" "$SERVER_PUBLIC" "$VPN_IFACE" "$VPN_SUBNET" \
    > /etc/amnezia/amneziawg/server.env
printf "SERVER_ENDPOINT=%s\nSERVER_ENDPOINT_BACKUP=%s\n" \
    "$SERVER_ENDPOINT" "$SERVER_ENDPOINT_BACKUP" \
    >> /etc/amnezia/amneziawg/server.env
printf "JC=%s\nJMIN=%s\nJMAX=%s\nS1=%s\nS2=%s\nH1=%s\nH2=%s\nH3=%s\nH4=%s\nI1=%s\n" \
    "$JC" "$JMIN" "$JMAX" "$S1" "$S2" "$H1" "$H2" "$H3" "$H4" "$I1" \
    >> /etc/amnezia/amneziawg/server.env
printf "PRIMARY_DNS=1.1.1.1\nSECONDARY_DNS=1.0.0.1\n" \
    >> /etc/amnezia/amneziawg/server.env
printf "TIMEZONE=%s\n" "$TIMEZONE" \
    >> /etc/amnezia/amneziawg/server.env
printf "REPO_BRANCH=%s\n" "$REPO_BRANCH" \
    >> /etc/amnezia/amneziawg/server.env

# ── Шаг 10: Скачиваем скрипты ─────────────────────────────────────────────────
log "Загрузка скриптов управления (ветка: ${REPO_BRANCH})..."
curl -fsSL "${REPO_RAW}/vpn.sh"                    -o /root/vpn.sh                    || err "Не удалось скачать vpn.sh"
mkdir -p /root/modules/bot
curl -fsSL "${REPO_RAW}/modules/bot/bot.py"        -o /root/modules/bot/bot.py        || err "Не удалось скачать bot.py"
curl -fsSL "${REPO_RAW}/awg_core.py"               -o /root/awg_core.py               || err "Не удалось скачать awg_core.py"
curl -fsSL "${REPO_RAW}/sites_data.py"    -o /root/sites_data.py    || err "Не удалось скачать sites_data.py"
curl -fsSL "${REPO_RAW}/module_loader.py" -o /root/module_loader.py || err "Не удалось скачать module_loader.py"
curl -fsSL "${REPO_RAW}/modules.conf"     -o /root/modules.conf     || err "Не удалось скачать modules.conf"

# ── Модули: создаём структуру каталогов и манифесты ──────────────────────────
mkdir -p /root/modules/bot /root/modules/tma
# Скачиваем __init__.py манифесты; если не найдены — создаём пустые заглушки
_dl_init() { curl -fsSL "${REPO_RAW}/$1" -o "/root/$1" 2>/dev/null || printf '# %s\n' "$1" > "/root/$1"; }
_dl_init "modules/__init__.py"
_dl_init "modules/bot/__init__.py"
_dl_init "modules/tma/__init__.py"

# Сохраняем setup.sh в /root/ — нужен для --update и --tma
# Важно: cp "$0" нельзя использовать при bash <(curl ...) — $0 это тот же pipe,
# из которого bash читает скрипт; cp съедает оставшийся скрипт → молчаливый crash.
curl -fsSL "${REPO_RAW}/setup.sh" -o /root/setup.sh || err "Не удалось сохранить setup.sh"
chmod +x /root/vpn.sh /root/setup.sh

# ── Шаг 11: Python зависимости ───────────────────────────────────────────────
log "Установка python-telegram-bot..."
if [[ "$INSTALL_MODE" == "1" ]]; then
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages > /dev/null || \
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" > /dev/null || \
    err "Не удалось установить python-telegram-bot."
else
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" --break-system-packages || \
    pip3 install "python-telegram-bot[job-queue]>=22.0,<23" || \
    err "Не удалось установить python-telegram-bot."
fi

log "Установка flask (веб-интерфейс TMA)..."
pip3 install "flask>=3.0" --break-system-packages --ignore-installed blinker > /dev/null 2>&1 || \
pip3 install "flask>=3.0" --ignore-installed blinker > /dev/null 2>&1 || \
pip3 install "flask>=3.0" > /dev/null 2>&1 || \
err "Не удалось установить flask."

# ── Шаг 11.5: Мониторинг трафика и сетевые инструменты ───────────────────────
log "Установка vnstat (статистика трафика)..."
apt-get install -y -qq vnstat
log "Установка mtr (диагностика сети)..."
apt-get install -y -qq mtr
systemctl enable vnstat --now 2>/dev/null || true
# Регистрируем основной интерфейс в vnstat если ещё не добавлен
HOST_IFACE=$(ip route get 8.8.8.8 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
HOST_IFACE=${HOST_IFACE:-eth0}
vnstat -i "$HOST_IFACE" --add 2>/dev/null || true
# Создаём лог-файл для поминутных замеров бота
touch /var/log/awg-bw.log
chmod 644 /var/log/awg-bw.log
# Создаём папку для диагностических отчётов
mkdir -p /etc/amnezia/amneziawg/diagnostics
info "Мониторинг трафика настроен (интерфейс: ${HOST_IFACE})"

# ── Шаг 12: Настройка бота ───────────────────────────────────────────────────
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

# ── Шаг 13: systemd сервис для бота ──────────────────────────────────────────
log "Настройка автозапуска бота..."
cat > /etc/systemd/system/awg-bot.service << 'EOF'
[Unit]
Description=AmneziaWG Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/modules/bot/bot.py
WorkingDirectory=/root
Environment=PYTHONPATH=/root
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

# ── Шаг 14: SSH автозапуск vpn.sh ────────────────────────────────────────────
log "Настройка автозапуска панели при SSH-подключении..."
BASHRC_MARKER="# awg-vpn-autorun"
if ! grep -q "$BASHRC_MARKER" /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc << 'BASHRCEOF'

# awg-vpn-autorun
# При SSH-подключении автоматически открывается панель управления AmneziaWG.
# Просто нажмите Enter чтобы выйти обратно в терминал.
if [[ -n "$SSH_CONNECTION" && -f /root/vpn.sh ]]; then
    bash /root/vpn.sh
fi
BASHRCEOF
    info "Автозапуск настроен: при SSH-входе будет открываться vpn.sh"
else
    info "Автозапуск уже настроен, пропускаем"
fi


# ── Выбор дополнительных модулей ─────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}Дополнительные модули${NC}"
echo -e "  Хотите настроить дополнительные модули (TMA, MTProxy, SOCKS5)?"
echo ""
read -p "  Открыть меню модулей? [y/N]: " _ASK_MODS
if [[ "${_ASK_MODS,,}" == "y" ]]; then
    _modules_menu
fi

# ── Готово ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}   Установка завершена!${NC}"
echo -e "${GREEN}${BOLD}══════════════════════════════════════════${NC}"
echo ""
info "AWG интерфейс: ${VPN_IFACE}  |  Подсеть: ${VPN_SUBNET}.x  |  Порт: ${AWG_PORT}/UDP"
info "AWG запущен с параметрами: Jc=$JC Jmin=$JMIN Jmax=$JMAX"
info "DNS: 1.1.1.1, 1.0.0.1 (можно изменить в /etc/amnezia/amneziawg/server.env)"
info "Бот: systemctl status awg-bot"
echo ""
echo -e "  Терминал: ${CYAN}bash /root/vpn.sh${NC}"
echo -e "  Telegram: ${CYAN}напишите /start вашему боту${NC}"
echo -e "  Логи:     ${YELLOW}journalctl -u awg-bot -f${NC}"
echo ""
echo -e "${CYAN}${BOLD}  Обновление всех файлов проекта:${NC}"
echo -e "  bash /root/setup.sh --update"
echo ""
echo -e "${CYAN}${BOLD}  Управление модулями (TMA, MTProxy, SOCKS5, ...):${NC}"
echo -e "  bash /root/setup.sh --modules"
echo ""
