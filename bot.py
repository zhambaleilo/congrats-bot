import os
import asyncio
import hashlib
import httpx
import logging
import sqlite3
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# ========== КОНФИГ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
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
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        free_used INTEGER DEFAULT 0,
        premium_until TEXT,
        is_admin INTEGER DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (ADMIN_ID,))
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT free_used, premium_until, is_admin FROM users WHERE user_id=?", (uid,))
    res = cur.fetchone()
    conn.close()
    if not res:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        return {"free_used": 0, "premium_until": None, "is_admin": 0}
    return {"free_used": res[0], "premium_until": res[1], "is_admin": res[2]}

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
PROMPT = """Ты — профессиональный копирайтер с 10-летним опытом. Сгенерируй тёплое, искреннее поздравление по данным:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
ТОН: {tone}

ПРАВИЛА ПО РЕЛИГИЯМ:
- Если тип="православный": используй ТОЛЬКО православные фразы: "Христос Воскресе", "светлого праздника", "Божией милостью", "спаси Господи". Без юмора.
- Если тип="мусульманский": используй ТОЛЬКО мусульманские фразы: "Рамадан Мубарак", "Ид Мубарак", "Аллаху Акбар", "да примет Аллах". Без юмора.
- Если тип="буддийский": используй ТОЛЬКО буддийские фразы: "Сагаан hараар", "Бурхан багша", "благопожелания", "белые мысли". Без юмора.
- НИКОГДА не смешивай религии! Для буддийского праздника не пиши "Христос Воскресе"!

ОБЩИЕ ПРАВИЛА:
Если тип="корпоративный": профессионально, с уважением, без панибратства.
Если тон="с юмором": лёгкий, добрый юмор. Без сарказма и обидных шуток.
Обязательно органично вплетай минимум 1 факт из {facts}.
Избегай клише: "счастья, здоровья, успехов, долгих лет, исполнения желаний, благополучия".
Используй живой, разговорный язык. Обращайся на "ты" (если не корпоративный и не старший человек).
Максимум 3-4 предложения. Без воды.
Правильные падежи и согласования.
Верни ТОЛЬКО текст поздравления. Без вступлений, пояснений и подписей."""

# ========== FSM ==========
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    tone = State()

# ========== ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ==========
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    get_user(m.from_user.id)
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤍 Душевный", callback_data="tone_soul")],
        [InlineKeyboardButton(text="📱 Для сторис", callback_data="tone_stories")],
        [InlineKeyboardButton(text="😈 С юмором", callback_data="tone_funny")]
    ])
    await m.answer("Выбери тон:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(CongratsFSM.tone)

@dp.callback_query(CongratsFSM.tone)
async def process_tone(cb: types.CallbackQuery, state: FSMContext):
    tone_map = {"tone_soul": "душевный", "tone_stories": "короткий для соцсетей", "tone_funny": "лёгкий, с юмором"}
    await state.update_data(tone=tone_map[cb.data])
    await cb.answer()
    uid = cb.from_user.id
    user = get_user(uid)

    if user["is_admin"] or (user["premium_until"] and user["premium_until"] != "None"):
        await generate_congrats(cb, state, uid)
        return

    if user["free_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)],
            [InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regen")]
        ])
        await cb.message.answer(
            text="🎁 Бесплатная попытка использована.\n\n🔓 Подписка: безлимитные генерации",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.clear()
        return

    await generate_congrats(cb, state, uid)

async def generate_congrats(cb, state, uid):
    await cb.message.answer(text="⏳ Генерирую...", parse_mode="HTML")
    data = await state.get_data()
    user = get_user(uid)
    
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["tone"])
        if not user["is_admin"] and not (user["premium_until"] and user["premium_until"] != "None"):
            set_used(uid)

        # ✅ ИСПРАВЛЕНО: Отправляем текст с возможностью копирования
        await cb.message.answer(text, parse_mode="HTML")

        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Скопировать текст", callback_data="copy")],
            [InlineKeyboardButton(text="📤 Поделиться ботом", url=share_url)],
            [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)]
        ])
        await cb.message.answer("👇 Действия:", reply_markup=kb)
    except Exception as e:
        logging.error(f"❌ Ошибка генерации: {e}", exc_info=True)
        await cb.message.answer(text="❌ Ошибка. Попробуй через минуту.", parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data == "copy")
async def copy_hint(cb: types.CallbackQuery):
    await cb.answer("💡 На ПК: выделите текст и нажмите Ctrl+C\n📱 На телефоне: долгое нажатие → Копировать", show_alert=True)

@dp.callback_query(F.data == "regen")
async def regen(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤍 Душевный", callback_data="tone_soul")],
        [InlineKeyboardButton(text="📱 Для сторис", callback_data="tone_stories")],
        [InlineKeyboardButton(text="😈 С юмором", callback_data="tone_funny")]
    ])
    await cb.message.answer("Выбери тон:", reply_markup=kb)
    await state.set_state(CongratsFSM.tone)

# ========== АДМИН-ПАНЕЛЬ ==========
@dp.message(Command("admin"))
async def cmd_admin(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return await m.answer("🚫 Доступ запрещён")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])
    await m.answer("🛡 <b>Админ-панель</b>\n\nВыберите действие:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_users")
async def admin_users(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id, free_used, premium_until FROM users").fetchall()
    conn.close()
    
    text = "👥 <b>Пользователи:</b>\n\n"
    for u in users:
        status = "💎 Премиум" if u[2] and u[2] != "None" else ("🆓 Бесплатно" if not u[1] else "❌ Лимит")
        text += f"<code>{u[0]}</code> — {status}\n"
    
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

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = conn.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until != 'None'").fetchone()[0]
    conn.close()
    
    await cb.message.answer(f"📊 <b>Статистика:</b>\n\nВсего пользователей: {total}\nПремиум: {premium}", parse_mode="HTML")

# ========== GROQ API ==========
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

# ========== ЗАПУСК ==========
async def main():
    init_db()
    logging.info("✅ Бот запущен. Ожидает сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
