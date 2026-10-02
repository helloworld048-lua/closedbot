import asyncio
import logging
import os
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "jokas_two").lstrip("@")
DB_PATH = Path("bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not MAIN_ADMIN_ID:
    raise RuntimeError("MAIN_ADMIN_ID is not set in .env")

router = Router()
dp = Dispatcher()
dp.include_router(router)


# -------------------- DB --------------------

async def db_execute(query, params=(), fetch=False, fetchone=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        if fetch:
            rows = await cur.fetchall()
            await db.commit()
            return rows
        if fetchone:
            row = await cur.fetchone()
            await db.commit()
            return row
        await db.commit()
        return None


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            is_main INTEGER NOT NULL DEFAULT 0,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS prices (
            key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            amount INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            discount INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            uses INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            assigned_admin INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tickets_user_status
        ON tickets(user_id, status);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        await db.execute(
            "INSERT OR IGNORE INTO admins(user_id, is_main) VALUES (?, 1)",
            (MAIN_ADMIN_ID,),
        )

        defaults = [
            ("audio", "Заказать Аудио", 249),
            ("distrokid", "DistroKid: дистрибуция аудио — 1 год", 1649),
            ("subscription", "Подписка на загрузку аудио", 1000),
        ]
        for key, title, amount in defaults:
            await db.execute(
                "INSERT OR IGNORE INTO prices(key, title, amount) VALUES (?, ?, ?)",
                (key, title, amount),
            )

        await db.commit()

    # Add admins from env without removing existing admins.
    raw_admins = os.getenv("ADMIN_IDS", "")
    for value in raw_admins.split(","):
        value = value.strip()
        if value.isdigit():
            await db_execute(
                "INSERT OR IGNORE INTO admins(user_id, is_main) VALUES (?, 0)",
                (int(value),),
            )


async def is_admin(user_id: int) -> bool:
    row = await db_execute(
        "SELECT 1 FROM admins WHERE user_id = ?", (user_id,), fetchone=True
    )
    return row is not None


async def is_main_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID


async def get_admin_ids():
    rows = await db_execute("SELECT user_id FROM admins", fetch=True)
    return [int(r["user_id"]) for r in rows]


async def get_price(key):
    row = await db_execute(
        "SELECT title, amount FROM prices WHERE key = ?", (key,), fetchone=True
    )
    return (row["title"], int(row["amount"])) if row else ("", 0)


async def get_user_price(user_id, key):
    title, amount = await get_price(key)
    promo_row = await db_execute(
        "SELECT value FROM settings WHERE key=?",
        (f"active_promo:{user_id}",),
        fetchone=True,
    )
    if not promo_row:
        return title, amount, None, 0

    promo = await db_execute(
        "SELECT code, discount, active FROM promo_codes WHERE code=?",
        (promo_row["value"],),
        fetchone=True,
    )
    if not promo or not promo["active"]:
        return title, amount, None, 0

    discount = int(promo["discount"])
    final_amount = max(0, round(amount * (100 - discount) / 100))
    return title, final_amount, promo["code"], discount


async def set_price(key, amount):
    await db_execute(
        "UPDATE prices SET amount = ? WHERE key = ?", (amount, key)
    )


async def get_open_ticket(user_id):
    return await db_execute(
        "SELECT * FROM tickets WHERE user_id = ? AND status = 'open' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
        fetchone=True,
    )


async def get_ticket(ticket_id):
    return await db_execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,), fetchone=True
    )


async def create_ticket(message: Message):
    await db_execute(
        "INSERT INTO tickets(user_id, username) VALUES (?, ?)",
        (message.from_user.id, message.from_user.username or ""),
    )


async def assign_ticket(ticket_id, admin_id):
    await db_execute(
        "UPDATE tickets SET assigned_admin = ? WHERE id = ? AND status = 'open'",
        (admin_id, ticket_id),
    )


async def close_ticket(ticket_id):
    await db_execute(
        "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
        (ticket_id,),
    )


# -------------------- UI --------------------

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎧 Заказать Аудио", callback_data="order:audio")],
        [
            InlineKeyboardButton(text="🔧 Тех. Поддержка", callback_data="support"),
            InlineKeyboardButton(text="🧧 Ввести промокод", callback_data="promo"),
            InlineKeyboardButton(text="🔄 Другое", callback_data="other"),
        ],
        [InlineKeyboardButton(text="🔑 Получить данные от DistroKid", callback_data="distrokid")],
    ])


