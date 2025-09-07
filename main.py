# -------------------------
# Configuration & constants
# -------------------------

import os
import asyncio
import httpx
import aiosqlite
from datetime import datetime, timedelta
from dateutil.parser import isoparse

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Добавляем aiohttp для веб-сервера
from aiohttp import web
import aiohttp

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
TP_CURRENCY = os.getenv("TP_CURRENCY", "rub")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 мин
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))
PORT = int(os.getenv("PORT", "10000"))  # Render.com использует переменную PORT

# URL вашего приложения на Render (автоматически определяется или задается вручную)
RENDER_APP_URL = "https://savia-w3zz.onrender.com"
KEEP_ALIVE_INTERVAL = 600  # 10 минут

# Настройки таймаутов для более стабильной работы
TELEGRAM_TIMEOUT = 30  # секунд
HTTP_TIMEOUT = 20  # секунд

# Создаем сессию с увеличенными таймаутами
session = AiohttpSession(
    connector=aiohttp.TCPConnector(
        limit=100,
        limit_per_host=30,
        ttl_dns_cache=300,
        use_dns_cache=True,
        keepalive_timeout=30,
        enable_cleanup_closed=True
    ),
    timeout=aiohttp.ClientTimeout(total=TELEGRAM_TIMEOUT)
)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=session
)
dp = Dispatcher()

# ================== HELPERS ==================
async def fetch_flights(origin, destination, date, adults=1):
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": date,
        "adults": adults,
        "currency": TP_CURRENCY,
        "token": TRAVELPAYOUTS_TOKEN,
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json().get("data", [])
            return []
    except Exception as e:
        print(f"Error fetching flights: {e}")
        return []

async def search_range(origin, destination, start_date, end_date, adults=1):
    results = []
    date = start_date
    while date <= end_date:
        flights = await fetch_flights(origin, destination, date.isoformat(), adults)
        for f in flights:
            f["search_date"] = date.isoformat()
        results.extend(flights)
        await asyncio.sleep(RATE_LIMIT_MS / 1000)
        date += timedelta(days=1)
    return results

def validate_date(date_str: str) -> datetime | None:
    """Проверка формата и что дата не в прошлом."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d < datetime.today().date():
            return None
        return d
    except ValueError:
        return None

# ================== KEEP ALIVE ==================
async def keep_alive():
    """Периодически пингует само приложение, чтобы оно не засыпало"""
    await asyncio.sleep(60)  # Ждем минуту после запуска
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{RENDER_APP_URL}/health")
                if response.status_code == 200:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Keep-alive ping successful")
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Keep-alive ping returned status {response.status_code}")
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Keep-alive ping failed: {e}")
        
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)

# ================== SAFE MESSAGE SENDING ==================
async def safe_send_message(user_id: int, text: str, max_retries: int = 3):
    """Безопасная отправка сообщений с повторными попытками"""
    for attempt in range(max_retries):
        try:
            await bot.send_message(user_id, text)
            return True
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed to send message to {user_id}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка
    return False

async def safe_answer_message(message: Message, text: str, max_retries: int = 3):
    """Безопасный ответ на сообщение с повторными попытками"""
    for attempt in range(max_retries):
        try:
            await message.answer(text)
            return True
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed to answer message: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка
    return False

# ================== DB ==================
async def init_db():
    async with aiosqlite.connect("alerts.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            origin TEXT,
            destination TEXT,
            start_date TEXT,
            end_date TEXT,
            adults INTEGER,
            threshold INTEGER
        )
        """)
        await db.commit()

async def add_alert(user_id, origin, destination, start_date, end_date, adults, threshold):
    async with aiosqlite.connect("alerts.db") as db:
        await db.execute(
            "INSERT INTO alerts (user_id, origin, destination, start_date, end_date, adults, threshold) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, origin, destination, start_date, end_date, adults, threshold),
        )
        await db.commit()

async def get_alerts():
    async with aiosqlite.connect("alerts.db") as db:
        async with db.execute("SELECT id, user_id, origin, destination, start_date, end_date, adults, threshold FROM alerts") as cur:
            return await cur.fetchall()

async def delete_alert(alert_id, user_id):
    async with aiosqlite.connect("alerts.db") as db:
        await db.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
        await db.commit()

# ================== FSM ==================
class SearchFlight(StatesGroup):
    origin = State()
    destination = State()
    date1 = State()
    date2 = State()
    adults = State()

