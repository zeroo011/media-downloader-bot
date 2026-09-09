#!/usr/bin/env bash
# ==============================================================================
# Скрипт автоматической установки и настройки Universal Media Downloader Bot
# Поддерживаемые ОС: Ubuntu 20.04+, Debian 11+
# ==============================================================================

set -e

# Цвета для красивого вывода в терминале
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    PURPLE='\033[0;35m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    NC='\033[0m' # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    PURPLE=''
    CYAN=''
    BOLD=''
    NC=''
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
DISK_FREE_MB=$(df -m / | awk 'NR==2 {print $4}')

echo -e "   • Процессор:   ${BOLD}${CPU_CORES} vCPU${NC}"
echo -e "   • Память RAM:  ${BOLD}${TOTAL_RAM_MB} МБ${NC}"
echo -e "   • Файл Swap:   ${BOLD}${TOTAL_SWAP_MB} МБ${NC}"
echo -e "   • Диск (свободно на /): ${BOLD}${DISK_FREE_HUMAN}${NC}"

# 3. Проверка и автоматическая настройка SWAP (если RAM < 2 ГБ)
if [ "${TOTAL_RAM_MB}" -lt 2048 ] && [ "${TOTAL_SWAP_MB}" -lt 1024 ]; then
    echo -e "\n${YELLOW}⚠️ Внимание: на сервере менее 2 ГБ оперативной памяти (${TOTAL_RAM_MB} МБ).${NC}"
    echo -e "${YELLOW}При склейке и конвертации тяжелых видео FFmpeg может вызывать OOM (Out Of Memory).${NC}"
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

# 4. Выбор профиля производительности
echo -e "\n${BLUE}⚙️ Выбор профиля оптимизации под ресурсы сервера:${NC}"

# Рекомендуемый профиль на основе ресурсов
if [ "$CPU_CORES" -ge 4 ] && [ "$TOTAL_RAM_MB" -ge 7000 ]; then
    REC_PROFILE=3
    REC_TEXT="High"
elif [ "$CPU_CORES" -ge 2 ] && [ "$TOTAL_RAM_MB" -ge 3500 ]; then
    REC_PROFILE=2
    REC_TEXT="Medium"
else
    REC_PROFILE=1
    REC_TEXT="Low"
fi

echo "  1) Low    (1 vCPU, 1-2 ГБ RAM) — 1 воркер, 1 поток FFmpeg, лимит 100 МБ"
echo "  2) Medium (2-4 vCPU, 4 ГБ RAM) — 2 воркера, 2 потока FFmpeg, лимит 200 МБ"
echo "  3) High   (4+ vCPU, 8+ ГБ RAM) — 4 воркера, 4 потока FFmpeg, лимит 500 МБ"
echo "  4) Автоматический выбор [Рекомендуется: ${REC_TEXT}]"

read -r -p "Выберите профиль [1-4, по умолчанию 4]: " PROFILE_CHOICE
PROFILE_CHOICE=${PROFILE_CHOICE:-4}

case "$PROFILE_CHOICE" in
    1)
        NUM_WORKERS=1
        FFMPEG_THREADS=1
        MAX_FILE_SIZE_MB=100
        MIN_FREE_DISK_GB=1.5
        SELECTED_PROFILE="Low"
        ;;
    2)
        NUM_WORKERS=2
        FFMPEG_THREADS=2
        MAX_FILE_SIZE_MB=200
        MIN_FREE_DISK_GB=2.0
        SELECTED_PROFILE="Medium"
        ;;
    3)
        NUM_WORKERS=4
        FFMPEG_THREADS=4
        MAX_FILE_SIZE_MB=500
        MIN_FREE_DISK_GB=4.0
        SELECTED_PROFILE="High"
        ;;
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

