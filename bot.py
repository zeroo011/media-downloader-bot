import asyncio
import glob
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, FSInputFile, InputMediaPhoto, BotCommand,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    InlineQueryResultVideo, InlineQueryResultCachedVideo,
    InlineQueryResultPhoto, InlineQueryResultCachedPhoto,
    InlineQueryResultAudio, InlineQueryResultCachedAudio
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

BOT_START_TIME = time.time()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "bot.db"))
COOKIES_PATH = os.getenv("COOKIES_PATH", os.path.join(DATA_DIR, "cookies.txt"))
WEB_DOWNLOADS_DIR = os.getenv("WEB_DOWNLOADS_DIR", os.path.join(DATA_DIR, "web_downloads"))
CACHE_DIR = os.getenv("CACHE_DIR", os.path.join(DATA_DIR, "cache"))
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").rstrip("/")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# Лимиты и квоты для безопасности диска и производительности
MAX_TG_FILE_SIZE_BYTES = 49 * 1024 * 1024        # 49 МБ (лимит отправки Telegram Bot API)
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "200"))
MAX_WEB_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024      # Лимит для веб-скачивания (по умолчанию 200 МБ)
WEB_TTL_SECONDS = int(os.getenv("WEB_TTL_SECONDS", str(7 * 60)))                         # 7 минут для веб-ссылок
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(4 * 60)))                       # 4 минуты для кнопок MP3/Кружочек

MAX_WEB_DIR_BYTES = int(float(os.getenv("MAX_WEB_DIR_GB", "3.5")) * 1024 * 1024 * 1024)   # Макс квота на web_downloads
MAX_CACHE_DIR_BYTES = int(float(os.getenv("MAX_CACHE_DIR_GB", "1.0")) * 1024 * 1024 * 1024) # Макс квота на cache
MIN_FREE_DISK_BYTES = int(float(os.getenv("MIN_FREE_DISK_GB", "2.0")) * 1024 * 1024 * 1024) # Мин свободного диска