# ================== BOT HANDLERS ==================
@dp.message(Command("start"))
async def start_cmd(message: Message):
    text = (
        "Привет! Я помогу искать билеты ✈️\n\n"
        "Команды:\n"
        "/search — пошаговый поиск билетов\n"
        "/alert ORIG DEST 2025-09-10 2025-09-15 1 5000 — создать оповещение (порог 5000₽)\n"
        "/alerts — список оповещений\n"
        "/cancel ID — удалить оповещение"
    )
    await safe_answer_message(message, text)

# ---------- ПОШАГОВЫЙ ПОИСК ----------
@dp.message(Command("search"))
async def start_search(message: Message, state: FSMContext):
    await safe_answer_message(message, "Введите ORIG — код аэропорта вылета (например: MOW):")
    await state.set_state(SearchFlight.origin)

@dp.message(SearchFlight.origin)
async def set_origin(message: Message, state: FSMContext):
    await state.update_data(origin=message.text.strip().upper())
    await safe_answer_message(message, "Введите DEST — код аэропорта назначения (например: LED):")
    await state.set_state(SearchFlight.destination)

@dp.message(SearchFlight.destination)
async def set_destination(message: Message, state: FSMContext):
    await state.update_data(destination=message.text.strip().upper())
    await safe_answer_message(message, "Введите DATE1 — начальную дату (в формате YYYY-MM-DD):")
    await state.set_state(SearchFlight.date1)

@dp.message(SearchFlight.date1)
async def set_date1(message: Message, state: FSMContext):
    d = validate_date(message.text.strip())
    if not d:
        await safe_answer_message(message, "❌ Неверная дата. Введите в формате YYYY-MM-DD и не в прошлом.")
        return
    await state.update_data(date1=str(d))
    await safe_answer_message(message, "Введите DATE2 — конечную дату (в формате YYYY-MM-DD):")
    await state.set_state(SearchFlight.date2)

@dp.message(SearchFlight.date2)
async def set_date2(message: Message, state: FSMContext):
    d = validate_date(message.text.strip())
    if not d:
        await safe_answer_message(message, "❌ Неверная дата. Введите в формате YYYY-MM-DD и не в прошлом.")
        return
    data = await state.get_data()
    if d < isoparse(data["date1"]).date():
        await safe_answer_message(message, "❌ Конечная дата не может быть раньше начальной. Попробуйте снова.")
        return
    await state.update_data(date2=str(d))
    await safe_answer_message(message, "Введите ADULTS — количество взрослых пассажиров (целое число):")
    await state.set_state(SearchFlight.adults)

@dp.message(SearchFlight.adults)
async def finish_search(message: Message, state: FSMContext):
    try:
        adults = int(message.text.strip())
        if adults < 1:
            raise ValueError
    except ValueError:
        await safe_answer_message(message, "❌ Введите корректное число пассажиров (минимум 1).")
        return

    await state.update_data(adults=adults)
    data = await state.get_data()
    await state.clear()

    # Уведомляем пользователя о начале поиска
    await safe_answer_message(message, "🔍 Ищу билеты, это может занять некоторое время...")

    # запуск поиска
    flights = await search_range(
        data["origin"],
        data["destination"],
        isoparse(data["date1"]).date(),
        isoparse(data["date2"]).date(),
        data["adults"]
    )

    if not flights:
        await safe_answer_message(message, "Билетов не найдено 😔")
        return

    flights = sorted(flights, key=lambda x: x.get("price", 999999))[:5]
    await safe_answer_message(message, f"Найдено билетов: {len(flights)}. Показываю лучшие:")
    
    for f in flights:
        text = (
            f"✈️ {f.get('origin')} → {f.get('destination')}\n"
            f"📅 {f.get('departure_at')}\n"
            f"💰 {f.get('price')} ₽\n"
            f"🛫 {f.get('airline', '—')}\n"
            f"🔗 https://www.aviasales.ru{f.get('link', '')}"
        )
        await safe_answer_message(message, text)
        await asyncio.sleep(0.5)  # Небольшая задержка между сообщениями