def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="home")]
    ])


def kb_support_start():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Открыть обращение", callback_data="ticket:create")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="home")],
    ])


def kb_ticket_user():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть обращение", callback_data="ticket:user_close")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
    ])


def kb_admin_ticket(ticket_id, assigned=False):
    buttons = []
    if not assigned:
        buttons.append(
            [InlineKeyboardButton(text="🙋 Взять обращение", callback_data=f"ticket:take:{ticket_id}")]
        )
    buttons.append(
        [InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"ticket:close:{ticket_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цены", callback_data="admin:prices")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="admin:promos")],
        [InlineKeyboardButton(text="👥 Администраторы", callback_data="admin:admins")],
        [InlineKeyboardButton(text="📨 Открытые обращения", callback_data="admin:tickets")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="home")],
    ])


def kb_price_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎧 Аудио", callback_data="admin:price:audio")],
        [InlineKeyboardButton(text="💿 DistroKid", callback_data="admin:price:distrokid")],
        [InlineKeyboardButton(text="📦 Подписка", callback_data="admin:price:subscription")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="admin:panel")],
    ])


def kb_admins_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Выдать админа", callback_data="admin:add")],
        [InlineKeyboardButton(text="➖ Снять админа", callback_data="admin:remove")],
        [InlineKeyboardButton(text="📋 Список админов", callback_data="admin:list")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="admin:panel")],
    ])


# -------------------- Message helpers --------------------

async def delete_message_safe(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def replace_with(message: Message, text: str, keyboard=None):
    # The requested behavior: old message disappears, new menu is sent.
    await delete_message_safe(message)
    return await message.answer(text, reply_markup=keyboard)


async def send_home(chat_id, bot: Bot):
    await bot.send_message(
        chat_id,
        "<b>Добро пожаловать в JOKAS Audio 🎧</b>\n\n"
        "Выберите нужный раздел:",
        reply_markup=kb_main(),
    )


async def admin_display(user_id: int):
    try:
        chat = await bot.get_chat(user_id)
        name = " ".join(x for x in [chat.first_name, chat.last_name] if x).strip()
        username = f"@{chat.username}" if chat.username else "без username"
        return f"{name or 'Без имени'} ({username}) — <code>{user_id}</code>"
    except Exception:
        return f"<code>{user_id}</code>"


# -------------------- /start and main menu --------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "<b>Добро пожаловать в JOKAS Audio 🎧</b>\n\n"
        "Выберите нужный раздел:",
        reply_markup=kb_main(),
    )


@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    await call.answer()
    await replace_with(
        call.message,
        "<b>JOKAS Audio 🎧</b>\n\nВыберите нужный раздел:",
        kb_main(),
    )


# -------------------- Orders / payment --------------------

@router.callback_query(F.data == "order:audio")
async def cb_audio(call: CallbackQuery):
    await call.answer()
    title, amount, promo_code, discount = await get_user_price(call.from_user.id, "audio")
    promo_line = f"\n🎟 Промокод <code>{promo_code}</code>: -{discount}%\n" if promo_code else ""
    await replace_with(
        call.message,
        f"🟢 <b>{title}</b>\n\n"
        f"Цена: <b>{amount} ₽</b>{promo_line}\n"
        "Оплата проходит через менеджера.\n"
        "Нажмите кнопку ниже, чтобы открыть обращение и договориться об оплате.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Оплатить через менеджера", callback_data="ticket:create:payment")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="home")],
        ]),
    )


