import os
import asyncio
import hashlib
import httpx
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# Загрузка .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnKeyBot")
PAYMENT_URL = os.getenv("PAYMENT_URL", "#")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------- База данных ----------
DB_PATH = os.path.join(os.path.dirname(__file__), "congrats.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        free_used INTEGER DEFAULT 0,
        premium_until TEXT
    )""")
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT free_used, premium_until FROM users WHERE user_id=?", (uid,))
    res = cur.fetchone()
    conn.close()
    if not res:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        return {"free_used": 0, "premium_until": None}
    return {"free_used": res[0], "premium_until": res[1]}

def set_used(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET free_used=1 WHERE user_id=?", (uid,))
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
        if key in t: return val
    return "светский"

# ---------- Промпт ----------
PROMPT = """Ты — профессиональный копирайтер. Сгенерируй поздравление по данным:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
ТОН: {tone}
ПРАВИЛА:
1. Если тип="православный": тон уважительный, традиционный. Без юмора.
2. Если тип="корпоративный": профессионально, без панибратства.
3. Обязательно вплетай минимум 1 факт.
4. Запрещены клише: "счастья, здоровья, успехов, долгих лет".
5. Максимум 3-4 предложения. Живой язык.
6. Верни ТОЛЬКО текст поздравления. Без вступлений и подписей."""

# ---------- FSM ----------
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    tone = State()

@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    get_user(m.from_user.id)
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)")
    await state.set_state(CongratsFSM.name)

@dp.message(CongratsFSM.name)
async def get_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await m.answer("🎈 <b>Какой повод?</b>\n(Новый год, Пасха, День рождения, корпоратив...)")
    await state.set_state(CongratsFSM.occasion)

@dp.message(CongratsFSM.occasion)
async def get_occasion(m: types.Message, state: FSMContext):
    await state.update_data(occasion=m.text.strip())
    await state.update_data(holiday_type=detect_type(m.text.strip()))
    await m.answer("🤫 <b>1-2 факта/детали.</b>\nПримеры: «вечно опаздывает», «любит рыбалку», «готовит лучшие блины»")
    await state.set_state(CongratsFSM.facts)

@dp.message(CongratsFSM.facts)
async def get_facts(m: types.Message, state: FSMContext):
    await state.update_data(facts=m.text.strip())
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🤍 Душевный", callback_data="tone_soul")],
        [InlineKeyboardButton("📱 Для сторис", callback_data="tone_stories")],
        [InlineKeyboardButton("😈 С юмором", callback_data="tone_funny")]
    ])
    await m.answer("Выбери тон:", reply_markup=kb)
    await state.set_state(CongratsFSM.tone)

@dp.callback_query(CongratsFSM.tone)
async def process_tone(cb: types.CallbackQuery, state: FSMContext):
    tone_map = {"tone_soul": "душевный", "tone_stories": "короткий для соцсетей", "tone_funny": "лёгкий, с юмором"}
    await state.update_data(tone=tone_map[cb.data])
    await cb.answer()

    uid = cb.from_user.id
    user = get_user(uid)
    if not user["premium_until"] and user["free_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("💳 Подписка 49₽/нед", url=PAYMENT_URL)],
            [InlineKeyboardButton("🔄 Перегенерировать", callback_data="regen")]
        ])
        await cb.message.answer("🎁 Бесплатная попытка использована.\n\n🔓 Подписка: безлимит + озвучка + приоритет")
        await state.clear()
        return

    await cb.message.answer("⏳ Генерирую...")
    data = await state.get_data()
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["tone"])
        if not user["premium_until"]:
            set_used(uid)

        await cb.message.answer(f"✅ Готово! Текст ниже — просто скопируй и отправь 👇\n\n{text}")
        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("📋 Скопировать", callback_data="copy")],
            [InlineKeyboardButton("🤖 Сделать так же", url=share_url)],
            [InlineKeyboardButton("💳 Подписка 49₽/нед", url=PAYMENT_URL)]
        ])
        await cb.message.answer("💡 Зажми сообщение с текстом → «Копировать»", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await cb.message.answer("❌ Ошибка генерации. Попробуй через минуту.")
    await state.clear()

@dp.callback_query(F.data == "copy")
async def copy_hint(cb: types.CallbackQuery):
    await cb.answer("📋 Зажми сообщение с текстом → «Копировать»", show_alert=True)

@dp.callback_query(F.data == "regen")
async def regen(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🤍 Душевный", callback_data="tone_soul")],
        [InlineKeyboardButton("📱 Для сторис", callback_data="tone_stories")],
        [InlineKeyboardButton("😈 С юмором", callback_data="tone_funny")]
    ])
    await cb.message.answer("Выбери тон:", reply_markup=kb)
    await state.set_state(CongratsFSM.tone)

async def call_groq(name, occasion, holiday_type, facts, tone):
    prompt = PROMPT.format(name=name, occasion=occasion, holiday_type=holiday_type, facts=facts, tone=tone)
    cache_key = hashlib.md5(f"{name}{occasion}{holiday_type}{facts}{tone}".encode()).hexdigest()
    if not hasattr(call_groq, "cache"): call_groq.cache = {}
    if cache_key in call_groq.cache:
        return call_groq.cache[cache_key]

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
    return result

async def main():
    init_db()
    logging.info("✅ Бот запущен. Ожидает сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())