import os
import asyncio
import hashlib
import httpx
import logging
import sqlite3
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand
from dotenv import load_dotenv

# ========== КОНФИГ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnBot")

# ✅ НОВЫЙ АДМИН
ADMIN_ID = 8858718958
ADMIN_USERNAME = "@AdBotov"

# ✅ НОВАЯ ССЫЛКА НА ОПЛАТУ
CURRENT_PAYMENT_URL = "https://www.tinkoff.ru/rm/r_fywFPJfgmN.idgWOeMoBc/Kjf5q79974"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "congrats.db"

def get_payment_link():
    return CURRENT_PAYMENT_URL or f"Ссылка временно недоступна. Напишите админу {ADMIN_USERNAME}"

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
        conn.execute("ALTER TABLE users ADD COLUMN is_sandbox INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN sandbox_role TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN last_reminder INTEGER DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT free_used, premium_until, is_admin, is_blocked, is_sandbox, sandbox_role, last_reminder FROM users WHERE user_id=?", (uid,))
    res = cur.fetchone()
    conn.close()
    if not res:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        return {"free_used": 0, "premium_until": None, "is_admin": 0, "is_blocked": 0, "is_sandbox": 0, "sandbox_role": None, "last_reminder": 0}
    return {
        "free_used": res[0], "premium_until": res[1], "is_admin": res[2], 
        "is_blocked": res[3], "is_sandbox": res[4], "sandbox_role": res[5], "last_reminder": res[6] or 0
    }