@router.callback_query(F.data == "distrokid")
async def cb_distrokid(call: CallbackQuery):
    await call.answer()
    title, amount, promo_code, discount = await get_user_price(call.from_user.id, "distrokid")
    promo_line = f"\n🎟 Промокод <code>{promo_code}</code>: -{discount}%\n" if promo_code else ""
    await replace_with(
        call.message,
        f"🔑 <b>{title}</b>\n\n"
        f"Стоимость: <b>{amount} ₽</b>{promo_line}\n"
        "Данные выдаются после оплаты через менеджера.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Оплатить через менеджера", callback_data="ticket:create:payment")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="home")],
        ]),
    )


@router.callback_query(F.data == "other")
async def cb_other(call: CallbackQuery):
    await call.answer()
    await replace_with(
        call.message,
        "📦 <b>Другие товары</b>\n\n"
        "• DistroKid — дистрибуция аудио на 1 год\n"
        "• Подписка на загрузку аудио\n\n"
        "Для заказа откройте обращение с менеджером.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать менеджеру", callback_data="ticket:create:payment")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="home")],
        ]),
    )


# -------------------- Promo --------------------

@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery):
    await call.answer()
    await replace_with(
        call.message,
        "🎟 <b>Введите промокод</b>\n\n"
        "Отправьте код отдельным сообщением, например: <code>WELCOME10</code>.",
        kb_back(),
    )
    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_promo:{call.from_user.id}", "1"),
    )


@router.message()
async def universal_message(message: Message):
    # Admin commands have priority.
    if message.text and message.text.startswith("/admin"):
        if await is_admin(message.from_user.id):
            await message.answer(
                "<b>Админ-панель</b>\n\nВыберите действие:",
                reply_markup=kb_admin_panel(),
            )
        return

    # Promo input.
    waiting = await db_execute(
        "SELECT value FROM settings WHERE key=?",
        (f"awaiting_promo:{message.from_user.id}",),
        fetchone=True,
    )
    if waiting and message.text:
        code = message.text.strip().upper()
        promo = await db_execute(
            "SELECT * FROM promo_codes WHERE code=? AND active=1",
            (code,),
            fetchone=True,
        )
        await db_execute(
            "DELETE FROM settings WHERE key=?",
            (f"awaiting_promo:{message.from_user.id}",),
        )
        if promo:
            await db_execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (f"active_promo:{message.from_user.id}", code),
            )
            await message.answer(
                f"✅ Промокод <code>{code}</code> активен.\n"
                f"Скидка: <b>{promo['discount']}%</b>\n\n"
                "Скидка будет учтена при обращении к менеджеру.",
                reply_markup=kb_main(),
            )
        else:
            await message.answer(
                "❌ Такой промокод не найден или уже отключён.",
                reply_markup=kb_main(),
            )
        return

    # Admin interactive input.
    if await handle_admin_text(message):
        return

    # Support chat.
    ticket = await get_open_ticket(message.from_user.id)
    if ticket:
        await relay_user_message(message, ticket)
        return


# -------------------- Support --------------------

@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    await call.answer()
    ticket = await get_open_ticket(call.from_user.id)
    if ticket:
        assigned = ticket["assigned_admin"]
        who = (
            f"Ваше обращение уже взял администратор <code>{assigned}</code>."
            if assigned else
            "Ожидайте администратора. Все администраторы получили уведомление."
        )
        text = (
            "💬 <b>Чат с поддержкой</b>\n\n"
            f"{who}\n\n"
            "Теперь просто отправляйте сообщения сюда."
        )
        await replace_with(call.message, text, kb_ticket_user())
        return

    await replace_with(
        call.message,
        "🔧 <b>Тех. Поддержка</b>\n\n"
        "Здесь можно открыть живое обращение с администраторами.\n"
        "Сообщения будут передаваться через бота.",
        kb_support_start(),
    )


