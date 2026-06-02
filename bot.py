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
        is_admin INTEGER DEFAULT 0,
        is_blocked INTEGER DEFAULT 0
    )""")
    conn.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (ADMIN_ID,))
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
    if not user["premium_until"] or user["premium_until"] == "None":
        return False
    try:
        return int(user["premium_until"]) > time.time()
    except (ValueError, TypeError):
        return False

# ========== ОПРЕДЕЛЕНИЕ ТИПА ПРАЗДНИКА ==========
HOLIDAY_MAP = {
    "пасха": "православный", "рождество": "православный", "крещение": "православный",
    "благовещение": "православный", "петр и феврония": "православный", "троица": "православный",
    "курбан байрам": "мусульманский", "рамадан": "мусульманский", "ураза байрам": "мусульманский",
    "сагаалган": "буддийский", "белый месяц": "буддийский", "цагаалган": "буддийский",
    "сагаан hараар": "буддийский",
    "новый год": "светский", "23 февраля": "светский", "8 марта": "светский",
    "9 мая": "светский", "день смеха": "светский", "день учителя": "светский",
    "день рождения": "личный", "годовщина": "личный", "свадьба": "личный",
    "корпоратив": "корпоративный", "юбилей компании": "корпоративный", "дембель": "корпоративный"
}

def detect_type(text):
    t = text.lower().strip()
    for key, val in HOLIDAY_MAP.items():
        if key in t:
            return val
    return "светский"

# ========== ПРОМПТ ==========
PROMPT = """Ты — профессиональный поэт и копирайтер. Сгенерируй поздравление:

ДАННЫЕ:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
СТИЛЬ: {style}

КРИТИЧЕСКИ ВАЖНО:
- Если стиль="стихи": создай НАСТОЯЩЕЕ рифмованное стихотворение!
- Используй ТОЛЬКО парную (ААББ) или перекрёстную (АБАБ) рифмовку
- Соблюдай ритм: 4-8 строк с одинаковым размером
- Каждая строка должна рифмоваться! ПРОВЕРЬ рифму перед выводом

ПРАВИЛА ПО РЕЛИГИЯМ:
- Если тип="православный": ТОЛЬКО "Христос Воскресе", "светлого праздника". Без юмора.
- Если тип="мусульманский": ТОЛЬКО "Рамадан Мубарак", "Ид Мубарак". Без юмора.
- Если тип="буддийский": ТОЛЬКО "Сагаан hараар", "Бурхан багша". Без юмора.
- НЕ СМЕШИВАЙ РЕЛИГИИ!

СТИЛИ:
- "душевный": тёплый, эмоциональный, от сердца
- "смешной": добрый юмор, шутки, игра слов
- "официальный": деловой, уважительный
- "креативный": с метафорами, оригинальными сравнениями

ОБРАБОТКА ЗНАМЕНИТОСТЕЙ:
- Если имя известное (Киркоров, Бузова и т.д.), добавь 1 упоминание их профессии
- Используй торжественный тон для знаменитостей

