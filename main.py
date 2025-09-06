# main.py
# -------------------------
# Configuration & constants
# -------------------------

import os
import asyncio
import httpx
import aiosqlite
import logging
from datetime import datetime, timedelta
from dateutil.parser import isoparse

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery as CallbackQueryType
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# Добавляем aiohttp для веб-сервера
from aiohttp import web

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
TP_CURRENCY = os.getenv("TP_CURRENCY", "rub")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 мин
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))
PORT = int(os.getenv("PORT", "10000"))  # Render.com использует переменную PORT

if not TELEGRAM_BOT_TOKEN or not TRAVELPAYOUTS_TOKEN:
    logger.error("Missing required environment variables: TELEGRAM_BOT_TOKEN or TRAVELPAYOUTS_TOKEN")
    exit(1)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ================== HELPERS ==================
async def fetch_flights(origin, destination, date, adults=1, retries=3):
    """Получение рейсов с retry механизмом и обработкой ошибок"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": date,
        "adults": adults,
        "currency": TP_CURRENCY,
        "token": TRAVELPAYOUTS_TOKEN,
    }
    
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, params=params)
                
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    logger.info(f"Successfully fetched {len(data)} flights for {origin}->{destination} on {date}")
                    return data
                elif resp.status_code == 429:  # Rate limit
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limit hit, waiting {wait_time}s (attempt {attempt + 1})")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"API error {resp.status_code} for {origin}->{destination} on {date}")
                    return []
                    
        except httpx.TimeoutException:
            logger.warning(f"Timeout for {origin}->{destination} on {date} (attempt {attempt + 1})")
        except Exception as e:
            logger.error(f"Request failed for {origin}->{destination} on {date} (attempt {attempt + 1}): {e}")
        
        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)
    
    logger.error(f"All {retries} attempts failed for {origin}->{destination} on {date}")
    return []

async def search_range(origin, destination, start_date, end_date, adults=1):
    """Поиск рейсов в диапазоне дат"""
    results = []
    current_date = start_date
    
    logger.info(f"Searching flights {origin}->{destination} from {start_date} to {end_date}")
    
    while current_date <= end_date:
        flights = await fetch_flights(origin, destination, current_date.isoformat(), adults)
        for f in flights:
            f["search_date"] = current_date.isoformat()
        results.extend(flights)
        await asyncio.sleep(RATE_LIMIT_MS / 1000)
        current_date += timedelta(days=1)
    
    logger.info(f"Found {len(results)} total flights in range")
    return results

def validate_date(date_str: str) -> Optional[datetime.date]:
    """Проверка формата даты и что дата не в прошлом"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d < datetime.today().date():
            return None
        return d
    except Exception:
        return None

def validate_airport_code(code: str) -> Optional[str]:
    """Валидация кода аэропорта"""
    if not code or len(code.strip()) != 3:
        return None
    return code.strip().upper()

# ================== DB ==================
DB_PATH = "alerts.db"

async def init_db():
    """Инициализация базы данных с индексами"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                adults INTEGER NOT NULL DEFAULT 1,
                threshold INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
            
            # Добавляем индексы для оптимизации
            await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_user_id ON alerts(user_id)
            """)
            await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_alerts_dates ON alerts(start_date, end_date)
            """)
            
            await db.commit()
            logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

async def add_alert(user_id, origin, destination, start_date, end_date, adults, threshold):
    """Добавление оповещения с обработкой ошибок"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO alerts (user_id, origin, destination, start_date, end_date, adults, threshold) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, origin, destination, start_date, end_date, adults, threshold),
            )
            await db.commit()
            logger.info(f"Alert added for user {user_id}: {origin}->{destination}")
    except Exception as e:
        logger.error(f"Failed to add alert for user {user_id}: {e}")
        raise

async def get_alerts():
    """Получение всех активных оповещений"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT id, user_id, origin, destination, start_date, end_date, adults, threshold FROM alerts") as cur:
                alerts = await cur.fetchall()
                logger.info(f"Retrieved {len(alerts)} alerts from database")
                return alerts
    except Exception as e:
        logger.error(f"Failed to get alerts: {e}")
        return []

async def delete_alert(alert_id, user_id):
    """Удаление оповещения"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
            await db.commit()
            if cursor.rowcount > 0:
                logger.info(f"Alert {alert_id} deleted for user {user_id}")
                return True
            return False
    except Exception as e:
        logger.error(f"Failed to delete alert {alert_id} for user {user_id}: {e}")
        return False