async def open_ticket_for_user(user_message: Message, reason="support"):
    existing = await get_open_ticket(user_message.from_user.id)
    if existing:
        return existing

    await create_ticket(user_message)
    ticket = await get_open_ticket(user_message.from_user.id)

    if reason == "payment":
        intro = "💳 Пользователь хочет оплатить товар через менеджера."
    else:
        intro = "🔧 Пользователь открыл обращение в поддержку."

    user = user_message.from_user
    username = f"@{user.username}" if user.username else "без username"
    admin_text = (
        f"📨 <b>Новое обращение #{ticket['id']}</b>\n\n"
        f"Пользователь: {user.full_name}\n"
        f"Username: {username}\n"
        f"ID: <code>{user.id}</code>\n\n"
        f"{intro}\n\n"
        "Кто первый нажмёт «Взять обращение», тот становится ответственным."
    )

    for admin_id in await get_admin_ids():
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=kb_admin_ticket(ticket["id"]),
            )
        except Exception as e:
            logging.warning("Cannot notify admin %s: %s", admin_id, e)

    return ticket


@router.callback_query(F.data.startswith("ticket:create"))
async def cb_create_ticket(call: CallbackQuery):
    await call.answer("Обращение создаётся...")
    reason = "payment" if call.data.endswith(":payment") else "support"
    ticket = await open_ticket_for_user(call.message, reason)

    await replace_with(
        call.message,
        f"💬 <b>Обращение #{ticket['id']}</b>\n\n"
        "Готово. Все администраторы получили уведомление.\n"
        "Пишите сообщения прямо сюда - это ваш чат с поддержкой.",
        kb_ticket_user(),
    )


@router.callback_query(F.data == "ticket:user_close")
async def cb_user_close(call: CallbackQuery):
    ticket = await get_open_ticket(call.from_user.id)
    if not ticket:
        await call.answer("Открытых обращений нет.")
        return

    await close_ticket(ticket["id"])
    if ticket["assigned_admin"]:
        try:
            await bot.send_message(
                ticket["assigned_admin"],
                f"🔒 Пользователь закрыл обращение #{ticket['id']}.",
            )
        except Exception:
            pass

    await call.answer("Обращение закрыто.")
    await replace_with(
        call.message,
        "Обращение закрыто.\n\nВы вернулись в главное меню.",
        kb_main(),
    )


async def relay_user_message(message: Message, ticket):
    assigned = ticket["assigned_admin"]
    if not assigned:
        await message.answer(
            "⏳ Обращение ещё никто не взял.\n"
            "Сообщение сохранено. Дождитесь администратора.",
        )
        return

    await bot.send_message(
        assigned,
        f"👤 <b>Обращение #{ticket['id']}</b>\n"
        f"Пользователь ID: <code>{message.from_user.id}</code>\n\n"
        "Новое сообщение:",
    )
    await copy_message_safe(
        bot,
        message.chat.id,
        assigned,
        message.message_id,
    )


