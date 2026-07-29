import os
import re
import json
import time
import asyncio
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не установлен в переменных окружения!")

# === Источники прокси ===
# Каналы читаем через веб-превью t.me/s/<name> — только там видны сообщения
# (обычная страница t.me/<name> — заглушка без постов!)
CHANNELS = [
    "proxy",
    "addlist",
    "ProxyFree_Ru",
    "ProxyFree_RuBot",
    "ProxyFree_Russ",
    "ProxyFree_Ru_bot",
    "ProxyFreeMTProto",
    "mtp4tg",
    "memtproxy",
]
EXTRA_SOURCES = [
    "https://proxy.telegram.org",
]

TIMEOUT = 5.0
MAX_PROXIES_TO_CHECK = 80   # сколько кандидатов максимум проверять
MAX_WORKING = 20            # сколько рабочих показывать в ответе
CHANNEL_PAGES = 2           # страниц истории на канал (~20 постов каждая) => последние ~40 постов
CHECK_CONCURRENCY = 10      # параллельных проверок портов
CHECK_COOLDOWN = 60         # пауза между проверками для обычных пользователей (сек)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# tg://proxy?server=...&port=...&secret=...  (в HTML может быть с &amp; — лечим unescape)
TG_PROXY_RE = re.compile(r"tg://proxy\?[^\s\"'<>)]+", re.IGNORECASE)
# Текстовый формат  ip:port:secret  (secret — 32+ hex-символов)
RAW_PROXY_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5}):([0-9a-fA-F]{32,})\b")
SECRET_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === Кнопки ===
CHECK_BTN = "🔍 Чекнуть прокси"

# постоянная клавиатура под полем ввода
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CHECK_BTN)]],
    resize_keyboard=True,
)

# инлайн-кнопка под результатом проверки
REFRESH_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="check_again")]
    ]
)


def request_kb(user_id: int) -> InlineKeyboardMarkup:
    """Кнопки решения админа по заявке."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"approve:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{user_id}"),
    ]])


# === Состояние ===
last_check: Dict[int, float] = {}   # user_id -> время последней проверки
check_lock = asyncio.Lock()         # только одна проверка одновременно

# === Доступ: база заявок/разрешений (файл) ===
DB_FILE = "access_db.json"
approved: set = set()   # кому разрешили
pending: set = set()    # заявки в ожидании
rejected: set = set()   # кого отклонили


def load_db():
    global approved, pending, rejected
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            data = json.load(f)
        approved = set(data.get("approved", []))
        pending = set(data.get("pending", []))
        rejected = set(data.get("rejected", []))
        print(f"📂 Доступ загружен: {len(approved)} разрешено, {len(pending)} в ожидании")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ Не удалось загрузить {DB_FILE}: {e}")


def save_db():
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "approved": sorted(approved),
                "pending": sorted(pending),
                "rejected": sorted(rejected),
            }, f)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить {DB_FILE}: {e}")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def has_access(user_id: int) -> bool:
    return is_admin(user_id) or user_id in approved


def user_name(user) -> str:
    if user.username:
        return f"@{user.username}"
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or "без имени"


async def request_access(message: Message, user):
    """Отправка заявки админам с инлайн-кнопками решения."""
    if user.id in pending:
        await message.answer("⏳ Ваша заявка уже на рассмотрении у администратора.")
        return

    # повторная заявка после отказа — разрешаем
    rejected.discard(user.id)
    pending.add(user.id)
    save_db()

    await message.answer(
        "⏳ Заявка отправлена администратору.\n"
        "Как только доступ одобрят — вам придёт уведомление."
    )

    text = (
        f"📩 Запрос на доступ к боту\n"
        f"Пользователь: {user_name(user)}\n"
        f"ID: {user.id}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=request_kb(user.id))
        except Exception as e:
            print(f"⚠️ Не удалось отправить заявку админу {admin_id}: {e}")


def parse_tg_link(raw: str) -> Optional[Tuple[str, int, str]]:
    """Разбор ссылки tg://proxy?server=..&port=..&secret=.."""
    try:
        query = parse_qs(urlparse(unescape(raw)).query)
        host = query.get("server", [None])[0]
        secret = query.get("secret", [None])[0]
        port = int(query.get("port", ["0"])[0])

        if host and secret and 0 < port < 65536 and SECRET_RE.match(secret):
            return host, port, secret
    except Exception:
        pass
    return None


def extract_proxies(content: str) -> Dict[Tuple[str, int], Tuple[str, int, str]]:
    """Все прокси со страницы: и кликабельные ссылки, и текстовый формат."""
    found: Dict[Tuple[str, int], Tuple[str, int, str]] = {}

    # 1) tg://proxy ссылки (кнопки и href — основной формат в каналах)
    for match in TG_PROXY_RE.finditer(content):
        proxy = parse_tg_link(match.group(0))
        if proxy:
            found[(proxy[0], proxy[1])] = proxy

    # 2) текстовый ip:port:secret
    for match in RAW_PROXY_RE.finditer(content):
        host, port_str, secret = match.groups()
        if SECRET_RE.match(secret):
            found.setdefault((host, int(port_str)), (host, int(port_str), secret))

    return found