async def cleanup_expired_alerts():
    """Удаление просроченных оповещений"""
    try:
        today = datetime.now().date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("DELETE FROM alerts WHERE end_date < ?", (today,))
            await db.commit()
            if cursor.rowcount > 0:
                logger.info(f"Cleaned up {cursor.rowcount} expired alerts")
    except Exception as e:
        logger.error(f"Failed to cleanup expired alerts: {e}")

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
    
    # Преобразуем все даты в date объекты для консистентности
    normalized_dates = []
    for d in selected_dates:
        if isinstance(d, str):
            try:
                normalized_dates.append(datetime.fromisoformat(d).date())
            except:
                continue
        elif isinstance(d, datetime):
            normalized_dates.append(d.date())
        elif hasattr(d, 'date'):  # datetime.date
            normalized_dates.append(d)
    
    selected_dates = normalized_dates
    
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
    logger.info(f"User {message.from_user.id} started the bot")
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
    origin = validate_airport_code(message.text)
    if not origin:
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
    destination = validate_airport_code(message.text)
    if not destination:
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
    except (ValueError, IndexError):
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
        _, year_str, month_str, day_str = callback.data.split("_")
        selected_date = datetime(int(year_str), int(month_str), int(day_str)).date()
    except (ValueError, IndexError):
        await callback.answer()
        return
    
    data = await state.get_data()
    selected_dates = data.get("selected_dates", [])
    
    # Нормализуем все даты к date объектам
    normalized_dates = []
    for d in selected_dates:
        if isinstance(d, str):
            try:
                normalized_dates.append(datetime.fromisoformat(d).date())
            except:
                continue
        elif isinstance(d, datetime):
            normalized_dates.append(d.date())
        elif hasattr(d, 'date') and callable(getattr(d, 'date')):
            normalized_dates.append(d.date())
        else:
            normalized_dates.append(d)  # уже date объект
    
    if selected_date in normalized_dates:
        # Убираем дату если уже выбрана
        normalized_dates.remove(selected_date)
    else:
        # Добавляем дату
        normalized_dates.append(selected_date)
        normalized_dates.sort()
        
        # Ограничиваем выбор двумя датами
        if len(normalized_dates) > 2:
            normalized_dates = normalized_dates[:2]
    
    await state.update_data(selected_dates=normalized_dates)
    
    await callback.message.edit_reply_markup(
        reply_markup=get_calendar_keyboard(int(year_str), int(month_str), normalized_dates)
    )
    await callback.answer()