NUM_WORKERS = max(1, int(os.getenv("NUM_WORKERS", "2")))   # Параллельные воркеры для очереди
FFMPEG_THREADS = max(1, int(os.getenv("FFMPEG_THREADS", "1"))) # Потоки для кодирования FFmpeg
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(WEB_DOWNLOADS_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

if not TOKEN:
    logging.error("BOT_TOKEN не задан!")
    sys.exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

DOWNLOAD_QUEUE: asyncio.Queue = asyncio.Queue(maxsize=50)
QUEUE_JOBS: list["DownloadJob"] = []
BUSY_WORKERS: set[int] = set()
USER_ACTIVE_COUNT: dict[int, int] = {}
USER_ACTIVE_TIMESTAMP: dict[int, float] = {}
USER_COOLDOWNS: dict[int, float] = {}
ACTIVE_CONVERSIONS: set[int] = set()  # Защита от DoS спама кнопками конвертации

# Реестры файлов
DOWNLOAD_LINKS: dict[str, dict] = {}
CONVERT_CACHE: dict[str, dict] = {}

@dataclass
class DownloadJob:
    message: Message
    status_msg: Message
    url: str
    user_id: int
    mode: str = "auto"  # "auto", "audio", "round"
    last_status_text: str = ""
    is_active: bool = False

class Support(StatesGroup):
    waiting_for_message = State()

class Broadcast(StatesGroup):
    waiting_for_content = State()
    waiting_for_confirm = State()

# --- СИСТЕМНЫЕ МЕТРИКИ И МОНИТОРИНГ ДИСКА ---

def make_progress_bar(percent: float, length: int = 10) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = int(round(length * (clamped / 100.0)))
    return "█" * filled + "░" * (length - filled)

def get_dir_size_mb(path: str) -> float:
    total_size = 0
    if os.path.exists(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    try:
                        total_size += os.path.getsize(fp)
                    except OSError:
                        pass
    return round(total_size / (1024 * 1024), 1)

def get_system_metrics() -> dict:
    total, used, free = shutil.disk_usage("/")
    disk_percent = (used / total) * 100.0 if total else 0

    mem_total, mem_avail = 0, 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem_total = int(line.split()[1]) // 1024
                elif "MemAvailable" in line:
                    mem_avail = int(line.split()[1]) // 1024
        mem_used = mem_total - mem_avail
        mem_percent = (mem_used / mem_total) * 100.0 if mem_total else 0
    except Exception:
        mem_used, mem_total, mem_avail, mem_percent = 0, 0, 0, 0

    load_1, load_5, load_15 = 0.0, 0.0, 0.0
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load_1, load_5, load_15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        pass

    server_uptime = "н/д"
    try:
        with open("/proc/uptime") as f:
            up_sec = float(f.read().split()[0])
            days = int(up_sec // 86400)
            hours = int((up_sec % 86400) // 3600)
            mins = int((up_sec % 3600) // 60)
            server_uptime = f"{days}д {hours}ч {mins}м" if days else f"{hours}ч {mins}м"
    except Exception:
        pass

    bot_sec = int(time.time() - BOT_START_TIME)
    b_days = bot_sec // 86400
    b_hours = (bot_sec % 86400) // 3600
    b_mins = (bot_sec % 3600) // 60
    bot_uptime = f"{b_days}д {b_hours}ч {b_mins}м" if b_days else f"{b_hours}ч {b_mins}м"

    return {
        "disk_total_gb": round(total / (1024**3), 1),
        "disk_used_gb": round(used / (1024**3), 1),
        "disk_free_gb": round(free / (1024**3), 1),
        "disk_free_bytes": free,
        "disk_percent": round(disk_percent, 1),
        "mem_total_mb": mem_total,
        "mem_used_mb": mem_used,
        "mem_percent": round(mem_percent, 1),
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "server_uptime": server_uptime,
        "bot_uptime": bot_uptime,
        "web_size_mb": get_dir_size_mb(WEB_DOWNLOADS_DIR),
        "cache_size_mb": get_dir_size_mb(CACHE_DIR),
        "active_links": len(DOWNLOAD_LINKS),
        "cached_files": len(CONVERT_CACHE)
    }

def enforce_dir_quota(dir_path: str, max_bytes: int, registry: dict):
    """FIFO-удаление старых файлов при превышении квоты директории."""
    if not os.path.exists(dir_path):
        return
    files = []
    total_size = 0
    for f in os.listdir(dir_path):
        fp = os.path.join(dir_path, f)
        if os.path.isfile(fp):
            try:
                sz = os.path.getsize(fp)
                mtime = os.path.getmtime(fp)
                total_size += sz
                files.append((fp, sz, mtime))
            except OSError:
                pass

    if total_size > max_bytes:
        files.sort(key=lambda x: x[2])
        for fp, sz, _ in files:
            try:
                os.remove(fp)
                total_size -= sz
                for k, v in list(registry.items()):
                    if v.get("path") == fp:
                        del registry[k]
            except OSError:
                pass
            if total_size <= max_bytes * 0.75:
                break

def emergency_disk_cleanup():
    """Экстренная очистка всех временных файлов при низком свободном месте."""
    logging.warning("Запуск экстренной очистки диска!")
    for d, reg in [(CACHE_DIR, CONVERT_CACHE), (WEB_DOWNLOADS_DIR, DOWNLOAD_LINKS)]:
        if os.path.exists(d):
            for f in glob.glob(f"{d}/*"):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                    elif os.path.isdir(f):
                        shutil.rmtree(f, ignore_errors=True)
                except OSError:
                    pass
        reg.clear()

def is_bot_temp_entry(path: str) -> bool:
    """Проверка, принадлежит ли файл/папка боту (32-символьный hex UUID задачи или суффикс)."""
    name = os.path.basename(path)
    base = name.split(".")[0].split("_")[0]
    return len(base) == 32 and all(c in "0123456789abcdefABCDEF" for c in base)

def clean_tmp_dir():
    """Очистка временных папок и старого кэша при старте."""
    now = time.time()
    for f in glob.glob("/tmp/*"):
        if not is_bot_temp_entry(f):
            continue
        try:
            if os.path.isdir(f):
                shutil.rmtree(f, ignore_errors=True)
            elif os.path.isfile(f) or os.path.islink(f):
                os.remove(f)
        except OSError:
            pass

    for d, ttl in [(CACHE_DIR, CACHE_TTL_SECONDS), (WEB_DOWNLOADS_DIR, WEB_TTL_SECONDS)]:
        if os.path.exists(d):
            for f in glob.glob(f"{d}/*"):
                try:
                    if os.path.isfile(f) and (now - os.path.getmtime(f) > ttl):
                        os.remove(f)
                except OSError:
                    pass

# --- БАЗА ДАННЫХ ---

DB_CONN: aiosqlite.Connection | None = None
DB_LOCK: asyncio.Lock | None = None

@asynccontextmanager
async def get_db():
    global DB_CONN, DB_LOCK
    if DB_LOCK is None:
        DB_LOCK = asyncio.Lock()
    async with DB_LOCK:
        if DB_CONN is not None:
            try:
                await DB_CONN.execute("SELECT 1;")
            except Exception:
                try:
                    await DB_CONN.close()
                except Exception:
                    pass
                DB_CONN = None
        if DB_CONN is None:
            DB_CONN = await aiosqlite.connect(DB_PATH, timeout=30.0)
            await DB_CONN.execute("PRAGMA journal_mode=WAL;")
            await DB_CONN.execute("PRAGMA busy_timeout=5000;")
            await DB_CONN.execute("PRAGMA synchronous=NORMAL;")
            await DB_CONN.execute("PRAGMA wal_autocheckpoint=100;")
        yield DB_CONN

async def close_db():
    global DB_CONN
    if DB_CONN is not None:
        try:
            await DB_CONN.close()
        except Exception:
            pass
        DB_CONN = None

async def init_db():
    async with get_db() as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY);")
        await db.execute("CREATE TABLE IF NOT EXISTS tickets (admin_msg_id INTEGER PRIMARY KEY, user_id INTEGER);")
        await db.execute("CREATE TABLE IF NOT EXISTS stats (key TEXT PRIMARY KEY, value INTEGER DEFAULT 0);")
        await db.execute("INSERT OR IGNORE INTO stats (key, value) VALUES ('success', 0), ('failed', 0);")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS media_cache (
                url TEXT PRIMARY KEY,
                file_id TEXT,
                media_type TEXT,
                title TEXT,
                created_at REAL
            );
        """)
        await db.commit()

async def save_media_cache(url: str, file_id: str, media_type: str, title: str):
    clean = url.split("?")[0].rstrip("/")
    now = time.time()
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT OR REPLACE INTO media_cache (url, file_id, media_type, title, created_at) VALUES (?, ?, ?, ?, ?);",
                (url, file_id, media_type, title, now)
            )
            await db.execute(
                "INSERT OR REPLACE INTO media_cache (url, file_id, media_type, title, created_at) VALUES (?, ?, ?, ?, ?);",
                (clean, file_id, media_type, title, now)
            )
            await db.commit()
    except Exception as e:
        logging.warning(f"Failed to save media_cache: {e}")

async def get_media_cache(url: str):
    clean = url.split("?")[0].rstrip("/")
    try:
        async with get_db() as db:
            async with db.execute(
                "SELECT file_id, media_type, title FROM media_cache WHERE url = ? OR url = ? LIMIT 1;",
                (url, clean)
            ) as cursor:
                return await cursor.fetchone()
    except Exception:
        return None

async def add_user(user_id: int):
    async with get_db() as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?);", (user_id,))
        await db.commit()

async def increment_stat(key: str):
    try:
        async with get_db() as db:
            await db.execute("UPDATE stats SET value = value + 1 WHERE key = ?;", (key,))
            await db.commit()
    except Exception as e:
        logging.error(f"Stat update error: {e}")

async def get_stats() -> dict:
    async with get_db() as db:
        async with db.execute("SELECT key, value FROM stats;") as cursor:
            rows = await cursor.fetchall()
            return {r[0]: r[1] for r in rows}

async def get_all_users():
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM users;") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def get_users_count() -> int:
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users;") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def save_ticket_msg(admin_msg_id: int, user_id: int):
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO tickets (admin_msg_id, user_id) VALUES (?, ?);", (admin_msg_id, user_id))
        await db.commit()

async def get_user_by_ticket_msg(admin_msg_id: int):
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM tickets WHERE admin_msg_id = ?;", (admin_msg_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

def clean_cooldowns():
    now = time.time()
    if len(USER_COOLDOWNS) > 2000:
        keys_to_del = [uid for uid, t in USER_COOLDOWNS.items() if now - t > 60]
        for uid in keys_to_del:
            USER_COOLDOWNS.pop(uid, None)

# --- ВЕБ-СЕРВЕР ---

async def handle_root(request: web.Request) -> web.Response:
    if BOT_USERNAME:
        raise web.HTTPFound(f"https://t.me/{BOT_USERNAME}")
    return web.Response(text="Universal Media Downloader Bot Web Server")

async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

async def handle_download(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token")
    info = DOWNLOAD_LINKS.get(token)
    now = time.time()
    if not info or now > info["expire_at"] or not os.path.exists(info["path"]):
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Файл недоступен</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:1rem;}
.box{background:#1e293b;padding:2.5rem;border-radius:1rem;text-align:center;max-width:440px;box-shadow:0 10px 30px rgba(0,0,0,0.5);border:1px solid #334155;}
h2{color:#f87171;margin-top:0;}
p{color:#94a3b8;line-height:1.6;}
.badge{display:inline-block;background:#334155;color:#e2e8f0;padding:0.25rem 0.75rem;border-radius:9999px;font-size:0.875rem;margin-top:1rem;}
</style></head>
<body><div class="box">
<h2>⏳ Срок ссылки истек</h2>
<p>Файл был автоматически удален с сервера через <b>7 минут</b> после создания для защиты диска от переполнения.</p>
<div class="badge">Запросите файл заново в Telegram-боте</div>
</div></body></html>"""
        return web.Response(text=html, content_type="text/html", status=404)

    # Безопасное формирование Content-Disposition по стандарту RFC 6266 / RFC 5987
    raw_filename = info.get("filename", "media.mp4")
    clean_name = raw_filename.replace('"', '').replace('\r', '').replace('\n', '')
    ext = os.path.splitext(clean_name)[1] or ".mp4"
    ascii_name = re.sub(r'[^a-zA-Z0-9_\.-]', '_', clean_name)
    if not ascii_name.strip('_'):
        ascii_name = f"download{ext}"
    encoded_name = urllib.parse.quote(clean_name, safe=".-_")
    disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'

    return web.FileResponse(
        info["path"],
        headers={
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
        }
    )

async def handle_thumb(request: web.Request) -> web.StreamResponse:
    token = request.match_info.get("token")
    info = DOWNLOAD_LINKS.get(token)
    if info and "thumb_path" in info and os.path.exists(info["thumb_path"]):
        return web.FileResponse(info["thumb_path"], headers={"Content-Type": "image/jpeg"})
    default_thumb = os.path.join(DATA_DIR, "default_thumb.jpg")
    if os.path.exists(default_thumb):
        return web.FileResponse(default_thumb, headers={"Content-Type": "image/jpeg"})
    return web.Response(status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_head("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/dl/thumb/{token}.jpg", handle_thumb)
    app.router.add_get("/dl/{token}/{filename}", handle_download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {WEB_PORT}")
    return runner

async def cleanup_worker():
    """Фоновый сборщик мусора и защитник дискового пространства."""
    while True:
        try:
            await asyncio.sleep(25)
            now = time.time()

            # 1. Проверка свободного места
            _, _, free_bytes = shutil.disk_usage("/")
            if free_bytes < MIN_FREE_DISK_BYTES:
                emergency_disk_cleanup()

            # 2. Удаление просроченных веб-ссылок (>7 минут)
            expired_tokens = [tok for tok, item in list(DOWNLOAD_LINKS.items()) if now > item["expire_at"]]
            for tok in expired_tokens:
                item = DOWNLOAD_LINKS.pop(tok, None)
                if item and os.path.exists(item["path"]):
                    try:
                        os.remove(item["path"])
                    except Exception:
                        pass

            # 3. Удаление просроченного кэша конвертаций (>4 минут)
            expired_cache = [cid for cid, item in list(CONVERT_CACHE.items()) if now > item["expire_at"]]
            for cid in expired_cache:
                item = CONVERT_CACHE.pop(cid, None)
                if item and os.path.exists(item["path"]):
                    try:
                        os.remove(item["path"])
                    except Exception:
                        pass

            # 3.1 Физическая очистка файлов на диске старше TTL (включая файлы после рестарта)
            for f in glob.glob(f"{CACHE_DIR}/*"):
                try:
                    if os.path.isfile(f) and (now - os.path.getmtime(f) > CACHE_TTL_SECONDS):
                        os.remove(f)
                except OSError:
                    pass

            for f in glob.glob(f"{WEB_DOWNLOADS_DIR}/*"):
                try:
                    if os.path.isfile(f) and (now - os.path.getmtime(f) > WEB_TTL_SECONDS):
                        os.remove(f)
                except OSError:
                    pass

            # 4. Контроль квот директорий (FIFO)
            enforce_dir_quota(WEB_DOWNLOADS_DIR, MAX_WEB_DIR_BYTES, DOWNLOAD_LINKS)
            enforce_dir_quota(CACHE_DIR, MAX_CACHE_DIR_BYTES, CONVERT_CACHE)

            # 5. Очистка остаточных папок /tmp старше 4 минут (только файлов и папок бота)
            for tp in glob.glob("/tmp/*"):
                if is_bot_temp_entry(tp):
                    try:
                        if now - os.path.getmtime(tp) > 240:
                            if os.path.isdir(tp):
                                shutil.rmtree(tp, ignore_errors=True)
                            elif os.path.isfile(tp) or os.path.islink(tp):
                                os.remove(tp)
                    except OSError:
                        pass

            # 6. Сброс зависших локов пользователей старше 5 минут
            for uid in list(USER_ACTIVE_COUNT.keys()):
                if USER_ACTIVE_COUNT.get(uid, 0) > 0:
                    ts = USER_ACTIVE_TIMESTAMP.get(uid)
                    if ts is None or (now - ts > 300):
                        USER_ACTIVE_COUNT[uid] = 0
                        USER_ACTIVE_TIMESTAMP.pop(uid, None)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

# --- ОБРАБОТКА ОШИБОК И URL ---

def is_private_or_local_url(url: str) -> bool:
    """Защита от SSRF: блокировка localhost, private subnets, cloud metadata и DNS rebinding."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True
        host = parsed.hostname
        if not host:
            return True
        host_lower = host.lower()
        if host_lower in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        if host_lower.endswith((".local", ".internal", ".localhost", ".onion")):
            return True
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
        except ValueError:
            try:
                addr_info = socket.getaddrinfo(host, None)
                for res in addr_info:
                    ip_str = res[4][0]
                    ip = ipaddress.ip_address(ip_str)
                    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                        return True
            except (socket.gaierror, ValueError):
                return True
    except Exception:
        return True
    return False

def check_unsupported_url(url: str) -> str | None:
    if is_private_or_local_url(url):
        return "Локальные и приватные сетевые адреса не поддерживаются."
    if any(d in url.lower() for d in ["reddit.com", "redd.it"]):
        return "Сервис Reddit заблокировал доступ со стороны серверов (HTTP 403 Forbidden). Загрузка с Reddit временно недоступна."
    if "youtube.com/playlist" in url:
        return "Плейлисты пока не поддерживаются. Отправь ссылку на конкретное видео."
    if "youtube.com/live/" in url:
        return "Прямые эфиры и стримы не поддерживаются."
    if re.search(r'youtube\.com/(@[\w.-]+|channel/|c/)(/featured|/videos)?(\?.*)?$', url):
        return "Это ссылка на канал. Отправь ссылку на видео или Shorts."
    if re.search(r'tiktok\.com/@[\w.-]+/?(\?.*)?$', url):
        return "Это ссылка на профиль TikTok. Отправь ссылку на публикацию."
    if re.search(r'instagram\.com/(?!p/|reel/|tv/|stories/)[\w.-]+/?(\?.*)?$', url):
        return "Это ссылка на профиль Instagram. Отправь ссылку на пост или Reels."
    return None

def parse_ytdlp_error(stderr_text: str) -> str:
    lower = stderr_text.lower()
    if "sign in to confirm you're not a bot" in lower:
        return "YouTube заблокировал запрос (проверка на бота). Требуется обновить cookies в боте."
    if "private video" in lower or "this video is private" in lower:
        return "Видео приватное (доступ только по персональному приглашению)."
    if "video unavailable" in lower:
        return "Видео недоступно (удалено автором или заблокировано в регионе)."
    if "age-restricted" in lower or "confirm your age" in lower:
        return "Видео имеет возрастное ограничение 18+. Требуются cookies аккаунта."
    if "copyright" in lower or "blocked on copyright grounds" in lower:
        return "Видео заблокировано правообладателем по авторским правам."
    if "join this channel" in lower or "members-only" in lower:
        return "Видео доступно только спонсорам канала."
    if "reddit" in lower or "account authentication is required" in lower:
        return "Сервис Reddit заблокировал доступ со стороны серверов (HTTP 403 Forbidden). Загрузка с Reddit временно недоступна."
    if "max-filesize" in lower:
        return f"Файл превышает лимит сервера ({MAX_FILE_SIZE_MB} МБ)."
    return f"Не удалось скачать. Видео приватное, превышен лимит {MAX_FILE_SIZE_MB} МБ или сервис временно недоступен."

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ПРОГРЕСС-БАРА И ВРЕМЕНИ СКАЧИВАНИЯ ---

def strip_ansi(text: str) -> str:
    """Очищает строку от ANSI escape-последовательностей (цвета терминала)."""
    if not text:
        return ""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)

def format_eta_seconds(seconds: int) -> str:
    """Форматирует секунды в понятное время на русском языке."""
    if seconds <= 0:
        return "несколько секунд"
    if seconds < 60:
        return f"~{seconds} сек"
    mins = seconds // 60
    secs = seconds % 60
    if mins < 60:
        return f"~{mins} мин {secs} сек" if secs > 0 else f"~{mins} мин"
    hours = mins // 60
    rem_mins = mins % 60
    return f"~{hours} ч {rem_mins} мин" if rem_mins > 0 else f"~{hours} ч"

def parse_and_format_eta(eta_str: str) -> str:
    """Преобразует ETA из любого формата (секунды, MM:SS, HH:MM:SS) в человекопонятный вид."""
    if not eta_str:
        return ""
    clean_eta = strip_ansi(eta_str).strip()
    if not clean_eta or clean_eta.upper() in ("NA", "UNKNOWN", "NONE", "N/A"):
        return ""
    if clean_eta in ("00:00", "0", "00"):
        return "завершение..."
    # Если это число секунд
    try:
        sec = int(float(clean_eta))
        return format_eta_seconds(sec)
    except ValueError:
        pass
    # Если формат MM:SS или HH:MM:SS
    parts = clean_eta.split(":")
    try:
        if len(parts) == 2:
            mins, secs = int(parts[0]), int(parts[1])
            total_sec = mins * 60 + secs
            return format_eta_seconds(total_sec)
        elif len(parts) == 3:
            hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
            total_sec = hours * 3600 + mins * 60 + secs
            return format_eta_seconds(total_sec)
    except ValueError:
        pass
    return f"~{clean_eta}"

def format_speed_eta(speed_str: str, eta_str: str) -> str:
    """Формирует строку скорости и оставшегося времени скачивания."""
    parts = []
    if speed_str:
        clean_speed = strip_ansi(speed_str).strip()
        if clean_speed and clean_speed.upper() not in ("NA", "UNKNOWN", "NONE", "N/A"):
            parts.append(f"⚡ {clean_speed}")
    if eta_str:
        human_eta = parse_and_format_eta(eta_str)
        if human_eta:
            parts.append(f"⏳ {human_eta}")
    return " • ".join(parts)

# --- ПРЯМЫЕ МЕДИА ССЫЛКИ И СОЦСЕТИ (REDDIT, PINTEREST, TWITTER) ---

DIRECT_MEDIA_EXTENSIONS = (
    '.mp4', '.mov', '.m4v', '.webm', '.mkv',
    '.mp3', '.wav', '.m4a', '.ogg', '.opus', '.flac', '.aac',
    '.jpg', '.jpeg', '.png', '.webp', '.gif'
)

DIRECT_MEDIA_CDNS = (
    'i.redd.it', 'preview.redd.it', 'cf.preview.redd.it', 'external-preview.redd.it',
    'i.imgur.com', 'pbs.twimg.com', 'i.pinimg.com', 'cdn.discordapp.com', 'media.discordapp.net'
)

def is_direct_media_url(url: str) -> bool:
    clean_url = url.split("?")[0].split("#")[0].lower()
    if any(clean_url.endswith(ext) for ext in DIRECT_MEDIA_EXTENSIONS):
        return True
    try:
        parsed = urllib.parse.urlparse(url)
        query = parsed.query.lower()
        if any(f"format={ext[1:]}" in query or f"ext={ext[1:]}" in query for ext in DIRECT_MEDIA_EXTENSIONS):
            return True
        if any(query.endswith(ext) or f"{ext}&" in query for ext in DIRECT_MEDIA_EXTENSIONS):
            return True
        host = parsed.netloc.lower()
        if any(cdn in host for cdn in DIRECT_MEDIA_CDNS):
            return True
    except Exception:
        pass
    return False

def resolve_reddit_cdn_url(url: str) -> str:
    # Handles preview.redd.it, cf.preview.redd.it, i.redd.it
    m = re.search(r'([a-zA-Z0-9_-]+)\.(jpe?g|png|webp|gif)', url, re.IGNORECASE)
    if m:
        filename = m.group(0)
        if "-v0-" in filename:
            filename = filename.split("-v0-")[-1]
        return f"https://i.redd.it/{filename}"
    return url

async def download_direct_http(url: str, task_dir: str, status_msg: Message = None) -> bool:
    """Прямой стриминговый загрузчик файлов с живым обновлением прогресс-бара и защитой от SSRF/DoS."""
    if is_private_or_local_url(url):
        logging.warning(f"Blocked private/local URL attempt: {url}")
        return False

    orig_url = url
    if any(h in url for h in ["redd.it", "preview.redd"]):
        url = resolve_reddit_cdn_url(url)

    parsed = urllib.parse.urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": domain
    }

    dest_file = None
    try:
        timeout = aiohttp.ClientTimeout(total=120, connect=15)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            target_url = url
            resp = await session.get(target_url, allow_redirects=True)
            if resp.status != 200 and target_url != orig_url:
                resp.close()
                target_url = orig_url
                parsed = urllib.parse.urlparse(target_url)
                resp = await session.get(target_url, allow_redirects=True)

            try:
                if resp.status != 200:
                    logging.warning(f"Direct download failed: HTTP {resp.status} for {target_url}")
                    return False

                # Защита от SSRF через редиректы
                if is_private_or_local_url(str(resp.url)):
                    logging.warning(f"Blocked redirect to private/local URL: {resp.url}")
                    return False

                content_type = resp.headers.get("Content-Type", "").lower()
                ext = ""
                path_part = parsed.path.lower()
                for e in DIRECT_MEDIA_EXTENSIONS:
                    if path_part.endswith(e):
                        ext = e
                        break
                if not ext:
                    if "video/mp4" in content_type:
                        ext = ".mp4"
                    elif "image/jpeg" in content_type:
                        ext = ".jpg"
                    elif "image/png" in content_type:
                        ext = ".png"
                    elif "image/webp" in content_type:
                        ext = ".webp"
                    elif "image/gif" in content_type:
                        ext = ".gif"
                    elif "audio/mpeg" in content_type or "audio/mp3" in content_type:
                        ext = ".mp3"
                    elif "video/webm" in content_type:
                        ext = ".webm"
                    else:
                        ext = ".mp4"

                try:
                    total_size = int(resp.headers.get("Content-Length", 0) or 0)
                except (ValueError, TypeError):
                    total_size = 0
                if total_size > MAX_WEB_FILE_SIZE_BYTES:
                    logging.warning(f"Direct file exceeds {MAX_FILE_SIZE_MB}MB ({total_size} bytes)")
                    return False

                # Сохраняем оригинальное имя файла, если оно валидно
                raw_name = os.path.basename(urllib.parse.unquote(parsed.path))
                clean_name = re.sub(r'[^a-zA-Z0-9_\u0400-\u04FF\.\-]', '_', raw_name).strip('_')
                if clean_name and any(clean_name.lower().endswith(e) for e in DIRECT_MEDIA_EXTENSIONS):
                    dest_file = f"{task_dir}/{clean_name}"
                else:
                    dest_file = f"{task_dir}/direct_01{ext}"

                downloaded = 0
                last_edit_time = time.time()
                start_time = time.time()

                with open(dest_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(128 * 1024):
                        downloaded += len(chunk)
                        # Защита от DoS бесконечного потока / zip-бомбы
                        if downloaded > MAX_WEB_FILE_SIZE_BYTES:
                            logging.warning(f"Direct download exceeded {MAX_WEB_FILE_SIZE_BYTES} bytes, aborting")
                            f.close()
                            if os.path.exists(dest_file):
                                os.remove(dest_file)
                            return False
                        f.write(chunk)

                        if status_msg and total_size > 0:
                            now = time.time()
                            if now - last_edit_time >= 3.0:
                                last_edit_time = now
                                percent = min(100.0, (downloaded / total_size) * 100)
                                elapsed = now - start_time
                                speed = (downloaded / elapsed) if elapsed > 0 else 0
                                speed_mb = speed / (1024 * 1024)
                                if speed > 0 and total_size >= downloaded:
                                    rem_sec = int((total_size - downloaded) / speed)
                                    eta_display = format_eta_seconds(rem_sec)
                                else:
                                    eta_display = "несколько секунд"
                                bar = make_progress_bar(percent)
                                progress_text = (
                                    f"⚡ <b>Скачивание файла:</b>\n"
                                    f"<code>[{bar}] {percent:.1f}%</code>\n"
                                    f"⚡ {speed_mb:.1f} МБ/с • ⏳ {eta_display}"
                                )
                                try:
                                    await status_msg.edit_text(progress_text, parse_mode="HTML")
                                except Exception:
                                    pass

                return os.path.exists(dest_file) and os.path.getsize(dest_file) > 0
            finally:
                resp.close()
    except Exception as e:
        logging.error(f"Direct HTTP download error for {url}: {e}")
        if dest_file and os.path.exists(dest_file):
            try:
                os.remove(dest_file)
            except OSError:
                pass
        return False

async def download_reddit_post(url: str, task_dir: str) -> bool:
    """Извлечение медиафайлов и галерей из Reddit-постов и коротких ссылок."""
    REDDIT_PLACEHOLDER_DOMAINS = (
        "redditstatic.com", "redditmedia.com", "thumbs.redditmedia.com",
        "b.thumbs.redditmedia.com", "styles.redditmedia.com",
    )
    MEDIA_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4')

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    # Вспомогательная функция: скачать файл по ссылке
    async def fetch_media(session: aiohttp.ClientSession, media_url: str, filename: str) -> bool:
        try:
            m_headers = {"User-Agent": headers["User-Agent"], "Referer": "https://www.reddit.com/"}
            async with session.get(media_url, headers=m_headers, allow_redirects=True) as m_resp:
                if m_resp.status == 200:
                    # Убедимся, что не скачиваем HTML вместо медиа
                    ct = m_resp.headers.get("Content-Type", "")
                    if "text/html" in ct:
                        return False
                    content = await m_resp.read()
                    if len(content) < 1000:
                        return False  # слишком маленький файл — скорее всего заглушка
                    with open(filename, "wb") as f:
                        f.write(content)
                    return True
        except Exception:
            pass
        return False

    # Вспомогательная функция: проверить, является ли URL заглушкой Reddit
    def is_placeholder(img_url: str) -> bool:
        return any(d in img_url for d in REDDIT_PLACEHOLDER_DOMAINS)

    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:

            # --- ПУТЬ 1: rapidsave.com — лучший источник для видео и i.redd.it фото ---
            rapidsave_url = f"https://rapidsave.com/info?url={urllib.parse.quote(url, safe='')}"
            try:
                async with session.get(rapidsave_url) as resp:
                    if resp.status == 200:
                        html_text = await resp.text()

                        # Ищем только РЕАЛЬНЫЕ медиафайлы с расширением
                        ireddit_links = list(dict.fromkeys(re.findall(
                            r'https://i\.redd\.it/[a-zA-Z0-9_\-\.]+(?:\.jpg|\.jpeg|\.png|\.webp|\.gif)',
                            html_text, re.IGNORECASE
                        )))
                        dls = re.findall(r"""href=["']([^"']+)["'][^>]*download""", html_text)
                        v_links = [html.unescape(l) for l in dls if any(k in l for k in ["v.redd.it", ".mp4", "rapidsave.com"])]

                        target_links: list = []
                        if ireddit_links:
                            target_links = ireddit_links
                        elif v_links:
                            target_links = v_links[:1]
                        elif dls:
                            valid = [html.unescape(l) for l in dls
                                     if not any(x in l for x in ["reddit.com/r/", "itunes.apple", "ko-fi", "redditstatic", "redditmedia"])]
                            if valid:
                                target_links = valid[:1]

                        if target_links:
                            success = False
                            for idx, media_url in enumerate(target_links[:10]):
                                ext = ".jpg"
                                ml = media_url.lower()
                                if ".png" in ml: ext = ".png"
                                elif ".webp" in ml: ext = ".webp"
                                elif ".gif" in ml: ext = ".gif"
                                elif ".mp4" in ml or "video" in ml: ext = ".mp4"
                                ok = await fetch_media(session, media_url, f"{task_dir}/{idx:02d}_reddit{ext}")
                                if ok:
                                    success = True
                            if success:
                                return True
            except Exception as e:
                logging.warning(f"rapidsave fetch failed: {e}")

            # --- ПУТЬ 2: Reddit JSON API (работает для публичных постов) ---
            # Резолвим короткую ссылку в полный URL
            resolved_url = url
            try:
                async with session.get(url, headers=headers, allow_redirects=True, max_redirects=5) as rdr:
                    resolved_url = str(rdr.url)
            except Exception:
                pass

            # Убираем query-string, нормализуем к /r/sub/comments/id/
            clean_url = resolved_url.split("?")[0].rstrip("/")
            json_url = clean_url + ".json?limit=1"
            try:
                json_headers = {
                    "User-Agent": "Mozilla/5.0 (compatible; bot/1.0)",
                    "Accept": "application/json"
                }
                async with session.get(json_url, headers=json_headers) as jresp:
                    if jresp.status == 200:
                        try:
                            data = await jresp.json(content_type=None)
                        except Exception:
                            data = None

                        if data and isinstance(data, list) and data:
                            post_data = data[0].get("data", {}).get("children", [])
                            if post_data:
                                post = post_data[0].get("data", {})

                                # Галерея (несколько фото)
                                gallery = post.get("gallery_data") or {}
                                media_metadata = post.get("media_metadata") or {}
                                if gallery and media_metadata:
                                    items = gallery.get("items", [])
                                    success = False
                                    for idx, item in enumerate(items[:10]):
                                        mid = item.get("media_id", "")
                                        meta = media_metadata.get(mid, {})
                                        mimetype = meta.get("m", "")
                                        ext = ".jpg"
                                        if "png" in mimetype: ext = ".png"
                                        elif "gif" in mimetype: ext = ".gif"
                                        # Берём самое высокое разрешение из previews
                                        src = meta.get("s", {})
                                        img_url = src.get("u") or src.get("gif") or ""
                                        img_url = html.unescape(img_url)
                                        if img_url:
                                            ok = await fetch_media(session, img_url, f"{task_dir}/{idx:02d}_reddit{ext}")
                                            if ok:
                                                success = True
                                    if success:
                                        return True

                                # Одиночное фото/превью
                                preview = post.get("preview") or {}
                                preview_images = preview.get("images", [])
                                if preview_images:
                                    src = preview_images[0].get("source", {})
                                    img_url = html.unescape(src.get("url", ""))
                                    if img_url and not is_placeholder(img_url):
                                        orig_img_url = img_url
                                        direct_img_url = img_url.replace("preview.redd.it", "i.redd.it").split("?")[0]
                                        ok = await fetch_media(session, direct_img_url, f"{task_dir}/00_reddit.jpg")
                                        if not ok:
                                            ok = await fetch_media(session, orig_img_url, f"{task_dir}/00_reddit.jpg")
                                        if ok:
                                            return True

                                # Видео (v.redd.it)
                                media = post.get("media") or {}
                                reddit_video = media.get("reddit_video") or {}
                                vid_url = reddit_video.get("fallback_url") or reddit_video.get("hls_url", "")
                                if vid_url:
                                    ok = await fetch_media(session, vid_url, f"{task_dir}/00_reddit.mp4")
                                    if ok:
                                        return True

                                # Прямая ссылка на медиа в посте
                                post_url = post.get("url", "")
                                if post_url and any(post_url.lower().endswith(e) for e in MEDIA_EXTENSIONS):
                                    ext = os.path.splitext(post_url.lower())[1] or ".jpg"
                                    ok = await fetch_media(session, post_url, f"{task_dir}/00_reddit{ext}")
                                    if ok:
                                        return True
            except Exception as e:
                logging.warning(f"Reddit JSON API failed: {e}")

            # --- ПУТЬ 3: Fallback — OpenGraph через TelegramBot UA (только как последний resort) ---
            try:
                bot_headers = {"User-Agent": "TelegramBot (like TwitterBot)"}
                async with session.get(resolved_url, headers=bot_headers, allow_redirects=True) as post_resp:
                    if post_resp.status == 200:
                        post_html = await post_resp.text()
                        og_imgs = re.findall(
                            r'<meta\s+(?:property|name)=["\'](?:og:image|twitter:image)["\']\s+content=["\'](.*?)["\']',
                            post_html, re.IGNORECASE
                        )
                        for raw_og_img in og_imgs:
                            og_img = html.unescape(raw_og_img)
                            if is_placeholder(og_img):
                                continue
                            if not any(og_img.lower().endswith(e) for e in MEDIA_EXTENSIONS):
                                # Убираем параметры из URL (CDN resize), оставляем путь
                                og_img = og_img.split("?")[0]
                            if is_placeholder(og_img):
                                continue
                            ok = await fetch_media(session, og_img, f"{task_dir}/00_reddit_preview.jpg")
                            if ok:
                                return True
            except Exception as e:
                logging.warning(f"Reddit OG fallback failed: {e}")

    except Exception as e:
        logging.error(f"download_reddit_post error: {e}")
    return False



async def download_pinterest_photo(url: str, task_dir: str) -> bool:
    """Извлечение фото максимального качества из Pinterest."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    }
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    originals = re.findall(r'https://i\.pinimg\.com/originals/[^\s"\'<>&]+(?:\.jpg|\.png|\.webp)', html_text)
                    other_imgs = re.findall(r'https://i\.pinimg\.com/[^\s"\'<>&]+(?:\.jpg|\.png|\.webp)', html_text)
                    best_img = None
                    if originals:
                        best_img = originals[0]
                    elif other_imgs:
                        for img in other_imgs:
                            if not any(k in img for k in ["75x75", "140x140", "60x60", "30x30", "board_thumbnail"]):
                                best_img = re.sub(r'/(?:236x|474x|564x|736x|1200x)/', '/originals/', img)
                                break
                    if best_img:
                        best_img = html.unescape(best_img)
                        async with session.get(best_img, headers={"User-Agent": "Mozilla/5.0"}) as img_resp:
                            if img_resp.status == 200:
                                content = await img_resp.read()
                                ext = ".png" if ".png" in best_img.lower() else ".jpg"
                                with open(f"{task_dir}/00_pin{ext}", "wb") as f:
                                    f.write(content)
                                return True
    except Exception as e:
        logging.error(f"Pinterest download error: {e}")
    return False

async def download_twitter_media(url: str, task_dir: str) -> bool:
    """Извлечение фото-альбомов и видео из Twitter/X."""
    m = re.search(r'(?:twitter\.com|x\.com)/([a-zA-Z0-9_]+)/status/(\d+)', url)
    if not m:
        m2 = re.search(r'(?:twitter\.com|x\.com)/i/status/(\d+)', url)
        if m2:
            user = "i"
            tweet_id = m2.group(1)
        else:
            return False
    else:
        user = m.group(1)
        tweet_id = m.group(2)

    api_url = f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(api_url) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                tweet = data.get("tweet", {})
                media = tweet.get("media", {})
                photos = media.get("photos", [])
                videos = media.get("videos", [])

                if photos:
                    for idx, p in enumerate(photos[:25]):
                        p_url = p.get("url")
                        if p_url:
                            async with session.get(p_url) as p_resp:
                                if p_resp.status == 200:
                                    content = await p_resp.read()
                                    with open(f"{task_dir}/{idx:02d}_twitter.jpg", "wb") as f:
                                        f.write(content)
                    return True

                if videos:
                    v_url = videos[0].get("url")
                    if v_url:
                        async with session.get(v_url) as v_resp:
                            if v_resp.status == 200:
                                content = await v_resp.read()
                                with open(f"{task_dir}/video.mp4", "wb") as f:
                                    f.write(content)
                                return True
    except Exception as e:
        logging.error(f"Twitter media download error: {e}")
    return False

async def download_tiktok_api(url: str, task_dir: str) -> bool:
    api_url = f"https://www.tikwm.com/api/?url={url}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if data.get("code") != 0:
                    return False

                payload = data.get("data", {})
                images = payload.get("images", [])
                music = payload.get("music")

                if images:
                    for i, img_url in enumerate(images[:25]):
                        async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=10)) as img_resp:
                            if img_resp.status == 200:
                                content = await img_resp.read()
                                with open(f"{task_dir}/{i:02d}_photo.jpg", "wb") as f:
                                    f.write(content)
                    if music:
                        async with session.get(music, timeout=aiohttp.ClientTimeout(total=10)) as mus_resp:
                            if mus_resp.status == 200:
                                content = await mus_resp.read()
                                with open(f"{task_dir}/audio.mp3", "wb") as f:
                                    f.write(content)
                    return True

                play_url = payload.get("play")
                if play_url:
                    async with session.get(play_url, timeout=aiohttp.ClientTimeout(total=30)) as vid_resp:
                        if vid_resp.status == 200:
                            content = await vid_resp.read()
                            with open(f"{task_dir}/video.mp4", "wb") as f:
                                f.write(content)
                            return True
    except Exception as e:
        logging.error(f"TikWM API error: {e}")
    return False

# --- ОБРАБОТКА ЗАДАЧ И ЗАГРУЗКА ---

async def process_download_job(job: DownloadJob):
    # 1. Проверка доступности пользователя перед началом тяжелой работы
    try:
        await bot.send_chat_action(job.message.chat.id, "typing")
    except (TelegramForbiddenError, TelegramBadRequest):
        logging.info(f"Пользователь {job.user_id} заблокировал бота или удалил чат. Пропускаем задачу.")
        return

    # 2. Проверка свободного места на диске
    disk_check_path = DATA_DIR if os.path.exists(DATA_DIR) else "/"
    _, _, free_bytes = shutil.disk_usage(disk_check_path)
    if free_bytes < MIN_FREE_DISK_BYTES:
        emergency_disk_cleanup()
        _, _, free_after = shutil.disk_usage(disk_check_path)
        if free_after < MIN_FREE_DISK_BYTES:
            try:
                await job.status_msg.edit_text("⚠️ Сервер сейчас перегружен. Подожди 2-3 минуты, пока освободится место на диске.")
            except Exception:
                pass
            await increment_stat("failed")
            return

    task_dir = f"/tmp/{uuid.uuid4().hex}"
    os.makedirs(task_dir, exist_ok=True)
    proc = None

    try:
        try:
            if job.mode == "audio":
                status_text = "🎵 Извлекаю аудиодорожку..."
            elif job.mode == "round":
                status_text = "⭕ Скачиваю и конвертирую в кружочек..."
            else:
                status_text = "⚡ Твоя очередь подошла, скачиваю..."
            await job.status_msg.edit_text(status_text)
        except Exception:
            pass

        url = job.url
        is_tiktok = any(d in url for d in ["tiktok.com", "douyin.com"])
        downloaded = False
        last_ytdlp_error = ""

        # 1. Заглушка для Reddit (HTTP 403 Forbidden со стороны серверов)
        if any(d in url.lower() for d in ["reddit.com", "redd.it"]):
            try:
                await job.status_msg.edit_text("⚠️ Сервис Reddit заблокировал доступ со стороны серверов (HTTP 403 Forbidden). Загрузка с Reddit временно недоступна.")
            except Exception:
                pass
            return

        # 2. Прямой загрузчик медиа (mp4, mp3, jpg, imgur и т.д.)
        if is_direct_media_url(url) and job.mode != "round":
            downloaded = await download_direct_http(url, task_dir, status_msg=job.status_msg)

        # 3. Pinterest (фото высокого разрешения)
        if not downloaded and any(d in url for d in ["pinterest.com", "pin.it"]) and job.mode != "round":
            downloaded = await download_pinterest_photo(url, task_dir)

        # 4. Twitter / X (фото-альбомы и посты)
        if not downloaded and any(d in url for d in ["twitter.com", "x.com"]) and job.mode != "round":
            downloaded = await download_twitter_media(url, task_dir)

        # 5. TikTok фото-карусель
        if not downloaded and is_tiktok and ("/photo/" in url) and job.mode != "round":
            downloaded = await download_tiktok_api(url, task_dir)

        # 6. yt-dlp загрузка с живым интерактивным прогресс-баром
        if not downloaded:
            output_template = f"{task_dir}/%(autonumber)02d_%(id)s.%(ext)s"
            progress_args = [
                "--color", "no_color",
                "--progress-template", "DOWNLOAD_PROGRESS:%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
                "--newline"
            ]

            if job.mode == "audio":
                cmd = [
                    "yt-dlp",
                    *progress_args,
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "0",
                    "--max-filesize", f"{MAX_FILE_SIZE_MB}M",
                    "--match-filter", "duration <= 7200 & !is_live",
                    "--no-playlist",
                    "--no-write-thumbnail",
                    "--no-write-description",
                    "--no-write-info-json",
                    "--no-write-comments",
                    "--socket-timeout", "15",
                    "--postprocessor-args", f"ffmpeg:-threads {FFMPEG_THREADS}"
                ]
            else:
                max_web_size = max(45, int(MAX_FILE_SIZE_MB * 0.95))
                max_web_approx = max(40, int(MAX_FILE_SIZE_MB * 0.90))
                format_rule = (
                    "bestvideo[height>=1000][filesize_approx<46M]+(bestaudio[abr<=128]/bestaudio)/"
                    "best[height>=1000][filesize<47M]/"
                    "bestvideo[height>=700][height<1000][filesize_approx<46M]+(bestaudio[abr<=128]/bestaudio)/"
                    "best[height>=700][height<1000][filesize<47M]/"
                    f"bestvideo[height<=1080][filesize_approx<{max_web_approx}M][filesize<=?{max_web_size}M]+(bestaudio[abr<=128]/bestaudio)/"
                    f"best[height<=1080][filesize<{max_web_size}M]/"
                    f"bestvideo[height<=720][filesize_approx<{max_web_approx}M][filesize<=?{max_web_size}M]+(bestaudio[abr<=128]/bestaudio)/"
                    f"best[height<=720][filesize<{max_web_size}M]/"
                    f"bestvideo[height<=480][filesize_approx<{max_web_approx}M][filesize<=?{max_web_size}M]+bestaudio/"
                    f"best[height<=480][filesize<{max_web_size}M]/"
                    f"best[filesize<{max_web_size}M]/"
                    "best"
                )
                is_youtube = "youtube.com" in url or "youtu.be" in url
                playlist_args = ["--no-playlist"] if is_youtube else ["--yes-playlist", "--playlist-end", "10"]

                cmd = [
                    "yt-dlp",
                    *progress_args,
                    "--format", format_rule,
                    "--merge-output-format", "mp4",
                    "--max-filesize", f"{MAX_FILE_SIZE_MB}M",
                    "--match-filter", "duration <= 7200 & !is_live",
                    *playlist_args,
                    "--no-write-thumbnail",
                    "--no-write-description",
                    "--no-write-info-json",
                    "--no-write-comments",
                    "--socket-timeout", "15",
                    "--postprocessor-args", f"ffmpeg:-threads {FFMPEG_THREADS}"
                ]

            if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
                cmd.extend(["--cookies", COOKIES_PATH])

            cmd.extend(["--output", output_template, "--", url])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid
            )

            last_edit_time = time.time()

            async def read_stdout(stream):
                nonlocal last_edit_time
                while True:
                    line_b = await stream.readline()
                    if not line_b:
                        break
                    line = strip_ansi(line_b.decode("utf-8", errors="ignore")).strip()
                    if line.startswith("DOWNLOAD_PROGRESS:"):
                        data_part = line.split("DOWNLOAD_PROGRESS:", 1)[1]
                        parts = data_part.split("|")
                        if len(parts) >= 3:
                            pct_str = parts[0].replace("%", "").strip()
                            speed_str = parts[1].strip()
                            eta_str = parts[2].strip()
                            try:
                                pct_float = float(pct_str)
                            except ValueError:
                                pct_float = 0.0

                            now = time.time()
                            if now - last_edit_time >= 3.0:
                                last_edit_time = now
                                bar = make_progress_bar(pct_float)
                                info_line = format_speed_eta(speed_str, eta_str)
                                text = f"⚡ <b>Скачивание:</b>\n<code>[{bar}] {pct_float:.1f}%</code>"
                                if info_line:
                                    text += f"\n{info_line}"
                                try:
                                    await job.status_msg.edit_text(text, parse_mode="HTML")
                                except Exception:
                                    pass
                    elif any(k in line for k in ("[Merger]", "[ExtractAudio]", "[Fixup", "[VideoConvertor]", "Deleting original file")):
                        now = time.time()
                        if now - last_edit_time >= 2.0:
                            last_edit_time = now
                            try:
                                await job.status_msg.edit_text("⚙️ <b>Склеиваю и подготавливаю медиа...</b>\n⏳ Ещё несколько секунд", parse_mode="HTML")
                            except Exception:
                                pass

            async def read_stderr(stream):
                chunks = []
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

            stdout_task = asyncio.create_task(read_stdout(proc.stdout))
            stderr_task = asyncio.create_task(read_stderr(proc.stderr))

            try:
                await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task, proc.wait()), timeout=110.0)
                stderr_bytes = stderr_task.result() if stderr_task.done() else b""
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                await proc.wait()
                stderr_bytes = b""
                logging.warning(f"yt-dlp timeout for {url}")

            if proc.returncode != 0:
                last_ytdlp_error = stderr_bytes.decode("utf-8", errors="ignore").strip()
                logging.warning(f"yt-dlp failed (code {proc.returncode}) for {url}:\n{last_ytdlp_error}")

        found_files = sorted(glob.glob(f"{task_dir}/*"))
        valid_files = [f for f in found_files if not f.endswith(('.part', '.ytdl', '.temp')) and os.path.isfile(f)]

        if not valid_files and is_tiktok and job.mode != "round":
            await download_tiktok_api(url, task_dir)
            found_files = sorted(glob.glob(f"{task_dir}/*"))
            valid_files = [f for f in found_files if not f.endswith(('.part', '.ytdl', '.temp')) and os.path.isfile(f)]

        if not valid_files:
            error_message = parse_ytdlp_error(last_ytdlp_error)
            try:
                await job.status_msg.edit_text(f"⚠️ {error_message}")
            except Exception:
                pass
            await increment_stat("failed")
            return

        photos = [f for f in valid_files if os.path.splitext(f)[1].lower() in ['.jpg', '.jpeg', '.png', '.webp', '.gif']]
        videos = [f for f in valid_files if os.path.splitext(f)[1].lower() in ['.mp4', '.mkv', '.mov', '.webm']]
        audios = [f for f in valid_files if os.path.splitext(f)[1].lower() in ['.mp3', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.flac']]

        # Если режим audio и скачалось видео, извлекаем MP3
        if job.mode == "audio" and videos and not audios:
            mp3_path = f"{task_dir}/extracted_audio.mp3"
            ff_cmd = [
                "ffmpeg", "-y", "-i", videos[0],
                "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                "-threads", str(FFMPEG_THREADS), mp3_path
            ]
            ff_proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            try:
                await asyncio.wait_for(ff_proc.wait(), timeout=45.0)
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                    audios = [mp3_path]
                    videos = []
            except asyncio.TimeoutError:
                if ff_proc and ff_proc.returncode is None:
                    try:
                        os.killpg(os.getpgid(ff_proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    try:
                        await ff_proc.wait()
                    except Exception:
                        pass
            except Exception:
                pass

        # Если запрошен режим audio, но в публикации только фото
        if job.mode == "audio" and photos and not videos and not audios:
            try:
                await job.status_msg.edit_text("⚠️ В этой публикации только фото, аудиодорожка отсутствует.")
            except Exception:
                pass
            await increment_stat("failed")
            return

        # Если запрошен режим round, но в публикации нет видео
        if job.mode == "round" and not videos:
            try:
                await job.status_msg.edit_text("⚠️ В этой публикации нет видео для создания кружочка.")
            except Exception:
                pass
            await increment_stat("failed")
            return

        try:
            await job.status_msg.edit_text("📤 Отправляю файл...")
        except Exception:
            pass

        # 1. Режим кружочка
        if job.mode == "round" and videos:
            vid = videos[0]
            round_path = f"{task_dir}/round_out.mp4"
            round_cmd = [
                "ffmpeg", "-y", "-i", vid,
                "-t", "60",
                "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480,setsar=1",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-threads", str(FFMPEG_THREADS),
                round_path
            ]
            r_proc = await asyncio.create_subprocess_exec(
                *round_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            try:
                await asyncio.wait_for(r_proc.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                if r_proc and r_proc.returncode is None:
                    try:
                        os.killpg(os.getpgid(r_proc.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    try:
                        await r_proc.wait()
                    except Exception:
                        pass

            if os.path.exists(round_path) and os.path.getsize(round_path) > 0:
                await bot.send_chat_action(job.message.chat.id, "record_video_note")
                await job.message.answer_video_note(video_note=FSInputFile(round_path))
                await increment_stat("success")
                try:
                    await job.status_msg.delete()
                except Exception:
                    pass
                return
            else:
                await job.message.answer("❌ Не удалось сконвертировать видео в кружочек.")
                await increment_stat("failed")
                return

        # 2. Обычные видео
        if videos:
            for vid in videos:
                file_size = os.path.getsize(vid)
                file_size_mb = round(file_size / (1024 * 1024), 1)

                # Удаляем старый веб-файл этого пользователя, если есть (лимит: 1 на юзера)
                for tok, item in list(DOWNLOAD_LINKS.items()):
                    if item.get("user_id") == job.user_id:
                        old_f = DOWNLOAD_LINKS.pop(tok, None)
                        if old_f and os.path.exists(old_f["path"]):
                            try:
                                os.remove(old_f["path"])
                            except OSError:
                                pass

                # Файл отправляется в Telegram (<49 МБ)
                if file_size <= MAX_TG_FILE_SIZE_BYTES:
                    base_name = os.path.basename(vid)
                    cache_id = uuid.uuid4().hex[:12]
                    cached_file = f"{CACHE_DIR}/{cache_id}.mp4"
                    shutil.copy2(vid, cached_file)
                    CONVERT_CACHE[cache_id] = {
                        "path": cached_file,
                        "filename": base_name,
                        "expire_at": time.time() + CACHE_TTL_SECONDS,
                        "user_id": job.user_id
                    }

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🎵 Извлечь MP3", callback_data=f"ext_audio:{cache_id}"),
                            InlineKeyboardButton(text="⭕ В кружочек", callback_data=f"ext_round:{cache_id}")
                        ],
                        [
                            InlineKeyboardButton(text="📁 Файлом (без сжатия)", callback_data=f"ext_doc:{cache_id}"),
                            InlineKeyboardButton(text="🖼 Обложка", callback_data=f"ext_thumb:{cache_id}")
                        ]
                    ])

                    await bot.send_chat_action(job.message.chat.id, "upload_video")
                    video_file = FSInputFile(vid)
                    video_msg = await job.message.answer_video(
                        video=video_file,
                        caption="🎬 Готово.",
                        supports_streaming=True,
                        reply_markup=kb
                    )
                    if video_msg and video_msg.video:
                        await save_media_cache(job.url, video_msg.video.file_id, "video", base_name)
                    await increment_stat("success")

                # Файл больше 49 МБ (до 200 МБ) — отдаем через веб-сервер
                elif file_size <= MAX_WEB_FILE_SIZE_BYTES:
                    token = uuid.uuid4().hex[:16]
                    base_name = os.path.basename(vid)
                    dest_file = f"{WEB_DOWNLOADS_DIR}/{token}_{base_name}"
                    shutil.copy2(vid, dest_file)

                    DOWNLOAD_LINKS[token] = {
                        "path": dest_file,
                        "filename": base_name,
                        "expire_at": time.time() + WEB_TTL_SECONDS,
                        "size_mb": file_size_mb,
                        "user_id": job.user_id
                    }

                    cache_id = uuid.uuid4().hex[:12]
                    CONVERT_CACHE[cache_id] = {
                        "path": dest_file,
                        "filename": base_name,
                        "expire_at": time.time() + CACHE_TTL_SECONDS,
                        "user_id": job.user_id
                    }

                    clean_url_filename = urllib.parse.quote(base_name)
                    download_url = f"{WEB_BASE_URL}/dl/{token}/{clean_url_filename}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"📥 Скачать файл ({file_size_mb} МБ)", url=download_url)],
                        [InlineKeyboardButton(text="🎵 Извлечь MP3 (в чат)", callback_data=f"ext_audio:{cache_id}")]
                    ])

                    msg_text = (
                        f"📹 <b>Видео готово!</b>\n\n"
                        f"Размер: <b>{file_size_mb} МБ</b> (лимит отправки в Telegram — 50 МБ).\n"
                        f"🔗 <a href='{download_url}'>Скачать напрямую с сервера</a>\n\n"
                        f"⏳ <i>Ссылка действует 7 минут, затем файл автоматически удалится.</i>"
                    )
                    await job.message.answer(msg_text, parse_mode="HTML", reply_markup=kb)
                    await increment_stat("success")

                else:
                    await job.message.answer(
                        f"⚠️ Видео ({file_size_mb} МБ) превышает лимит сервера (200 МБ).\n"
                        f"Для таких тяжелых видео можно скачать только аудиодорожку:\n<code>/audio {url}</code>",
                        parse_mode="HTML"
                    )
                    await increment_stat("failed")

        elif audios:
            for aud in audios:
                base_name = os.path.basename(aud)
                file_size = os.path.getsize(aud)
                file_size_mb = round(file_size / (1024 * 1024), 1)

                if file_size <= MAX_TG_FILE_SIZE_BYTES:
                    await bot.send_chat_action(job.message.chat.id, "upload_voice")
                    audio_file = FSInputFile(aud)
                    audio_msg = await job.message.answer_audio(audio=audio_file, caption="🎵 Аудиодорожка")
                    if audio_msg and audio_msg.audio:
                        await save_media_cache(job.url, audio_msg.audio.file_id, "audio", base_name)
                    await increment_stat("success")
                elif file_size <= MAX_WEB_FILE_SIZE_BYTES:
                    token = uuid.uuid4().hex[:16]
                    dest_file = f"{WEB_DOWNLOADS_DIR}/{token}_{base_name}"
                    shutil.copy2(aud, dest_file)

                    DOWNLOAD_LINKS[token] = {
                        "path": dest_file,
                        "filename": base_name,
                        "expire_at": time.time() + WEB_TTL_SECONDS,
                        "size_mb": file_size_mb,
                        "user_id": job.user_id
                    }
                    clean_url_filename = urllib.parse.quote(base_name)
                    download_url = f"{WEB_BASE_URL}/dl/{token}/{clean_url_filename}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"📥 Скачать MP3 ({file_size_mb} МБ)", url=download_url)]
                    ])
                    await job.message.answer(
                        f"🎵 <b>Аудио готово!</b>\n\nРазмер: <b>{file_size_mb} МБ</b> (больше 50 МБ).\n"
                        f"🔗 <a href='{download_url}'>Скачать напрямую с сервера</a>\n\n"
                        f"⏳ <i>Ссылка действует 7 минут.</i>",
                        parse_mode="HTML",
                        reply_markup=kb
                    )
                    await increment_stat("success")
                else:
                    await job.message.answer(f"⚠️ Аудиофайл ({file_size_mb} МБ) превышает лимит сервера 200 МБ.")
                    await increment_stat("failed")

        elif photos:
            if len(photos) == 1:
                photo_file = FSInputFile(photos[0])
                base_name = os.path.basename(photos[0])
                cache_id = uuid.uuid4().hex[:12]
                cached_file = f"{CACHE_DIR}/{cache_id}_{base_name}"
                shutil.copy2(photos[0], cached_file)
                CONVERT_CACHE[cache_id] = {
                    "path": cached_file,
                    "filename": base_name,
                    "expire_at": time.time() + CACHE_TTL_SECONDS,
                    "user_id": job.user_id
                }
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📁 Файлом (без сжатия)", callback_data=f"ext_doc:{cache_id}")]
                ])
                photo_msg = await job.message.answer_photo(photo=photo_file, caption="🖼 Готово.", reply_markup=kb)
                if photo_msg and photo_msg.photo:
                    await save_media_cache(job.url, photo_msg.photo[-1].file_id, "photo", base_name)
            else:
                all_file_ids = []
                for i in range(0, len(photos), 10):
                    chunk = photos[i:i + 10]
                    if len(chunk) == 1:
                        p_msg = await job.message.answer_photo(photo=FSInputFile(chunk[0]))
                        if p_msg and p_msg.photo:
                            all_file_ids.append(p_msg.photo[-1].file_id)
                    else:
                        media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
                        if i == 0:
                            media[0].caption = f"🖼 Фото-карусель ({len(photos)} фото) готова."
                        sent_msgs = await job.message.answer_media_group(media=media)
                        if sent_msgs:
                            for sm in sent_msgs:
                                if sm.photo:
                                    all_file_ids.append(sm.photo[-1].file_id)

                if all_file_ids:
                    await save_media_cache(job.url, json.dumps(all_file_ids), "photos", f"Карусель ({len(all_file_ids)} фото)")

            for aud in audios:
                try:
                    await job.message.answer_audio(audio=FSInputFile(aud), caption="🎵 Звук из публикации")
                except Exception:
                    pass

            await increment_stat("success")

        try:
            await job.status_msg.delete()
        except Exception:
            pass

    finally:
        if proc and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        shutil.rmtree(task_dir, ignore_errors=True)

