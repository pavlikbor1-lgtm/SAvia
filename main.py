# ---------- ТЕКСТОВЫЕ КОМАНДЫ (для совместимости) ----------
@dp.message(Command("search"))
async def search_cmd(message: Message, state: FSMContext):
    await message.answer(
        "🔍 <b>Поиск билетов</b>\n\n"
        "Выберите аэропорт отправления:",
        reply_markup=get_airports_keyboard(for_destination=False)
    )
    await state.set_state(SearchFlight.origin)# -------------------------
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
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Добавляем aiohttp для веб-сервера
from aiohttp import web

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
TP_CURRENCY = os.getenv("TP_CURRENCY", "rub")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 мин
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))
PORT = int(os.getenv("PORT", "10000"))  # Render.com использует переменную PORT

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
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
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params)
        if resp.status_code == 200:
            return resp.json().get("data", [])
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
    await message.answer(
        "Привет! Я помогу искать билеты ✈️\n\n"
        "Команды:\n"
        "/search — пошаговый поиск билетов\n"
        "/alert ORIG DEST 2025-09-10 2025-09-15 1 5000 — создать оповещение (порог 5000₽)\n"
        "/alerts — список оповещений\n"
        "/cancel ID — удалить оповещение"
    )

# ---------- ПОШАГОВЫЙ ПОИСК ----------
@dp.message(Command("search"))
async def start_search(message: Message, state: FSMContext):
    await message.answer("Введите ORIG — код аэропорта вылета (например: MOW):")
    await state.set_state(SearchFlight.origin)

@dp.message(SearchFlight.origin)
async def set_origin(message: Message, state: FSMContext):
    await state.update_data(origin=message.text.strip().upper())
    await message.answer("Введите DEST — код аэропорта назначения (например: LED):")
    await state.set_state(SearchFlight.destination)

@dp.message(SearchFlight.destination)
async def set_destination(message: Message, state: FSMContext):
    await state.update_data(destination=message.text.strip().upper())
    await message.answer("Введите DATE1 — начальную дату (в формате YYYY-MM-DD):")
    await state.set_state(SearchFlight.date1)

@dp.message(SearchFlight.date1)
async def set_date1(message: Message, state: FSMContext):
    d = validate_date(message.text.strip())
    if not d:
        await message.answer("❌ Неверная дата. Введите в формате YYYY-MM-DD и не в прошлом.")
        return
    await state.update_data(date1=str(d))
    await message.answer("Введите DATE2 — конечную дату (в формате YYYY-MM-DD):")
    await state.set_state(SearchFlight.date2)

@dp.message(SearchFlight.date2)
async def set_date2(message: Message, state: FSMContext):
    d = validate_date(message.text.strip())
    if not d:
        await message.answer("❌ Неверная дата. Введите в формате YYYY-MM-DD и не в прошлом.")
        return
    data = await state.get_data()
    if d < isoparse(data["date1"]).date():
        await message.answer("❌ Конечная дата не может быть раньше начальной. Попробуйте снова.")
        return
    await state.update_data(date2=str(d))
    await message.answer("Введите ADULTS — количество взрослых пассажиров (целое число):")
    await state.set_state(SearchFlight.adults)

@dp.message(SearchFlight.adults)
async def finish_search(message: Message, state: FSMContext):
    try:
        adults = int(message.text.strip())
        if adults < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число пассажиров (минимум 1).")
        return

    await state.update_data(adults=adults)
    data = await state.get_data()
    await state.clear()

    # запуск поиска
    flights = await search_range(
        data["origin"],
        data["destination"],
        isoparse(data["date1"]).date(),
        isoparse(data["date2"]).date(),
        data["adults"]
    )

    if not flights:
        await message.answer("Билетов не найдено 😔")
        return

    flights = sorted(flights, key=lambda x: x.get("price", 999999))[:5]
    for f in flights:
        text = (
            f"✈️ {f.get('origin')} → {f.get('destination')}\n"
            f"📅 {f.get('departure_at')}\n"
            f"💰 {f.get('price')} ₽\n"
            f"🛫 {f.get('airline', '—')}\n"
            f"🔗 https://www.aviasales.ru{f.get('link', '')}"
        )
        await message.answer(text)

# ---------- ALERTS ----------
# ---------- ТЕКСТОВЫЕ КОМАНДЫ (для совместимости) ----------
@dp.message(Command("search"))
async def search_cmd(message: Message, state: FSMContext):
    await message.answer(
        "🔍 <b>Поиск билетов</b>\n\n"
        "Выберите аэропорт отправления:",
        reply_markup=get_airports_keyboard(for_destination=False)
    )
    await state.set_state(SearchFlight.origin)