def set_used(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET free_used=free_used+1 WHERE user_id=?", (uid,))  # ✅ +1 вместо =1
    conn.commit()
    conn.close()

def grant_premium(uid, days=30):
    until = int(time.time()) + (days * 86400)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    conn.commit()
    conn.close()
    return until

def set_last_reminder(uid):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET last_reminder=? WHERE user_id=?", (int(time.time()), uid))
    conn.commit()
    conn.close()

def is_premium_active(user):
    if not user["premium_until"] or str(user["premium_until"]) == "None":
        return False
    try:
        return int(user["premium_until"]) > time.time()
    except (ValueError, TypeError):
        return False

def check_user_access(uid):
    user = get_user(uid)
    if user["is_blocked"]:
        return {"can_generate": False, "reason": "blocked"}

    if user.get("is_sandbox"):
        role = user.get("sandbox_role")
        now = int(time.time())
        if role == "new_user":
            return {"can_generate": True, "is_premium": False, "free_used": 0}
        elif role == "limit_used":
            return {"can_generate": False, "is_premium": False, "free_used": 1, "reason": "limit"}
        elif role == "premium_active":
            return {"can_generate": True, "is_premium": True, "premium_until": now + 30*86400}
        elif role == "premium_3days":
            return {"can_generate": True, "is_premium": True, "premium_until": now + 3*86400, "show_reminder": True}
        elif role == "premium_tomorrow":
            return {"can_generate": True, "is_premium": True, "premium_until": now + 1*86400, "show_reminder": True}
        elif role == "premium_today":
            return {"can_generate": True, "is_premium": True, "premium_until": now + 12*3600, "show_reminder": True}
        elif role == "premium_expired":
            return {"can_generate": False, "is_premium": False, "premium_until": now - 86400, "reason": "expired"}
        elif role == "awaiting_check":
            return {"can_generate": False, "is_premium": False, "reason": "awaiting_check"}

    is_prem = is_premium_active(user)
    free_used = user["free_used"]

    if user["is_admin"] or is_prem:
        show_reminder = False
        if is_prem:
            time_left = int(user["premium_until"]) - int(time.time())
            if 0 < time_left <= 3 * 86400:
                if int(time.time()) - user.get("last_reminder", 0) > 86400:
                    show_reminder = True
        return {
            "can_generate": True, "is_premium": is_prem, "free_used": free_used, 
            "show_reminder": show_reminder, "premium_until": user["premium_until"]
        }

    if free_used >= 3:  # ✅ изменить число здесь
        return {"can_generate": False, "is_premium": False, "free_used": free_used, "reason": "limit"}

    return {"can_generate": True, "is_premium": False, "free_used": 0}

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
PROMPT = """Ты — профессиональный копирайтер. Сгенерируй поздравление:
ДАННЫЕ:
ИМЯ: {name}
ПОВОД: {occasion}
ТИП: {holiday_type}
ФАКТЫ: {facts}
СТИЛЬ: {style}
КРИТИЧЕСКИ ВАЖНО:
Если пользователь запрашивает поздравление с теми же параметрами повторно, ОБЯЗАТЕЛЬНО создай совершенно новый текст. Не повторяй предыдущие фразы, меняй структуру, метафоры, ритм и порядок мыслей. Каждый ответ должен быть уникальным.
ПРАВИЛА ПО РЕЛИГИЯМ:
Если тип="православный": ТОЛЬКО "Христос Воскресе", "светлого праздника" и т.д. Без юмора.
Если тип="мусульманский": ТОЛЬКО "Рамадан Мубарак", "Ид Мубарак" и т.д.. Без юмора.
Если тип="буддийский": ТОЛЬКО "Сагаан hараар", "Бурхан багша" и т.д. Без юмора.
НЕ СМЕШИВАЙ РЕЛИГИИ!
СТИЛИ:
"душевный": тёплый, эмоциональный, от сердца
"смешной": добрый юмор, шутки, игра слов
"официальный": деловой, уважительный
"креативный": с метафорами, оригинальными сравнениями
ОБРАБОТКА ЗНАМЕНИТОСТЕЙ:
Если имя известное (Киркоров, Бузова, Билли Айлиш и т.д.), добавь 1 упоминание их профессии
Используй торжественный тон для знаменитостей
ОБЩИЕ ПРАВИЛА:
Обязательно вплетай факты из {facts}
Избегай клише: "счастья, здоровья, успехов"
Верни ТОЛЬКО готовый текст. Без пояснений.
Избегай политические темы."""

# ========== FSM ==========
class CongratsFSM(StatesGroup):
    name = State()
    occasion = State()
    facts = State()
    style = State()

class PaymentFSM(StatesGroup):
    waiting_check = State()

class AdminFSM(StatesGroup):
    waiting_grant = State()
    waiting_block = State()
    waiting_broadcast = State()
    waiting_new_link = State()

# ========== ПРИОРИТЕТНЫЕ ХЕНДЛЕРЫ ==========
@dp.message(Command("start", "new", "cancel", "admin", "pay"))
async def cmd_priority(m: types.Message, state: FSMContext):
    await state.clear()
    
    if m.text == "/cancel":
        return await m.answer("❌ Сценарий отменён. Напишите /start, чтобы начать заново.")
        
    if m.text == "/admin":
        if m.from_user.id != ADMIN_ID:
            return await m.answer("🚫 Доступ запрещён")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant")],
            [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_block")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🧪 Тестирование доступа", callback_data="admin_sandbox")],
            [InlineKeyboardButton(text="🔗 Обновить ссылку оплаты", callback_data="admin_update_link")]
        ])
        return await m.answer("🛡 <b>Админ-панель</b>", reply_markup=kb, parse_mode="HTML")

    # ✅ ИСПРАВЛЕНИЕ 1: Команда /pay теперь устанавливает состояние для приёма чека
    if m.text == "/pay":
        link = get_payment_link()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", url=link)]])
        await m.answer(
            "💳 <b>Вы перешли к оплате.</b>\n\n"
            "После оплаты отправьте скриншот чека в этот чат.\n"
            f"Наш администратор ({ADMIN_USERNAME}) проверит платёж и активирует премиум.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.set_state(PaymentFSM.waiting_check)
        return

    user = get_user(m.from_user.id)
    if user["is_blocked"]:
        return await m.answer("🚫 Ваш доступ ограничен.")
    
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)