@dp.callback_query(F.data == "calendar_done")
async def handle_calendar_done(callback: CallbackQueryType, state: FSMContext):
    data = await state.get_data()
    selected_dates = data.get("selected_dates", [])
    
    # Нормализуем даты
    normalized_dates = []
    for d in selected_dates:
        if isinstance(d, str):
            try:
                normalized_dates.append(datetime.fromisoformat(d).date())
            except:
                continue
        elif isinstance(d, datetime):
            normalized_dates.append(d.date())
        else:
            normalized_dates.append(d)
    
    if not normalized_dates or len(normalized_dates) < 2:
        await callback.answer("❌ Выберите две даты!", show_alert=True)
        return
    
    date1, date2 = sorted(normalized_dates)
    origin = data.get("origin")
    destination = data.get("destination")
    
    await state.clear()
    
    await callback.message.edit_text(
        f"🔍 <b>Поиск билетов...</b>\n\n"
        f"Маршрут: {origin} → {destination}\n"
        f"Даты: {date1} - {date2}\n\n"
        "⏳ Выполняю поиск, сейчас покажу лучшие варианты."
    )
    
    try:
        flights = await search_range(origin, destination, date1, date2, 1)
        
        if not flights:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_flights")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ])
            
            await callback.message.edit_text(
                "😔 <b>Билетов не найдено</b>\n\n"
                "Попробуйте изменить даты или маршрут.\n\n"
                "Возможные причины:\n"
                "• Нет рейсов на выбранные даты\n"
                "• Неверные коды аэропортов\n"
                "• Временные проблемы с API",
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
                f"💰 {f.get('price', 'N/A')} ₽\n"
                f"🛫 {f.get('airline', '—')}\n"
                f"🔗 <a href='https://www.aviasales.ru{f.get('link', '')}'>Купить билет</a>\n\n"
            )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_flights")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(results_text, reply_markup=keyboard, disable_web_page_preview=True)
        
    except Exception as e:
        logger.error(f"Error during flight search: {e}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Попробовать снова", callback_data="search_flights")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(
            "❌ <b>Ошибка поиска</b>\n\n"
            "Произошла ошибка при поиске билетов. Попробуйте еще раз.",
            reply_markup=keyboard
        )
    
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
    try:
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
                f"👥 {adults} взрослых\n"
                f"💰 до {threshold} ₽\n"
                f"🆔 ID: {id_}\n\n"
            )
        
        text += "\nДля удаления оповещения используйте:\n<code>/cancel ID</code>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать еще", callback_data="create_alert")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error showing alerts for user {callback.from_user.id}: {e}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "❌ Ошибка при загрузке оповещений. Попробуйте позже.",
            reply_markup=keyboard
        )
    
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
    user_id = message.from_user.id
    logger.info(f"User {user_id} creating alert: {message.text}")
    
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
            _, origin, destination, d1, d2, adults_str, threshold = parts
            adults = int(adults_str)
            if adults < 1 or adults > 9:
                raise ValueError("Количество взрослых должно быть от 1 до 9")
        
        # Валидация аэропортов
        origin = validate_airport_code(origin)
        destination = validate_airport_code(destination)
        if not origin or not destination:
            raise ValueError("Неверный формат кода аэропорта")
        
        if origin == destination:
            raise ValueError("Аэропорты отправления и назначения не могут совпадать")
        
        # Валидация дат
        try:
            start_date = isoparse(d1).date()
            end_date = isoparse(d2).date()
        except:
            raise ValueError("Неверный формат даты. Используйте YYYY-MM-DD")
        
        # Проверка дат
        today = datetime.now().date()
        if start_date < today:
            raise ValueError("Начальная дата не может быть в прошлом")
        
        if end_date < start_date:
            raise ValueError("Конечная дата не может быть раньше начальной")
        
        # Проверка диапазона дат (не более 30 дней)
        if (end_date - start_date).days > 30:
            raise ValueError("Период поиска не может превышать 30 дней")
        
        # Валидация цены
        threshold = int(threshold)
        if threshold <= 0:
            raise ValueError("Цена должна быть положительным числом")
        
        if threshold > 1000000:
            raise ValueError("Максимальная цена не может превышать 1,000,000 рублей")
        
        await add_alert(user_id, origin, destination, str(start_date), str(end_date), adults, threshold)
        
        await message.answer(
            f"✅ <b>Оповещение создано!</b>\n\n"
            f"Маршрут: {origin} → {destination}\n"
            f"Период: {start_date} — {end_date}\n"
            f"Количество взрослых: {adults}\n"
            f"Максимальная цена: {threshold:,} ₽\n\n"
            "Вы получите уведомление, когда цена опустится ниже указанного порога.",
            reply_markup=get_main_menu()
        )
        
    except ValueError as e:
        await message.answer(
            f"❌ <b>Ошибка создания оповещения</b>\n\n"
            f"Проблема: {str(e)}\n\n"
            "Используйте формат:\n"
            "<code>/alert ORIG DEST YYYY-MM-DD YYYY-MM-DD ADULTS ЦЕНА</code>\n\n"
            "<b>Пример:</b>\n"
            "<code>/alert MOW LED 2025-12-01 2025-12-15 1 8000</code>"
        )
    except Exception as e:
        logger.error(f"Error creating alert for user {user_id}: {e}")
        await message.answer(
            "❌ <b>Внутренняя ошибка</b>\n\n"
            "Не удалось создать оповещение. Попробуйте позже или обратитесь к администратору."
        )

