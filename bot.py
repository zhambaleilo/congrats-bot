# bot.py
import os
import asyncio
import hashlib
import httpx
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ✅ Убраны пробелы
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnBot")
PAYMENT_URL = os.getenv("PAYMENT_URL", "https://example.com")  # ✅ Валидный URL по умолчанию
ADMIN_IDS = set(os.getenv("ADMIN_IDS", "").split(","))  # ✅ Ваш ID для админ-доступа

# Логирование
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- База данных ----------
DB_PATH = "congrats.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            free_attempts_used INTEGER DEFAULT 0,
            premium_until INTEGER,
            is_blocked INTEGER DEFAULT 0,
            total_attempts INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            attempt_type TEXT,
            status TEXT,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    """)
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT free_attempts_used, premium_until, is_blocked, total_attempts FROM users WHERE user_id=?", 
        (uid,)
    )
    res = cur.fetchone()
    conn.close()
    if not res:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (uid, None, None)
        )
        conn.commit()
        conn.close()
        return {"free_attempts_used": 0, "premium_until": None, "is_blocked": 0, "total_attempts": 0}
    return {
        "free_attempts_used": res[0], 
        "premium_until": res[1], 
        "is_blocked": res[2], 
        "total_attempts": res[3]
    }

def update_user(uid, **kwargs):
    conn = sqlite3.connect(DB_PATH)
    for key, value in kwargs.items():
        conn.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, uid))
    conn.commit()
    conn.close()

def log_usage(uid, attempt_type, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO usage_log (user_id, attempt_type, status) VALUES (?, ?, ?)",
        (uid, attempt_type, status)
    )
    conn.commit()
    conn.close()

# ---------- Авто-определение типа праздника ----------
HOLIDAY_MAP = {
    "пасха": "православный", "рождество": "православный", "крещение": "православный",
    "благовещение": "православный", "петр и феврония": "православный",
    "новый год": "светский", "23 февраля": "светский", "8 марта": "светский",
    "9 мая": "светский", "день смеха": "светский", "день учителя": "светский",
    "день рождения": "личный", "годовщина": "личный", "свадьба": "личный",
    "корпоратив": "корпоративный", "юбилей компании": "корпоративный"
}

def detect_type(text):
    t = text.lower().strip()
    for key, val in HOLIDAY_MAP.items():
        if key in t:
            return val
    return "светский"

# ---------- Промпт ----------
PROMPT = """Ты — профессиональный копирайтер с 10-летним опытом. Сгенерируй тёплое, искреннее поздравление по данным:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
ТОН: {tone}
ПРАВИЛА:
Если тип="православный": тон уважительный, традиционный. Используй "Христос Воскресе", "светлого праздника". Без юмора.
Если тип="корпоративный": профессионально, с уважением, без панибратства.
Если тон="с юмором": лёгкий, добрый юмор. Без сарказма и обидных шуток.
Обязательно органично вплетай минимум 1 факт из {facts}.
Избегай клише: "счастья, здоровья, успехов, долгих лет, исполнения желаний, благополучия".
Используй живой, разговорный язык. Обращайся на "ты" (если не корпоративный).
Максимум 3-4 предложения. Без воды.
Правильные падежи и согласования.
Верни ТОЛЬКО текст поздравления. Без вступлений, пояснений и подписей."""

# ---------- FSM ----------
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    tone = State()

# ---------- Хелперы ----------
def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS

def check_access(user: dict, user_id: int) -> tuple[bool, str]:
    """Проверка доступа к генерации. Возвращает (разрешено, причина_отказа)"""
    if user["is_blocked"]:
        return False, "blocked"
    if is_admin(user_id):
        return True, "admin"
    if user["premium_until"] and user["premium_until"] > time.time():
        return True, "premium"
    if user["free_attempts_used"] < 1:
        return True, "free"
    return False, "limit_reached"

# ---------- Клавиатуры ----------
def get_tone_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤍 Душевный", callback_data="tone_soul")],
        [InlineKeyboardButton(text="📱 Для сторис", callback_data="tone_stories")],
        [InlineKeyboardButton(text="😈 С юмором", callback_data="tone_funny")]
    ])

def get_result_kb(generated_text: str):
    share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Скопировать", copy_text=generated_text)],  # ✅ One-tap copy
        [InlineKeyboardButton(text="🤖 Сделать так же", url=share_url)],
        [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)],  # ✅ Новая цена
        [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regen")]
    ])

def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_block")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])

# ---------- Хендлеры ----------
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    # Регистрация пользователя
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (m.from_user.id, m.from_user.username, m.from_user.first_name)
    )
    conn.commit()
    conn.close()
    
    welcome = (
        f"🎉 Привет, {m.from_user.first_name or 'друг'}!\n\n"
        f"Я создам живое поздравление за 10 секунд:\n"
        f"1. Напиши имя 👤\n"
        f"2. Укажи повод 🎈\n"
        f"3. Добавь 1-2 факта про человека 🤫\n"
        f"4. Выбери тон — и получи готовый текст!\n\n"
        f"🎁 Первая попытка — бесплатно.\n"
        f"🔓 Подписка 200₽/месяц: безлимит + приоритет."
    )
    await m.answer(welcome, parse_mode="HTML")
    await m.answer("✍️ <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

@dp.message(CongratsFSM.name)
async def get_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("🎈 <b>Какой повод?</b>\n(Новый год, Пасха, День рождения, корпоратив...)?", parse_mode="HTML")
    await state.set_state(CongratsFSM.occasion)

@dp.message(CongratsFSM.occasion)
async def get_occasion(m: types.Message, state: FSMContext):
    occasion = m.text.strip()
    await state.update_data(occasion=occasion, holiday_type=detect_type(occasion))
    await m.answer(
        "🤫 <b>1-2 факта/детали.</b>\nПримеры: «вечно опаздывает», «любит рыбалку», «готовит лучшие блины»",
        parse_mode="HTML"
    )
    await state.set_state(CongratsFSM.facts)

@dp.message(CongratsFSM.facts)
async def get_facts(m: types.Message, state: FSMContext):
    await state.update_data(facts=m.text.strip())
    await m.answer("Выбери тон:", reply_markup=get_tone_kb(), parse_mode="HTML")
    await state.set_state(CongratsFSM.tone)

@dp.callback_query(CongratsFSM.tone)
async def process_tone(cb: types.CallbackQuery, state: FSMContext):
    tone_map = {"tone_soul": "душевный", "tone_stories": "короткий для соцсетей", "tone_funny": "лёгкий, с юмором"}
    await state.update_data(tone=tone_map[cb.data])
    await cb.answer()
    
    uid = cb.from_user.id
    user = get_user(uid)
    
    # Проверка доступа
    allowed, reason = check_access(user, uid)
    if not allowed:
        if reason == "blocked":
            await cb.message.answer("🚫 Ваш доступ ограничен. Обратитесь к администратору.")
        else:  # limit_reached
            await cb.message.answer(
                "🎁 Бесплатная попытка использована.\n\n"
                "🔓 Подписка 200₽/мес: безлимит + озвучка + приоритет.\n"
                "После оплаты нажмите 📤 Отправить чек или напишите @admin_username",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить 200₽ (Т-Банк)", url=PAYMENT_URL)],
                    [InlineKeyboardButton(text="📤 Отправить чек", callback_data="send_receipt")]
                ])
            )
            log_usage(uid, "free", "rejected_limit")
        await state.clear()
        return
    
    await cb.message.answer("⏳ Генерирую...")
    data = await state.get_data()
    
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["tone"])
        
        # Обновляем статистику
        if not is_admin(uid) and reason == "free":
            update_user(uid, free_attempts_used=1, total_attempts=user["total_attempts"] + 1)
        elif not is_admin(uid):
            update_user(uid, total_attempts=user["total_attempts"] + 1)
        
        log_usage(uid, reason, "success")
        
        # Отправляем результат с кнопками
        await cb.message.answer(text, parse_mode="HTML")
        await cb.message.answer("👇 Действия:", reply_markup=get_result_kb(text))
        
    except httpx.HTTPStatusError as e:
        logging.error(f"Groq API error: {e.response.status_code} - {e.response.text}")
        if e.response.status_code == 429:
            msg = "⏳ Лимит запросов. Подожди 15 секунд и попробуй снова."
        else:
            msg = "⚠️ Ошибка сервера генерации. Попробуй позже."
        await cb.message.answer(msg)
        log_usage(uid, reason, "error_api")
    except asyncio.TimeoutError:
        logging.error("Groq request timed out")
        await cb.message.answer("🐢 Нейросеть не отвечает. Попробуй через минуту.")
        log_usage(uid, reason, "error_timeout")
    except Exception as e:
        logging.exception("Unhandled error in generation")
        await cb.message.answer("❌ Произошла ошибка. Админ уведомлён.")
        log_usage(uid, reason, "error_unknown")
    
    await state.clear()

@dp.callback_query(F.data == "regen")
async def regen(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.answer("Выбери тон:", reply_markup=get_tone_kb())
    await state.set_state(CongratsFSM.tone)

@dp.callback_query(F.data == "send_receipt")
async def cb_send_receipt(cb: types.CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "📤 Перешлите скриншот оплаты или напишите номер транзакции. "
        "Админ проверит и активирует премиум в течение 15 минут."
    )
    await cb.message.set_state("waiting_for_receipt")

@dp.message(StateFilter("waiting_for_receipt"))
async def process_receipt(message: types.Message):
    admin_id = next(iter(ADMIN_IDS), None)
    if admin_id:
        await bot.send_message(
            int(admin_id),
            f"🆕 Заявка на премиум:\n"
            f"👤 {message.from_user.id} (@{message.from_user.username})\n"
            f"📎 {message.text or 'Скриншот прикреплён'}"
        )
    await message.answer("✅ Заявка принята. Ожидайте активации.")
    await message.set_state(None)

# ---------- Админ-команды ----------
@dp.message(Command("admin"))
async def cmd_admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return await message.answer("🚫 Доступ запрещён.")
    
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM users WHERE is_blocked=0").fetchone()[0]
    premium = conn.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (time.time(),)).fetchone()[0]
    conn.close()
    
    await message.answer(
        f"🛡 <b>Админ-панель</b>\n\n"
        f"👥 Всего: {total}\n"
        f"✅ Активных: {active}\n"
        f"💎 Премиум: {premium}\n\n"
        f"Выберите действие:",
        reply_markup=get_admin_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫", show_alert=True)
    
    conn = sqlite3.connect(DB_PATH)
    today = time.time() - 86400
    stats = conn.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN attempt_type='free' THEN 1 ELSE 0 END) as free_attempts,
            SUM(CASE WHEN attempt_type='premium' THEN 1 ELSE 0 END) as premium_attempts,
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successful
        FROM usage_log 
        WHERE created_at > ?
    """, (today,)).fetchone()
    conn.close()
    
    await cb.message.answer(
        f"📊 <b>Статистика за 24ч:</b>\n"
        f"Всего попыток: {stats[0] or 0}\n"
        f"Бесплатных: {stats[1] or 0}\n"
        f"Премиум: {stats[2] or 0}\n"
        f"Успешных: {stats[3] or 0}",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_grant")
async def cb_admin_grant(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫", show_alert=True)
    await cb.message.edit_text("✍️ Введите ID пользователя для выдачи премиума:")
    await cb.message.set_state("admin_waiting_grant")

@dp.message(StateFilter("admin_waiting_grant"))
async def process_grant(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
        premium_until = int(time.time()) + 30 * 86400  # +30 дней
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (target_id,))
        conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (premium_until, target_id))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Пользователю {target_id} выдан премиум до {datetime.fromtimestamp(premium_until).strftime('%d.%m')}")
        await cmd_admin_panel(message)
    except ValueError:
        await message.answer("❌ Введите корректный числовой ID.")
    await message.set_state(None)

@dp.callback_query(F.data == "admin_block")
async def cb_admin_block(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫", show_alert=True)
    await cb.message.edit_text("✍️ Введите ID пользователя для блокировки:")
    await cb.message.set_state("admin_waiting_block")

@dp.message(StateFilter("admin_waiting_block"))
async def process_block(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
        update_user(target_id, is_blocked=1)
        await message.answer(f"🚫 Пользователь {target_id} заблокирован.")
        await cmd_admin_panel(message)
    except ValueError:
        await message.answer("❌ Введите корректный ID.")
    await message.set_state(None)

@dp.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(cb: types.CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫", show_alert=True)
    await cb.message.edit_text("✍️ Введите текст рассылки (или перешлите сообщение):")
    await cb.message.set_state("admin_waiting_broadcast")

@dp.message(StateFilter("admin_waiting_broadcast"))
async def process_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    text = message.text or (message.forward_from.text if message.forward_from else None)
    if not text:
        return await message.answer("❌ Не удалось получить текст для рассылки.")
    
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id FROM users WHERE is_blocked=0").fetchall()
    conn.close()
    
    sent, failed = 0, 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.5)  # Защита от лимитов Telegram
        except Exception:
            failed += 1
    
    await message.answer(f"✅ Рассылка завершена:\nОтправлено: {sent}\nОшибки: {failed}")
    await message.set_state(None)

# ---------- Groq API ----------
async def call_groq(name, occasion, holiday_type, facts, tone):
    prompt = PROMPT.format(name=name, occasion=occasion, holiday_type=holiday_type, facts=facts, tone=tone)
    cache_key = hashlib.md5(f"{name}{occasion}{holiday_type}{facts}{tone}".encode()).hexdigest()
    
    if not hasattr(call_groq, "cache"):
        call_groq.cache = {}
    if cache_key in call_groq.cache:
        return call_groq.cache[cache_key]
    
    logging.info(f"🔗 Запрос к Groq: {name} / {occasion}")
    
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
                "max_tokens": 250
            },
            timeout=15.0
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()
    
    call_groq.cache[cache_key] = result
    logging.info("✅ Ответ от Groq получен")
    return result

# ---------- Точка входа ----------
async def main():
    init_db()
    logging.info("✅ Бот запущен. Ожидает сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":  # ✅ Исправлено
    asyncio.run(main())