# ========== ОСНОВНОЙ СЦЕНАРИЙ ==========
@dp.message(CongratsFSM.name)
async def get_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    
    uid = m.from_user.id
    access = check_user_access(uid)
    
    if not access["can_generate"]:
        if access["reason"] == "blocked":
            await m.answer("🚫 Ваш доступ ограничен.")
        elif access["reason"] == "limit":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Подписка 200₽/мес", callback_data="action_pay")]])
            remaining = 3 - access["free_used"]  # ✅ добавить
            await m.answer(f"🎁 Осталось бесплатных попыток: {remaining}\n\n🔓 Подписка: безлимит", reply_markup=kb)  # ✅ изменить
        elif access["reason"] == "awaiting_check":
            await m.answer("⏳ Ваша заявка на премиум находится на проверке у админа.")
        elif access["reason"] == "expired":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить подписку", callback_data="action_pay")]])
            await m.answer("❌ Премиум истёк. Продли подписку.", reply_markup=kb)
        await state.clear()
        return
    
    await m.answer("🎈 <b>Какой повод?</b>\n(Новый год, Пасха, День рождения, Сагаалган, Рамадан, корпоратив, покупка жилья...)?", parse_mode="HTML")
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
    
    access = check_user_access(uid)
    if not access["can_generate"]:
        if access["reason"] == "blocked":
            return await cb.message.answer("🚫 Ваш доступ ограничен.")
        elif access["reason"] == "limit":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Подписка 200₽/мес", callback_data="action_pay")]])
            remaining = 3 - access["free_used"]  # ✅ добавить
            await m.answer(f"🎁 Осталось бесплатных попыток: {remaining}\n\n🔓 Подписка: безлимит", reply_markup=kb)  # ✅ изменить
            await state.clear()
            return
        elif access["reason"] == "awaiting_check":
            return await cb.message.answer("⏳ Ваша заявка на премиум находится на проверке у админа.")
        elif access["reason"] == "expired":
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить подписку", callback_data="action_pay")]])
            return await cb.message.answer("❌ Премиум истёк. Продли подписку.", reply_markup=kb)

    await generate_congrats(cb, state, uid, access)

async def generate_congrats(cb: types.CallbackQuery, state: FSMContext, uid: int, access: dict):
    await cb.message.answer(text="⏳ Генерирую...")
    data = await state.get_data()
    
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["style"])
        
        if not access["is_premium"] and access["free_used"] < 3:
            set_used(uid)

        # ✅ Разделение поздравления и напоминания на 2 сообщения
        await cb.message.answer(text, parse_mode="HTML")
        
        if access.get("show_reminder"):
            time_left = int(access["premium_until"]) - int(time.time())
            if time_left <= 86400:
                reminder_text = "⚠️ Премиум истекает сегодня. Продли сейчас:"
            elif time_left <= 2 * 86400:
                reminder_text = "⏳ Премиум истекает завтра! Продли:"
            else:
                reminder_text = f"💎 Премиум действует ещё {time_left // 86400} дн. Продлить:"
            
            set_last_reminder(uid)
            await asyncio.sleep(0.5)
            rem_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить", callback_data="action_pay")]])
            await cb.message.answer(reminder_text, reply_markup=rem_kb, parse_mode="HTML")

        await cb.message.answer("📋 <i>Скопируй текст выше</i>", parse_mode="HTML")

        # ✅ ИСПРАВЛЕНИЕ 2: Кнопка "Поделиться ботом" с простой ссылкой
        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=🎉 Бот для поздравлений — живые тексты за 10 секунд!"
        buttons = [[InlineKeyboardButton(text="📤 Поделиться ботом", url=share_url)]]
        
        if not access["is_premium"]:
            buttons.append([InlineKeyboardButton(text="💳 Подписка 200₽/мес", callback_data="action_pay")])
        
        buttons.append([InlineKeyboardButton(text="🆕 Новое поздравление", callback_data="new_congrats")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await cb.message.answer("👇 Действия:", reply_markup=kb)
        
    except httpx.HTTPStatusError as e:
        logging.exception(f"❌ Groq HTTP {e.response.status_code}: {e.response.text}")
        msg = "⏳ Лимит. Подожди 15 сек." if e.response.status_code == 429 else "⚠️ Ошибка сервера." if e.response.status_code >= 500 else "❌ Ошибка генерации."
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
    access = check_user_access(uid)
        
    if not access["can_generate"] and access.get("reason") == "limit":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Подписка 200₽/мес", callback_data="action_pay")]])
        remaining = 3 - access["free_used"]  # ✅ добавить
    return await cb.message.answer(f"🎁 Осталось бесплатных попыток: {remaining}", reply_markup=kb)  # ✅ изменить
    elif not access["can_generate"] and access.get("reason") == "expired":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить подписку", callback_data="action_pay")]])
        return await cb.message.answer("❌ Премиум истёк. Продли подписку.", reply_markup=kb)

    await cb.message.answer("✍️ <b>Новое поздравление</b>\n\nКого поздравляем? (имя)", parse_mode="HTML")
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

# ✅ ИСПРАВЛЕНИЕ 1: Callback action_pay теперь устанавливает состояние
@dp.callback_query(F.data == "action_pay")
async def action_pay(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    link = get_payment_link()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", url=link)]])
    await cb.message.answer(
        "💳 <b>Вы перешли к оплате.</b>\n\n"
        "После оплаты отправьте скриншот чека в этот чат.\n"
        f"Наш администратор ({ADMIN_USERNAME}) проверит платёж и активирует премиум.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(PaymentFSM.waiting_check)

# ========== ВАЛИДАЦИЯ ЧЕКА ==========
@dp.message(PaymentFSM.waiting_check, F.content_type.in_({'photo', 'text'}))
async def process_check(m: types.Message, state: FSMContext):
    uid = m.from_user.id
    username = m.from_user.username or "Нет юзернейма"
    date_str = datetime.now().strftime("%d.%m.%Y")
    
    admin_text = f"🆕 <b>Новая заявка на премиум:</b>\n\n👤 Пользователь: @{username} (ID: <code>{uid}</code>)\n💳 Сумма: 200₽\n📅 Дата: {date_str}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активировать премиум", callback_data=f"admin_approve_check_{uid}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_check_{uid}")]
    ])
    
    try:
        if m.photo:
            await bot.send_photo(ADMIN_ID, photo=m.photo[-1].file_id, caption=admin_text, reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_ID, text=admin_text + f"💬 Текст от пользователя:\n<code>{m.text}</code>", reply_markup=kb, parse_mode="HTML")
            
        await m.answer("✅ Чек отправлен админу! Ожидайте активации в течение 15 минут.")
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET is_sandbox=1, sandbox_role='awaiting_check' WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.exception("Ошибка отправки чека админу")
        await m.answer(f"❌ Произошла ошибка. Напишите админу {ADMIN_USERNAME}")
    
    await state.clear()