# --- ПАРАЛЛЕЛЬНЫЕ ВОРКЕРЫ И ДИНАМИЧЕСКАЯ ОЧЕРЕДЬ ---

async def notify_queue_positions():
    """Динамическое обновление позиции для ожидающих в очереди пользователей."""
    for idx, pending_job in enumerate(list(QUEUE_JOBS)[:5]):
        if getattr(pending_job, "is_active", False) or pending_job not in QUEUE_JOBS:
            continue
        pos = idx + 1
        est_time_str = format_eta_seconds(((pos - 1) // NUM_WORKERS + 1) * 20)
        new_text = f"⏳ Твоя очередь приближается! Позиция: #{pos}. Ожидание: {est_time_str}."
        if pending_job.last_status_text != new_text:
            try:
                await pending_job.status_msg.edit_text(new_text)
                pending_job.last_status_text = new_text
            except Exception:
                pass

async def download_worker(worker_id: int):
    """Один из двух параллельных воркеров обработки очереди."""
    while True:
        job: DownloadJob = await DOWNLOAD_QUEUE.get()
        job.is_active = True
        BUSY_WORKERS.add(worker_id)
        if job in QUEUE_JOBS:
            QUEUE_JOBS.remove(job)

        asyncio.create_task(notify_queue_positions())

        try:
            await asyncio.wait_for(process_download_job(job), timeout=120.0)
        except asyncio.TimeoutError:
            logging.error(f"Job timeout for url: {job.url}")
            await increment_stat("failed")
            try:
                await job.status_msg.edit_text("⚠️ Таймаут: сервер источника отдает поток слишком медленно (лимит 2 мин).")
            except Exception:
                pass
        except Exception as e:
            logging.error(f"Worker task error: {e}", exc_info=True)
            await increment_stat("failed")
            try:
                await job.status_msg.edit_text("❌ Произошла ошибка при обработке файла.")
            except Exception:
                pass
        finally:
            BUSY_WORKERS.discard(worker_id)
            USER_ACTIVE_COUNT[job.user_id] = max(0, USER_ACTIVE_COUNT.get(job.user_id, 1) - 1)
            USER_ACTIVE_TIMESTAMP.pop(job.user_id, None)
            DOWNLOAD_QUEUE.task_done()
            asyncio.create_task(notify_queue_positions())

# --- КНОПКИ ПОД ВИДЕО (MP3, КРУЖОЧЕК, ОБЛОЖКА) С ЗАЩИТОЙ ОТ DOS ---

@dp.callback_query(F.data.startswith("ext_audio:"))
async def extract_audio_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in ACTIVE_CONVERSIONS:
        await callback.answer("⏳ Извлечение аудио уже выполняется, подожди пару секунд!", show_alert=True)
        return

    cache_id = callback.data.split(":", 1)[1]
    info = CONVERT_CACHE.get(cache_id)
    if not info or not os.path.exists(info["path"]):
        await callback.answer("⏳ Срок действия кэша истек. Отправь ссылку с командой /audio", show_alert=True)
        return

    ACTIVE_CONVERSIONS.add(user_id)
    out_path = f"/tmp/{uuid.uuid4().hex}.mp3"
    proc = None
    try:
        await callback.answer("🎵 Извлекаю MP3...")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", info["path"],
            "-vn", "-c:a", "libmp3lame", "-q:a", "2", "-threads", str(FFMPEG_THREADS),
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        await asyncio.wait_for(proc.wait(), timeout=60.0)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            await bot.send_chat_action(callback.message.chat.id, "upload_voice")
            audio_file = FSInputFile(out_path)
            await callback.message.reply_audio(audio=audio_file, caption="🎵 Аудиодорожка")
        else:
            await callback.message.reply("❌ Не удалось извлечь аудиодорожку.")
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        await callback.message.reply("⚠️ Время извлечения аудио превысило 60 секунд.")
    except Exception as e:
        logging.error(f"Error in extract_audio_callback: {e}")
        await callback.message.reply("❌ Произошла ошибка при извлечении аудио.")
    finally:
        ACTIVE_CONVERSIONS.discard(user_id)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

@dp.callback_query(F.data.startswith("ext_round:"))
async def extract_round_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in ACTIVE_CONVERSIONS:
        await callback.answer("⏳ Создание кружочка уже выполняется, подожди немного!", show_alert=True)
        return

    cache_id = callback.data.split(":", 1)[1]
    info = CONVERT_CACHE.get(cache_id)
    if not info or not os.path.exists(info["path"]):
        await callback.answer("⏳ Срок действия кэша истек. Отправь ссылку с командой /round", show_alert=True)
        return

    ACTIVE_CONVERSIONS.add(user_id)
    out_path = f"/tmp/{uuid.uuid4().hex}_round.mp4"
    proc = None
    try:
        await callback.answer("⭕ Создаю кружочек...")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", info["path"],
            "-t", "60",
            "-vf", "crop=min(iw\\,ih):min(iw\\,ih),scale=480:480,setsar=1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-threads", str(FFMPEG_THREADS),
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        await asyncio.wait_for(proc.wait(), timeout=60.0)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            await bot.send_chat_action(callback.message.chat.id, "record_video_note")
            round_file = FSInputFile(out_path)
            await callback.message.reply_video_note(video_note=round_file)
        else:
            await callback.message.reply("❌ Не удалось создать кружочек.")
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        await callback.message.reply("⚠️ Конвертация в кружочек превысила 60 секунд.")
    except Exception as e:
        logging.error(f"Error in extract_round_callback: {e}")
        await callback.message.reply("❌ Произошла ошибка при создании кружочка.")
    finally:
        ACTIVE_CONVERSIONS.discard(user_id)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

@dp.callback_query(F.data.startswith("ext_thumb:"))
async def extract_thumb_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in ACTIVE_CONVERSIONS:
        await callback.answer("⏳ Извлечение обложки уже выполняется!", show_alert=True)
        return

    cache_id = callback.data.split(":", 1)[1]
    info = CONVERT_CACHE.get(cache_id)
    if not info or not os.path.exists(info["path"]):
        await callback.answer("⏳ Срок действия кэша истек.", show_alert=True)
        return

    ACTIVE_CONVERSIONS.add(user_id)
    out_path = f"/tmp/{uuid.uuid4().hex}_thumb.jpg"
    proc = None
    try:
        await callback.answer("🖼 Извлекаю обложку...")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", "00:00:01", "-i", info["path"],
            "-vframes", "1", "-q:v", "2",
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        await asyncio.wait_for(proc.wait(), timeout=20.0)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            thumb_file = FSInputFile(out_path)
            await callback.message.reply_photo(photo=thumb_file, caption="🖼 Кадр / Обложка видео")
        else:
            await callback.message.reply("❌ Не удалось извлечь обложку.")
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
        await callback.message.reply("⚠️ Время извлечения обложки превысило лимит.")
    except Exception as e:
        logging.error(f"Error in extract_thumb_callback: {e}")
        await callback.message.reply("❌ Произошла ошибка при получении обложки.")
    finally:
        ACTIVE_CONVERSIONS.discard(user_id)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass

@dp.callback_query(F.data.startswith("ext_doc:"))
async def extract_doc_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in ACTIVE_CONVERSIONS:
        await callback.answer("⏳ Запрос уже выполняется, подожди секунду!", show_alert=True)
        return

    cache_id = callback.data.split(":", 1)[1]
    info = CONVERT_CACHE.get(cache_id)
    if not info or not os.path.exists(info["path"]):
        await callback.answer("⏳ Срок действия кэша истек.", show_alert=True)
        return

    file_size = os.path.getsize(info["path"])
    if file_size > MAX_TG_FILE_SIZE_BYTES:
        await callback.answer("⚠️ Размер файла превышает лимит отправки в Telegram (50 МБ).", show_alert=True)
        return

    ACTIVE_CONVERSIONS.add(user_id)
    try:
        await callback.answer("📁 Отправляю файл как документ без сжатия...")
        await bot.send_chat_action(callback.message.chat.id, "upload_document")
        doc_filename = info.get("filename") or f"file_{cache_id[:6]}"
        doc_file = FSInputFile(info["path"], filename=doc_filename)
        await callback.message.reply_document(
            document=doc_file,
            caption="📁 Файл в исходном качестве (без сжатия Telegram)"
        )
    except Exception as e:
        logging.error(f"Error in extract_doc_callback: {e}")
        await callback.message.reply("❌ Не удалось отправить документ.")
    finally:
        ACTIVE_CONVERSIONS.discard(user_id)

# --- ОЧЕРЕДЬ И ВАЛИДАЦИЯ ---

async def queue_download(message: Message, url: str, mode: str = "auto"):
    user_id = message.from_user.id
    clean_cooldowns()
    now = time.time()

    # 1. Защита от спама: не более 1 активной задачи на пользователя
    if USER_ACTIVE_COUNT.get(user_id, 0) >= 1:
        await message.answer("⚠️ У тебя уже обрабатывается или ожидает в очереди один запрос. Дождись его отправки!")
        return

    if user_id in USER_COOLDOWNS and now - USER_COOLDOWNS[user_id] < 7:
        await message.answer("⏱ Подожди 7 секунд между запросами.")
        return

    warning = check_unsupported_url(url)
    if warning:
        await message.answer(f"⚠️ {warning}")
        return

    current_queue_len = len(QUEUE_JOBS)
    if current_queue_len >= 30:
        await message.answer("⚠️ Очередь переполнена. Попробуй через пару минут.")
        return

    USER_COOLDOWNS[user_id] = now
    USER_ACTIVE_COUNT[user_id] = USER_ACTIVE_COUNT.get(user_id, 0) + 1
    USER_ACTIVE_TIMESTAMP[user_id] = now

    active_workers_count = len(BUSY_WORKERS)

    try:
        if active_workers_count < NUM_WORKERS and current_queue_len == 0:
            if mode == "audio":
                status_text = "🔍 Анализирую аудиопоток..."
            elif mode == "round":
                status_text = "⭕ Готовлю создание кружочка..."
            else:
                status_text = "🔍 Анализирую ссылку..."
            status_msg = await message.answer(status_text)
        else:
            pos = current_queue_len + 1
            est_time_str = format_eta_seconds(((pos - 1) // NUM_WORKERS + 1) * 20)
            status_text = f"⏳ Ты в очереди: позиция #{pos}. Ожидание: {est_time_str}."
            status_msg = await message.answer(status_text)

        job = DownloadJob(message=message, status_msg=status_msg, url=url, user_id=user_id, mode=mode, last_status_text=status_text)
        QUEUE_JOBS.append(job)
        await DOWNLOAD_QUEUE.put(job)
    except Exception as e:
        USER_ACTIVE_COUNT[user_id] = max(0, USER_ACTIVE_COUNT.get(user_id, 1) - 1)
        USER_ACTIVE_TIMESTAMP.pop(user_id, None)
        if 'job' in locals() and job in QUEUE_JOBS:
            QUEUE_JOBS.remove(job)
        logging.error(f"Error queueing job: {e}")
        try:
            await message.answer("❌ Ошибка при постановке в очередь.")
        except Exception:
            pass

# --- ХЭНДЛЕРЫ КОМАНД ---

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await add_user(message.from_user.id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📖 Инструкция и сервисы", callback_data="open_help"),
            InlineKeyboardButton(text="💬 Поддержка", callback_data="open_support")
        ]
    ])

    text = (
        "👋 <b>Привет! Я бот для быстрой загрузки медиа.</b>\n\n"
        "📥 <b>Как пользоваться:</b>\n"
        "Просто отправь мне ссылку на видео, аудио или прямое фото!\n\n"
        "⚡ <b>Быстрые команды:</b>\n"
        "• <code>/audio ссылка</code> — скачать только MP3 аудио\n"
        "• <code>/round ссылка</code> — видеосообщение в виде кружочка\n"
        "• <code>/help</code> — список поддерживаемых площадок\n\n"
        "📊 Видео до 50 МБ отправляются в чат, от 50 до 200 МБ — по защищённой ссылке HTTPS."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("help"))
async def help_command(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="open_support")]
    ])
    text = (
        "📖 <b>Справочник возможностей бота:</b>\n\n"
        "🌐 <b>Поддерживаемые площадки и форматы:</b>\n"
        "• <b>Прямые ссылки на фото и файлы</b> — любые фото (JPG, PNG, WEBP, GIF), аудио и видео по прямым ссылкам с любых сайтов и хостингов (Imgur, Catbox, Telegra.ph и др.)\n"
        "• <b>YouTube & Shorts</b> — видео в Full HD и музыка\n"
        "• <b>TikTok & Douyin</b> — видео без водяных знаков и фото-карусели со звуком\n"
        "• <b>Instagram</b> — Reels, видео и фото\n"
        "• <b>VK Видео & VK Клипы</b> — ролики и трансляции\n"
        "• <b>Rutube & Дзен (Dzen)</b> — видеоролики и клипы\n"
        "• <b>Twitter / X</b> — видео и фото-альбомы\n"
        "• <b>Twitch</b> — клипы стримеров (Clips)\n"
        "• <b>Pinterest</b> — видео и оригинальные пины в высоком качестве\n"
        "• <b>SoundCloud, Bandcamp & Mixcloud</b> — треки и музыка\n"
        "• <b>Likee & Coub</b> — короткие видео и зацикленные ролики\n"
        "• <b>Vimeo, Bilibili, Facebook & Threads</b> — видео в высоком качестве\n\n"
        "💡 <b>Полезные фичи:</b>\n"
        "• Для фото и видео доступна кнопка: <b>«📁 Файлом (без сжатия)»</b> — отправка оригинального файла как документ без потери качества Telegram.\n"
        "• Под видео доступны кнопки: <b>«🎵 Извлечь MP3»</b>, <b>«⭕ В кружочек»</b> и <b>«🖼 Обложка»</b>.\n"
        "• Тяжелые файлы (до 200 МБ) отдаются по ссылке с нашего защищенного домена HTTPS."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data == "open_help")