@dp.message(Command("alerts"))
async def alerts_cmd(message: Message):
    user_id = message.from_user.id
    try:
        alerts = await get_alerts()
        user_alerts = [a for a in alerts if a[1] == user_id]
        
        if not user_alerts:
            await message.answer(
                "📋 У вас нет активных оповещений.\n\n"
                "Создайте оповещение командой /alert или через меню.",
                reply_markup=get_main_menu()
            )
            return
        
        text = f"📋 <b>Ваши оповещения ({len(user_alerts)}):</b>\n\n"
        for a in user_alerts:
            id_, user_id, origin, destination, start_date, end_date, adults, threshold = a
            text += f"🆔 <b>ID {id_}</b>: {origin} → {destination}\n📅 {start_date} — {end_date}\n👥 {adults} взрослых, 💰 до {threshold:,}₽\n\n"
        
        text += "Для удаления используйте: <code>/cancel ID</code>"
        await message.answer(text, reply_markup=get_main_menu())
        
    except Exception as e:
        logger.error(f"Error getting alerts for user {user_id}: {e}")
        await message.answer("❌ Ошибка при загрузке оповещений.")

@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    user_id = message.from_user.id
    try:
        parts = message.text.split()
        if len(parts) != 2:
            raise ValueError("Неверный формат команды")
        
        alert_id = int(parts[1])
        success = await delete_alert(alert_id, user_id)
        
        if success:
            await message.answer(
                f"✅ Оповещение {alert_id} удалено.",
                reply_markup=get_main_menu()
            )
            logger.info(f"User {user_id} deleted alert {alert_id}")
        else:
            await message.answer(
                f"❌ Оповещение {alert_id} не найдено или не принадлежит вам.",
                reply_markup=get_main_menu()
            )
            
    except ValueError:
        await message.answer(
            "❌ Используйте формат: <code>/cancel ID</code>\n"
            "Например: <code>/cancel 123</code>"
        )
    except Exception as e:
        logger.error(f"Error deleting alert for user {user_id}: {e}")
        await message.answer("❌ Ошибка при удалении оповещения.")

# ---------- ПРОСТОЙ ПОШАГОВЫЙ ПОИСК (через сообщения) ----------
@dp.message(Command("search_simple"))
async def start_search_simple(message: Message, state: FSMContext):
    # альтернатива: если пользователь хочет использовать старый пошаговый поиск без кнопок
    await message.answer("Введите ORIG — код аэропорта вылета (например: MOW):")
    await state.set_state(SearchFlight.origin)

# ---------- ОБРАБОТКА НЕИЗВЕСТНЫХ CALLBACK'ОВ ----------
@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQueryType):
    await callback.answer()

# ================== WEB SERVER ==================
async def health_check(request):
    return web.Response(text="Telegram Bot is running! 🤖", status=200)

async def status_check(request):
    try:
        alerts_count = len(await get_alerts())
        me = await bot.get_me()
        return web.json_response({
            "status": "ok",
            "alerts_count": alerts_count,
            "bot_username": me.username if me else None,
            "uptime": "running"
        })
    except Exception as e:
        logger.error(f"Status check error: {e}")
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

