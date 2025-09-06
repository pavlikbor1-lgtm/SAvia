# main.py
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
from aiogram.filters import Command, CallbackQuery
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery as CallbackQueryType
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
    except Exception:
        return None

# ================== DB ==================
DB_PATH = "alerts.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts (user_id, origin, destination, start_date, end_date, adults, threshold) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, origin, destination, start_date, end_date, adults, threshold),
        )
        await db.commit()

async def get_alerts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, user_id, origin, destination, start_date, end_date, adults, threshold FROM alerts") as cur:
            return await cur.fetchall()

async def delete_alert(alert_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
        await db.commit()

# ================== FSM ==================
class SearchFlight(StatesGroup):
    origin = State()
    destination = State()
    date1 = State()
    date2 = State()
    adults = State()

# ================== KEYBOARDS ==================
def get_main_menu():
    """Главное меню бота"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск билетов", callback_data="search_flights")],
        [InlineKeyboardButton(text="🔔 Управление оповещениями", callback_data="manage_alerts")],
        [InlineKeyboardButton(text="📋 Мои оповещения", callback_data="show_alerts")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])
    return keyboard

def get_alerts_menu():
    """Меню управления оповещениями"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать оповещение", callback_data="create_alert")],
        [InlineKeyboardButton(text="📋 Мои оповещения", callback_data="show_alerts")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    return keyboard

def get_airports_keyboard(for_destination=False):
    """Клавиатура выбора аэропортов"""
    airports = {
        "MOW": "🏛️ Москва",
        "LED": "🏰 Санкт-Петербург",
        "AER": "🏖️ Сочи",
        "MRV": "🏔️ Минеральные Воды",
        "KZN": "🕌 Казань",
        "CSY": "🌊 Чебоксары"
    }
    
    keyboard = []
    for code, name in airports.items():
        callback_data = f"dest_{code}" if for_destination else f"orig_{code}"
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    
    # Добавляем кнопку "Ввести свой вариант"
    other_callback = "dest_other" if for_destination else "orig_other"
    keyboard.append([InlineKeyboardButton(text="✏️ Ввести свой код", callback_data=other_callback)])
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_calendar_keyboard(year, month, selected_dates=None):
    """Генерирует календарь для выбора дат"""
    import calendar
    
    if selected_dates is None:
        selected_dates = []
    
    # Названия месяцев на русском
    month_names = [
        "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ]
    
    keyboard = []
    
    # Заголовок с месяцем и годом
    keyboard.append([InlineKeyboardButton(
        text=f"{month_names[month]} {year}", 
        callback_data="ignore"
    )])
    
    # Дни недели
    keyboard.append([
        InlineKeyboardButton(text="Пн", callback_data="ignore"),
        InlineKeyboardButton(text="Вт", callback_data="ignore"),
        InlineKeyboardButton(text="Ср", callback_data="ignore"),
        InlineKeyboardButton(text="Чт", callback_data="ignore"),
        InlineKeyboardButton(text="Пт", callback_data="ignore"),
        InlineKeyboardButton(text="Сб", callback_data="ignore"),
        InlineKeyboardButton(text="Вс", callback_data="ignore"),
    ])
    
    # Получаем календарь месяца
    cal = calendar.monthcalendar(year, month)
    today = datetime.now().date()
    
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                current_date = datetime(year, month, day).date()
                
                if current_date < today:
                    # Прошедшие даты - неактивны
                    row.append(InlineKeyboardButton(text="❌", callback_data="ignore"))
                elif current_date in selected_dates:
                    # Уже выбранные даты
                    row.append(InlineKeyboardButton(text=f"✅{day}", callback_data=f"date_{year}_{month}_{day}"))
                else:
                    # Доступные для выбора даты
                    row.append(InlineKeyboardButton(text=str(day), callback_data=f"date_{year}_{month}_{day}"))
        keyboard.append(row)
    
    # Навигация по месяцам
    prev_month, prev_year = (month - 1, year) if month > 1 else (12, year - 1)
    next_month, next_year = (month + 1, year) if month < 12 else (1, year + 1)
    
    keyboard.append([
        InlineKeyboardButton(text="◀️", callback_data=f"cal_{prev_year}_{prev_month}"),
        InlineKeyboardButton(text="▶️", callback_data=f"cal_{next_year}_{next_month}")
    ])
    
    # Кнопки действий
    if len(selected_dates) >= 2:
        keyboard.append([InlineKeyboardButton(text="✅ Готово", callback_data="calendar_done")])
    elif len(selected_dates) == 1:
        keyboard.append([InlineKeyboardButton(text="📅 Выберите вторую дату", callback_data="ignore")])
    else:
        keyboard.append([InlineKeyboardButton(text="📅 Выберите первую дату", callback_data="ignore")])
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ================== BOT HANDLERS ==================
@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer(
        "✈️ <b>Добро пожаловать в бота поиска авиабилетов!</b>\n\n"
        "Я помогу вам:\n"
        "🔍 Искать дешевые билеты\n"
        "🔔 Создавать оповещения о низких ценах\n"
        "📊 Отслеживать изменения цен\n\n"
        "Выберите действие:",
        reply_markup=get_main_menu()
    )

# ================== CALLBACK HANDLERS ==================
@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: CallbackQueryType):
    await callback.message.edit_text(
        "✈️ <b>Главное меню</b>\n\n"
        "Выберите действие:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "help")
async def show_help(callback: CallbackQueryType):
    help_text = (
        "📖 <b>Справка по использованию бота</b>\n\n"
        "🔍 <b>Поиск билетов:</b>\n"
        "Выберите аэропорты вылета и назначения, укажите даты поиска. "
        "Бот найдет 5 самых дешевых вариантов.\n\n"
        "🔔 <b>Оповещения:</b>\n"
        "Создайте оповещение с указанием маршрута, дат и максимальной цены. "
        "Бот будет уведомлять вас, когда найдет подходящие билеты.\n\n"
        "✈️ <b>Коды аэропортов:</b>\n"
        "• MOW - Москва\n"
        "• LED - Санкт-Петербург\n"
        "• AER - Сочи\n"
        "• MRV - Минеральные Воды\n"
        "• KZN - Казань\n"
        "• CSY - Чебоксары\n\n"
        "Или вводите любой другой IATA код аэропорта."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(help_text, reply_markup=keyboard)
    await callback.answer()

# ---------- ПОИСК БИЛЕТОВ (через кнопки) ----------
@dp.callback_query(F.data == "search_flights")
async def start_search_callback(callback: CallbackQueryType, state: FSMContext):
    await callback.message.edit_text(
        "🛫 <b>Поиск авиабилетов</b>\n\n"
        "Выберите аэропорт отправления:",
        reply_markup=get_airports_keyboard(for_destination=False)
    )
    await state.set_state(SearchFlight.origin)
    await callback.answer()

@dp.callback_query(F.data.startswith("orig_"))
async def handle_origin_selection(callback: CallbackQueryType, state: FSMContext):
    airport_code = callback.data.split("_", 1)[1]
    
    if airport_code == "other":
        await callback.message.edit_text(
            "✏️ Введите код аэропорта отправления (например: SVO, VKO, DME):"
        )
        await callback.answer()
        return
    
    await state.update_data(origin=airport_code)
    await callback.message.edit_text(
        f"✅ Отправление: <b>{airport_code}</b>\n\n"
        "🛬 Выберите аэропорт назначения:",
        reply_markup=get_airports_keyboard(for_destination=True)
    )
    await state.set_state(SearchFlight.destination)
    await callback.answer()

@dp.message(SearchFlight.origin)
async def handle_origin_text(message: Message, state: FSMContext):
    origin = message.text.strip().upper()
    if len(origin) != 3:
        await message.answer("❌ Код аэропорта должен состоять из 3 букв. Попробуйте еще раз:")
        return
    
    await state.update_data(origin=origin)
    await message.answer(
        f"✅ Отправление: <b>{origin}</b>\n\n"
        "🛬 Выберите аэропорт назначения:",
        reply_markup=get_airports_keyboard(for_destination=True)
    )
    await state.set_state(SearchFlight.destination)

@dp.callback_query(F.data.startswith("dest_"))
async def handle_destination_selection(callback: CallbackQueryType, state: FSMContext):
    airport_code = callback.data.split("_", 1)[1]
    
    if airport_code == "other":
        await callback.message.edit_text(
            "✏️ Введите код аэропорта назначения (например: LED, KZN, AER):"
        )
        await callback.answer()
        return
    
    data = await state.get_data()
    origin = data.get("origin")
    if origin and airport_code == origin:
        await callback.answer("❌ Аэропорт назначения не может совпадать с отправлением!", show_alert=True)
        return
    
    await state.update_data(destination=airport_code, selected_dates=[])
    
    # Показываем календарь
    now = datetime.now()
    await callback.message.edit_text(
        f"✅ Маршрут: <b>{origin} → {airport_code}</b>\n\n"
        "📅 Выберите даты поездки (сначала дата вылета, затем дата возвращения):",
        reply_markup=get_calendar_keyboard(now.year, now.month, [])
    )
    await state.set_state(SearchFlight.date1)
    await callback.answer()

@dp.message(SearchFlight.destination)
async def handle_destination_text(message: Message, state: FSMContext):
    destination = message.text.strip().upper()
    if len(destination) != 3:
        await message.answer("❌ Код аэропорта должен состоять из 3 букв. Попробуйте еще раз:")
        return
    
    data = await state.get_data()
    if destination == data.get("origin"):
        await message.answer("❌ Аэропорт назначения не может совпадать с отправлением! Попробуйте еще раз:")
        return
    
    await state.update_data(destination=destination, selected_dates=[])
    
    # Показываем календарь
    now = datetime.now()
    await message.answer(
        f"✅ Маршрут: <b>{data['origin']} → {destination}</b>\n\n"
        "📅 Выберите даты поездки (сначала дата вылета, затем дата возвращения):",
        reply_markup=get_calendar_keyboard(now.year, now.month, [])
    )
    await state.set_state(SearchFlight.date1)

@dp.callback_query(F.data.startswith("cal_"))
async def handle_calendar_navigation(callback: CallbackQueryType, state: FSMContext):
    try:
        _, year_str, month_str = callback.data.split("_")
        year, month = int(year_str), int(month_str)
    except Exception:
        await callback.answer()
        return
    
    data = await state.get_data()
    selected_dates = data.get("selected_dates", [])
    
    await callback.message.edit_reply_markup(
        reply_markup=get_calendar_keyboard(year, month, selected_dates)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("date_"))
async def handle_date_selection(callback: CallbackQueryType, state: FSMContext):
    try:
        _, year, month, day = callback.data.split("_")
        selected_date = datetime(int(year), int(month), int(day)).date()
    except Exception:
        await callback.answer()
        return
    
    data = await state.get_data()
    selected_dates = data.get("selected_dates", [])
    
    # Normalize list of date objects
    selected_dates = [d if isinstance(d, datetime) else d for d in selected_dates]
    
    if selected_date in selected_dates:
        # Убираем дату если уже выбрана
        selected_dates.remove(selected_date)
    else:
        # Добавляем дату
        selected_dates.append(selected_date)
        selected_dates.sort()
        
        # Ограничиваем выбор двумя датами
        if len(selected_dates) > 2:
            selected_dates = selected_dates[:2]
    
    await state.update_data(selected_dates=selected_dates)
    
    await callback.message.edit_reply_markup(
        reply_markup=get_calendar_keyboard(int(year), int(month), selected_dates)
    )
    await callback.answer()

@dp.callback_query(F.data == "calendar_done")
async def handle_calendar_done(callback: CallbackQueryType, state: FSMContext):
    data = await state.get_data()
    selected_dates = data.get("selected_dates", [])
    
    if not selected_dates or len(selected_dates) < 2:
        await callback.answer("❌ Выберите две даты!", show_alert=True)
        return
    
    date1, date2 = sorted(selected_dates)
    # Сохраняем даты в стейт (строки) если нужно, но далее используем date объекты
    await state.update_data(date1=str(date1), date2=str(date2))
    # Забираем origin/destination до очистки
    origin = data.get("origin")
    destination = data.get("destination")
    await state.clear()
    
    await callback.message.edit_text(
        f"🔍 <b>Поиск билетов...</b>\n\n"
        f"Маршрут: {origin} → {destination}\n"
        f"Даты: {date1} - {date2}\n\n"
        "⏳ Выполняю поиск, сейчас покажу лучшие варианты."
    )
    
    flights = await search_range(origin, destination, date1, date2, 1)
    
    if not flights:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_flights")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(
            "😔 <b>Билетов не найдено</b>\n\n"
            "Попробуйте изменить даты или маршрут.",
            reply_markup=keyboard
        )
        await callback.answer()
        return
    
    # Показываем результаты
    flights = sorted(flights, key=lambda x: x.get("price", 999999))[:5]
    
    results_text = f"✅ <b>Топ {len(flights)} найденных билетов:</b>\n\n"
    for i, f in enumerate(flights, 1):
        results_text += (
            f"<b>{i}. {f.get('origin')} → {f.get('destination')}</b>\n"
            f"📅 {f.get('departure_at')}\n"
            f"💰 {f.get('price')} ₽\n"
            f"🛫 {f.get('airline', '—')}\n"
            f"🔗 <a href='https://www.aviasales.ru{f.get('link', '')}'>Купить билет</a>\n\n"
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_flights")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(results_text, reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()

@dp.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQueryType, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Поиск отменен.\n\n"
        "Выберите действие:",
        reply_markup=get_main_menu()
    )
    await callback.answer()

# ---------- УПРАВЛЕНИЕ ОПОВЕЩЕНИЯМИ (кнопки) ----------
@dp.callback_query(F.data == "manage_alerts")
async def manage_alerts_menu(callback: CallbackQueryType):
    await callback.message.edit_text(
        "🔔 <b>Управление оповещениями</b>\n\n"
        "Оповещения помогают отслеживать цены на билеты. "
        "Когда цена опустится ниже указанного порога, вы получите уведомление.\n\n"
        "Выберите действие:",
        reply_markup=get_alerts_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "create_alert")
async def create_alert_callback(callback: CallbackQueryType):
    await callback.message.edit_text(
        "➕ <b>Создание оповещения</b>\n\n"
        "Используйте команду в следующем формате:\n\n"
        "<code>/alert ORIG DEST YYYY-MM-DD YYYY-MM-DD ADULTS ЦЕНА</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>/alert MOW LED 2025-12-01 2025-12-15 1 8000</code>\n\n"
        "Где:\n"
        "• ORIG - код аэропорта отправления\n"
        "• DEST - код аэропорта назначения\n"
        "• Первая дата - начало периода поиска\n"
        "• Вторая дата - конец периода поиска\n"
        "• ADULTS - количество взрослых\n"
        "• ЦЕНА - максимальная цена в рублях",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "show_alerts")
async def show_alerts_callback(callback: CallbackQueryType):
    alerts = await get_alerts()
    user_alerts = [a for a in alerts if a[1] == callback.from_user.id]
    
    if not user_alerts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать оповещение", callback_data="create_alert")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(
            "📋 <b>Ваши оповещения</b>\n\n"
            "У вас пока нет активных оповещений.\n"
            "Создайте первое оповещение, чтобы отслеживать цены на билеты!",
            reply_markup=keyboard
        )
        await callback.answer()
        return
    
    text = "📋 <b>Ваши активные оповещения:</b>\n\n"
    for i, alert in enumerate(user_alerts, 1):
        id_, user_id, origin, destination, start_date, end_date, adults, threshold = alert
        text += (
            f"<b>{i}. {origin} → {destination}</b>\n"
            f"📅 {start_date} — {end_date}\n"
            f"👥 {adults} adults\n"
            f"💰 до {threshold} ₽\n"
            f"🆔 ID: {id_}\n\n"
        )
    
    text += "\nДля удаления оповещения используйте:\n<code>/cancel ID</code>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать еще", callback_data="create_alert")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ---------- ТЕКСТОВЫЕ КОМАНДЫ (совместимость) ----------
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
        # Поддерживаем формат с 6 или 7 частями (старый вариант без adults или с adults)
        if len(parts) not in (6, 7):
            raise ValueError("Неверное количество параметров")
        # формат /alert ORIG DEST YYYY-MM-DD YYYY-MM-DD [ADULTS] PRICE
        if len(parts) == 6:
            _, origin, destination, d1, d2, threshold = parts
            adults = 1
        else:
            _, origin, destination, d1, d2, adults, threshold = parts
            adults = int(adults)
        start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
        threshold = int(threshold)
        
        # Проверка дат
        if start_date < datetime.now().date():
            await message.answer("❌ Начальная дата не может быть в прошлом!")
            return
        
        if end_date < start_date:
            await message.answer("❌ Конечная дата не может быть раньше начальной!")
            return
        
        await add_alert(message.from_user.id, origin.upper(), destination.upper(), str(start_date), str(end_date), adults, threshold)
        
        await message.answer(
            f"✅ <b>Оповещение создано!</b>\n\n"
            f"Маршрут: {origin.upper()} → {destination.upper()}\n"
            f"Период: {start_date} — {end_date}\n"
            f"Количество взрослых: {adults}\n"
            f"Максимальная цена: {threshold} ₽\n\n"
            "Вы получите уведомление, когда цена опустится ниже указанного порога.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        await message.answer(
            "❌ <b>Ошибка создания оповещения</b>\n\n"
            "Используйте формат:\n"
            "<code>/alert ORIG DEST YYYY-MM-DD YYYY-MM-DD ADULTS ЦЕНА</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/alert MOW LED 2025-12-01 2025-12-15 1 8000</code>\n\n"
            f"Детали ошибки: {str(e)}"
        )

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    alerts = await get_alerts()
    user_alerts = [a for a in alerts if a[1] == message.from_user.id]
    if not user_alerts:
        await message.answer("У вас нет активных оповещений")
        return
    text = "Ваши оповещения:\n"
    for a in user_alerts:
        id_, user_id, origin, destination, start_date, end_date, adults, threshold = a
        text += f"ID {id_}: {origin} → {destination}, {start_date}–{end_date}, {adults} adults, до {threshold}₽\n"
    await message.answer(text)

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    try:
        _, alert_id = message.text.split()
        await delete_alert(int(alert_id), message.from_user.id)
        await message.answer("Оповещение удалено ✅")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")

