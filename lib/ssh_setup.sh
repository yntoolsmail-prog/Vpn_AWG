#!/bin/bash
# ── SSH защита ────────────────────────────────────────────────────────────────
# Используется из setup.sh. Переменные AWG_DIR, CYAN, BOLD, NC и др. — из родительского скрипта.

_ssh_status_report() {
    local port pass_auth permit_root fail2ban_status root_keys=0
    port=$(grep "^Port " /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' || echo "22")
    pass_auth=$(grep "^PasswordAuthentication " /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' || echo "yes")
    permit_root=$(grep "^PermitRootLogin " /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' || echo "yes")
    fail2ban_status=$(systemctl is-active fail2ban 2>/dev/null || echo "inactive")
    [[ -f /root/.ssh/authorized_keys && -s /root/.ssh/authorized_keys ]] && root_keys=1

    echo ""
    echo -e "  ${BOLD}Состояние SSH-защиты:${NC}"
    echo ""
    echo -e "  Порт SSH: ${CYAN}${port}${NC}"
    if [[ "$pass_auth" == "no" ]]; then
        echo -e "  Вход по паролю:  ${GREEN}[✓] отключён${NC}"
    else
        echo -e "  Вход по паролю:  ${RED}[✗] включён — опасно!${NC}"
    fi
    if [[ "$permit_root" == "prohibit-password" || "$permit_root" == "no" ]]; then
        echo -e "  Root-логин:      ${GREEN}[✓] только по ключу или запрещён${NC}"
    else
        echo -e "  Root-логин:      ${RED}[✗] разрешён паролем — опасно!${NC}"
    fi
    if [[ "$root_keys" -eq 1 ]]; then
        echo -e "  SSH-ключ root:   ${GREEN}[✓] настроен${NC}"
    else
        echo -e "  SSH-ключ root:   ${RED}[✗] не настроен${NC}"
    fi
    if [[ "$fail2ban_status" == "active" ]]; then
        local banned
        banned=$(fail2ban-client status sshd 2>/dev/null | grep "Currently banned" | awk '{print $NF}' || echo "0")
        echo -e "  fail2ban:        ${GREEN}[✓] работает${NC}  (заблокировано IP: ${banned})"
    else
        echo -e "  fail2ban:        ${RED}[✗] не активен${NC}"
    fi
    echo ""
}

_ssh_install_fail2ban() {
    if systemctl is-active --quiet fail2ban 2>/dev/null; then
        ok "fail2ban уже работает."
        return 0
    fi
    log "Обновление пакетов..."
    apt-get update -qq
    log "Установка fail2ban..."
    apt-get install -y -qq fail2ban || { warn "Не удалось установить fail2ban."; return 1; }
    cat > /etc/fail2ban/jail.local << 'JAILEOF'
[sshd]
enabled = true
maxretry = 5
findtime = 600
bantime = 3600
JAILEOF
    systemctl enable fail2ban --now
    ok "fail2ban запущен: блокировка на 1 час после 5 неверных попыток за 10 минут."
}

_ssh_show_key_instructions() {
    local server_ip="$1" ssh_port="$2" os_choice="$3"
    case "$os_choice" in
        1)
            echo ""
            echo -e "${CYAN}${BOLD}  Windows (PowerShell)${NC}"
            echo ""
            echo -e "  ${BOLD}Шаг 1.${NC} Создайте ключ (один раз — если уже есть, пропустите):"
            echo -e "  ${YELLOW}  ssh-keygen -t ed25519 -C \"vpn-server\"${NC}"
            echo -e "  (нажмите Enter 3 раза чтобы принять defaults)"
            echo ""
            echo -e "  ${BOLD}Шаг 2.${NC} Скопируйте ключ на сервер:"
            echo -e "  ${YELLOW}  type \"\$env:USERPROFILE\\.ssh\\id_ed25519.pub\" | ssh -p ${ssh_port} root@${server_ip} \"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\"${NC}"
            echo ""
            echo -e "  ${BOLD}Шаг 3.${NC} Проверьте вход в НОВОМ окне PowerShell:"
            echo -e "  ${YELLOW}  ssh -p ${ssh_port} -i \"\$env:USERPROFILE\\.ssh\\id_ed25519\" root@${server_ip}${NC}"
            ;;
        2)
            echo ""
            echo -e "${CYAN}${BOLD}  Linux / macOS / Termux (Terminal)${NC}"
            echo ""
            echo -e "  ${BOLD}Шаг 1.${NC} Создайте ключ (один раз — если уже есть, пропустите):"
            echo -e "  ${YELLOW}  ssh-keygen -t ed25519 -C \"vpn-server\"${NC}"
            echo -e "  (нажмите Enter 3 раза чтобы принять defaults)"
            echo ""
            echo -e "  ${BOLD}Шаг 2.${NC} Скопируйте ключ на сервер:"
            echo -e "  ${YELLOW}  ssh-copy-id -p ${ssh_port} root@${server_ip}${NC}"
            echo -e "  (в Termux: если нет ssh-copy-id, используйте команду из варианта Windows/Шаг 2"
            echo -e "   но с: cat ~/.ssh/id_ed25519.pub | ssh -p ${ssh_port} root@${server_ip} ...)"
            echo ""
            echo -e "  ${BOLD}Шаг 3.${NC} Проверьте вход в НОВОМ терминале:"
            echo -e "  ${YELLOW}  ssh -p ${ssh_port} root@${server_ip}${NC}"
            ;;
        3)
            echo ""
            echo -e "${CYAN}${BOLD}  iOS (Termius / Blink Shell)${NC}"
            echo ""
            echo -e "  ${BOLD}Шаг 1.${NC} В приложении Termius:"
            echo -e "  Settings → Keychain → + → Generate Key → Ed25519"
            echo -e "  Дайте имя ключу (например: vpn-server)"
            echo ""
            echo -e "  ${BOLD}Шаг 2.${NC} Экспортируйте публичный ключ:"
            echo -e "  Нажмите на ключ → Share Public Key → скопируйте текст"
            echo ""
            echo -e "  ${BOLD}Шаг 3.${NC} Добавьте ключ на сервер (выполните здесь на сервере):"
            echo -e "  ${YELLOW}  echo 'вставьте_сюда_публичный_ключ' >> /root/.ssh/authorized_keys${NC}"
            echo -e "  ${YELLOW}  chmod 600 /root/.ssh/authorized_keys${NC}"
            echo ""
            echo -e "  ${BOLD}Шаг 4.${NC} В Termius при добавлении хоста выберите этот ключ."
            ;;
    esac
}

