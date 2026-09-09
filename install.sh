#!/usr/bin/env bash
# ==============================================================================
# Скрипт автоматической установки и настройки Universal Media Downloader Bot
# Поддерживает развертывание:
#   1) Docker & Docker Compose (изолированный стек: бот + Caddy с авто-SSL)
#   2) Systemd (нативный сервис Python venv на хосте)
# Поддерживаемые ОС: Ubuntu 20.04+, Debian 11+
# ==============================================================================

set -e

# Цвета для терминала
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    PURPLE='\033[0;35m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' PURPLE='' CYAN='' BOLD='' NC=''
fi

echo -e "${CYAN}${BOLD}"
echo "===================================================================="
echo "    🚀 УСТАНОВКА И НАСТРОЙКА UNIVERSAL MEDIA DOWNLOADER BOT"
echo "===================================================================="
echo -e "${NC}"

# 1. Проверка прав суперпользователя
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}❌ Ошибка: этот скрипт должен быть запущен с правами root или через sudo.${NC}"
    echo -e "Пример запуска: ${BOLD}sudo bash install.sh${NC}"
    exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo -e "${BLUE}📁 Каталог установки:${NC} ${BOLD}${INSTALL_DIR}${NC}"

# 2. Определение аппаратных ресурсов хоста
echo -e "\n${BLUE}🔍 Сканирование аппаратных ресурсов сервера...${NC}"

CPU_CORES=$(nproc 2>/dev/null || echo 1)
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
TOTAL_SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
DISK_FREE_HUMAN=$(df -h / | awk 'NR==2 {print $4}')

echo -e "   • Процессор:            ${BOLD}${CPU_CORES} vCPU${NC}"
echo -e "   • Память RAM:           ${BOLD}${TOTAL_RAM_MB} МБ${NC}"
echo -e "   • Файл Swap:            ${BOLD}${TOTAL_SWAP_MB} МБ${NC}"
echo -e "   • Диск (свободно на /): ${BOLD}${DISK_FREE_HUMAN}${NC}"

# 3. Проверка и автоматическая настройка SWAP (если RAM < 2 ГБ)
if [ "${TOTAL_RAM_MB}" -lt 2048 ] && [ "${TOTAL_SWAP_MB}" -lt 1024 ]; then
    echo -e "\n${YELLOW}⚠️ Внимание: на сервере менее 2 ГБ оперативной памяти (${TOTAL_RAM_MB} МБ).${NC}"
    echo -e "${YELLOW}При конвертации и склейке медиа через FFmpeg возможен OOM (Out Of Memory).${NC}"
    read -r -p "Создать и подключить swap-файл на 2 ГБ для стабильной работы? [Y/n]: " SWAP_CHOICE
    SWAP_CHOICE=${SWAP_CHOICE:-Y}
    if [[ "$SWAP_CHOICE" =~ ^[YyДд]$ ]]; then
        echo -e "${CYAN}⚙️ Настройка SWAP 2 ГБ...${NC}"
        if fallocate -l 2G /swapfile 2>/dev/null; then
            echo -e "   [OK] Выделено 2 ГБ через fallocate"
        else
            dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
        fi
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        if ! grep -q '/swapfile' /etc/fstab; then
            echo '/swapfile none swap sw 0 0' >> /etc/fstab
        fi
        sysctl vm.swappiness=10 >/dev/null 2>&1 || true
        TOTAL_SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
        echo -e "${GREEN}✅ SWAP успешно создан и активирован! Текущий Swap: ${TOTAL_SWAP_MB} МБ${NC}"
    fi
fi

# 4. Выбор метода развертывания
echo -e "\n${BLUE}🐳 Выберите метод развертывания:${NC}"
echo "  1) Docker & Docker Compose [Рекомендуется]"
echo "     • Изолированный контейнер с ботом + встроенный Deno для обхода защиты YouTube"
echo "     • Автоматический контейнер Caddy с выпуском SSL при наличии домена"
echo "     • Удобное управление через docker compose"
echo "  2) Systemd"
echo "     • Нативный сервис в системе (Python 3 venv + системный FFmpeg)"
echo "     • Прямой запуск без Docker"

