import asyncio
import logging
import sqlite3
import os
import secrets
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message, ChatJoinRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from dotenv import load_dotenv

# ====== ЗАГРУЗКА НАСТРОЕК ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK")
OWNER_ID = int(os.getenv("OWNER_ID"))

SUPER_ADMIN_IDS = [OWNER_ID]
DB_NAME = "bot_database.db"
TEMP_DIR = "temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# ====== ИНИЦИАЛИЗАЦИЯ ======
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
logging.basicConfig(level=logging.INFO)

# ====== СОСТОЯНИЯ ДЛЯ КЛЮЧА ======
class RegisterState(StatesGroup):
    waiting_for_key = State()

# ====== БАЗА ДАННЫХ ======
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            admin_id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            subject TEXT NOT NULL,
            class_level TEXT NOT NULL,
            price INTEGER NOT NULL,
            PRIMARY KEY (subject, class_level)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            client_user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            class_level TEXT NOT NULL,
            region INTEGER,
            price INTEGER NOT NULL,
            status TEXT CHECK(status IN ('approved', 'rejected')) NOT NULL,
            screenshot_path TEXT,
            link_sent BOOLEAN DEFAULT 0,
            joined BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('region_required', '0')")
    conn.commit()
    conn.close()

def get_setting(key: str) -> str:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def is_region_required() -> bool:
    return get_setting('region_required') == '1'