async def open_help_callback(callback: CallbackQuery):
    await help_command(callback.message)
    await callback.answer()

@dp.message(Command("audio", "mp3"))
async def audio_cmd(message: Message):
    user_id = message.from_user.id
    await add_user(user_id)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пришли ссылку вместе с командой:\n<code>/audio https://...</code>", parse_mode="HTML")
        return
    match = re.search(r'(https?://[^\s]+)', parts[1])
    if not match:
        await message.answer("Не удалось найти ссылку.")
        return
    await queue_download(message, match.group(0), mode="audio")

@dp.message(Command("round", "circle"))
async def round_cmd(message: Message):
    user_id = message.from_user.id
    await add_user(user_id)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пришли ссылку вместе с командой:\n<code>/round https://...</code>", parse_mode="HTML")
        return
    match = re.search(r'(https?://[^\s]+)', parts[1])
    if not match:
        await message.answer("Не удалось найти ссылку.")
        return
    await queue_download(message, match.group(0), mode="round")

# --- ПОЛНАЯ АДМИН-ПАНЕЛЬ С МОНИТОРИНГОМ ---

def format_admin_text(stats: dict, users_count: int, m: dict) -> str:
    disk_bar = make_progress_bar(m["disk_percent"])
    mem_bar = make_progress_bar(m["mem_percent"])

    cookies_exist = os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0
    cookies_status = "✅ Подключены" if cookies_exist else "❌ Отсутствуют"

    success_count = stats.get("success", 0)
    failed_count = stats.get("failed", 0)
    queue_len = len(QUEUE_JOBS)
    active_w = len(BUSY_WORKERS)
    worker_status = f"🔴 {active_w}/{NUM_WORKERS} в работе" if active_w > 0 else "🟢 Все свободны"

    return (
        f"📊 <b>Мониторинг сервера и бота</b>\n\n"
        f"🖥 <b>Системные ресурсы:</b>\n"
        f"• <b>CPU Load:</b> <code>{m['load_1']:.2f}, {m['load_5']:.2f}, {m['load_15']:.2f}</code>\n"
        f"• <b>RAM:</b> <code>{m['mem_used_mb']} / {m['mem_total_mb']} МБ</code> ({m['mem_percent']}%)\n"
        f"  <code>[{mem_bar}]</code>\n"
        f"• <b>Диск:</b> <code>{m['disk_used_gb']} / {m['disk_total_gb']} ГБ</code> (Свободно: <b>{m['disk_free_gb']} ГБ</b>)\n"
        f"  <code>[{disk_bar}]</code>\n"
        f"• <b>Аптайм сервера:</b> {m['server_uptime']}\n"
        f"• <b>Аптайм бота:</b> {m['bot_uptime']}\n\n"
        f"🤖 <b>Статистика сервиса:</b>\n"
        f"• <b>Пользователей:</b> <code>{users_count}</code>\n"
        f"• <b>Успешно:</b> <code>{success_count}</code> | <b>Ошибок:</b> <code>{failed_count}</code>\n"
        f"• <b>Воркеры:</b> {worker_status} | <b>Очередь:</b> <code>{queue_len}</code>\n"
        f"• <b>Веб-раздача:</b> <code>{m['active_links']}</code> файлов ({m['web_size_mb']} МБ)\n"
        f"• <b>Быстрый кэш:</b> <code>{m['cached_files']}</code> файлов ({m['cache_size_mb']} МБ)\n"
        f"• <b>Cookies:</b> {cookies_status}"
    )

