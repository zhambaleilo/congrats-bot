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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand
from dotenv import load_dotenv

# ========== КОНФИГ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnBot")
ADMIN_USERNAME = "@zhambaleilo"  # ✅ ТЗ: Упоминание админа

# Глобальная переменная для ссылки оплаты (позволяет обновлять без перезапуска)
_raw_payment_url = os.getenv("PAYMENT_URL", "#")
CURRENT_PAYMENT_URL = _raw_payment_url if _raw_payment_url and _raw_payment_url != "#" else None

ADMIN_ID = 5174945583
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "congrats.db"

def get_payment_link():
    """✅ ТЗ: Фолбэк для ссылки оплаты"""
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
    
    # ✅ ТЗ: Добавление полей для Sandbox и напоминаний
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN is_sandbox INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN sandbox_role TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN last_reminder INTEGER DEFAULT 0")
    except Exception:
        pass  # Колонки уже существуют
        
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    # ✅ ТЗ: Запрос новых полей
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

def set_last_reminder(uid):
    """✅ ТЗ: Обновление времени последнего напоминания"""
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

# ✅ ТЗ: Единая функция проверки доступа (поддерживает Sandbox и напоминания)
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

    # Обычная логика
    is_prem = is_premium_active(user)
    free_used = user["free_used"]

    if user["is_admin"] or is_prem:
        show_reminder = False
        if is_prem:
            time_left = int(user["premium_until"]) - int(time.time())
            if 0 < time_left <= 3 * 86400:  # <= 3 дней
                if int(time.time()) - user.get("last_reminder", 0) > 86400:  # Не чаще 1 раза в 24ч
                    show_reminder = True
        return {
            "can_generate": True, "is_premium": is_prem, "free_used": free_used, 
            "show_reminder": show_reminder, "premium_until": user["premium_until"]
        }

    if free_used:
        return {"can_generate": False, "is_premium": False, "free_used": 1, "reason": "limit"}

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

# ========== FSM<think>
Here's a thinking process that leads to the final response:

1.  **Understand the Goal**: The user wants to update an existing Telegram bot (`bot (1).py`) based on a specific Technical Specification (TZ). The bot generates holiday greetings using Groq API.