async def copy_message_safe(bot_obj, from_chat_id, to_chat_id, message_id):
    try:
        await bot_obj.copy_message(
            chat_id=to_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
    except Exception as e:
        logging.warning("copy_message failed: %s", e)
        try:
            await bot_obj.send_message(
                to_chat_id,
                "⚠️ Не удалось передать это сообщение. "
                "Отправьте его текстом или попробуйте ещё раз.",
            )
        except Exception:
            pass


# -------------------- Admin ticket actions --------------------

@router.callback_query(F.data.startswith("ticket:take:"))
async def cb_take_ticket(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket or ticket["status"] != "open":
        await call.answer("Обращение уже закрыто.", show_alert=True)
        return

    if ticket["assigned_admin"] and ticket["assigned_admin"] != call.from_user.id:
        await call.answer("Это обращение уже взял другой администратор.", show_alert=True)
        return

    await assign_ticket(ticket_id, call.from_user.id)

    await call.answer("Обращение взято.")
    try:
        await call.message.edit_text(
            call.message.text + "\n\n✅ <b>Взято вами.</b>",
            reply_markup=kb_admin_ticket(ticket_id, assigned=True),
        )
    except TelegramBadRequest:
        pass

    await bot.send_message(
        ticket["user_id"],
        f"👨‍💼 Администратор подключился к обращению #{ticket_id}.\n\n"
        "Можете писать сюда. Ответы администратора будут приходить в этот чат.",
        reply_markup=kb_ticket_user(),
    )


@router.callback_query(F.data.startswith("ticket:close:"))
async def cb_admin_close_ticket(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await get_ticket(ticket_id)
    if not ticket:
        await call.answer("Обращение не найдено.", show_alert=True)
        return

    if ticket["assigned_admin"] not in (None, call.from_user.id) and not await is_main_admin(call.from_user.id):
        await call.answer("Закрыть это обращение может ответственный или главный админ.", show_alert=True)
        return

    await close_ticket(ticket_id)
    await call.answer("Обращение закрыто.")

    try:
        await call.message.edit_text(
            (call.message.text or "") + "\n\n🔒 <b>Закрыто.</b>",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            ticket["user_id"],
            f"🔒 Обращение #{ticket_id} закрыто администратором.\n\n"
            "Если понадобится помощь, можно открыть новое обращение.",
            reply_markup=kb_main(),
        )
    except Exception:
        pass


# -------------------- Admin panel --------------------

@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return
    await call.answer()
    await replace_with(call.message, "<b>Админ-панель</b>\n\nВыберите действие:", kb_admin_panel())


@router.callback_query(F.data == "admin:prices")
async def cb_admin_prices(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    audio = await get_price("audio")
    distro = await get_price("distrokid")
    sub = await get_price("subscription")

    await call.answer()
    await replace_with(
        call.message,
        "💰 <b>Цены</b>\n\n"
        f"🎧 Аудио: <b>{audio[1]} ₽</b>\n"
        f"💿 DistroKid: <b>{distro[1]} ₽</b>\n"
        f"📦 Подписка: <b>{sub[1]} ₽</b>\n\n"
        "Выберите товар, цену которого хотите изменить:",
        kb_price_menu(),
    )


@router.callback_query(F.data.startswith("admin:price:"))
async def cb_admin_price(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    key = call.data.rsplit(":", 1)[1]
    title, amount = await get_price(key)

    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_price:{call.from_user.id}", key),
    )

    await call.answer()
    await replace_with(
        call.message,
        f"✏️ <b>Изменение цены</b>\n\n"
        f"{title}\n"
        f"Текущая цена: <b>{amount} ₽</b>\n\n"
        "Отправьте новую цену одним сообщением, только число.\n"
        "Например: <code>299</code>.",
        kb_back(),
    )


@router.callback_query(F.data == "admin:admins")
async def cb_admin_admins(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("Только главный администратор.", show_alert=True)
        return

    await call.answer()
    await replace_with(call.message, "👥 <b>Администраторы</b>", kb_admins_menu())


@router.callback_query(F.data == "admin:list")
async def cb_admin_list(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("Только главный администратор.", show_alert=True)
        return

    rows = await db_execute(
        "SELECT user_id, is_main FROM admins ORDER BY is_main DESC, user_id",
        fetch=True,
    )
    lines = ["👥 <b>Список администраторов</b>\n"]
    for row in rows:
        role = "👑 Главный" if row["is_main"] else "🛡 Админ"
        lines.append(f"{role}: <code>{row['user_id']}</code>")
    await call.answer()
    await replace_with(call.message, "\n".join(lines), kb_admins_menu())


@router.callback_query(F.data == "admin:add")
async def cb_admin_add(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("Только главный администратор.", show_alert=True)
        return

    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_admin_add:{call.from_user.id}", "1"),
    )
    await call.answer()
    await replace_with(
        call.message,
        "➕ <b>Выдать администратора</b>\n\n"
        "Отправьте Telegram ID пользователя числом.\n"
        "Например: <code>123456789</code>.",
        kb_back(),
    )


@router.callback_query(F.data == "admin:remove")
async def cb_admin_remove(call: CallbackQuery):
    if not await is_main_admin(call.from_user.id):
        await call.answer("Только главный администратор.", show_alert=True)
        return

    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_admin_remove:{call.from_user.id}", "1"),
    )
    await call.answer()
    await replace_with(
        call.message,
        "➖ <b>Снять администратора</b>\n\n"
        "Отправьте Telegram ID администратора.\n"
        "Главного администратора снять через эту кнопку нельзя.",
        kb_back(),
    )


@router.callback_query(F.data == "admin:promos")
async def cb_admin_promos(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    rows = await db_execute(
        "SELECT code, discount, active, uses FROM promo_codes ORDER BY code",
        fetch=True,
    )
    if rows:
        lines = ["🎟 <b>Промокоды</b>\n"]
        for r in rows:
            status = "активен" if r["active"] else "выключен"
            lines.append(
                f"<code>{r['code']}</code> — {r['discount']}% — {status} — использований: {r['uses']}"
            )
        text = "\n".join(lines)
    else:
        text = "🎟 <b>Промокоды</b>\n\nПока нет промокодов."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo:add")],
        [InlineKeyboardButton(text="🔁 Включить/выключить", callback_data="admin:promo:toggle")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="admin:panel")],
    ])
    await call.answer()
    await replace_with(call.message, text, kb)


@router.callback_query(F.data == "admin:promo:add")
async def cb_promo_add(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_promo_create:{call.from_user.id}", "1"),
    )
    await call.answer()
    await replace_with(
        call.message,
        "➕ <b>Создание промокода</b>\n\n"
        "Отправьте в формате:\n"
        "<code>КОД СКИДКА</code>\n\n"
        "Например: <code>WELCOME 10</code>",
        kb_back(),
    )


@router.callback_query(F.data == "admin:promo:toggle")
async def cb_promo_toggle(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    await db_execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
        (f"awaiting_promo_toggle:{call.from_user.id}", "1"),
    )
    await call.answer()
    await replace_with(
        call.message,
        "🔁 <b>Включение/выключение промокода</b>\n\n"
        "Отправьте код промокода.",
        kb_back(),
    )


@router.callback_query(F.data == "admin:tickets")
async def cb_admin_tickets(call: CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("Нет доступа.", show_alert=True)
        return

    rows = await db_execute(
        "SELECT * FROM tickets WHERE status='open' ORDER BY id DESC LIMIT 30",
        fetch=True,
    )
    if not rows:
        text = "📨 Открытых обращений нет."
    else:
        lines = ["📨 <b>Открытые обращения</b>\n"]
        for r in rows:
            assigned = f"админ <code>{r['assigned_admin']}</code>" if r["assigned_admin"] else "ожидает администратора"
            lines.append(f"#{r['id']} — {assigned} — пользователь <code>{r['user_id']}</code>")
        text = "\n".join(lines)

    await call.answer()
    await replace_with(
        call.message,
        text,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="admin:panel")]
        ]),
    )