def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить статус", callback_data="admin_refresh"),
            InlineKeyboardButton(text="🧹 Очистить кэш", callback_data="admin_clean_cache")
        ],
        [
            InlineKeyboardButton(text="💾 Скачать бэкап базы", callback_data="admin_backup_db"),
            InlineKeyboardButton(text="🔄 Обновить yt-dlp", callback_data="update_engine")
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="start_broadcast")
        ]
    ])

@dp.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    count = await get_users_count()
    stats = await get_stats()
    m = get_system_metrics()
    await message.answer(format_admin_text(stats, count, m), parse_mode="HTML", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin_refresh")
async def admin_refresh_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    count = await get_users_count()
    stats = await get_stats()
    m = get_system_metrics()
    try:
        await callback.message.edit_text(format_admin_text(stats, count, m), parse_mode="HTML", reply_markup=get_admin_keyboard())
    except Exception:
        pass
    await callback.answer("Данные обновлены!")

@dp.callback_query(F.data == "admin_clean_cache")
async def admin_clean_cache_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    emergency_disk_cleanup()
    await callback.answer("✅ Весь кэш и временные файлы удалены!", show_alert=True)
    count = await get_users_count()
    stats = await get_stats()
    m = get_system_metrics()
    try:
        await callback.message.edit_text(format_admin_text(stats, count, m), parse_mode="HTML", reply_markup=get_admin_keyboard())
    except Exception:
        pass

@dp.callback_query(F.data == "admin_backup_db")
async def admin_backup_db_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    if not os.path.exists(DB_PATH):
        await callback.answer("База данных не найдена!", show_alert=True)
        return
    await callback.answer("Отправляю бэкап...")
    db_file = FSInputFile(DB_PATH, filename=f"bot_backup_{time.strftime('%Y%m%d_%H%M%S')}.db")
    await callback.message.reply_document(
        document=db_file,
        caption=f"💾 <b>Резервная копия bot.db</b>\nСоздана: <code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code>",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "update_engine")
async def update_engine_handler(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    status_msg = await callback.message.answer("Обновляю yt-dlp...")
    await callback.answer()

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    if proc.returncode == 0:
        await status_msg.edit_text("✅ Парсер обновлен. Перезапускаю контейнер...")
        await asyncio.sleep(1)
        os._exit(0)
    else:
        await status_msg.edit_text("❌ Ошибка при обновлении pip пакета.")

# --- РАССЫЛКА И ТИКЕТЫ ---

@dp.callback_query(F.data == "start_broadcast")
async def broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]
    ])
    await callback.message.answer("Отправь сообщение для рассылки (текст, фото или видео):", reply_markup=cancel_kb)
    await state.set_state(Broadcast.waiting_for_content)
    await callback.answer()

