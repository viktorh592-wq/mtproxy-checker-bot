import os
import re
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

if not ADMIN_IDS:
    raise RuntimeError("❌ ADMIN_IDS не установлены!")

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
MAX_PROXIES_TO_CHECK = 60   # сколько кандидатов максимум проверять
MAX_WORKING = 10            # сколько рабочих показывать в ответе
CHANNEL_PAGES = 2           # страниц истории на канал (~20 постов каждая) => последние ~40 постов
CHECK_CONCURRENCY = 10      # параллельных проверок портов
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


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
    url = f"https://t.me/s/{name}"

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
    if not is_admin(user_id):
        await message.answer("🔒 Доступ запрещён. Вы не администратор.")
        return

    start_time = time.time()
    status = await message.answer("⏳ Собираю прокси из каналов...")

    # синхронный парсинг уводим в отдельный поток, чтобы не блокировать бота
    candidates = await asyncio.to_thread(scrape_all)

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

    lines = [f"🔍 Найдено {len(working)} рабочих прокси:\n"]

    for idx, p in enumerate(working, 1):
        link = (
            f"tg://proxy?server={p['host']}&port={p['port']}&secret={p['secret']}"
        )
        lines.append(
            f"{idx}. {p['host']}:{p['port']} ({p['type']})\n"
            f"   Ping: {p['ping']} мс\n"
            f"   {link}\n"
        )

    lines.append(
        f"\n✅ Проверка завершена за {int(time.time() - start_time)} сек."
    )

    full_msg = "\n".join(lines)

    if len(full_msg) > 4000:
        full_msg = full_msg[:3900] + "\n\n...(обрезано)"

    # результат появляется в том же сообщении-статусе, с кнопкой "ещё раз"
    await status.edit_text(
        full_msg,
        reply_markup=REFRESH_KB,
        disable_web_page_preview=True,
    )


# === Хендлеры: команда, кнопка, инлайн-кнопка ===
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "🤖 Бот MTProxy Checker готов.\n"
            "Жмите кнопку ниже или /check.",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer("🔒 Доступ запрещён.")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    await run_check(message, message.from_user.id)


@dp.message(F.text == CHECK_BTN)
async def btn_check(message: Message):
    await run_check(message, message.from_user.id)


@dp.callback_query(F.data == "check_again")
async def cb_check(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("🔒 Доступ запрещён.", show_alert=True)
        return
    await callback.answer("Запускаю проверку...")
    await run_check(callback.message, callback.from_user.id)


# === Запуск ===
async def healthcheck(request):
    return web.Response(text="ok")


async def main():
    # сброс старых сессий/апдейтов, чтобы не конфликтовать с прошлым инстансом
    await bot.delete_webhook(drop_pending_updates=True)

    # меню команд (кнопка ☰ рядом с полем ввода)
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="check", description="Чекнуть прокси"),
    ])

    # HTTP-заглушка: Render Web Service требует открытый порт
    app = web.Application()
    app.router.add_get("/", healthcheck)
    app.router.add_get("/health", healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 HTTP-заглушка слушает порт {port}")

    print("🚀 Бот запущен. Ожидание команд...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