_ssh_setup_keys() {
    local server_ip ssh_port
    server_ip=$(grep "^SERVER_IP=" "${AWG_DIR}/server.env" 2>/dev/null | cut -d= -f2 || \
                curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || echo "IP_СЕРВЕРА")
    ssh_port=$(grep "^Port " /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' || echo "22")

    echo ""
    echo -e "  ${BOLD}Добавление персонального SSH-ключа${NC}"
    echo ""
    if [[ -f /root/.ssh/awg_admin_key ]]; then
        echo -e "  ${GREEN}✓ Admin-ключ уже сгенерирован${NC} — скачайте его на устройство (vpn.sh → 10 → 1)"
        echo -e "    или через бота: Техобслуживание → SSH-доступ → Скачать ключ."
        echo ""
        echo -e "  Здесь можно добавить персональный ключ устройства как альтернативу."
    else
        echo -e "  Добавьте ключ своего устройства чтобы войти без пароля."
    fi
    echo ""

    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    touch /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys

    while true; do
        local key_count
        key_count=$(grep -s "ssh-" /root/.ssh/authorized_keys 2>/dev/null | wc -l)
        echo -e "  Ключей в authorized_keys: ${CYAN}${key_count}${NC}"
        echo ""
        echo -e "  ${CYAN}1)${NC} Добавить ключ с Windows"
        echo -e "  ${CYAN}2)${NC} Добавить ключ с Linux / macOS / Termux"
        echo -e "  ${CYAN}3)${NC} Добавить ключ с iOS (Termius)"
        echo -e "  ${CYAN}4)${NC} Вставить публичный ключ вручную"
        if [[ "$key_count" -gt 0 ]]; then
            echo ""
            echo -e "  ${GREEN}d)${NC} Ключи добавлены — отключить вход по паролю"
        fi
        echo -e "  ${CYAN}0)${NC} Назад"
        echo ""
        read -p "  Ваш выбор: " _SSH_OS

        case "$_SSH_OS" in
            0) return ;;
            1|2|3)
                _ssh_show_key_instructions "$server_ip" "$ssh_port" "$_SSH_OS"
                echo ""
                echo -e "  ${RED}Не закрывайте этот терминал пока не проверите вход в новом окне!${NC}"
                echo ""
                read -p "  Вход проверен? [y/N]: " _KEY_OK
                [[ "${_KEY_OK,,}" != "y" ]] && { info "Хорошо, продолжайте когда будете готовы."; echo ""; }
                ;;
            4)
                echo ""
                echo -e "  Вставьте публичный ключ (ssh-ed25519 AAAA... или ssh-rsa AAAA...):"
                read -p "  > " _MANUAL_KEY
                if [[ "$_MANUAL_KEY" == ssh-* ]]; then
                    echo "$_MANUAL_KEY" >> /root/.ssh/authorized_keys
                    ok "Ключ добавлен."
                else
                    warn "Не похоже на публичный ключ — должно начинаться с ssh-ed25519 или ssh-rsa."
                fi
                echo ""
                ;;
            d|D)
                local key_count_final
                key_count_final=$(grep -s "ssh-" /root/.ssh/authorized_keys 2>/dev/null | wc -l)
                if [[ "$key_count_final" -eq 0 ]]; then
                    warn "Нет ни одного ключа — пароль НЕ отключаем."
                    continue
                fi
                echo ""
                echo -e "  ${YELLOW}Ключей в authorized_keys: ${key_count_final}${NC}"
                echo -e "  ${RED}После отключения пароля войти можно будет ТОЛЬКО по ключу.${NC}"
                echo ""
                read -p "  Отключить вход по паролю? [y/N]: " _DISABLE_OK
                [[ "${_DISABLE_OK,,}" != "y" ]] && { info "Отменено."; continue; }
                log "Применяю на primary и всех slave..."
                python3 -c "