@dp.message(StateFilter(Broadcast.waiting_for_content), F.from_user.id == ADMIN_ID)
async def broadcast_preview(message: Message, state: FSMContext):
    await state.update_data(broadcast_msg_id=message.message_id)
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать рассылку", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_broadcast")]
    ])
    await message.reply("Сообщение сохранено. Запустить отправку всем пользователям?", reply_markup=confirm_kb)
    await state.set_state(Broadcast.waiting_for_confirm)

@dp.callback_query(F.data == "confirm_broadcast", StateFilter(Broadcast.waiting_for_confirm))
async def run_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    msg_id = data.get("broadcast_msg_id")
    await state.clear()

    users = await get_all_users()
    total = len(users)

    status_msg = await callback.message.answer(f"Рассылка запущена. Получателей: {total}")
    await callback.answer()

    successful = 0
    blocked = 0
    errors = 0

    for user_id in users:
        retries = 3
        while retries > 0:
            try:
                await bot.copy_message(chat_id=user_id, from_chat_id=ADMIN_ID, message_id=msg_id)
                successful += 1
                await asyncio.sleep(0.05)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                retries -= 1
            except TelegramForbiddenError:
                blocked += 1
                try:
                    async with get_db() as db:
                        await db.execute("DELETE FROM users WHERE user_id = ?;", (user_id,))
                        await db.commit()
                except Exception:
                    pass
                break
            except TelegramBadRequest:
                errors += 1
                break
            except Exception:
                errors += 1
                break

    report = (
        f"📊 <b>Отчет по рассылке</b>\n\n"
        f"• Всего в базе: {total}\n"
        f"• Доставлено: {successful}\n"
        f"• Заблокировали бота: {blocked}\n"
        f"• Ошибки: {errors}"
    )
    await status_msg.edit_text(report, parse_mode="HTML")