# ================== BACKGROUND TASK ==================
async def monitor_alerts():
    """Фоновый мониторинг оповещений с улучшенной обработкой ошибок"""
    logger.info("Alert monitoring started")
    consecutive_errors = 0
    max_consecutive_errors = 5
    
    while True:
        try:
            # Очищаем просроченные оповещения
            await cleanup_expired_alerts()
            
            alerts = await get_alerts()
            logger.info(f"Checking {len(alerts)} alerts")
            
            for alert in alerts:
                try:
                    id_, user_id, origin, destination, d1, d2, adults, threshold = alert
                    
                    # Валидация дат
                    try:
                        start_date = isoparse(d1).date()
                        end_date = isoparse(d2).date()
                    except:
                        logger.error(f"Invalid dates in alert {id_}: {d1}, {d2}")
                        continue
                    
                    # Пропускаем просроченные оповещения
                    if end_date < datetime.now().date():
                        continue
                    
                    # Ограничиваем поиск текущей датой и будущими
                    search_start = max(start_date, datetime.now().date())
                    if search_start > end_date:
                        continue
                    
                    flights = await search_range(origin, destination, search_start, end_date, adults)
                    
                    # Проверяем найденные рейсы
                    matching_flights = []
                    for f in flights:
                        price = f.get("price")
                        if price and price <= threshold:
                            matching_flights.append(f)
                    
                    # Уведомляем пользователя о найденных дешевых билетах
                    for f in matching_flights[:3]:  # Максимум 3 уведомления за раз
                        price = f.get("price", 0)
                        text = (
                            f"🔥 <b>Найдена низкая цена: {price:,} ₽</b>!\n\n"
                            f"✈️ {f.get('origin')} → {f.get('destination')}\n"
                            f"📅 {f.get('departure_at')}\n"
                            f"🛫 {f.get('airline', '—')}\n\n"
                            f"💰 Ваш лимит: {threshold:,} ₽\n"
                            f"💸 Экономия: {threshold - price:,} ₽\n\n"
                            f"🔗 <a href='https://www.aviasales.ru{f.get('link', '')}'>Купить билет</a>"
                        )
                        
                        try:
                            await bot.send_message(
                                user_id, 
                                text, 
                                disable_web_page_preview=True,
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                                ])
                            )
                            logger.info(f"Alert sent to user {user_id} for flight {origin}->{destination} at {price}₽")
                            
                            # Небольшая задержка между отправками
                            await asyncio.sleep(1)
                            
                        except Exception as send_error:
                            logger.warning(f"Failed to send alert to user {user_id}: {send_error}")
                            # Пользователь мог заблокировать бота или удалить аккаунт
                    
                    # Задержка между проверкой разных оповещений
                    await asyncio.sleep(2)
                    
                except Exception as alert_error:
                    logger.error(f"Error processing alert {id_}: {alert_error}")
                    continue
            
            # Сброс счетчика ошибок при успешном выполнении
            consecutive_errors = 0
            logger.info(f"Alert check completed. Next check in {POLL_INTERVAL_SECONDS} seconds")
            
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Alert monitoring error ({consecutive_errors}/{max_consecutive_errors}): {e}")
            
            # Если слишком много ошибок подряд, увеличиваем интервал
            if consecutive_errors >= max_consecutive_errors:
                extended_interval = POLL_INTERVAL_SECONDS * 2
                logger.warning(f"Too many consecutive errors, extending interval to {extended_interval} seconds")
                await asyncio.sleep(extended_interval)
                consecutive_errors = 0  # Сброс после продолжительной паузы
                continue
        
        # Обычная пауза между проверками
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

# ================== ERROR HANDLERS ==================
@dp.error()
async def error_handler(event, error):
    """Глобальный обработчик ошибок"""
    logger.error(f"Unhandled error: {error}")
    
    if event.update.message:
        try:
            await event.update.message.answer(
                "❌ Произошла ошибка. Попробуйте позже или используйте /start для перезапуска.",
                reply_markup=get_main_menu()
            )
        except:
            pass
    elif event.update.callback_query:
        try:
            await event.update.callback_query.message.edit_text(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=get_main_menu()
            )
            await event.update.callback_query.answer()
        except:
            pass
    
    return True

# ================== MAIN ==================
async def main():
    """Главная функция запуска"""
    try:
        # Проверка обязательных переменных окружения
        if not TELEGRAM_BOT_TOKEN:
            logger.error("TELEGRAM_BOT_TOKEN not set!")
            return
        
        if not TRAVELPAYOUTS_TOKEN:
            logger.error("TRAVELPAYOUTS_TOKEN not set!")
            return
        
        logger.info("Starting Telegram Bot...")
        logger.info(f"Poll interval: {POLL_INTERVAL_SECONDS} seconds")
        logger.info(f"Rate limit: {RATE_LIMIT_MS} ms")
        logger.info(f"Port: {PORT}")
        
        # Инициализация базы данных
        await init_db()
        
        # Запускаем мониторинг alerts в фоне
        monitor_task = asyncio.create_task(monitor_alerts())
        
        # Создаем и запускаем веб-сервер
        app = await create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        
        logger.info(f"Starting web server on port {PORT}")
        
        # Запускаем сервер и бота одновременно
        await asyncio.gather(
            site.start(),
            dp.start_polling(bot, handle_signals=False),
            monitor_task
        )
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Critical error: {e}")
        raise
    finally:
        logger.info("Bot shutdown")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