# ---------- ALERTS ----------
@dp.message(Command("alert"))
async def alert_cmd(message: Message):
    try:
        _, origin, destination, d1, d2, adults, threshold = message.text.split()
        start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
        adults, threshold = int(adults), int(threshold)
        await add_alert(message.from_user.id, origin, destination, str(start_date), str(end_date), adults, threshold)
        await safe_answer_message(message, "Оповещение добавлено ✅")
    except Exception as e:
        await safe_answer_message(message, f"Ошибка: {e}\n\nПример: /alert MOW LED 2025-09-10 2025-09-15 1 5000")

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    alerts = await get_alerts()
    user_alerts = [a for a in alerts if a[1] == message.from_user.id]
    if not user_alerts:
        await safe_answer_message(message, "У вас нет активных оповещений")
        return
    text = "Ваши оповещения:\n"
    for a in user_alerts:
        text += f"ID {a[0]}: {a[2]} → {a[3]}, {a[4]}–{a[5]}, до {a[7]}₽\n"
    await safe_answer_message(message, text)

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    try:
        _, alert_id = message.text.split()
        await delete_alert(int(alert_id), message.from_user.id)
        await safe_answer_message(message, "Оповещение удалено ✅")
    except Exception as e:
        await safe_answer_message(message, f"Ошибка: {e}\n\nПример: /cancel 1")

# ================== WEB SERVER ==================
async def health_check(request):
    return web.Response(
        text=f"Telegram Bot is running! 🤖\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 
        status=200
    )

async def status_check(request):
    try:
        alerts_count = len(await get_alerts())
        bot_info = await bot.get_me()
        uptime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return web.json_response({
            "status": "ok",
            "alerts_count": alerts_count,
            "bot_username": bot_info.username,
            "uptime": uptime,
            "keep_alive_active": True,
            "app_url": RENDER_APP_URL
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "error": str(e)
        }, status=500)

async def create_app():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_check)
    return app

# ================== BACKGROUND TASKS ==================
async def monitor_alerts():
    """Мониторинг оповещений о билетах"""
    await asyncio.sleep(30)  # Ждем полминуты после запуска
    
    while True:
        try:
            alerts = await get_alerts()
            if alerts:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 Checking {len(alerts)} alerts...")
            
            for alert in alerts:
                try:
                    id_, user_id, origin, destination, d1, d2, adults, threshold = alert
                    start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
                    
                    # Пропускаем устаревшие оповещения
                    if end_date < datetime.now().date():
                        continue
                        
                    flights = await search_range(origin, destination, start_date, end_date, adults)
                    
                    for f in flights:
                        if f.get("price", 999999) <= threshold:
                            text = (
                                f"🔥 Найден билет по цене {f.get('price')} ₽!\n"
                                f"✈️ {f.get('origin')} → {f.get('destination')}\n"
                                f"📅 {f.get('departure_at')}\n"
                                f"🛫 {f.get('airline', '—')}\n"
                                f"🔗 https://www.aviasales.ru{f.get('link', '')}"
                            )
                            success = await safe_send_message(user_id, text)
                            if success:
                                print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Alert sent to user {user_id}")
                            else:
                                print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Failed to send alert to user {user_id}")
                            break  # Отправляем только одно уведомление за раз
                            
                    await asyncio.sleep(2)  # Пауза между проверками alerts
                    
                except Exception as e:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error processing alert {alert[0]}: {e}")
                    
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error in monitor_alerts: {e}")
        
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ================== ERROR HANDLER ==================
@dp.error()
async def error_handler(event, data):
    """Глобальный обработчик ошибок"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error occurred: {event.exception}")
    return True  # Помечаем ошибку как обработанную

# ================== MAIN ==================
async def main():
    print("🚀 Starting Flight Alert Bot...")
    print(f"📍 App URL: {RENDER_APP_URL}")
    print(f"🔄 Keep-alive interval: {KEEP_ALIVE_INTERVAL}s")
    print(f"📊 Alert check interval: {POLL_INTERVAL_SECONDS}s")
    
    try:
        await init_db()
        print("✅ Database initialized")
        
        # Проверяем подключение к боту
        bot_info = await bot.get_me()
        print(f"✅ Bot connected: @{bot_info.username}")
        
        # Запускаем все фоновые задачи
        asyncio.create_task(monitor_alerts())
        asyncio.create_task(keep_alive())
        print("✅ Background tasks started")
        
        # Создаем и запускаем веб-сервер
        app = await create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        
        print(f"✅ Starting web server on port {PORT}")
        print("✅ Starting Telegram bot polling...")
        print("🎉 Bot is ready!")
        
        # Запускаем сервер и бота одновременно
        await asyncio.gather(
            site.start(),
            dp.start_polling(bot, skip_updates=True)
        )
        
    except Exception as e:
        print(f"❌ Failed to start bot: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