def add_admin(admin_id: int, key: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO admins (admin_id, key) VALUES (?, ?)", (admin_id, key))
    conn.commit()
    conn.close()

def get_admin(admin_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT is_active FROM admins WHERE admin_id = ?", (admin_id,))
    row = cur.fetchone()
    conn.close()
    return row

def activate_admin_by_key(key: str, admin_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE admins SET admin_id = ? WHERE key = ? AND is_active = 1", (admin_id, key))
    conn.commit()
    conn.close()
    return cur.rowcount > 0

def ban_admin(admin_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE admins SET is_active = 0 WHERE admin_id = ?", (admin_id,))
    conn.commit()
    conn.close()

def generate_key():
    return secrets.token_urlsafe(8)

def get_price(subject: str, class_level: str) -> int | None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT price FROM prices WHERE subject = ? AND class_level = ?", (subject, class_level))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_price(subject: str, class_level: str, price: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO prices (subject, class_level, price) VALUES (?, ?, ?)", (subject, class_level, price))
    conn.commit()
    conn.close()

def save_application(admin_id: int, client_user_id: int, subject: str, class_level: str,
                     region: int | None, price: int, status: str, screenshot_path: str) -> int:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO applications (admin_id, client_user_id, subject, class_level, region, price, status, screenshot_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (admin_id, client_user_id, subject, class_level, region, price, status, screenshot_path))
    app_id = cur.lastrowid
    conn.commit()
    conn.close()
    return app_id

def update_link_sent(app_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE applications SET link_sent = 1 WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()

def update_joined(client_user_id: int, subject: str, class_level: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        UPDATE applications 
        SET joined = 1 
        WHERE client_user_id = ? AND subject = ? AND class_level = ? 
          AND status = 'approved' AND joined = 0
    """, (client_user_id, subject, class_level))
    conn.commit()
    conn.close()

def get_pending_application(client_user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, subject, class_level, admin_id
        FROM applications 
        WHERE client_user_id = ? AND status = 'approved' AND link_sent = 1 AND joined = 0
    """, (client_user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def parse_caption(caption: str, region_required: bool):
    if region_required:
        pattern = r'^@(\w+)\s+([а-яА-ЯёЁa-zA-Z\s]+?)\s+(\d{1,2}\s*[а-яА-Я]?)\s+(\d{1,2})$'
        match = re.match(pattern, caption.strip())
        if match:
            username = match.group(1)
            subject = match.group(2).strip().capitalize()
            class_level = match.group(3).strip()
            region = int(match.group(4))
            return username, subject, class_level, region
        return None, None, None, None
    else:
        pattern = r'^@(\w+)\s+([а-яА-ЯёЁa-zA-Z\s]+?)\s+(\d{1,2}\s*[а-яА-Я]?)$'
        match = re.match(pattern, caption.strip())
        if match:
            username = match.group(1)
            subject = match.group(2).strip().capitalize()
            class_level = match.group(3).strip()
            return username, subject, class_level, None
        return None, None, None, None

async def send_invite_to_user(bot: Bot, user_id: int, subject: str, class_level: str, app_id: int, admin_id: int):
    try:
        await bot.send_message(
            user_id,
            f"✅ Ваш доступ к каналу «{subject} {class_level}» активирован.\n"
            f"🔗 Перейдите по ссылке и нажмите «Вступить»:\n{CHANNEL_LINK}"
        )
        update_link_sent(app_id)
        return True
    except Exception as e:
        logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
        await bot.send_message(OWNER_ID, f"⚠️ Сбой отправки ссылки\nАдмин: {admin_id}\nПользователь: {user_id}\nОшибка: {e}")
        return False

async def check_admin_rights(message: Message, bot: Bot) -> bool:
    user_id = message.from_user.id
    if user_id in SUPER_ADMIN_IDS:
        return True
    row = get_admin(user_id)
    if not row or row[0] == 0:
        await message.reply("⛔ Ваш доступ заблокирован или вы не зарегистрированы. Введите /start для активации.")
        return False
    return True

@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if user_id in SUPER_ADMIN_IDS:
        await message.reply("👑 Вы владелец. Все команды доступны.")
        return

    row = get_admin(user_id)
    if row:
        if row[0] == 1:
            await message.reply("✅ Вы уже активированы. Отправляйте заявки.")
        else:
            await message.reply("⛔ Ваш доступ заблокирован. Обратитесь к владельцу.")
        return

    await message.reply("🔑 Введите ключ доступа, который вы получили от владельца:")
    await state.set_state(RegisterState.waiting_for_key)

@dp.message(RegisterState.waiting_for_key)
async def process_key(message: Message, state: FSMContext, bot: Bot):
    key = message.text.strip()
    user_id = message.from_user.id
    success = activate_admin_by_key(key, user_id)
    if success:
        if is_region_required():
            await message.reply("✅ Ключ принят! Формат заявки: @username Предмет Класс Регион (например: @ivanov Математика 9 77) + фото чека.")
        else:
            await message.reply("✅ Ключ принят! Формат заявки: @username Предмет Класс (например: @ivanov Математика 9) + фото чека.")
    else:
        await message.reply("❌ Неверный или неактивный ключ. Попробуйте снова.")
    await state.clear()

@dp.message(Command("region_on"))
async def region_on_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    set_setting('region_required', '1')
    await message.reply("✅ Режим региона ВКЛЮЧЁН. Админы обязаны указывать регион.")

@dp.message(Command("region_off"))
async def region_off_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    set_setting('region_required', '0')
    await message.reply("✅ Режим региона ВЫКЛЮЧЕН. Админы пишут без региона.")

@dp.message(Command("add_admin"))
async def add_admin_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Формат: /add_admin @username")
        return
    username = args[1].replace('@', '')
    try:
        user = await bot.get_chat(f"@{username}")
        admin_id = user.id
    except:
        await message.reply("Пользователь не найден")
        return
    
    key = generate_key()
    add_admin(admin_id, key)
    await message.reply(f"✅ Админ @{username} добавлен.\nКлюч: `{key}`\nОтправьте этот ключ ему в ЛС.")
    try:
        await bot.send_message(admin_id, f"🔑 Вам выдан ключ доступа к боту: `{key}`. Напишите /start и введите его.")
    except:
        pass

@dp.message(Command("ban_admin"))
async def ban_admin_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Формат: /ban_admin @username")
        return
    username = args[1].replace('@', '')
    try:
        user = await bot.get_chat(f"@{username}")
        admin_id = user.id
    except:
        await message.reply("Пользователь не найден")
        return
    ban_admin(admin_id)
    await message.reply(f"⛔ Администратор @{username} заблокирован.")

@dp.message(F.photo)
async def handle_application(message: Message, bot: Bot):
    if not await check_admin_rights(message, bot):
        return

    caption = message.caption or ""
    region_required = is_region_required()
    username, subject, class_level, region = parse_caption(caption, region_required)

    if not username or not subject or not class_level:
        if region_required:
            await message.reply("❌ Неверный формат. Нужно: @username Предмет Класс Регион (например: @ivanov Математика 9 77)")
        else:
            await message.reply("❌ Неверный формат. Нужно: @username Предмет Класс (например: @ivanov Математика 9)")
        return

    if region_required:
        if region is None:
            await message.reply("❌ Вы не указали регион. Формат: @username Предмет Класс Регион")
            return
        if region < 1 or region > 99:
            await message.reply("❌ Регион должен быть числом от 1 до 99.")
            return

    try:
        user = await bot.get_chat(f"@{username}")
        client_id = user.id
    except:
        await message.reply(f"❌ Пользователь @{username} не найден.")
        return

    price = get_price(subject, class_level)
    if price is None:
        await message.reply(f"❌ Цена для {subject} {class_level} не установлена. Используйте /addprice")
        return

    file = await bot.get_file(message.photo[-1].file_id)
    file_path = f"{TEMP_DIR}/{file.file_id}.jpg"
    await bot.download_file(file.file_path, file_path)

    app_id = save_application(
        message.from_user.id, client_id, subject, class_level, region,
        price, 'approved', file_path
    )
    
    sent = await send_invite_to_user(bot, client_id, subject, class_level, app_id, message.from_user.id)
    region_text = f" (регион {region})" if region else ""
    await message.reply(f"✅ Заявка одобрена! Ссылка отправлена @{username}{region_text}." if sent else "⚠️ Одобрено, но ссылку не отправить. Сообщено владельцу.")

@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest, bot: Bot):
    if event.chat.id != CHANNEL_ID:
        return
    pending = get_pending_application(event.from_user.id)
    if pending:
        app_id, subject, class_level, admin_id = pending
        await bot.approve_chat_join_request(chat_id=CHANNEL_ID, user_id=event.from_user.id)
        update_joined(event.from_user.id, subject, class_level)
        await bot.send_message(admin_id, f"✅ Пользователь вступил в канал «{subject} {class_level}».")
    else:
        await bot.decline_chat_join_request(chat_id=CHANNEL_ID, user_id=event.from_user.id)

@dp.message(Command("my_stats"))
async def my_stats_command(message: Message, bot: Bot):
    if not await check_admin_rights(message, bot):
        return
    admin_id = message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT subject, class_level, region,
               COUNT(CASE WHEN status='approved' THEN 1 END) as approved,
               SUM(CASE WHEN status='approved' THEN price ELSE 0 END) as total_sum
        FROM applications
        WHERE admin_id = ?
        GROUP BY subject, class_level, region
    """, (admin_id,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await message.reply("У вас пока нет заявок.")
        return
    output = "📊 Моя статистика\n\n"
    total_count = 0
    total_sum = 0
    for subject, cls, region, approved, s in rows:
        region_text = f" (регион {region})" if region else ""
        output += f"📚 {subject} {cls}{region_text}: ✅{approved}  💰{s or 0:,} руб.\n"
        total_count += approved
        total_sum += (s or 0)
    output += f"\n📈 Итого: {total_count} продаж на сумму {total_sum:,} руб."
    await message.reply(output)

@dp.message(Command("stats"))
async def stats_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT admin_id, 
               COUNT(CASE WHEN status='approved' THEN 1 END) as approved,
               SUM(CASE WHEN status='approved' THEN price ELSE 0 END) as total
        FROM applications
        GROUP BY admin_id
    """)
    rows = cur.fetchall()
    if not rows:
        await message.reply("Нет данных.")
        conn.close()
        return
    output = "📊 Общая статистика по администраторам\n\n"
    grand_total = 0
    for admin_id, approved, total in rows:
        try:
            user = await bot.get_chat(admin_id)
            name = user.username or f"ID{admin_id}"
        except:
            name = f"ID{admin_id}"
        output += f"👤 @{name}: {approved} продаж, 💰{total or 0:,} руб.\n"
        grand_total += (total or 0)
    output += f"\n💰 Общая выручка всех админов: {grand_total:,} руб."
    conn.close()
    await message.reply(output)

@dp.message(Command("stats_admin"))
async def stats_admin_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.reply("Формат: /stats_admin ID")
        return
    try:
        admin_id = int(args[1])
    except:
        await message.reply("ID должен быть числом.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM applications WHERE admin_id = ?", (admin_id,))
    if cur.fetchone()[0] == 0:
        await message.reply("Нет заявок.")
        conn.close()
        return
    cur.execute("""
        SELECT subject, class_level, region,
               COUNT(CASE WHEN status='approved' THEN 1 END) as approved,
               COUNT(CASE WHEN status='rejected' THEN 1 END) as rejected,
               SUM(CASE WHEN status='approved' THEN price ELSE 0 END) as total_sum
        FROM applications
        WHERE admin_id = ?
        GROUP BY subject, class_level, region
    """, (admin_id,))
    rows = cur.fetchall()
    output = f"📊 Детально по ID {admin_id}\n\n"
    total_app = 0
    total_sum = 0
    for subject, cls, region, approved, rejected, s in rows:
        region_text = f" (регион {region})" if region else ""
        output += f"📚 {subject} {cls}{region_text}: ✅{approved} ❌{rejected} 💰{s or 0:,} руб.\n"
        total_app += approved
        total_sum += (s or 0)
    output += f"\n📈 Итого: {total_app} продаж на сумму {total_sum:,} руб."
    conn.close()
    await message.reply(output)

@dp.message(Command("addprice"))
async def add_price_command(message: Message, bot: Bot):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    args = message.text.split(maxsplit=3)
    if len(args) < 4:
        await message.reply("Формат: /addprice Предмет Класс Цена")
        return
    _, subject, class_level, price_str = args
    try:
        price = int(price_str)
    except:
        await message.reply("Цена должна быть числом.")
        return
    set_price(subject, class_level, price)
    await message.reply(f"✅ Цена {subject} {class_level} = {price} руб.")

# ====== ЗАПУСК ======
async def main():
    init_db()
    logging.info("Бот запущен и готов к работе")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())