# ========== АДМИНКА ==========
@dp.callback_query(F.data == "admin_users")
async def admin_users(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT user_id, free_used, premium_until, is_blocked, is_sandbox, sandbox_role FROM users").fetchall()
    conn.close()
    text = "👥 <b>Пользователи:</b>\n\n"
    for u in users:
        status = []
        if u[3]: status.append("🚫 Заблок.")
        elif u[4] and u[5]: status.append(f"🧪 Sandbox: {u[5]}")
        elif u[2] and is_premium_active({"premium_until": u[2]}): status.append("💎 Премиум")
        elif not u[1]: status.append("🆓 Бесплатно")
        else: status.append("❌ Лимит")
        text += f"<code>{u[0]}</code> — {', '.join(status)}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 Вернуться в Админку", callback_data="admin_back")]])
    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_grant")
async def admin_grant(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID пользователя:")
    await state.set_state(AdminFSM.waiting_grant)

@dp.message(AdminFSM.waiting_grant)
async def process_grant(m: types.Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(m.text.strip())
        until = grant_premium(target_id, 30)
        await m.answer(f"✅ Премиум до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}")
    except ValueError:
        await m.answer("❌ Введите корректный ID")
    await state.clear()

@dp.callback_query(F.data == "admin_block")
async def admin_block(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID для блокировки:")
    await state.set_state(AdminFSM.waiting_block)

@dp.message(AdminFSM.waiting_block)
async def process_block(m: types.Message, state: FSMContext):
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
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите текст рассылки:")
    await state.set_state(AdminFSM.waiting_broadcast)

@dp.message(AdminFSM.waiting_broadcast)
async def process_broadcast(m: types.Message, state: FSMContext):
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
        except Exception as e:
            logging.warning(f"Не удалось отправить {uid}: {e}")
            failed += 1
        await asyncio.sleep(0.5)
    
    await m.answer(f"✅ Отправлено: {sent}\n❌ Ошибки: {failed}")
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT() FROM users").fetchone()[0]
    premium = conn.execute("SELECT COUNT() FROM users WHERE premium_until IS NOT NULL").fetchone()[0]
    blocked = conn.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
    conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 Вернуться в Админку", callback_data="admin_back")]])
    await cb.message.answer(f"📊 <b>Статистика:</b>\n\nВсего: {total}\nПремиум: {premium}\nЗаблокировано: {blocked}", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "admin_sandbox")
async def admin_sandbox(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новый юзер (0 попыток)", callback_data="sandbox_new_user")],
        [InlineKeyboardButton(text="🚫 Лимит исчерпан", callback_data="sandbox_limit_used")],
        [InlineKeyboardButton(text="💎 Премиум активен", callback_data="sandbox_premium_active")],
        [InlineKeyboardButton(text="⏳ Истекает через 3 дня", callback_data="sandbox_premium_3days")],
        [InlineKeyboardButton(text="⏳ Истекает завтра", callback_data="sandbox_premium_tomorrow")],
        [InlineKeyboardButton(text="⚠️ Истекает сегодня", callback_data="sandbox_premium_today")],
        [InlineKeyboardButton(text="❌ Подписка истекла", callback_data="sandbox_premium_expired")],
        [InlineKeyboardButton(text="💳 Ожидает чека", callback_data="sandbox_awaiting_check")],
        [InlineKeyboardButton(text="🛡 Вернуться в Админку", callback_data="admin_back")]
    ])
    await cb.message.edit_text("🧪 <b>Выберите роль для тестирования (применится к ВАШЕМУ аккаунту):</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("sandbox_"))
async def apply_sandbox(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    role = cb.data.replace("sandbox_", "")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_sandbox=1, sandbox_role=? WHERE user_id=?", (role, cb.from_user.id))
    conn.commit()
    conn.close()

    await cb.answer(f"✅ Роль '{role}' применена!", show_alert=True)
    await cb.message.edit_text(f"🧪 <b>Режим песочницы активен!</b>\nВаша текущая роль: <code>{role}</code>\n\nТеперь вы можете протестировать логику бота. Чтобы выйти, нажмите 'Вернуться в Админку'.", reply_markup=cb.message.reply_markup, parse_mode="HTML")

@dp.callback_query(F.data == "admin_update_link")
async def admin_update_link(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите новую ссылку на оплату (или 'нет', чтобы сбросить):")
    await state.set_state(AdminFSM.waiting_new_link)

@dp.message(AdminFSM.waiting_new_link)
async def process_update_link(m: types.Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    global CURRENT_PAYMENT_URL
    if m.text.strip().lower() in ["нет", "no", "-"]:
        CURRENT_PAYMENT_URL = None
        await m.answer("✅ Ссылка сброшена. Теперь пользователям будет показано сообщение с контактом админа.")
    else:
        CURRENT_PAYMENT_URL = m.text.strip()
        await m.answer(f"✅ Ссылка на оплату обновлена:\n{CURRENT_PAYMENT_URL}")
    await state.clear()

@dp.callback_query(F.data == "admin_back")
async def admin_back(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_sandbox=0, sandbox_role=NULL WHERE user_id=?", (cb.from_user.id,))
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="💎 Выдать премиум", callback_data="admin_grant")],
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data="admin_block")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🧪 Тестирование доступа", callback_data="admin_sandbox")],
        [InlineKeyboardButton(text="🔗 Обновить ссылку оплаты", callback_data="admin_update_link")]
    ])
    await cb.message.edit_text("🛡 <b>Админ-панель</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_approve_check_"))
async def admin_approve_check(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    target_uid = int(cb.data.replace("admin_approve_check_", ""))
    until = grant_premium(target_uid, 30)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_sandbox=0, sandbox_role=NULL WHERE user_id=?", (target_uid,))
    conn.commit()
    conn.close()

    try:
        await bot.send_message(
            target_uid,
            "✅ <b>Ваш премиум успешно активирован!</b>\n\n"
            f"Срок действия: до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}\n"
            "Теперь вы можете пользоваться всеми функциями без ограничений.\n\n"
            "Спасибо за оплату! 🎉",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить уведомление пользователю {target_uid}: {e}")

    await cb.answer("✅ Премиум активирован!", show_alert=True)
    await cb.message.edit_text(cb.message.text + "\n\n✅ <b>СТАТУС: ОДОБРЕНО И АКТИВИРОВАНО</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin_reject_check_"))
async def admin_reject_check(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    target_uid = int(cb.data.replace("admin_reject_check_", ""))

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_sandbox=0, sandbox_role=NULL WHERE user_id=?", (target_uid,))
    conn.commit()
    conn.close()

    await cb.answer("❌ Заявка отклонена", show_alert=True)
    await cb.message.edit_text(cb.message.text + "\n\n❌ <b>СТАТУС: ОТКЛОНЕНО</b>", parse_mode="HTML")

    try:
        await bot.send_message(target_uid, f"❌ К сожалению, ваш чек не прошёл проверку. Пожалуйста, проверьте данные и попробуйте снова или напишите админу {ADMIN_USERNAME}.")
    except:
        pass

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
    
    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands(
            text="🆕 Начать",
            command="start"
        )
    )
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="pay", description="Оформить подписку"),
        BotCommand(command="cancel", description="Отменить сценарий"),
        BotCommand(command="admin", description="Панель администратора")
    ])
    
    logging.info("✅ Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