# -------------------- Admin text state --------------------

async def handle_admin_text(message: Message) -> bool:
    user_id = message.from_user.id
    if not await is_admin(user_id):
        return False

    # Main admin: add/remove admin.
    state = await db_execute(
        "SELECT key FROM settings WHERE key IN (?, ?, ?, ?, ?) LIMIT 1",
        (
            f"awaiting_admin_add:{user_id}",
            f"awaiting_admin_remove:{user_id}",
            f"awaiting_promo_create:{user_id}",
            f"awaiting_promo_toggle:{user_id}",
            f"awaiting_price:{user_id}",
        ),
        fetchone=True,
    )

    if not state:
        # Admin chat relay: any message type can be forwarded.
        open_rows = await db_execute(
            "SELECT * FROM tickets WHERE status='open' AND assigned_admin=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
            fetch=True,
        )
        if open_rows:
            ticket = open_rows[0]
            await bot.send_message(
                ticket["user_id"],
                "👨‍💼 <b>Администратор:</b>",
            )
            await copy_message_safe(
                bot,
                user_id,
                ticket["user_id"],
                message.message_id,
            )
            return True
        return False

    key = state["key"]

    if key == f"awaiting_price:{user_id}":
        if not message.text or not message.text.strip().isdigit():
            await message.answer("❌ Отправьте только целое число, например 299.")
            return True
        amount = int(message.text.strip())
        if amount <= 0 or amount > 10_000_000:
            await message.answer("❌ Некорректная сумма.")
            return True

        # Read the actual selected key from the state value.
        row = await db_execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
            fetchone=True,
        )
        selected = row["value"] if row else None
        if selected:
            await set_price(selected, amount)
        await db_execute("DELETE FROM settings WHERE key=?", (key,))
        await message.answer(
            f"✅ Цена изменена: <b>{amount} ₽</b>.",
            reply_markup=kb_admin_panel(),
        )
        return True

    if key == f"awaiting_admin_add:{user_id}":
        if not await is_main_admin(user_id):
            return True
        if not message.text or not message.text.strip().isdigit():
            await message.answer("❌ Нужен Telegram ID числом.")
            return True
        target = int(message.text.strip())
        await db_execute(
            "INSERT OR REPLACE INTO admins(user_id, is_main) VALUES (?, 0)",
            (target,),
        )
        await db_execute("DELETE FROM settings WHERE key=?", (key,))
        await message.answer(
            f"✅ Пользователь <code>{target}</code> назначен администратором.",
            reply_markup=kb_admin_panel(),
        )
        try:
            await bot.send_message(target, "🛡 Вам выдали права администратора JOKAS Audio.")
        except Exception:
            pass
        return True

    if key == f"awaiting_admin_remove:{user_id}":
        if not await is_main_admin(user_id):
            return True
        if not message.text or not message.text.strip().isdigit():
            await message.answer("❌ Нужен Telegram ID числом.")
            return True
        target = int(message.text.strip())
        if target == MAIN_ADMIN_ID:
            await message.answer("❌ Главного администратора снять нельзя.")
            return True
        await db_execute("DELETE FROM admins WHERE user_id=?", (target,))
        await db_execute("DELETE FROM settings WHERE key=?", (key,))
        await message.answer(
            f"✅ Права администратора сняты с <code>{target}</code>.",
            reply_markup=kb_admin_panel(),
        )
        try:
            await bot.send_message(target, "ℹ️ Ваши права администратора JOKAS Audio сняты.")
        except Exception:
            pass
        return True

    if key == f"awaiting_promo_create:{user_id}":
        if not message.text:
            return True
        parts = message.text.strip().split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("❌ Формат: <code>WELCOME 10</code>")
            return True
        code = parts[0].upper()
        discount = int(parts[1])
        if not 1 <= discount <= 100:
            await message.answer("❌ Скидка должна быть от 1 до 100%.")
            return True
        await db_execute(
            "INSERT OR REPLACE INTO promo_codes(code, discount, active) VALUES (?, ?, 1)",
            (code, discount),
        )
        await db_execute("DELETE FROM settings WHERE key=?", (key,))
        await message.answer(
            f"✅ Промокод <code>{code}</code> создан. Скидка: <b>{discount}%</b>.",
            reply_markup=kb_admin_panel(),
        )
        return True

    if key == f"awaiting_promo_toggle:{user_id}":
        if not message.text:
            return True
        code = message.text.strip().upper()
        row = await db_execute(
            "SELECT active FROM promo_codes WHERE code=?", (code,), fetchone=True
        )
        if not row:
            await message.answer("❌ Промокод не найден.")
            return True
        new_status = 0 if row["active"] else 1
        await db_execute(
            "UPDATE promo_codes SET active=? WHERE code=?",
            (new_status, code),
        )
        await db_execute("DELETE FROM settings WHERE key=?", (key,))
        await message.answer(
            f"✅ Промокод <code>{code}</code> теперь "
            f"{'активен' if new_status else 'выключен'}.",
            reply_markup=kb_admin_panel(),
        )
        return True

    return False


# -------------------- Startup --------------------

async def main():
    global bot
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