@dp.message(Command("alert"))
async def alert_cmd(message: Message):
    try:
        parts = message.text.split()
        if len(parts) != 6:
            raise ValueError("Неверное количество параметров")
        
        _, origin, destination, d1, d2, threshold = parts
        start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
        threshold = int(threshold)
        
        # Проверка дат
        if start_date < datetime.now().date():
            await message.answer("❌ Начальная дата не может быть в прошлом!")
            return
        
        if end_date < start_date:
            await message.answer("❌ Конечная дата не может быть раньше начальной!")
            return
        
        await add_alert(message.from_user.id, origin.upper(), destination.upper(), str(start_date), str(end_date), 1, threshold)
        
        await message.answer(
            f"✅ <b>Оповещение создано!</b>\n\n"
            f"Маршрут: {origin.upper()} → {destination.upper()}\n"
            f"Период: {start_date} — {end_date}\n"
            f"Максимальная цена: {threshold} ₽\n\n"
            "Вы получите уведомление, когда цена опустится ниже указанного порога.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        await message.answer(
            "❌ <b>Ошибка создания оповещения</b>\n\n"
            "Используйте формат:\n"
            "<code>/alert ORIG DEST YYYY-MM-DD YYYY-MM-DD ЦЕНА</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/alert MOW LED 2025-12-01 2025-12-15 8000</code>\n\n"
            f"Детали ошибки: {str(e)}"
        )

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    alerts = await get_alerts()
    user_alerts = [a for a in alerts if a[1] == message.from_user.id]
    
    if not user_alerts:
        await message.answer(
            "📋 <b>Ваши оповещения</b>\n\n"
            "У вас нет активных оповещений.\n\n"
            "Создайте оповещение командой:\n"
            "<code>/alert MOW LED 2025-12-01 2025-12-15 8000</code>",
            reply_markup=get_main_menu()
        )
        return
    
    text = "📋 <b>Ваши активные оповещения:</b>\n\n"
    for i, alert in enumerate(user_alerts, 1):
        id_, user_id, origin, destination, start_date, end_date, adults, threshold = alert
        text += (
            f"<b>{i}. {origin} → {destination}</b>\n"
            f"📅 {start_date} — {end_date}\n"
            f"💰 до {threshold} ₽\n"
            f"🆔 ID: {id_}\n\n"
        )
    
    text += "Для удаления используйте: <code>/cancel ID</code>"
    
    await message.answer(text, reply_markup=get_main_menu())

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    try:
        parts = message.text.split()
        if len(parts) != 2:
            raise ValueError("Укажите ID оповещения")
        
        alert_id = int(parts[1])
        
        # Проверяем, что оповещение принадлежит пользователю
        alerts = await get_alerts()
        user_alert = None
        for alert in alerts:
            if alert[0] == alert_id and alert[1] == message.from_user.id:
                user_alert = alert
                break
        
        if not user_alert:
            await message.answer("❌ Оповещение не найдено или не принадлежит вам!")
            return
        
        await delete_alert(alert_id, message.from_user.id)
        
        await message.answer(
            f"✅ <b>Оповещение удалено!</b>\n\n"
            f"Удалено: {user_alert[2]} → {user_alert[3]}\n"
            f"Период: {user_alert[4]} — {user_alert[5]}",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка:</b> {str(e)}\n\n"
            "Используйте формат: <code>/cancel ID</code>\n"
            "ID можно узнать в списке оповещений (/alerts)"
        )

# Обработчик для игнорируемых callback'ов
@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQueryType):
    await callback.answer()

# ================== WEB SERVER ==================
async def health_check(request):
    return web.Response(
        text="✅ Telegram Bot is running! 🤖\nTime: " + datetime.now().isoformat(), 
        status=200,
        content_type='text/plain; charset=utf-8'
    )

async def status_check(request):
    try:
        alerts_count = len(await get_alerts())
        bot_info = await bot.get_me()
        return web.json_response({
            "status": "ok",
            "alerts_count": alerts_count,
            "bot_username": bot_info.username,
            "bot_id": bot_info.id,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return web.json_response({
            "status": "error", 
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, status=500)

async def create_app():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_check)
    
    # Добавляем логирование запросов
    async def log_requests(request, handler):
        start = datetime.now()
        response = await handler(request)
        duration = (datetime.now() - start).total_seconds()
        print(f"📡 {request.method} {request.path} - {response.status} ({duration:.3f}s)")
        return response
    
    app.middlewares.append(log_requests)
    return app

# ================== BACKGROUND TASK ==================
async def monitor_alerts():
    while True:
        alerts = await get_alerts()
        for alert in alerts:
            id_, user_id, origin, destination, d1, d2, adults, threshold = alert
            start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
            flights = await search_range(origin, destination, start_date, end_date, adults)
            for f in flights:
                if f.get("price", 999999) <= threshold:
                    text = (
                        f"🔥 Найден билет по {f.get('price')} ₽!\n"
                        f"✈️ {f.get('origin')} → {f.get('destination')}\n"
                        f"📅 {f.get('departure_at')}\n"
                        f"🛫 {f.get('airline', '—')}\n"
                        f"🔗 https://www.aviasales.ru{f.get('link', '')}"
                    )
                    try:
                        await bot.send_message(user_id, text)
                    except Exception:
                        pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ================== MAIN ==================
async def main():
    await init_db()
    
    # Запускаем мониторинг alerts в фоне
    asyncio.create_task(monitor_alerts())
    
    # Создаем и запускаем веб-сервер
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    # Запускаем сервер и бота одновременно
    await asyncio.gather(
        site.start(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