ОБЩИЕ ПРАВИЛА:
1. Обязательно вплетай факты из {facts}
2. Избегай клише: "счастья, здоровья, успехов"
3. Верни ТОЛЬКО готовый текст. Без пояснений."""

# ========== FSM (БЕЗ ФОРМАТА) ==========
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    style = State()

# ========== ХЕНДЛЕРЫ ==========
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(m.from_user.id)
    if user["is_blocked"]:
        return await m.answer("🚫 Ваш доступ ограничен.")
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

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
    await m.answer("🤫 <b>1-2 факта/детали.</b>\nПримеры: «вечно опаздывает», «любит рыбалку», «готовит лучшие блины»", parse_mode="HTML")
    await state.set_state(CongratsFSM.facts)

@dp.message(CongratsFSM.facts)
async def get_facts(m: types.Message, state: FSMContext):
    await state.update_data(facts=m.text.strip())
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
    style_map = {"style_soul": "душевный", "style_funny": "смешной", "style_formal": "официальный", "style_creative": "креативный"}
    await state.update_data(style=style_map[cb.data])
    await cb.answer()
    
    uid = cb.from_user.id
    user = get_user(uid)
    
    if user["is_blocked"]:
        return await cb.message.answer("🚫 Ваш доступ ограничен.")
    
    if user["is_admin"] or is_premium_active(user):
        await generate_congrats(cb, state, uid)
        return
    
    if user["free_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)],
            [InlineKeyboardButton(text="🔄 Попробовать другой стиль", callback_data="regen_style")]
        ])
        await cb.message.answer("🎁 Бесплатная попытка использована.\n\n🔓 Подписка: безлимит", reply_markup=kb)
        await state.clear()
        return
    
    await generate_congrats(cb, state, uid)

async def generate_congrats(cb: types.CallbackQuery, state: FSMContext, uid: int):
    await cb.message.answer(text="⏳ Генерирую...")
    data = await state.get_data()
    user = get_user(uid)
    
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["style"])
        
        if not user["is_admin"] and not is_premium_active(user):
            set_used(uid)

        # ✅ Отправляем текст поздравления
        await cb.message.answer(text, parse_mode="HTML")

        # ✅ Текстовая инструкция вместо кнопки "Копировать"
        await cb.message.answer("📋 <i>Скопируй текст выше</i>", parse_mode="HTML")

        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
        buttons = [
            [InlineKeyboardButton(text="📤 Поделиться ботом", url=share_url)]
        ]
        
        # Показываем оплату только НЕ премиум пользователям
        if not user["is_admin"] and not is_premium_active(user):
            buttons.append([InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)])
        
        # Кнопка нового поздравления
        buttons.append([InlineKeyboardButton(text="🆕 Новое поздравление", callback_data="new_congrats")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await cb.message.answer("👇 Действия:", reply_markup=kb)
        
    except httpx.HTTPStatusError as e:
        logging.exception(f"❌ Groq HTTP {e.response.status_code}: {e.response.text}")
        if e.response.status_code == 429:
            msg = "⏳ Лимит. Подожди 15 сек."
        elif e.response.status_code >= 500:
            msg = "⚠️ Ошибка сервера."
        else:
            msg = "❌ Ошибка генерации."
        await cb.message.answer(msg)
    except asyncio.TimeoutError:
        logging.exception("❌ Timeout Groq")
        await cb.message.answer("🐢 Нейросеть думает. Попробуй позже.")
    except Exception as e:
        logging.exception(f"❌ Unhandled: {e}")
        await cb.message.answer("❌ Ошибка. Админ уведомлён.")
    
    await state.clear()

@dp.callback_query(F.data == "new_congrats")
async def new_congrats(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    uid = cb.from_user.id
    user = get_user(uid)
    
    if not user["is_admin"] and not is_premium_active(user) and user["free_used"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=PAYMENT_URL)]])
        await cb.message.answer("🎁 Бесплатная попытка использована.", reply_markup=kb)
        return
    
    await cb.message.answer("✍️ <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
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

# ========== АДМИНКА ==========
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
    await m.answer("🛡 <b>Админ-панель</b>", reply_markup=kb, parse_mode="HTML")

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
    await cb.message.answer("✍️ Введите ID пользователя:")
    await cb.message.set_state("admin_waiting_grant")

@dp.message(StateFilter("admin_waiting_grant"))
async def process_grant(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(m.text.strip())
        until = grant_premium(target_id, 30)
        await m.answer(f"✅ Премиум до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}")
    except ValueError:
        await m.answer("❌ Введите корректный ID")
    await m.set_state(None)

@dp.callback_query(F.data == "admin_block")
async def admin_block(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID для блокировки:")
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

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите текст рассылки:")
    await cb.message.set_state("admin_waiting_broadcast")

@dp.message(StateFilter("admin_waiting_broadcast"))
async def process_broadcast(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    text = m.text
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id FROM users WHERE is_blocked=0").fetchall()
    conn.close()
    sent, failed = 0, 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.5)
        except:
            failed += 1
    await m.answer(f"✅ Отправлено: {sent}\n❌ Ошибки: {failed}")
    await m.set_state(None)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = conn.execute("SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
    conn.close()
    await cb.message.answer(f"📊 <b>Статистика:</b>\n\nВсего: {total}\nПремиум: {premium}\nЗаблокировано: {blocked}", parse_mode="HTML")

# ========== GROQ API ==========
async def call_groq(name, occasion, holiday_type, facts, style):
    prompt = PROMPT.format(name=name, occasion=occasion, holiday_type=holiday_type, facts=facts, style=style)
    cache_key = hashlib.md5(f"{name}{occasion}{holiday_type}{facts}{style}".encode()).hexdigest()
    if not hasattr(call_groq, "cache"):
        call_groq.cache = {}
    if cache_key in call_groq.cache:
        return call_groq.cache[cache_key]

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 350},
            timeout=15.0
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"].strip()

    call_groq.cache[cache_key] = result
    return result

# ========== ЗАПУСК ==========
async def main():
    init_db()
    logging.info("✅ Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