def fetch_html(url: str) -> str:
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
        if resp.status_code == 200:
            return resp.text
        print(f"⚠️ {url}: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ Не удалось загрузить {url}: {e}")
    return ""


def scrape_channel(name: str) -> Dict[Tuple[str, int], Tuple[str, int, str]]:
    """Веб-превью канала t.me/s/<name> с листанием назад (?before=<id>)."""
    proxies: Dict[Tuple[str, int], Tuple[str, int, str]] = {}
    url = fhttps://t.me/s/{name}"

    for _ in range(CHANNEL_PAGES):
        html = fetch_html(url)
        if not html:
            break

        proxies.update(extract_proxies(html))

        # id самого раннего сообщения на странице — для следующей страницы истории
        ids = [int(x) for x in re.findall(r'data-post="[^"]*/(\d+)"', html)]
        if not ids:
            break
        url = f"https://t.me/s/{name}?before={min(ids)}"

    return proxies


def scrape_all() -> List[Tuple[str, int, str]]:
    """Сбор кандидатов со всех источников (синхронно, запускать через to_thread)."""
    proxies: Dict[Tuple[str, int], Tuple[str, int, str]] = {}

    for url in EXTRA_SOURCES:
        print(f"🔍 Парсинг: {url}")
        proxies.update(extract_proxies(fetch_html(url)))

    for name in CHANNELS:
        print(f"🔍 Парсинг: t.me/{name}")
        proxies.update(scrape_channel(name))

    print(f"📦 Всего уникальных кандидатов: {len(proxies)}")
    return list(proxies.values())[:MAX_PROXIES_TO_CHECK]


async def check_port(host: str, port: int) -> bool:
    """Проверка доступности TCP порта."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=TIMEOUT
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def detect_proxy_type(secret: str) -> str:
    """Определение типа MTProxy по secret."""
    low = secret.lower()
    if low.startswith("ee"):
        return "Fake-TLS"
    if low.startswith("dd"):
        return "TLS (random padding)"
    if len(secret) == 32:
        return "Обычный"
    return "Неизвестный"


async def probe(sem: asyncio.Semaphore, host: str, port: int, secret: str):
    """Проверка одного прокси под семафором (чтобы не дудосить)."""
    async with sem:
        start = time.time()
        is_ok = await check_port(host, port)
        ping_ms = int((time.time() - start) * 1000)
        return (host, port, secret), ping_ms, is_ok


# === Логика проверки (общая для команды, кнопки и инлайн-кнопки) ===
async def run_check(message: Message, user_id: int):
    # доступ: только админы и одобренные пользователи
    if not has_access(user_id):
        await message.answer(
            "🔒 Доступ по заявке. Нажмите /start, чтобы отправить её администратору."
        )
        return

    # кулдаун для обычных пользователей (админы — без ограничений)
    if not is_admin(user_id):
        wait = int(CHECK_COOLDOWN - (time.time() - last_check.get(user_id, 0)))
        if wait > 0:
            await message.answer(
                f"⏳ Проверку уже запускали недавно. "
                f"Попробуйте снова через {wait} сек."
            )
            return

    # не запускаем вторую проверку, пока идёт предыдущая
    if check_lock.locked():
        await message.answer("⏳ Идёт другая проверка, подождите немного...")
        return

    last_check[user_id] = time.time()

    async with check_lock:
        start_time = time.time()
        status = await message.answer("⏳ Собираю прокси из каналов...")

        # синхронный парсинг уводим в отдельный поток, чтобы не блокировать бота
        try:
            candidates = await asyncio.to_thread(scrape_all)
        except Exception as e:
            await status.edit_text(f"❌ Ошибка при сборе прокси: {e}")
            return

        if not candidates:
            await status.edit_text("❌ Не найдено ни одного прокси.")
            return

        await status.edit_text(
            f"🔍 Найдено {len(candidates)} кандидатов. Проверяю порты..."
        )

        sem = asyncio.Semaphore(CHECK_CONCURRENCY)
        results = await asyncio.gather(
            *(probe(sem, host, port, secret) for host, port, secret in candidates)
        )

        working = []
        for (host, port, secret), ping_ms, is_ok in results:
            if is_ok:
                working.append({
                    "host": host,
                    "port": port,
                    "secret": secret,
                    "type": detect_proxy_type(secret),
                    "ping": ping_ms,
                })

        if not working:
            await status.edit_text(
                "❌ Рабочих прокси не найдено.",
                reply_markup=REFRESH_KB,
            )
            return

        # самые быстрые — в топ
        working.sort(key=lambda p: p["ping"])
        working = working[:MAX_WORKING]
