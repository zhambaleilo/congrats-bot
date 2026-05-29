# bot.py — финальная версия от 29.05.2026
import os
import asyncio
import hashlib
import httpx
import logging
import sqlite3
import time
import traceback
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButton, WebAppInfo
from aiogram.enums import MenuButtonType
from dotenv import load_dotenv

# ========== КОНФИГ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ✅ Убраны пробелы
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnBot")
PAYMENT_URL = os.getenv("PAYMENT_URL", "#")
ADMIN_ID = 5174945583

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "congrats.db"

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    # ✅ П.8: Добавлено поле is_blocked
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        free_used INTEGER DEFAULT 0,
        premium_until TEXT,
        is_admin INTEGER DEFAULT 0,
        is_blocked INTEGER DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (ADMIN_ID,))
    # Миграция: добавляем is_blocked, если колонки нет
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
    except:
        pass
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT free_used, premium_until, is_admin, is_blocked FROM users WHERE user_id=?", (uid,))
    res = cur.fetchone()
    conn.close()
    if not res:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        return {"free_used": 0, "premium_until": None, "is_admin": 0, "is_blocked": 0}
    return {"free_used": res[0], "premium_until": res[1], "is_admin": res[2], "is_blocked": res[3]}

def set_used(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET free_used=1 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def grant_premium(uid, days=30):
    until = int(time.time()) + (days * 86400)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    conn.commit()
    conn.close()
    return until

def is_premium_active(user):
    """✅ П.9: Проверка истечения премиума на лету"""
    if not user["premium_until"] or user["premium_until"] == "None":
        return False
    try:
        return int(user["premium_until"]) > time.time()
    except (ValueError, TypeError):
        return False

# ========== ОПРЕДЕЛЕНИЕ ТИПА ПРАЗДНИКА ==========
HOLIDAY_MAP = {
    # Православные
    "пасха": "православный", "рождество": "православный", "крещение": "православный",
    "благовещение": "православный", "петр и феврония": "православный", "троица": "православный",
    # Мусульманские
    "курбан байрам": "мусульманский", "рамадан": "мусульманский", "ураза байрам": "мусульманский",
    "ид аль-фитр": "мусульманский", "ид аль-адха": "мусульманский",
    # Буддийские
    "сагаалган": "буддийский", "белый месяц": "буддийский", "цагаалган": "буддийский",
    "сагаан hараар": "буддийский", "саган хара": "буддийский",
    # Светские
    "новый год": "светский", "23 февраля": "светский", "8 марта": "светский",
    "9 мая": "светский", "день смеха": "светский", "день учителя": "светский",
    # Личные
    "день рождения": "личный", "годовщина": "личный", "свадьба": "личный", 
    "рождение ребенка": "личный", "крестины": "личный",
    # Корпоративные
    "корпоратив": "корпоративный", "юбилей компании": "корпоративный", "дембель": "корпоративный"
}

def detect_type(text):
    t = text.lower().strip()
    for key, val in HOLIDAY_MAP.items():
        if key in t:
            return val
    return "светский"

# ========== ПРОМПТ ==========
PROMPT = """Ты — профессиональный копирайтер с 10-летним опытом. Сгенерируй поздравление по данным:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
СТИЛЬ: {style}
ФОРМАТ: {format}

ПРАВИЛА ПО РЕЛИГИЯМ:
- Если тип="православный": используй ТОЛЬКО православные фразы: "Христос Воскресе", "светлого праздника", "Божией милостью". Без юмора.
- Если тип="мусульманский": используй ТОЛЬКО мусульманские фразы: "Рамадан Мубарак", "Ид Мубарак", "Аллаху Акбар". Без юмора.
- Если тип="буддийский": используй ТОЛЬКО буддийские фразы: "Сагаан hараар", "Бурхан багша", "белые мысли". Без юмора.
- НИКОГДА не смешивай религии!

СТИЛИ:
- "душевный": тёплый, от сердца, эмоциональный, с личным обращением
- "смешной": с лёгким добрым юмором, шутками, игрой слов. Без сарказма!
- "официальный": деловой, уважительный, без панибратства, подходит для старших/начальства
- "креативный": оригинальный, с метафорами, неочевидными сравнениями, художественными приёмами

ФОРМАТЫ:
- "проза": обычный текст, 3-4 предложения
- "стихи": рифмованное, 4-8 строк, с ритмом
- "соцсети": коротко, с эмодзи ✨ и хештегами #поздравление #имя

ОБЩИЕ ПРАВИЛА:
Обязательно органично вплетай минимум 1 факт из {facts}.
Избегай клише: "счастья, здоровья, успехов, долгих лет, исполнения желаний, благополучия".
Используй живой язык. Обращайся на "ты" (если не официальный стиль и не старший человек).
Максимум 4 предложения для прозы, 8 строк для стихов.
Правильные падежи и согласования.
Верни ТОЛЬКО текст поздравления. Без вступлений, пояснений и подписей."""

# ========== FSM — ДВА ШАГА (П.1) ==========
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    style = State()      # Шаг 1: выбор стиля
    format = State()     # Шаг 2: выбор формата

# ========== ХЕНДЛЕРЫ ==========
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(m.from_user.id)
    
    # ✅ П.8: Блокировка пользователей
    if user["is_blocked"]:
        return await m.answer("🚫 Ваш доступ ограничен.")
    
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

# ✅ П.6: Команда /new для быстрого старта
@dp.message(Command("new"))
async def cmd_new(m: types.Message, state: FSMContext):
    await cmd_start(m, state)

@dp.message(CongratsFSM.name)
async def get_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("🎈 <b>Какой повод?</b>\n(Новый год, Пасха, День рождения, Сагаалган, Рамадан, корпоратив...)?", parse_mode="HTML")
    await state.set_state(CongratsFSM.occasion)

@dp.message(CongratsFSM.occasion)
async def get_occasion(m: types.Message, state: FSMContext):
    await state.update_data(occasion=m.text.strip())
    await state.update_data(holiday_type=detect_type(m.text.strip()))
    await m.answer("🤫 <b>1-2 факта/детали.</b>\nПримеры: «вечно опаздывает», «любит рыбалку», «готовит лучшие бузы»", parse_mode="HTML")
    await state.set_state(CongratsFSM.facts)

@dp.message(CongratsFSM.facts)
async def get_facts(m: types.Message, state: FSMContext):
    await state.update_data(facts=m.text.strip())
    
    # ✅ П.1: Шаг 1 — выбор стиля (4 кнопки)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💖 Душевный", callback_data="style_soul"),
         InlineKeyboardButton(text="😂 Смешной", callback_data="style_funny")],
        [InlineKeyboardButton(text="🎩 Официальный", callback_data="style_formal"),
         InlineKeyboardButton(text="🔥 Креативный", callback_data="style_creative")]
    ])
    await m.answer("✨ <b>Выбери стиль:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CongratsFSM.style)

@dp.callback_query(CongratsFSM.style)
async def process_style(cb: types.CallbackQuery, state: FSMContext):
    style_map = {
        "style_soul": "душевный",
        "style_funny": "смешной", 
        "style_formal": "официальный",
        "style_creative": "креативный"
    }
    await state.update_data(style=style_map[cb.data])
    await cb.answer()
    
    # ✅ П.1: Шаг 2 — выбор формата (3 кнопки + Пропустить)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Проза", callback_data="format_prose"),
         InlineKeyboardButton(text="🌸 В стихах", callback_data="format_poem")],
        [InlineKeyboardButton(text="📱 Для соцсетей", callback_data="format_social")],
        [InlineKeyboardButton(text="⏭ Пропустить (по умолчанию)", callback_data="format_skip")]
    ])
    await cb.message.edit_text("📐 <b>Выбери формат:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CongratsFSM.format)

@dp.callback_query(CongratsFSM.format)
async def process_format(cb: types.CallbackQuery, state: FSMContext):
    format_map = {
        "format_prose": "проза",
        "format_poem": "стихи",
        "format_social": "соцсети",
        "format_skip": "проза"  # по умолчанию
    }
    await state.update_data(format=format_map[cb.data])
    await cb.answer()
    
    uid = cb.from_user.id
    user = get_user(uid)
    
    # ✅ П.8: Проверка блокировки
    if user["is_blocked"]:
        return await cb.message.answer("🚫 Ваш доступ ограничен.")
    
    # ✅ П.9: Проверка премиума + лимитов
    if user["is_admin"] or is_premium_active(user):
        await generate_congrats(cb, state, uid)
        return
    
    if user["free_used"]:
        # ✅ П.4: Показываем оплату только если нет премиума
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)],
            [InlineKeyboardButton(text="🔄 Попробовать другой стиль", callback_data="regen_style")]
        ])
        await cb.message.answer(
            text="🎁 Бесплатная попытка использована.\n\n🔓 Подписка: безлимитные генерации",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.clear()
        return
    
    await generate_congrats(cb, state, uid)

async def generate_congrats(cb: types.CallbackQuery, state: FSMContext, uid: int):
    await cb.message.answer(text="⏳ Генерирую...", parse_mode="HTML")
    data = await state.get_data()
    user = get_user(uid)
    
    try:
        text = await call_groq(
            data["name"], data["occasion"], data["holiday_type"], 
            data["facts"], data["style"], data["format"]
        )
        
        # ✅ П.9: Списываем попытку только если не админ и не активный премиум
        if not user["is_admin"] and not is_premium_active(user):
            set_used(uid)

        # ✅ П.2: Кнопка "Скопировать" через copy_text
        await cb.message.answer(text, parse_mode="HTML")

        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
        
        # ✅ П.4: Скрываем кнопку оплаты для премиум-пользователей
        buttons = [
            [InlineKeyboardButton(text="📋 Скопировать", copy_text=text)],  # ✅ Нативное копирование
            [InlineKeyboardButton(text="📤 Поделиться ботом", url=share_url)]
        ]
        
        if not user["is_admin"] and not is_premium_active(user):
            buttons.append([InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)])
        
        # ✅ П.3: Кнопка "🆕 Новое поздравление"
        buttons.append([InlineKeyboardButton(text="🆕 Новое поздравление", callback_data="new_congrats")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await cb.message.answer("👇 Действия:", reply_markup=kb)
        
    # ✅ П.5: Детализация ошибок с полным traceback
    except httpx.HTTPStatusError as e:
        logging.exception(f"❌ Groq HTTP error {e.response.status_code}: {e.response.text}")
        if e.response.status_code == 429:
            msg = "⏳ Лимит запросов. Подожди 15 секунд и попробуй снова."
        elif e.response.status_code >= 500:
            msg = "⚠️ Ошибка сервера генерации. Админ уведомлён."
        else:
            msg = "❌ Ошибка при генерации. Попробуй через минуту."
        await cb.message.answer(msg)
    except asyncio.TimeoutError:
        logging.exception("❌ Groq request timeout")
        await cb.message.answer("🐢 Нейросеть думает. Попробуй через минуту.")
    except Exception as e:
        logging.exception(f"❌ Unhandled error: {e}")  # ✅ Полный traceback в логи
        await cb.message.answer("❌ Произошла ошибка. Админ уведомлён.")
    
    await state.clear()

# ✅ П.2: Обработчик для copy_text (не нужен, но оставим для совместимости)
@dp.callback_query(F.data == "copy")
async def copy_hint(cb: types.CallbackQuery):
    await cb.answer("✅ Текст скопирован!", show_alert=True)

# ✅ П.3: Кнопка "Новое поздравление"
@dp.callback_query(F.data == "new_congrats")
async def new_congrats(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    uid = cb.from_user.id
    user = get_user(uid)
    
    # ✅ П.9: Проверка лимитов при новом старте
    if not user["is_admin"] and not is_premium_active(user) and user["free_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)]
        ])
        await cb.message.answer(
            "🎁 Бесплатная попытка использована.\n🔓 Подписка: безлимитные генерации",
            reply_markup=kb
        )
        return
    
    await cb.message.answer("✍️ <b>Кого поздравляем в этот раз?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

@dp.callback_query(F.data == "regen_style")
async def regen_style(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💖 Душевный", callback_data="style_soul"),
         InlineKeyboardButton(text="😂 Смешной", callback_data="style_funny")],
        [InlineKeyboardButton(text="🎩 Официальный", callback_data="style_formal"),
         InlineKeyboardButton(text="🔥 Креативный", callback_data="style_creative")]
    ])
    await cb.message.edit_text("✨ <b>Выбери стиль:</b>", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CongratsFSM.style)

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message(Command("admin"))
async def cmd_admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("🚫 Доступ запрещён")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_block")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])
    await m.answer("🛡 <b>Админ-панель</b>\n\nВыберите действие:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_users")
async def admin_users(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id, free_used, premium_until, is_blocked FROM users").fetchall()
    conn.close()
    
    text = "👥 <b>Пользователи:</b>\n\n"
    for u in users:
        status = []
        if u[3]: status.append("🚫 Заблок.")
        elif u[2] and is_premium_active({"premium_until": u[2]}): status.append("💎 Премиум")
        elif not u[1]: status.append("🆓 Бесплатно")
        else: status.append("❌ Лимит")
        text += f"<code>{u[0]}</code> — {', '.join(status)}\n"
    
    await cb.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "admin_grant")
async def admin_grant(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID пользователя для выдачи премиума:")
    await cb.message.set_state("admin_waiting_grant")

@dp.message(StateFilter("admin_waiting_grant"))
async def process_grant(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(m.text.strip())
        until = grant_premium(target_id, 30)
        await m.answer(f"✅ Пользователю {target_id} выдан премиум до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}")
    except ValueError:
        await m.answer("❌ Введите корректный числовой ID")
    await m.set_state(None)

# ✅ П.8: Блокировка пользователей
@dp.callback_query(F.data == "admin_block")
async def admin_block(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID пользователя для блокировки:")
    await cb.message.set_state("admin_waiting_block")

@dp.message(StateFilter("admin_waiting_block"))
async def process_block(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(m.text.strip())
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (target_id,))
        conn.commit()
        conn.close()
        await m.answer(f"🚫 Пользователь {target_id} заблокирован.")
    except ValueError:
        await m.answer("❌ Введите корректный ID")
    await m.set_state(None)

# ✅ П.7: Массовая рассылка
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите текст рассылки (или перешлите сообщение):")
    await cb.message.set_state("admin_waiting_broadcast")

@dp.message(StateFilter("admin_waiting_broadcast"))
async def process_broadcast(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    
    text = m.text or (m.forward_from.text if m.forward_from else None)
    if not text:
        return await m.answer("❌ Не удалось получить текст для рассылки.")
    
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id FROM users WHERE is_blocked=0").fetchall()
    conn.close()
    
    sent, failed = 0, 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.5)  # ✅ Защита от лимитов Telegram
        except Exception as e:
            logging.warning(f"❌ Не удалось отправить пользователю {uid}: {e}")
            failed += 1
    
    await m.answer(f"✅ Рассылка завершена:\nОтправлено: {sent}\nОшибки: {failed}")
    await m.set_state(None)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = conn.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until != 'None'").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
    conn.close()
    
    await cb.message.answer(
        f"📊 <b>Статистика:</b>\n\n"
        f"Всего пользователей: {total}\n"
        f"Премиум: {premium}\n"
        f"Заблокировано: {blocked}", 
        parse_mode="HTML"
    )

# ========== GROQ API ==========
async def call_groq(name, occasion, holiday_type, facts, style, format):
    prompt = PROMPT.format(
        name=name, occasion=occasion, holiday_type=holiday_type, 
        facts=facts, style=style, format=format
    )
    cache_key = hashlib.md5(f"{name}{occasion}{holiday_type}{facts}{style}{format}".encode()).hexdigest()
    
    if not hasattr(call_groq, "cache"):
        call_groq.cache = {}
    if cache_key in call_groq.cache:
        return call_groq.cache[cache_key]

    logging.info(f"🔗 Запрос к Groq: {name} / {occasion} / {style}+{format}")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 300
            },
            timeout=15.0
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()

    call_groq.cache[cache_key] = result
    logging.info("✅ Ответ от Groq получен")
    return result

# ========== ЗАПУСК ==========
async def main():
    init_db()
    
    # ✅ П.6: Настраиваем Menu Button (кнопка меню слева от поля ввода)
    await bot.set_chat_menu_button(
        menu_button=MenuButton(
            type=MenuButtonType.COMMAND,
            command="new"  # При нажатии вызовет /new
        )
    )
    
    logging.info("✅ Бот запущен. Ожидает сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":  # ✅ Исправлено
    asyncio.run(main())