@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()

@dp.callback_query(F.data == "open_support")
async def support_callback(callback: CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_support")]
    ])
    await callback.message.answer("Опиши проблему или задай вопрос. Ответ администратора придет прямо сюда.", reply_markup=cancel_kb)
    await state.set_state(Support.waiting_for_message)
    await callback.answer()

@dp.callback_query(F.data == "cancel_support")
async def cancel_support(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Обращение отменено.")
    await callback.answer()

@dp.message(StateFilter(Support.waiting_for_message))
async def receive_ticket(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await add_user(user_id)
    username = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"

    header = await bot.send_message(ADMIN_ID, f"📩 <b>Тикет от</b> <code>{user_id}</code> ({username}):", parse_mode="HTML")
    copied = await message.copy_to(ADMIN_ID)

    await save_ticket_msg(header.message_id, user_id)
    await save_ticket_msg(copied.message_id, user_id)

    await message.answer("✅ Сообщение передано администратору. Жди ответа.")
    await state.clear()

@dp.message(F.reply_to_message & (F.from_user.id == ADMIN_ID))
async def admin_reply(message: Message):
    reply_msg_id = message.reply_to_message.message_id
    user_id = await get_user_by_ticket_msg(reply_msg_id)

    if not user_id:
        return

    try:
        await message.copy_to(user_id)
        await save_ticket_msg(message.message_id, user_id)

        follow_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ответить администратору", callback_data="open_support")]
        ])
        await bot.send_message(user_id, "💡 Нажми кнопку ниже, чтобы ответить.", reply_markup=follow_kb)
        await message.answer("✅ Ответ доставлен пользователю.")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")

# --- ХЭНДЛЕРЫ ТЕКСТА И ССЫЛОК ---

def extract_clean_url(text: str) -> str | None:
    """Извлекает чистый URL из текста, убирая @mention бота и мусор в конце ссылки."""
    # Ищем первый http(s):// в тексте
    match = re.search(r'(https?://\S+)', text)
    if not match:
        return None
    url = match.group(0)
    # Убираем trailing мусор: ,)"]>. — символы, не являющиеся частью URL
    url = re.sub(r'[,)\]">\.]+$', '', url)
    return url


INLINE_ACTIVE_TASKS: dict[str, asyncio.Task] = {}