echo -e "${GREEN}✅ Выбран профиль: ${BOLD}${SELECTED_PROFILE}${NC} (Воркеров: ${NUM_WORKERS}, Потоков FFmpeg: ${FFMPEG_THREADS}, Макс. файл: ${MAX_FILE_SIZE_MB} МБ)"

# 5. Интерактивный ввод параметров бота
echo -e "\n${BLUE}🔑 Ввод параметров бота:${NC}"

# Валидация BOT_TOKEN
while true; do
    read -r -p "Введи токен Telegram-бота (от @BotFather): " BOT_TOKEN
    BOT_TOKEN=$(echo "$BOT_TOKEN" | tr -d '[:space:]')
    if [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{35,}$ ]]; then
        break
    else
        echo -e "${RED}❌ Неверный формат токена бота! Пример правильного формата: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ1234567890${NC}"
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
DEFAULT_WEB_URL="http://${EXTERNAL_IP}:8080"

read -r -p "Публичный URL для веб-скачивания и инлайн-превью [по умолчанию: ${DEFAULT_WEB_URL}]: " INPUT_WEB_URL
WEB_BASE_URL=${INPUT_WEB_URL:-$DEFAULT_WEB_URL}
WEB_BASE_URL=$(echo "$WEB_BASE_URL" | sed 's:/*$::')

# 6. Установка системных зависимостей
echo -e "\n${BLUE}📦 Установка системных пакетов (ffmpeg, python3, venv, pip, curl)...${NC}"
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

# 7. Развертывание виртуального окружения Python
echo -e "\n${BLUE}🐍 Создание виртуального окружения Python...${NC}"
rm -rf "${INSTALL_DIR}/venv"
python3 -m venv "${INSTALL_DIR}/venv"

echo -e "${CYAN}📥 Установка Python-зависимостей из requirements.txt...${NC}"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

# 8. Каталоги для хранения данных
mkdir -p "${INSTALL_DIR}/data/web_downloads"
mkdir -p "${INSTALL_DIR}/data/cache"
chmod 755 "${INSTALL_DIR}/data"

# 9. Создание конфигурационного файла .env
echo -e "\n${BLUE}📝 Создание файла конфигурации .env...${NC}"
cat << ENV_CONFIG > "${INSTALL_DIR}/.env"
# Сгенерировано автоматически скриптом install.sh ($(date))
BOT_TOKEN=${BOT_TOKEN}
ADMIN_ID=${ADMIN_ID}
BOT_USERNAME=
WEB_BASE_URL=${WEB_BASE_URL}
WEB_PORT=8080
DATA_DIR=${INSTALL_DIR}/data

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

# 10. Создание и регистрация systemd-сервиса
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

# 11. Проверка статуса
echo -e "\n${CYAN}🔍 Проверка статуса запуска...${NC}"
if systemctl is-active --quiet mega-bot.service; then
    echo -e "${GREEN}${BOLD}🎉 БОТ УСПЕШНО УСТАНОВЛЕН И ЗАПУЩЕН!${NC}"
else
    echo -e "${RED}⚠️ Сервис был создан, но статус не 'active'. Проверьте логи ниже:${NC}"
    journalctl -u mega-bot.service --no-pager -n 25
    exit 1
fi

echo -e "\n${GREEN}====================================================================${NC}"
echo -e "${GREEN}${BOLD}📋 ПОЛЕЗНЫЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ БОТОМ:${NC}"
echo -e "  • Статус сервиса:        ${BOLD}systemctl status mega-bot${NC}"
echo -e "  • Живой лог сообщений:   ${BOLD}journalctl -u mega-bot -f -n 50${NC}"
echo -e "  • Перезапуск бота:       ${BOLD}systemctl restart mega-bot${NC}"
echo -e "  • Остановка бота:        ${BOLD}systemctl stop mega-bot${NC}"
echo -e "  • Конфигурация:          ${BOLD}nano ${INSTALL_DIR}/.env${NC}"
echo -e "${GREEN}====================================================================${NC}\n"