2.  **Analyze the Constraints & Requirements**:
    *   **Chat Name**: "Кодирование от 02.06." (Just a mental note for context, doesn't change code).
    *   **Admin Mention**: Replace any "write to admin" or similar with `@zhambaleilo`.
    *   **Preserve Existing Logic**: Do not break existing functionality. Only modify/add where necessary.
    *   **Implement TZ Items**:
        1.  *Menu Button*: Add a persistent menu button for `/new`.
        2.  *Sandbox Mode*: Add `is_sandbox`, `sandbox_role` to DB. Add admin UI to toggle these states (New user, Limit used, Premium active, Expires in 3 days, Expires tomorrow, Expires today, Awaiting check). Add a "Return to Admin" button.
        3.  *Check Validation (Simplified)*: User sends a check (text/photo). Bot forwards to admin with Approve/Reject buttons. Admin clicks -> grants premium or rejects.
        4.  *Payment Link Fallback*: If `PAYMENT_URL` is empty/invalid, show "Ссылка временно недоступна. Напишите админу @zhambaleilo". Add "Update link" button in admin panel.
        5.  *Command Priority over FSM*: `/start`, `/admin`, `/cancel` must work *anytime*, clearing state.
        6.  *`/cancel` Command*: Clear state and inform user.
        7.  *Premium Expiration Reminders*: Check `premium_until`. If <= 3 days, show a reminder. Limit to once per 24h (need a new DB column or just track last reminder time, or keep it simple by checking if `last_reminder < now - 86400`). Let's add `last_reminder` to DB to be safe and accurate.

3.  **Step-by-Step Implementation Plan**:

    *   **Step 1: Database Updates**:
        *   Add `is_sandbox INTEGER DEFAULT 0`, `sandbox_role TEXT`, `last_reminder INTEGER DEFAULT 0` to the `users` table.
        *   Update `get_user` to return these new fields.
        *   Add helper functions for sandbox and reminders.

    *   **Step 2: Configuration & Constants**:
        *   Ensure `ADMIN_USERNAME = "@zhambaleilo"` is defined.
        *   Create a fallback function for `PAYMENT_URL`.

    *   **Step 3: Command Priority & `/cancel`**:
        *   Move `/start`, `/admin`, `/cancel` to the top of handlers or ensure they don't have `StateFilter` restricting them, and explicitly call `await state.clear()`.

    *   **Step 4: Menu Button**:
        *   Add `bot.set_chat_menu_button(...)` in `main()` or a startup handler.

    *   **Step 5: Sandbox Mode Logic**:
        *   Create a unified `check_access(uid)` function that handles both real and sandbox logic.
        *   Add admin callbacks for setting sandbox roles.

    *   **Step 6: Check Validation Flow**:
        *   Add a handler for `F.content_type.in_({ContentType.PHOTO, ContentType.TEXT})` when user is prompted to send a check, OR a specific command like `/pay` or a button "💳 Подписка" that triggers "Send check" state.
        *   Let's add a `/pay` or handle the "Подписка" button to start `PaymentFSM`.
        *   Forward the check to `ADMIN_ID` with inline buttons `✅ Активировать` / `❌ Отклонить`.
        *   Admin callback handlers to process approval/rejection.

    *   **Step 7: Premium Reminders**:
        *   In `check_access` or before generating, calculate time left.
        *   If `0 < time_left <= 3 days` and `now - last_reminder > 86400`, append reminder text and update `last_reminder`.

    *   **Step 8: Payment Link Fallback & Admin Update**:
        *   Admin panel: Add "🔗 Обновить ссылку оплаты".
        *   Function to update `PAYMENT_URL` in memory (or `.env`, but memory is easier for runtime without restart, though `.env` is better. Let's just update a global variable or a DB setting. A global variable `CURRENT_PAYMENT_URL` is easiest).

4.  **Drafting the Code Modifications** (Mental or Scratchpad):

    *   *DB Init*:
        ```python
        def init_db():
            # ... existing ...
            try:
                conn.execute("ALTER TABLE users ADD COLUMN is_sandbox INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE users ADD COLUMN sandbox_role TEXT")
                conn.execute("ALTER TABLE users ADD COLUMN last_reminder INTEGER DEFAULT 0")
            except: pass
        ```
    *   *Global Vars*:
        ```python
        ADMIN_USERNAME = "@zhambaleilo"
        CURRENT_PAYMENT_URL = PAYMENT_URL if PAYMENT_URL and PAYMENT_URL != "#" else None
        def get_payment_link():
            return CURRENT_PAYMENT_URL or f"Ссылка временно недоступна. Напишите админу {ADMIN_USERNAME}"
        ```
    *   *Access Check*:
        ```python
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
                    return {"can_generate": True, "is_premium": True, "premium_until": now, "show_reminder": True} # Actually <=0 means expired, but let's say it expires in a few hours
                elif role == "premium_expired":
                    return {"can_generate": False, "is_premium": False, "premium_until": now - 86400, "reason": "expired"}
                elif role == "awaiting_check":
                    return {"can_generate": False, "is_premium": False, "reason": "awaiting_check"}

            # Normal logic
            is_prem = is_premium_active(user)
            free_used = user["free_used"]

            if user["is_admin"] or is_prem:
                # Check reminder
                show_reminder = False
                if is_prem:
                    time_left = int(user["premium_until"]) - int(time.time())
                    if 0 < time_left <= 3 * 86400:
                        if int(time.time()) - user.get("last_reminder", 0) > 86400:
                            show_reminder = True
                            # update last_reminder in DB here or later
                return {"can_generate": True, "is_premium": is_prem, "free_used": free_used, "show_reminder": show_reminder, "premium_until": user["premium_until"]}

            if free_used:
                return {"can_generate": False, "is_premium": False, "free_used": 1, "reason": "limit"}

            return {"can_generate": True, "is_premium": False, "free_used": 0}
        ```
    *   *Wait, the DB update for `last_reminder` needs to happen.* Let's add `set_last_reminder(uid)`.

    *   *Payment Flow*:
        Need a state `PaymentFSM` or just use a specific state in `CongratsFSM`? Better to make a separate `PaymentFSM` or handle it via a command `/pay` or button.
        Let's add `class PaymentFSM(StatesGroup): waiting_for_check = State()`
        When user clicks "💳 Подписка", it asks for check.
        User sends text/photo. Bot forwards to admin.

    *   *Admin Check Approval*:
        Callback `admin_approve_check_{uid}` -> `grant_premium(uid)`, notify user.
        Callback `admin_reject_check_{uid}` -> notify user.

    *   *Priority Commands*:
        ```python
        @dp.message(Command("start", "new", "cancel", "admin"))
        async def cmd_priority(m: types.Message, state: FSMContext):
            await state.clear()
            if m.text == "/start" or m.text == "/new":
                # ... start logic
            elif m.text == "/cancel":
                await m.answer("❌ Сценарий отменён. Напишите /start, чтобы начать заново.")
            elif m.text == "/admin":
                # ... admin logic
        ```
        *Correction*: The prompt says "Хендлеры `/start` и `/admin` должны работать всегда... Зарегистрировать их ПЕРЕД хендлерами состояний". I will place them at the top.

    *   *Menu Button*:
        ```python
        from aiogram.types import MenuButtonCommands, BotCommand
        # in main():
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands(text="🆕 Новое поздравление", command="new"))
        await bot.set_my_commands([BotCommand(command="start", description="Запустить бота"), BotCommand(command="new", description="Новое поздравление"), BotCommand(command="cancel", description="Отменить текущий сценарий"), BotCommand(command="pay", description="Оформить подписку")])
        ```

    *   *Refining the Sandbox Admin UI*:
        Add button "🧪 Тестирование доступа" in `/admin`.
        Show inline keyboard with roles.
        Handler sets `is_sandbox=1` and `sandbox_role=...` in DB.
        Button "🛡 Вернуться в Админку" sets `is_sandbox=0`, `sandbox_role=NULL`.

    *   *Refining Check Validation*:
        User clicks "💳 Подписка" -> Bot says: "📤 Отправьте скриншот оплаты или номер транзакции...\n\n✅ Проверьте, что в чеке указано:\n• Сумма: 200₽\n• Дата: сегодня\n• Получатель: [Ваше имя]\n\nАдмин проверит и активирует премиум в течение 15 минут."
        State: `PaymentFSM.waiting_check`.
        Handler for `F.content_type.in_({'photo', 'text'})` in `PaymentFSM.waiting_check`.
        Forwards to `ADMIN_ID` with text:
        "🆕 Новая заявка на премиум:\n\n👤 Пользователь: @{username} (ID: {uid})\n💳 Сумма: 200₽\n📅 Дата: {date}\n\n[Скриншот чека]" + photo if present.
        Inline buttons: `✅ Активировать премиум` (`admin_approve_{uid}`), `❌ Отклонить` (`admin_reject_{uid}`).

5.  **Review against Constraints**:
    *   *Admin mention*: `@zhambaleilo` used in fallback and instructions.
    *   *Preserve content*: I will output the *entire* updated file so the user can just copy-paste, ensuring all original logic (Groq, FSM, DB init) is intact but enhanced.
    *   *Command Priority*: Placed `Command("start", "new", "cancel", "admin")` at the very top of handlers.
    *   *Sandbox*: Implemented with DB columns and admin UI.
    *   *Check Validation*: Implemented with forwarding to admin.
    *   *Payment Link Fallback*: `get_payment_link()` function used everywhere.
    *   *Reminders*: Logic added to `check_user_access` and DB update.

6.  **Detailed Code Construction (Mental Check of the Python file)**:
    *   Imports: Add `MenuButtonCommands`, `BotCommand`, `ContentType` (or just check `m.photo` / `m.text`).
    *   Globals: `ADMIN_USERNAME = "@zhambaleilo"`, `CURRENT_PAYMENT_URL`.
    *   DB: `init_db` needs the `ALTER TABLE` blocks. `get_user` needs to fetch `is_sandbox`, `sandbox_role`, `last_reminder`.
    *   `check_access` function replaces inline checks in `process_style` and `new_congrats`.
    *   `generate_congrats` needs to append reminder text if `show_reminder` is True, and call `set_last_reminder(uid)`.
    *   Admin panel: Add "🧪 Тестирование доступа" and "🔗 Обновить ссылку оплаты".
    *   Payment flow: New FSM `PaymentFSM`.

7.  **Refining the `check_access` and Reminder Logic**:
    ```python
    def set_last_reminder(uid):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET last_reminder=? WHERE user_id=?", (int(time.time()), uid))
        conn.commit()
        conn.close()

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
                return {"can_generate": True, "is_premium": True, "premium_until": now + 12*3600, "show_reminder": True} # e.g., 12 hours left
            elif role == "premium_expired":
                return {"can_generate": False, "is_premium": False, "premium_until": now - 86400, "reason": "expired"}
            elif role == "awaiting_check":
                return {"can_generate": False, "is_premium": False, "reason": "awaiting_check"}

        # Normal logic
        is_prem = is_premium_active(user)
        free_used = user["free_used"]

        if user["is_admin"] or is_prem:
            show_reminder = False
            if is_prem:
                time_left = int(user["premium_until"]) - int(time.time())
                if 0 < time_left <= 3 * 86400:
                    if int(time.time()) - user.get("last_reminder", 0) > 86400:
                        show_reminder = True
            return {"can_generate": True, "is_premium": is_prem, "free_used": free_used, "show_reminder": show_reminder, "premium_until": user["premium_until"]}

        if free_used:
            return {"can_generate": False, "is_premium": False, "free_used": 1, "reason": "limit"}

        return {"can_generate": True, "is_premium": False, "free_used": 0}
    ```

8.  **Handling the "Update Payment Link" Admin Feature**:
    *   Add state `admin_waiting_new_link`.
    *   Admin clicks button, bot asks for new link.
    *   Admin sends link, bot updates `CURRENT_PAYMENT_URL` global variable.

9.  **Formatting the Output**:
    *   Provide the *complete* updated `bot.py` code so the user can directly replace their file. This is the safest way to ensure "не меняя его текущее содержание, то есть изменяй и дополняй только там где нужно" is respected without creating fragmented patches.
    *   Add comments indicating where TZ items were implemented.

10. **Final Polish**:
    *   Check for `@zhambaleilo` usage.
    *   Ensure `/cancel` works.
    *   Ensure `MenuButtonCommands` is imported and used.
    *   Verify DB schema updates won't crash on existing DBs (using `try...except` for `ALTER TABLE`).
    *   Make sure `PaymentFSM` is defined.
    *   Ensure the check forwarding includes the photo if the user sent one.

This thought process covers all requirements, ensures backward compatibility, and structures the code cleanly.
</think>

Вот полностью обновлённый код бота с внедрением всех пунктов ТЗ. Я сохранил всю существующую логику, аккуратно интегрировав новые функции (Sandbox, валидацию чеков, приоритет команд, меню, напоминания и т.д.). 

Скопируйте этот код целиком и замените им содержимое вашего файла `bot (1).py`.

```python
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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, BotCommand
from dotenv import load_dotenv

# ========== КОНФИГ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
BOT_USERNAME = os.getenv("BOT_USERNAME", "CongratsTurnBot")
ADMIN_USERNAME = "@zhambaleilo"  # ✅ ТЗ: Упоминание админа

# Глобальная переменная для ссылки оплаты (позволяет обновлять без перезапуска)
_raw_payment_url = os.getenv("PAYMENT_URL", "#")
CURRENT_PAYMENT_URL = _raw_payment_url if _raw_payment_url and _raw_payment_url != "#" else None

ADMIN_ID = 5174945583
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
DB_PATH = "congrats.db"

def get_payment_link():
    """✅ ТЗ: Фолбэк для ссылки оплаты"""
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
    
    # ✅ ТЗ: Добавление полей для Sandbox и напоминаний
    try:
        conn.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN is_sandbox INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN sandbox_role TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN last_reminder INTEGER DEFAULT 0")
    except Exception:
        pass  # Колонки уже существуют
        
    conn.commit()
    conn.close()

def get_user(uid):
    conn = sqlite3.connect(DB_PATH)
    # ✅ ТЗ: Запрос новых полей
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

def set_last_reminder(uid):
    """✅ ТЗ: Обновление времени последнего напоминания"""
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

# ✅ ТЗ: Единая функция проверки доступа (поддерживает Sandbox и напоминания)
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

    # Обычная логика
    is_prem = is_premium_active(user)
    free_used = user["free_used"]

    if user["is_admin"] or is_prem:
        show_reminder = False
        if is_prem:
            time_left = int(user["premium_until"]) - int(time.time())
            if 0 < time_left <= 3 * 86400:  # <= 3 дней
                if int(time.time()) - user.get("last_reminder", 0) > 86400:  # Не чаще 1 раза в 24ч
                    show_reminder = True
        return {
            "can_generate": True, "is_premium": is_prem, "free_used": free_used, 
            "show_reminder": show_reminder, "premium_until": user["premium_until"]
        }

    if free_used:
        return {"can_generate": False, "is_premium": False, "free_used": 1, "reason": "limit"}

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

# ✅ ТЗ: FSM для валидации чека
class PaymentFSM(StatesGroup):
    waiting_check = State()

# ✅ ТЗ: FSM для админских действий
class AdminFSM(StatesGroup):
    waiting_grant = State()
    waiting_block = State()
    waiting_broadcast = State()
    waiting_new_link = State()


# ========== ХЕНДЛЕРЫ (ПРИОРИТЕТНЫЕ) ==========
# ✅ ТЗ: Приоритет команд над FSM. Работают в любом состоянии.
@dp.message(Command("start", "new", "cancel", "admin"))
async def cmd_priority(m: types.Message, state: FSMContext):
    await state.clear()
    user = get_user(m.from_user.id)
    
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
            [InlineKeyboardButton(text="🧪 Тестирование доступа", callback_data="admin_sandbox")], # ✅ ТЗ
            [InlineKeyboardButton(text="🔗 Обновить ссылку оплаты", callback_data="admin_update_link")] # ✅ ТЗ
        ])
        return await m.answer("🛡 <b>Админ-панель</b>", reply_markup=kb, parse_mode="HTML")

    # Логика /start и /new
    if user["is_blocked"]:
        return await m.answer("🚫 Ваш доступ ограничен.")
    
    await m.answer("🎉 Привет! Напишу живое поздравление за 10 сек.\n\n👤 <b>Кого поздравляем?</b> (имя)", parse_mode="HTML")
    await state.set_state(CongratsFSM.name)


@dp.message(Command("pay"))
async def cmd_pay(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "📤 Отправьте скриншот оплаты или номер транзакции.\n\n"
        "✅ Проверьте, что в чеке указано:\n"
        "• Сумма: 200₽\n"
        "• Дата: сегодня\n"
        f"• Получатель: {ADMIN_USERNAME}\n\n"
        "Админ проверит и активирует премиум в течение 15 минут.",
        parse_mode="HTML"
    )
    await state.set_state(PaymentFSM.waiting_check)


# ========== ХЕНДЛЕРЫ ОСНОВНОГО СЦЕНАРИЯ ==========
@dp.message(CongratsFSM.name)
async def get_name(m: types.Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
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
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=get_payment_link())],
                [InlineKeyboardButton(text="🔄 Попробовать другой стиль", callback_data="regen_style")]
            ])
            await cb.message.answer("🎁 Бесплатная попытка использована.\n\n🔓 Подписка: безлимит", reply_markup=kb)
            await state.clear()
            return
        elif access["reason"] == "awaiting_check":
            return await cb.message.answer("⏳ Ваша заявка на премиум находится на проверке у админа.")
        elif access["reason"] == "expired":
             kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить подписку", url=get_payment_link())]])
             return await cb.message.answer("❌ Премиум истёк. Продли подписку: ", reply_markup=kb)

    await generate_congrats(cb, state, uid, access)


async def generate_congrats(cb: types.CallbackQuery, state: FSMContext, uid: int, access: dict):
    await cb.message.answer(text="⏳ Генерирую...")
    data = await state.get_data()
    
    try:
        text = await call_groq(data["name"], data["occasion"], data["holiday_type"], data["facts"], data["style"])
        
        if not access["is_premium"] and access["free_used"] == 0:
            set_used(uid)

        # ✅ ТЗ: Добавление напоминания об окончании премиума
        reminder_text = ""
        if access.get("show_reminder"):
            time_left = int(access["premium_until"]) - int(time.time())
            if time_left <= 86400:
                reminder_text = f"\n\n⚠️ Премиум истекает сегодня. Продли сейчас: {get_payment_link()}"
            elif time_left <= 2 * 86400:
                reminder_text = f"\n\n⏳ Премиум истекает завтра! Продли: {get_payment_link()}"
            else:
                reminder_text = f"\n\n💎 Премиум действует ещё {time_left // 86400} дн. Продлить: {get_payment_link()}"
            
            set_last_reminder(uid)

        # ✅ Отправляем текст поздравления + напоминание
        await cb.message.answer(text + reminder_text, parse_mode="HTML")

        # ✅ Текстовая инструкция вместо кнопки "Копировать"
        await cb.message.answer("📋 <i>Скопируй текст выше</i>", parse_mode="HTML")

        share_url = f"https://t.me/share/url?url=https://t.me/{BOT_USERNAME}&text=Готовое+поздравление"
        buttons = [
            [InlineKeyboardButton(text="📤 Поделиться ботом", url=share_url)]
        ]
        
        # Показываем оплату только НЕ премиум пользователям
        if not access["is_premium"]:
            buttons.append([InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=get_payment_link())])
        
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
    access = check_user_access(uid)
    
    if not access["can_generate"] and access.get("reason") == "limit":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Подписка 200₽/мес", url=get_payment_link())]])
        return await cb.message.answer("🎁 Бесплатная попытка использована.", reply_markup=kb)
    elif not access["can_generate"] and access.get("reason") == "expired":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Продлить подписку", url=get_payment_link())]])
        return await cb.message.answer("❌ Премиум истёк. Продли подписку: ", reply_markup=kb)

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


# ========== ВАЛИДАЦИЯ ЧЕКА (НОВЫЙ ПОТОК) ==========
@dp.message(PaymentFSM.waiting_check, F.content_type.in_({'photo', 'text'}))
async def process_check(m: types.Message, state: FSMContext):
    uid = m.from_user.id
    username = m.from_user.username or "Нет юзернейма"
    date_str = datetime.now().strftime("%d.%m.%Y")
    
    # Формируем сообщение для админа
    admin_text = (
        f"🆕 <b>Новая заявка на премиум:</b>\n\n"
        f"👤 Пользователь: @{username} (ID: <code>{uid}</code>)\n"
        f"💳 Сумма: 200₽\n"
        f"📅 Дата: {date_str}\n\n"
    )
    
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
        
        # ✅ ТЗ: Переводим пользователя в режим ожидания чека (для Sandbox и логики)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE users SET is_sandbox=1, sandbox_role='awaiting_check' WHERE user_id=?", (uid,))
        conn.commit()
        conn.close()
        
    except Exception as e:
        logging.exception("Ошибка отправки чека админу")
        await m.answer("❌ Произошла ошибка при отправке чека. Попробуйте позже или напишите админу.")
    
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
async def admin_grant(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите ID пользователя:")
    await cb.message.set_state(AdminFSM.waiting_grant)

@dp.message(AdminFSM.waiting_grant)
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
    await cb.message.set_state(AdminFSM.waiting_block)

@dp.message(AdminFSM.waiting_block)
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
    await cb.message.set_state(AdminFSM.waiting_broadcast)

@dp.message(AdminFSM.waiting_broadcast)
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
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛡 Вернуться в Админку", callback_data="admin_back")]])
    await cb.message.answer(f"📊 <b>Статистика:</b>\n\nВсего: {total}\nПремиум: {premium}\nЗаблокировано: {blocked}", reply_markup=kb, parse_mode="HTML")


# ========== ✅ ТЗ: SANDBOX MODE ==========
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
    await cb.message.edit_text(f"🧪 <b>Режим песочницы активен!</b>\nВаша текущая роль: <code>{role}</code>\n\nТеперь вы можете протестировать логику бота как обычный пользователь. Чтобы выйти, нажмите 'Вернуться в Админку'.", reply_markup=cb.message.reply_markup, parse_mode="HTML")


@dp.callback_query(F.data == "admin_update_link")
async def admin_update_link(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    await cb.message.answer("✍️ Введите новую ссылку на оплату (или 'нет', чтобы сбросить и показать контакт админа):")
    await cb.message.set_state(AdminFSM.waiting_new_link)

@dp.message(AdminFSM.waiting_new_link)
async def process_update_link(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    global CURRENT_PAYMENT_URL
    if m.text.strip().lower() in ["нет", "no", "-"]:
        CURRENT_PAYMENT_URL = None
        await m.answer("✅ Ссылка сброшена. Теперь пользователям будет показано сообщение с контактом админа.")
    else:
        CURRENT_PAYMENT_URL = m.text.strip()
        await m.answer(f"✅ Ссылка на оплату обновлена:\n{CURRENT_PAYMENT_URL}")
    await m.set_state(None)


@dp.callback_query(F.data == "admin_back")
async def admin_back(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    # Сброс песочницы для админа
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


# ========== ✅ ТЗ: ОБРАБОТКА ЧЕКОВ АДМИНОМ ==========
@dp.callback_query(F.data.startswith("admin_approve_check_"))
async def admin_approve_check(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    target_uid = int(cb.data.replace("admin_approve_check_", ""))
    until = grant_premium(target_uid, 30)
    
    # Сбрасываем sandbox режим, если он был
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE users SET is_sandbox=0, sandbox_role=NULL WHERE user_id=?", (target_uid,))
    conn.commit()
    conn.close()
    
    await cb.answer("✅ Премиум активирован!", show_alert=True)
    await cb.message.edit_text(cb.message.text + "\n\n✅ <b>СТАТУС: ОДОБРЕНО И АКТИВИРОВАНО</b>", parse_mode="HTML")
    
    try:
        await bot.send_message(target_uid, f"✅ Ваша оплата проверена! Премиум активирован до {datetime.fromtimestamp(until).strftime('%d.%m.%Y')}. Спасибо!")
    except:
        pass

@dp.callback_query(F.data.startswith("admin_reject_check_"))
async def admin_reject_check(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("🚫", show_alert=True)
    
    target_uid = int(cb.data.replace("admin_reject_check_", ""))
    
    # Сбрасываем sandbox режим, чтобы пользователь мог попробовать снова
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
    
    # ✅ ТЗ: Настройка Menu Button и команд
    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands(
            text="🆕 Новое поздравление",
            command="new"
        )
    )
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="new", description="Новое поздравление"),
        BotCommand(command="pay", description="Оформить подписку"),
        BotCommand(command="cancel", description="Отменить текущий сценарий"),
        BotCommand(command="admin", description="Панель администратора")
    ])
    
    logging.info("✅ Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
