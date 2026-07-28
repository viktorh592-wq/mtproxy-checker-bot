import os
import re
import time
import asyncio
from typing import List, Tuple, Optional
from urllib.parse import urlparse

import requests
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from bs4 import BeautifulSoup

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не установлен в переменных окружения!")

if not ADMIN_IDS:
    raise RuntimeError("❌ ADMIN_IDS не установлены!")

# === Источники прокси ===
PROXY_SOURCES = [
    "https://proxy.telegram.org",
    "https://t.me/proxy",
    "https://t.me/addlist",
    "https://t.me/ProxyFree_Ru",
    "https://t.me/ProxyFree_RuBot",
]

TIMEOUT = 5.0
MAX_PROXIES_TO_CHECK = 50

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def parse_proxy_line(line: str) -> Optional[Tuple[str, int, str]]:
    """
    Извлекает IP:PORT:SECRET из строки.
    Поддерживает:
    - tg://proxy?server=ip&port=port&secret=secret
    - ip:port:secret
    """

    # tg://proxy
    if "tg://proxy" in line:
        try:
            url = urlparse(line)
            query = dict(q.split("=") for q in url.query.split("&"))
            server = query.get("server")
            port = query.get("port")
            secret = query.get("secret")

            if server and port and secret:
                return server, int(port), secret
        except Exception:
            pass

    # raw format
    parts = re.split(r'[:\s]+', line.strip())

    if len(parts) >= 3:
        ip = parts[0]

        try:
            port = int(parts[1])
            secret = parts[2]
            return ip, port, secret
        except ValueError:
            pass

    return None


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

    if len(secret) == 32 and secret.startswith(("ee", "dd")):
        return "TLS"
    elif len(secret) == 32:
        return "Обычный"
    else:
        return "Неизвестный"


async def fetch_page(url: str) -> str:
    """Загрузка страницы через aiohttp."""

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"}
            ) as resp:
                return await resp.text()

    except Exception as e:
        print(f"⚠️ Ошибка при загрузке {url}: {e}")
        return ""


async def scrape_proxies() -> List[Tuple[str, int, str]]:
    """Сбор прокси из источников."""

    proxies = set()

    for url in PROXY_SOURCES:
        print(f"🔍 Парсинг: {url}")

        try:
            resp = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )

            if "proxy.telegram.org" in url:
                text = resp.text
            else:
                soup = BeautifulSoup(resp.text, "html.parser")
                text = soup.get_text()

            for line in text.splitlines()[:200]:
                proxy = parse_proxy_line(line)

                if proxy:
                    proxies.add(proxy)

        except Exception as e:
            print(f"❌ Не удалось загрузить {url}: {e}")

    return list(proxies)[:MAX_PROXIES_TO_CHECK]


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "🤖 Бот MTProxy Checker готов.\n"
            "Используйте /check для проверки прокси."
        )
    else:
        await message.answer("🔒 Доступ запрещён.")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("🔒 Доступ запрещён. Вы не администратор.")
        return

    await message.answer("⏳ Начинаю проверку прокси...")

    proxies = await scrape_proxies()

    if not proxies:
        await message.answer("❌ Не найдено ни одного прокси.")
        return

    await message.answer(
        f"🔍 Найдено {len(proxies)} потенциальных прокси. Проверяю..."
    )

    working = []
    start_time = time.time()

    for i, (host, port, secret) in enumerate(proxies, 1):

        if len(working) >= 10:
            break

        ping_start = time.time()

        is_ok = await check_port(host, port)

        ping_ms = int((time.time() - ping_start) * 1000)

        if is_ok:
            proxy_type = detect_proxy_type(secret)

            working.append({
                "host": host,
                "port": port,
                "secret": secret,
                "type": proxy_type,
                "ping": ping_ms
            })

            print(
                f"✅ [{i}/{len(proxies)}] {host}:{port} "
                f"({proxy_type}) — {ping_ms} мс"
            )

        await asyncio.sleep(0.3)

    if not working:
        await message.answer("❌ Рабочих прокси не найдено.")
        return

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

    await message.answer(full_msg, disable_web_page_preview=True)


# === Запуск ===
async def healthcheck(request):
    return web.Response(text="ok")


async def main():
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