from awg_core import ssh_toggle_password_auth_all
res = ssh_toggle_password_auth_all(False)
ok = '✓' if res.get('primary') else '✗'
print(f'  {ok} Primary: пароль отключён')
for name, s in res.get('slaves', {}).items():
    ok = '✓' if s else '✗'
    print(f'  {ok} Slave {name}: пароль отключён')
" && ok "Готово." || warn "Не удалось перезапустить sshd — проверьте вручную."
                return
                ;;
            *) warn "Неверный выбор."; echo "" ;;
        esac
    done
}

_ssh_security_menu() {
    while true; do
        clear
        echo -e "${CYAN}${BOLD}"
        echo "  ╔══════════════════════════════════════════╗"
        echo "  ║       AmneziaWG — Защита SSH             ║"
        echo "  ╚══════════════════════════════════════════╝"
        echo -e "${NC}"
        _ssh_status_report
        echo -e "  ${BOLD}Действия:${NC}"
        echo ""
        echo -e "  ${CYAN}1)${NC} Установить fail2ban  (блокировка перебора паролей)"
        echo -e "  ${CYAN}2)${NC} Настроить SSH-ключ и отключить вход по паролю"
        echo -e "  ${CYAN}0)${NC} Выйти"
        echo ""
        read -p "  > " _SSH_ACT
        case "$_SSH_ACT" in
            1) echo ""; _ssh_install_fail2ban; read -p "  Нажмите Enter..." _d ;;
            2) _ssh_setup_keys; read -p "  Нажмите Enter..." _d ;;
            0) return ;;
            *) warn "Введите 0, 1 или 2."; sleep 1 ;;
        esac
    done
}