async def download_for_inline(url: str) -> dict | None:
    """Быстрая фоновая загрузка медиа для inline-отправки (макс 720p, лимит 48 МБ)."""
    task_dir = f"/tmp/{uuid.uuid4().hex}"
    os.makedirs(task_dir, exist_ok=True)
    try:
        if any(d in url.lower() for d in ["reddit.com", "redd.it"]):
            return None
        downloaded = False
        # 1. Direct media
        if is_direct_media_url(url):
            downloaded = await download_direct_http(url, task_dir)
        # 2. Pinterest
        if not downloaded and any(d in url for d in ["pinterest.com", "pin.it"]):
            downloaded = await download_pinterest_photo(url, task_dir)
        # 4. Twitter
        if not downloaded and any(d in url for d in ["twitter.com", "x.com"]):
            downloaded = await download_twitter_media(url, task_dir)
        # 5. TikTok
        if not downloaded and any(d in url for d in ["tiktok.com", "douyin.com"]):
            downloaded = await download_tiktok_api(url, task_dir)
        # 6. yt-dlp (быстрый пресет 720p/480p)
        if not downloaded:
            output_template = f"{task_dir}/%(autonumber)02d_%(id)s.%(ext)s"
            format_rule = "bestvideo[height<=720][filesize<45M]+bestaudio/best[height<=720][filesize<45M]/best[filesize<45M]/best"
            is_youtube = "youtube.com" in url or "youtu.be" in url
            playlist_args = ["--no-playlist"] if is_youtube else ["--yes-playlist", "--playlist-end", "20"]
            cmd = [
                "yt-dlp",
                *playlist_args,
                "--format", format_rule,
                "--output", output_template,
                "--max-filesize", "48M",
                "--no-write-thumbnail",
                "--no-write-description",
                "--no-write-info-json",
                "--socket-timeout", "10",
                "--postprocessor-args", f"ffmpeg:-threads {FFMPEG_THREADS}",
                url
            ]
            if os.path.exists(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
                cmd.extend(["--cookies", COOKIES_PATH])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    await proc.wait()
                except Exception:
                    pass

        found = sorted(glob.glob(f"{task_dir}/*"))
        valid = [f for f in found if not f.endswith(('.part', '.ytdl', '.temp')) and os.path.isfile(f)]
        if not valid:
            return None

        videos = [f for f in valid if os.path.splitext(f)[1].lower() in ['.mp4', '.mkv', '.mov', '.webm']]
        photos = [f for f in valid if os.path.splitext(f)[1].lower() in ['.jpg', '.jpeg', '.png', '.webp', '.gif']]
        audios = [f for f in valid if os.path.splitext(f)[1].lower() in ['.mp3', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.flac']]

        token = uuid.uuid4().hex[:16]
        now = time.time()

        # 1. Если есть фото (одиночное фото или карусель)
        if photos and not videos:
            photo_items = []
            total_p = min(len(photos), 25)
            for idx, p in enumerate(photos[:25]):
                item_token = f"{token}_{idx:02d}"
                ext = os.path.splitext(p)[1].lower()
                dest_file = f"{WEB_DOWNLOADS_DIR}/{item_token}.jpg"

                if ext in ['.jpg', '.jpeg']:
                    shutil.copy2(p, dest_file)
                else:
                    try:
                        c_proc = await asyncio.create_subprocess_exec(
                            "ffmpeg", "-y", "-i", p, "-q:v", "2", dest_file,
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                            preexec_fn=os.setsid
                        )
                        await asyncio.wait_for(c_proc.wait(), timeout=5.0)
                    except Exception:
                        pass
                    if not os.path.exists(dest_file):
                        shutil.copy2(p, dest_file)

                if os.path.exists(dest_file):
                    fsize = os.path.getsize(dest_file)
                    DOWNLOAD_LINKS[item_token] = {
                        "path": dest_file,
                        "filename": f"photo_{idx+1}.jpg",
                        "expire_at": now + WEB_TTL_SECONDS,
                        "size_mb": round(fsize / (1024 * 1024), 1),
                        "user_id": 0
                    }
                    photo_items.append({
                        "token": item_token,
                        "filename": f"photo_{idx+1}.jpg",
                        "index": idx + 1,
                        "total": total_p
                    })

            audio_item = None
            if audios:
                a = audios[0]
                aud_token = f"{token}_aud"
                dest_aud = f"{WEB_DOWNLOADS_DIR}/{aud_token}.mp3"
                shutil.copy2(a, dest_aud)
                DOWNLOAD_LINKS[aud_token] = {
                    "path": dest_aud,
                    "filename": "audio.mp3",
                    "expire_at": now + WEB_TTL_SECONDS,
                    "size_mb": round(os.path.getsize(dest_aud) / (1024 * 1024), 1),
                    "user_id": 0
                }
                audio_item = {
                    "token": aud_token,
                    "filename": "audio.mp3",
                    "title": "🎵 Звук из публикации"
                }

            if photo_items:
                return {
                    "type": "photos",
                    "items": photo_items,
                    "audio": audio_item,
                    "total": len(photo_items)
                }

        # 2. Если есть видео (одиночное или смешанная публикация)
        elif videos:
            video_items = []
            for idx, vid in enumerate(videos[:10]):
                item_token = f"{token}_{idx:02d}"
                target_ext = os.path.splitext(vid)[1].lower()
                dest_file = f"{WEB_DOWNLOADS_DIR}/{item_token}.mp4"
                thumb_path = f"{WEB_DOWNLOADS_DIR}/{item_token}_thumb.jpg"

                if target_ext != ".mp4":
                    ffmpeg_proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-y", "-i", vid, "-c", "copy", dest_file,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        preexec_fn=os.setsid
                    )
                    try:
                        await asyncio.wait_for(ffmpeg_proc.wait(), timeout=15.0)
                    except Exception:
                        pass
                    if not os.path.exists(dest_file):
                        shutil.copy2(vid, dest_file)
                else:
                    shutil.copy2(vid, dest_file)

                t_proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-ss", "00:00:01", "-i", dest_file, "-vframes", "1", "-q:v", "2", thumb_path,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    preexec_fn=os.setsid
                )
                try:
                    await asyncio.wait_for(t_proc.wait(), timeout=10.0)
                except Exception:
                    pass

                fsize = os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
                DOWNLOAD_LINKS[item_token] = {
                    "path": dest_file,
                    "thumb_path": thumb_path,
                    "filename": f"video_{idx+1}.mp4",
                    "expire_at": now + WEB_TTL_SECONDS,
                    "size_mb": round(fsize / (1024 * 1024), 1),
                    "user_id": 0
                }
                video_items.append({
                    "token": item_token,
                    "filename": f"video_{idx+1}.mp4",
                    "index": idx + 1,
                    "total": len(videos[:10]),
                    "title": os.path.basename(vid)
                })

            # Смешанная карусель: видео + фото
            extra_photos = []
            if photos:
                for idx, p in enumerate(photos[:10]):
                    item_token = f"{token}_p{idx:02d}"
                    dest_file = f"{WEB_DOWNLOADS_DIR}/{item_token}.jpg"
                    ext = os.path.splitext(p)[1].lower()
                    if ext in ['.jpg', '.jpeg']:
                        shutil.copy2(p, dest_file)
                    else:
                        try:
                            c_proc = await asyncio.create_subprocess_exec(
                                "ffmpeg", "-y", "-i", p, "-q:v", "2", dest_file,
                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                preexec_fn=os.setsid
                            )
                            await asyncio.wait_for(c_proc.wait(), timeout=5.0)
                        except Exception:
                            pass
                        if not os.path.exists(dest_file):
                            shutil.copy2(p, dest_file)

                    if os.path.exists(dest_file):
                        fsize = os.path.getsize(dest_file)
                        DOWNLOAD_LINKS[item_token] = {
                            "path": dest_file,
                            "filename": f"photo_{idx+1}.jpg",
                            "expire_at": now + WEB_TTL_SECONDS,
                            "size_mb": round(fsize / (1024 * 1024), 1),
                            "user_id": 0
                        }
                        extra_photos.append({
                            "token": item_token,
                            "filename": f"photo_{idx+1}.jpg",
                            "index": idx + 1,
                            "total": len(photos[:10])
                        })

            return {
                "type": "videos",
                "items": video_items,
                "photos": extra_photos,
                "total": len(video_items)
            }

        # 3. Если только аудио
        elif audios:
            a = audios[0]
            dest_file = f"{WEB_DOWNLOADS_DIR}/{token}_audio.mp3"
            shutil.copy2(a, dest_file)
            DOWNLOAD_LINKS[token] = {
                "path": dest_file,
                "filename": "audio.mp3",
                "expire_at": now + WEB_TTL_SECONDS,
                "size_mb": round(os.path.getsize(dest_file) / (1024 * 1024), 1),
                "user_id": 0
            }
            return {
                "type": "audio",
                "token": token,
                "filename": "audio.mp3",
                "title": "Аудио"
            }
    except Exception as e:
        logging.error(f"download_for_inline error for {url}: {e}")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
    return None


@dp.inline_query()
async def inline_query_handler(query: InlineQuery):
    """Обработка inline-запросов: отправляет настоящее видео/фото/аудио через юзера в ЛС и группы."""
    try:
        text = query.query.strip()
        url = extract_clean_url(text) if text else None

        bot_sign = f"@{BOT_USERNAME}" if BOT_USERNAME else "бота"
        example_hint = f"Пример: @{BOT_USERNAME} https://www.youtube.com/watch?v=..." if BOT_USERNAME else "Пример: вставь ссылку после юзернейма бота"
        direct_hint = f"📎 Отправь ссылку напрямую боту: @{BOT_USERNAME}" if BOT_USERNAME else "📎 Отправь ссылку напрямую боту"

        if not url:
            await query.answer(
                results=[
                    InlineQueryResultArticle(
                        id="no_url",
                        title="📎 Вставь ссылку на видео или фото",
                        description=example_hint,
                        input_message_content=InputTextMessageContent(
                            message_text=direct_hint
                        )
                    )
                ],
                cache_time=1,
                is_personal=True
            )
            return

        warning = check_unsupported_url(url)
        if warning:
            warn_title = "⚠️ Сервис Reddit заблокирован" if any(d in url.lower() for d in ["reddit.com", "redd.it"]) else "⚠️ Сервис не поддерживается"
            await query.answer(
                results=[
                    InlineQueryResultArticle(
                        id="warning",
                        title=warn_title,
                        description=warning,
                        input_message_content=InputTextMessageContent(
                            message_text=f"⚠️ {warning}"
                        )
                    )
                ],
                cache_time=60,
                is_personal=True
            )
            return

        # 1. Проверяем кэш базы данных
        cached = await get_media_cache(url)
        if cached:
            file_id, media_type, title = cached
            results = []
            if media_type == "video":
                results.append(
                    InlineQueryResultCachedVideo(
                        id=f"v_{uuid.uuid4().hex[:8]}",
                        video_file_id=file_id,
                        title="🎬 Отправить видео",
                        description=title or url[:60],
                        caption=f"🎬 Скачано через {bot_sign}"
                    )
                )
            elif media_type == "photo":
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=f"p_{uuid.uuid4().hex[:8]}",
                        photo_file_id=file_id,
                        title="🖼 Отправить фото",
                        description=title or url[:60],
                        caption=f"🖼 Скачано через {bot_sign}"
                    )
                )
            elif media_type == "photos":
                try:
                    fids = json.loads(file_id)
                    total = len(fids)
                    for idx, fid in enumerate(fids):
                        results.append(
                            InlineQueryResultCachedPhoto(
                                id=f"p_{uuid.uuid4().hex[:8]}_{idx}",
                                photo_file_id=fid,
                                title=f"🖼 Фото {idx+1}/{total}",
                                description=f"Нажми, чтобы отправить фото {idx+1} из {total}",
                                caption=f"🖼 Фото {idx+1}/{total} • Скачано через {bot_sign}"
                            )
                        )
                except Exception as e:
                    logging.warning(f"Error parsing cached photos: {e}")
            elif media_type == "audio":
                results.append(
                    InlineQueryResultCachedAudio(
                        id=f"a_{uuid.uuid4().hex[:8]}",
                        audio_file_id=file_id,
                        title="🎵 Отправить аудио",
                        caption=f"🎵 Скачано через {bot_sign}"
                    )
                )
            if results:
                await query.answer(results=results, cache_time=300, is_personal=False)
                return

        # 2. Если в кэше нет — запускаем быструю подготовку файла
        task = INLINE_ACTIVE_TASKS.get(url)
        if not task or task.done():
            task = asyncio.create_task(download_for_inline(url))
            INLINE_ACTIVE_TASKS[url] = task

        res = None
        try:
            res = await asyncio.wait_for(asyncio.shield(task), timeout=4.5)
        except asyncio.TimeoutError:
            res = None

        if res:
            mtype = res.get("type", "video")
            results = []

            if mtype in ("video", "videos"):
                items = res.get("items", [])
                if not items and "token" in res:
                    items = [{"token": res["token"], "filename": res["filename"], "index": 1, "total": 1}]

                total = len(items)
                for it in items:
                    t = it["token"]
                    fn = it["filename"]
                    idx = it.get("index", 1)
                    v_url = f"{WEB_BASE_URL}/dl/{t}/{fn}"
                    th_url = f"{WEB_BASE_URL}/dl/thumb/{t}.jpg"
                    if total > 1:
                        v_title = f"🎬 Видео {idx}/{total}"
                        v_desc = f"Нажми для отправки видео {idx} из {total}"
                        v_caption = f"🎬 Видео {idx}/{total} • Скачано через {bot_sign}"
                    else:
                        v_title = "🎬 Отправить видео"
                        v_desc = "Нажми для отправки видео в чат"
                        v_caption = f"🎬 Скачано через {bot_sign}"

                    results.append(
                        InlineQueryResultVideo(
                            id=f"v_{t}",
                            video_url=v_url,
                            mime_type="video/mp4",
                            thumbnail_url=th_url,
                            title=v_title,
                            description=v_desc,
                            caption=v_caption
                        )
                    )

                extra_photos = res.get("photos", [])
                p_total = len(extra_photos)
                for it in extra_photos:
                    t = it["token"]
                    fn = it["filename"]
                    idx = it.get("index", 1)
                    photo_url = f"{WEB_BASE_URL}/dl/{t}/{fn}"
                    results.append(
                        InlineQueryResultPhoto(
                            id=f"p_{t}",
                            photo_url=photo_url,
                            thumbnail_url=photo_url,
                            title=f"🖼 Фото {idx}/{p_total}",
                            description=f"Нажми для отправки фото {idx} из {p_total}",
                            caption=f"🖼 Фото {idx}/{p_total} • Скачано через {bot_sign}"
                        )
                    )

            elif mtype in ("photo", "photos"):
                items = res.get("items", [])
                if not items and "token" in res:
                    items = [{"token": res["token"], "filename": res["filename"], "index": 1, "total": 1}]

                total = len(items)
                for it in items:
                    t = it["token"]
                    fn = it["filename"]
                    idx = it.get("index", 1)
                    photo_url = f"{WEB_BASE_URL}/dl/{t}/{fn}"
                    if total > 1:
                        p_title = f"🖼 Фото {idx}/{total}"
                        p_desc = f"Нажми для отправки фото {idx} из {total}"
                        p_caption = f"🖼 Фото {idx}/{total} • Скачано через {bot_sign}"
                    else:
                        p_title = "🖼 Отправить фото"
                        p_desc = "Нажми для отправки фото в чат"
                        p_caption = f"🖼 Скачано через {bot_sign}"

                    results.append(
                        InlineQueryResultPhoto(
                            id=f"p_{t}",
                            photo_url=photo_url,
                            thumbnail_url=photo_url,
                            title=p_title,
                            description=p_desc,
                            caption=p_caption
                        )
                    )

                if res.get("audio"):
                    aud_info = res["audio"]
                    at = aud_info["token"]
                    afn = aud_info["filename"]
                    a_url = f"{WEB_BASE_URL}/dl/{at}/{afn}"
                    results.append(
                        InlineQueryResultAudio(
                            id=f"a_{at}",
                            audio_url=a_url,
                            title="🎵 Звук из публикации",
                            caption=f"🎵 Звук из публикации • Скачано через {bot_sign}"
                        )
                    )

            elif mtype == "audio":
                token = res["token"]
                filename = res["filename"]
                audio_url = f"{WEB_BASE_URL}/dl/{token}/{filename}"
                results.append(
                    InlineQueryResultAudio(
                        id=f"a_{token}",
                        audio_url=audio_url,
                        title=res.get("title", "🎵 Отправить аудио"),
                        caption=f"🎵 Скачано через {bot_sign}"
                    )
                )

            if results:
                await query.answer(
                    results=results,
                    cache_time=300,
                    is_personal=False
                )
                return

        # 3. Если скачивание ещё идет — кнопка авто-обновления запроса
        await query.answer(
            results=[
                InlineQueryResultArticle(
                    id="processing",
                    title="⏳ Медиа обрабатывается...",
                    description="Нажми кнопку ниже через 2-4 сек, чтобы отправить готовые файлы",
                    input_message_content=InputTextMessageContent(
                        message_text=f"⏳ Медиа {url} обрабатывается..."
                    ),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Отправить готовые медиа", switch_inline_query_current_chat=url)]
                    ])
                )
            ],
            cache_time=1,
            is_personal=True
        )
    except Exception as e:
        logging.warning(f"inline_query_handler error: {e}")


@dp.message(StateFilter(None), F.text.regexp(r'https?://'))
async def download_handler(message: Message):
    if not message.text:
        return
    lower = message.text.lower()
    if lower.startswith(("/start", "/help", "/admin")):
        return

    user_id = message.from_user.id
    await add_user(user_id)

    url = extract_clean_url(message.text)
    if not url:
        return

    mode = "auto"
    if "/audio" in lower or "/mp3" in lower or "soundcloud.com" in url:
        mode = "audio"
    elif "/round" in lower or "/circle" in lower:
        mode = "round"

    await queue_download(message, url, mode=mode)

@dp.message(StateFilter(None), F.text)
async def fallback_text_handler(message: Message):
    if message.from_user.id == ADMIN_ID and message.text.startswith("/"):
        return
    await add_user(message.from_user.id)
    text = (message.text or "").strip()
    lower = text.lower()

    # Быстрый перехват Reddit (даже если ссылка прислана без https://)
    if any(d in lower for d in ["reddit.com", "redd.it"]):
        await message.answer("⚠️ Сервис Reddit заблокировал доступ со стороны серверов (HTTP 403 Forbidden). Загрузка с Reddit временно недоступна.")
        return

    # Проверка, если прислана ссылка на поддерживаемый сервис без префикса https://
    url_match = re.search(r'(?:^|\s)((?:[a-zA-Z0-9-]+\.)+(?:com|ru|org|net|me|io|co|app|be|tv|ly|cc)(?:/[^\s]*)?)', text)
    if url_match:
        extracted = "https://" + url_match.group(1).rstrip(',)[]">.')
        warning = check_unsupported_url(extracted)
        if warning:
            await message.answer(f"⚠️ {warning}")
            return
        await queue_download(message, extracted, mode="auto")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Как пользоваться", callback_data="open_help")],
        [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="open_support")]
    ])
    await message.answer(
        "👋 Отправь мне ссылку на видео, аудио или прямое фото.\n\n"
        "Поддерживаются: <b>прямые ссылки на фото/файлы (JPG, PNG, WEBP, GIF, MP4), YouTube, TikTok, Instagram, VK, Rutube, Дзен, Twitter/X, Twitch, Pinterest, SoundCloud, Likee, Coub</b> и многие другие.",
        parse_mode="HTML",
        reply_markup=kb
    )


# --- ИНИЦИАЛИЗАЦИЯ И МЕНЮ КОМАНД ---

async def setup_bot_commands(b: Bot):
    commands = [
        BotCommand(command="start", description="Перезапуск и меню"),
        BotCommand(command="audio", description="Скачать только MP3 из ссылки"),
        BotCommand(command="round", description="Видеосообщение-кружочек"),
        BotCommand(command="help", description="Список поддерживаемых сайтов"),
        BotCommand(command="admin", description="Панель управления (админ)")
    ]
    try:
        await b.set_my_commands(commands)
    except Exception as e:
        logging.error(f"Failed to set bot commands: {e}")

async def main():
    global BOT_USERNAME
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    try:
        me = await bot.get_me()
        if me and me.username:
            BOT_USERNAME = me.username
        logging.info(f"Бот успешно авторизован: @{BOT_USERNAME} (ID: {me.id})")
    except Exception as e:
        logging.warning(f"Не удалось получить данные бота через API: {e}")

    clean_tmp_dir()
    await init_db()
    await setup_bot_commands(bot)

    web_runner = await start_web_server()
    cleanup_task = asyncio.create_task(cleanup_worker())
    worker_tasks = [asyncio.create_task(download_worker(i)) for i in range(NUM_WORKERS)]

    try:
        await dp.start_polling(bot)
    finally:
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        cleanup_task.cancel()
        await web_runner.cleanup()
        await close_db()

if __name__ == "__main__":
    asyncio.run(main())