read -r -p "Выберите вариант [1-2, по умолчанию 1]: " DEPLOY_METHOD
DEPLOY_METHOD=${DEPLOY_METHOD:-1}

# 5. Выбор профиля оптимизации под ресурсы
echo -e "\n${BLUE}⚙️ Выбор профиля оптимизации под ресурсы сервера:${NC}"
if [ "$CPU_CORES" -ge 4 ] && [ "$TOTAL_RAM_MB" -ge 7000 ]; then
    REC_PROFILE=3; REC_TEXT="High"
elif [ "$CPU_CORES" -ge 2 ] && [ "$TOTAL_RAM_MB" -ge 3500 ]; then
    REC_PROFILE=2; REC_TEXT="Medium"
else
    REC_PROFILE=1; REC_TEXT="Low"
fi

echo "  1) Low    (1 vCPU, 1-2 ГБ RAM) — 1 воркер, 1 поток FFmpeg, лимит 100 МБ"
echo "  2) Medium (2-4 vCPU, 4 ГБ RAM) — 2 воркера, 2 потока FFmpeg, лимит 200 МБ"
echo "  3) High   (4+ vCPU, 8+ ГБ RAM) — 4 воркера, 4 потока FFmpeg, лимит 500 МБ"
echo "  4) Автоматический выбор [Рекомендуется: ${REC_TEXT}]"

read -r -p "Выберите профиль [1-4, по умолчанию 4]: " PROFILE_CHOICE
PROFILE_CHOICE=${PROFILE_CHOICE:-4}

case "$PROFILE_CHOICE" in
    1) NUM_WORKERS=1; FFMPEG_THREADS=1; MAX_FILE_SIZE_MB=100; MIN_FREE_DISK_GB=1.5; SELECTED_PROFILE="Low" ;;
    2) NUM_WORKERS=2; FFMPEG_THREADS=2; MAX_FILE_SIZE_MB=200; MIN_FREE_DISK_GB=2.0; SELECTED_PROFILE="Medium" ;;
    3) NUM_WORKERS=4; FFMPEG_THREADS=4; MAX_FILE_SIZE_MB=500; MIN_FREE_DISK_GB=4.0; SELECTED_PROFILE="High" ;;
    *)
        if [ "$REC_PROFILE" -eq 3 ]; then
            NUM_WORKERS=4; FFMPEG_THREADS=4; MAX_FILE_SIZE_MB=500; MIN_FREE_DISK_GB=4.0; SELECTED_PROFILE="High"
        elif [ "$REC_PROFILE" -eq 2 ]; then
            NUM_WORKERS=2; FFMPEG_THREADS=2; MAX_FILE_SIZE_MB=200; MIN_FREE_DISK_GB=2.0; SELECTED_PROFILE="Medium"
        else
            NUM_WORKERS=1; FFMPEG_THREADS=1; MAX_FILE_SIZE_MB=100; MIN_FREE_DISK_GB=1.5; SELECTED_PROFILE="Low"
        fi
        ;;
esac

echo -e "${GREEN}✅ Выбран профиль: ${BOLD}${SELECTED_PROFILE}${NC} (Воркеров: ${NUM_WORKERS}, Потоков FFmpeg: ${FFMPEG_THREADS}, Лимит файла: ${MAX_FILE_SIZE_MB} МБ)"

# 6. Интерактивный ввод параметров бота
echo -e "\n${BLUE}🔑 Ввод параметров бота:${NC}"

# Валидация BOT_TOKEN
while true; do
    read -r -p "Введи токен Telegram-бота (от @BotFather): " BOT_TOKEN
    BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '[:space:]')
    if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{35,}$ ]]; then
        break
    else
        echo -e "${RED}❌ Неверный формат токена бота! Пример: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ1234567890${NC}"
    fi
done