# ---------- ПРОСТОЙ ПОШАГОВЫЙ ПОИСК (через сообщения) ----------
@dp.message(Command("search_simple"))
async def start_search_simple(message: Message, state: FSMContext):
    # альтернатива: если пользователь хочет использовать старый пошаговый поиск без кнопок
    await message.answer("Введите ORIG — код аэропорта вылета (например: MOW):")
    await state.set_state(SearchFlight.origin)

# ================== WEB SERVER ==================
async def health_check(request):
    return web.Response(text="Telegram Bot is running! 🤖", status=200)

async def status_check(request):
    alerts_count = len(await get_alerts())
    me = await bot.get_me()
    return web.json_response({
        "status": "ok",
        "alerts_count": alerts_count,
        "bot_username": me.username if me else None
    })

async def create_app():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_check)
    return app

# ================== BACKGROUND TASK ==================
async def monitor_alerts():
    while True:
        alerts = await get_alerts()
        for alert in alerts:
            id_, user_id, origin, destination, d1, d2, adults, threshold = alert
            try:
                start_date, end_date = isoparse(d1).date(), isoparse(d2).date()
            except Exception:
                continue
            # Ограничение: если период прошёл — можно удалить оповещение (необязательно)
            if end_date < datetime.now().date():
                # пропускаем просроченные оповещения (можно также удалять)
                continue
            flights = await search_range(origin, destination, start_date, end_date, adults)
            for f in flights:
                price = f.get("price", 999999)
                if price <= threshold:
                    text = (
                        f"🔥 <b>Найдена низкая цена: {price} ₽</b>!\n"
                        f"✈️ {f.get('origin')} → {f.get('destination')}\n"
                        f"📅 {f.get('departure_at')}\n"
                        f"🛫 {f.get('airline', '—')}\n"
                        f"🔗 https://www.aviasales.ru{f.get('link', '')}"
                    )
                    try:
                        await bot.send_message(user_id, text, disable_web_page_preview=True)
                    except Exception:
                        # пользователь мог заблокировать бота или др. ошибка
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
