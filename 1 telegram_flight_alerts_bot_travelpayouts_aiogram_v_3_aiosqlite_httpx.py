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
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
TP_CURRENCY = os.getenv("TP_CURRENCY", "rub")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 мин
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))

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
@dp.message(Command("alert"))
async def alert_cmd(message: Message):
    try:
        _, origin, destination, d1, d2, adults, threshold = message.text.split()
        start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
        adults, threshold = int(adults), int(threshold)
        await add_alert(message.from_user.id, origin, destination, str(start_date), str(end_date), adults, threshold)
        await message.answer("Оповещение добавлено ✅")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    alerts = await get_alerts()
    user_alerts = [a for a in alerts if a[1] == message.from_user.id]
    if not user_alerts:
        await message.answer("У вас нет активных оповещений")
        return
    text = "Ваши оповещения:\n"
    for a in user_alerts:
        text += f"ID {a[0]}: {a[2]} → {a[3]}, {a[4]}–{a[5]}, до {a[7]}₽\n"
    await message.answer(text)

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    try:
        _, alert_id = message.text.split()
        await delete_alert(int(alert_id), message.from_user.id)
        await message.answer("Оповещение удалено ✅")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

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
    asyncio.create_task(monitor_alerts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