# Валидация ADMIN_ID
while true; do
    read -r -p "Введи числовой Telegram ID главного администратора: " ADMIN_ID
    ADMIN_ID=$(echo "$ADMIN_ID" | tr -d '[:space:]')
    if [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
        break
    else
        echo -e "${RED}❌ ID администратора должен состоять только из цифр (например, 123456789).${NC}"
    fi
done

# Определение внешнего IP
EXTERNAL_IP=$(curl -s -m 4 https://api.ipify.org 2>/dev/null || curl -s -m 4 https://ifconfig.me 2>/dev/null || echo "127.0.0.1")

# Настройка домена и Caddy HTTPS
echo -e "\n${BLUE}🌐 Настройка домена и веб-сервера (для инлайн-превью и файлов > 50 МБ):${NC}"
echo -e "Для корректной работы инлайн-режима Telegram (карточки фото и видео) необходим ${BOLD}HTTPS${NC}."
echo -e "Скрипт может автоматически настроить веб-сервер ${BOLD}Caddy${NC} и выпустить бесплатный SSL (Let's Encrypt)."
read -r -p "Хотите подключить свой домен и настроить Caddy HTTPS? [Y/n]: " SETUP_CADDY_CHOICE
SETUP_CADDY_CHOICE=${SETUP_CADDY_CHOICE:-Y}

USE_CADDY=false
DOMAIN_NAME=""

if [[ "$SETUP_CADDY_CHOICE" =~ ^[YyДд]$ ]]; then
    while true; do
        read -r -p "Введите ваш домен (например, bot.mydomain.com): " DOMAIN_NAME
        DOMAIN_NAME=$(echo "$DOMAIN_NAME" | tr -d '[:space:]' | sed -e 's|^https\?://||' -e 's|/.*$||')
        if [[ "$DOMAIN_NAME" =~ ^[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}$ ]]; then
            USE_CADDY=true
            WEB_BASE_URL="https://${DOMAIN_NAME}"
            echo -e "${GREEN}✅ Домен принят: ${BOLD}${DOMAIN_NAME}${NC} (URL: ${WEB_BASE_URL})"
            echo -e "${YELLOW}ℹ️ Убедитесь, что A-запись домена ${DOMAIN_NAME} в DNS указывает на IP сервера: ${BOLD}${EXTERNAL_IP}${NC}"
            break
        else
            echo -e "${RED}❌ Некорректный формат домена! Введите домен без http:// (например: bot.example.com)${NC}"
        fi
    done
else
    DEFAULT_WEB_URL="http://${EXTERNAL_IP}:8080"
    read -r -p "Публичный URL для веб-скачивания [по умолчанию: ${DEFAULT_WEB_URL}]: " INPUT_WEB_URL
    WEB_BASE_URL=${INPUT_WEB_URL:-$DEFAULT_WEB_URL}
    WEB_BASE_URL=$(echo "$WEB_BASE_URL" | sed 's:/*$::')
fi

# Каталоги для данных
mkdir -p "${INSTALL_DIR}/data/web_downloads"
mkdir -p "${INSTALL_DIR}/data/cache"
chmod 755 "${INSTALL_DIR}/data"

# Генерация .env
COMPOSE_PROFILES=""
HOST_PORT_BIND="8080"
if [ "$USE_CADDY" = true ]; then
    COMPOSE_PROFILES="caddy"
    HOST_PORT_BIND="127.0.0.1:8080"
fi

cat << ENV_CONFIG > "${INSTALL_DIR}/.env"
# Сгенерировано автоматически скриптом install.sh ($(date))
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
BOT_USERNAME=
WEB_BASE_URL=${WEB_BASE_URL}
WEB_PORT=8080
DATA_DIR=${INSTALL_DIR}/data

# Docker Compose профили
COMPOSE_PROFILES=${COMPOSE_PROFILES}
HOST_PORT_BIND=${HOST_PORT_BIND}

# Параметры оптимизации сервера (${SELECTED_PROFILE})
NUM_WORKERS=${NUM_WORKERS}
FFMPEG_THREADS=${FFMPEG_THREADS}
MAX_FILE_SIZE_MB=${MAX_FILE_SIZE_MB}
MIN_FREE_DISK_GB=${MIN_FREE_DISK_GB}
WEB_TTL_SECONDS=420
CACHE_TTL_SECONDS=240
MAX_WEB_DIR_GB=3.5
MAX_CACHE_DIR_GB=1.0
ENV_CONFIG

chmod 600 "${INSTALL_DIR}/.env"
echo -e "${GREEN}✅ Файл .env успешно создан с защищенными правами (chmod 600).${NC}"

# ==============================================================================
# ВАРИАНТ 1: РАЗВЕРТЫВАНИЕ ЧЕРЕЗ DOCKER & DOCKER COMPOSE
# ==============================================================================
if [ "$DEPLOY_METHOD" -eq 1 ]; then
    echo -e "\n${BLUE}🐳 Настройка окружения Docker...${NC}"

    # Проверка наличия Docker
    if ! command -v docker &>/dev/null; then
        echo -e "${CYAN}📥 Установка Docker Engine...${NC}"
        curl -fsSL https://get.docker.com | sh
        systemctl enable --now docker
    else
        echo -e "   [OK] Docker уже установлен в системе."
    fi

    # Проверка наличия docker compose
    if ! docker compose version &>/dev/null; then
        echo -e "${CYAN}📥 Установка плагина docker-compose-plugin...${NC}"
        apt-get update -y
        apt-get install -y docker-compose-plugin
    else
        echo -e "   [OK] Docker Compose доступен."
    fi

    # Настройка Caddyfile для Docker
    if [ "$USE_CADDY" = true ]; then
        echo -e "${CYAN}⚙️ Создание Caddyfile для Docker...${NC}"
        cat << CADDY_DOCKER > "${INSTALL_DIR}/Caddyfile"
${DOMAIN_NAME} {
    handle /health* {
        reverse_proxy mega-bot:8080
    }
    handle /dl/* {
        reverse_proxy mega-bot:8080
    }
    handle {
        reverse_proxy mega-bot:8080
    }
}
CADDY_DOCKER

        # Открытие портов 80 и 443 в UFW
        if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
            echo -e "${CYAN}🔓 Открытие портов 80 и 443 в UFW файрволе...${NC}"
            ufw allow 80/tcp >/dev/null 2>&1 || true
            ufw allow 443/tcp >/dev/null 2>&1 || true
        fi
    else
        # Создаем заглушку файла Caddyfile, чтобы Docker не создавал папку при монтировании
        if [ ! -f "${INSTALL_DIR}/Caddyfile" ]; then
            cp "${INSTALL_DIR}/Caddyfile.example" "${INSTALL_DIR}/Caddyfile" 2>/dev/null || touch "${INSTALL_DIR}/Caddyfile"
        fi
    fi

    # Сборка и запуск контейнеров
    echo -e "\n${CYAN}🚀 Сборка и запуск Docker-стека...${NC}"
    cd "${INSTALL_DIR}"
    docker compose down 2>/dev/null || true
    docker compose up -d --build

    sleep 3

    echo -e "\n${CYAN}🔍 Проверка статуса контейнеров...${NC}"
    docker compose ps

    if docker compose ps --status running 2>/dev/null | grep -q "mega-bot"; then
        echo -e "\n${GREEN}====================================================================${NC}"
        echo -e "${GREEN}${BOLD}🎉 БОТ УСПЕШНО РАЗВЕРНУТ И ЗАПУЩЕН В DOCKER!${NC}"
        echo -e "===================================================================="
    else
        echo -e "\n${RED}⚠️ Ошибка: Контейнер mega-bot не смог запуститься. Логи:${NC}"
        docker compose logs --tail 30
        exit 1
    fi
    echo -e "${GREEN}${BOLD}📋 КОМАНДЫ УПРАВЛЕНИЯ DOCKER-СТЕКОМ:${NC}"
    echo -e "  • Статус контейнеров:    ${BOLD}docker compose ps${NC}"
    echo -e "  • Живые логи бота:       ${BOLD}docker compose logs -f mega-bot${NC}"
    if [ "$USE_CADDY" = true ]; then
    echo -e "  • Живые логи Caddy:      ${BOLD}docker compose logs -f caddy${NC}"
    fi
    echo -e "  • Перезапуск стека:      ${BOLD}docker compose restart${NC}"
    echo -e "  • Остановка:             ${BOLD}docker compose down${NC}"
    echo -e "  • Обновление и билд:     ${BOLD}docker compose up -d --build${NC}"
    echo -e "  • Конфигурация (.env):   ${BOLD}nano ${INSTALL_DIR}/.env${NC}"
    echo -e "${GREEN}====================================================================${NC}\n"
    exit 0
fi

# ==============================================================================
# ВАРИАНТ 2: РАЗВЕРТЫВАНИЕ ЧЕРЕЗ SYSTEMD НА ХОСТЕ
# ==============================================================================
echo -e "\n${BLUE}📦 Установка системных зависимостей хоста...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-pip \
    ffmpeg \
    git \
    curl \
    ca-certificates

# Установка Caddy на хост при наличии домена
if [ "$USE_CADDY" = true ]; then
    echo -e "\n${BLUE}🌐 Настройка Caddy на хосте...${NC}"
    if ! command -v caddy &>/dev/null; then
        echo -e "${CYAN}📥 Добавление репозитория Caddy...${NC}"
        apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        apt-get update -y
        apt-get install -y caddy
    fi

    mkdir -p /etc/caddy
    cat << CADDY_HOST_CONF > /etc/caddy/Caddyfile
${DOMAIN_NAME} {
    reverse_proxy 127.0.0.1:8080
}
CADDY_HOST_CONF

    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        echo -e "${CYAN}🔓 Открытие портов 80 и 443 в UFW...${NC}"
        ufw allow 80/tcp >/dev/null 2>&1 || true
        ufw allow 443/tcp >/dev/null 2>&1 || true
    fi

    systemctl daemon-reload
    systemctl enable caddy
    systemctl restart caddy
fi

# Виртуальное окружение Python
echo -e "\n${BLUE}🐍 Развертывание виртуального окружения Python...${NC}"
rm -rf "${INSTALL_DIR}/venv"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

# Регистрация systemd-сервиса
SERVICE_FILE="/etc/systemd/system/mega-bot.service"
echo -e "\n${BLUE}⚙️ Регистрация systemd-сервиса ${SERVICE_FILE}...${NC}"

cat << SYSTEMD_UNIT > "${SERVICE_FILE}"
[Unit]
Description=Universal Media Downloader Telegram Bot
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
LimitNOFILE=65536
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_UNIT

systemctl daemon-reload
systemctl enable mega-bot.service
echo -e "${CYAN}🔄 Запуск сервиса mega-bot...${NC}"
systemctl restart mega-bot.service

sleep 2

# Проверка статуса
echo -e "\n${CYAN}🔍 Проверка статуса запуска...${NC}"
if systemctl is-active --quiet mega-bot.service; then
    echo -e "${GREEN}${BOLD}🎉 БОТ УСПЕШНО УСТАНОВЛЕН И ЗАПУЩЕН ЧЕРЕЗ SYSTEMD!${NC}"
else
    echo -e "${RED}⚠️ Сервис создан, но статус не 'active'. Логи:${NC}"
    journalctl -u mega-bot.service --no-pager -n 25
    exit 1
fi

echo -e "\n${GREEN}====================================================================${NC}"
echo -e "${GREEN}${BOLD}📋 КОМАНДЫ УПРАВЛЕНИЯ SYSTEMD:${NC}"
echo -e "  • Статус сервиса:        ${BOLD}systemctl status mega-bot${NC}"
echo -e "  • Живые логи:            ${BOLD}journalctl -u mega-bot -f -n 50${NC}"
echo -e "  • Перезапуск:            ${BOLD}systemctl restart mega-bot${NC}"
echo -e "  • Остановка:             ${BOLD}systemctl stop mega-bot${NC}"
echo -e "  • Конфигурация (.env):   ${BOLD}nano ${INSTALL_DIR}/.env${NC}"
echo -e "${GREEN}====================================================================${NC}\n"
