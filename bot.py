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
DB_PATH = Path("bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")

if not MAIN_ADMIN_ID:
    raise RuntimeError("MAIN_ADMIN_ID не указан в .env")


router = Router()
dp = Dispatcher()
dp.include_router(router)

bot: Bot


# =========================================================
# DATABASE
# =========================================================

async def db_execute(
    query,
    params=(),
    fetch=False,
    fetchone=False,
):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(query, params)

        if fetch:
            result = await cursor.fetchall()
            await db.commit()
            return result

        if fetchone:
            result = await cursor.fetchone()
            await db.commit()
            return result

        await db.commit()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:

        await db.executescript(
            """
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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_tickets_user_status
            ON tickets(user_id, status);

            CREATE INDEX IF NOT EXISTS idx_tickets_admin_status
            ON tickets(assigned_admin, status);
            """
        )

        # Главный админ всегда существует
        await db.execute(
            """
            INSERT OR REPLACE INTO admins(user_id, is_main)
            VALUES (?, 1)
            """,
            (MAIN_ADMIN_ID,),
        )

        # Стандартные цены
        prices = [
            ("audio", "Заказать Аудио", 249),
            ("distrokid", "DistroKid: дистрибуция аудио — 1 год", 1649),
            ("subscription", "Подписка на загрузку аудио", 1000),
        ]

        for key, title, amount in prices:
            await db.execute(
                """
                INSERT OR IGNORE INTO prices(key, title, amount)
                VALUES (?, ?, ?)
                """,
                (key, title, amount),
            )

        await db.commit()

    # Старый ADMIN_IDS можно оставить в .env.
    # Эти пользователи автоматически получат админку.
    raw_admins = os.getenv("ADMIN_IDS", "")

    for value in raw_admins.split(","):
        value = value.strip()

        if value.isdigit():
            await db_execute(
                """
                INSERT OR IGNORE INTO admins(user_id, is_main)
                VALUES (?, 0)
                """,
                (int(value),),
            )


# =========================================================
# ADMIN
# =========================================================

async def is_admin(user_id: int) -> bool:
    row = await db_execute(
        """
        SELECT 1
        FROM admins
        WHERE user_id = ?
        """,
        (user_id,),
        fetchone=True,
    )

    return row is not None


async def is_main_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID


async def get_admin_ids():
    rows = await db_execute(
        """
        SELECT user_id
        FROM admins
        """,
        fetch=True,
    )

    return [int(row["user_id"]) for row in rows]


async def add_admin(user_id: int):
    await db_execute(
        """
        INSERT OR REPLACE INTO admins(user_id, is_main)
        VALUES (?, 0)
        """,
        (user_id,),
    )


async def remove_admin(user_id: int):
    if user_id == MAIN_ADMIN_ID:
        return False

    await db_execute(
        """
        DELETE FROM admins
        WHERE user_id = ?
        """,
        (user_id,),
    )

    # Если у админа были активные обращения,
    # они возвращаются в очередь поддержки.
    await db_execute(
        """
        UPDATE tickets
        SET assigned_admin = NULL
        WHERE assigned_admin = ?
        AND status = 'open'
        """,
        (user_id,),
    )

    # Удаляем активный выбранный тикет
    await db_execute(
        """
        DELETE FROM settings
        WHERE key = ?
        """,
        (f"active_ticket:{user_id}",),
    )

    return True


async def get_admin_count():
    row = await db_execute(
        """
        SELECT COUNT(*) AS count
        FROM admins
        """,
        fetchone=True,
    )

    return int(row["count"])


# =========================================================
# PRICES
# =========================================================

async def get_price(key):
    row = await db_execute(
        """
        SELECT title, amount
        FROM prices
        WHERE key = ?
        """,
        (key,),
        fetchone=True,
    )

    if not row:
        return "", 0

    return row["title"], int(row["amount"])


async def set_price(key, amount):
    await db_execute(
        """
        UPDATE prices
        SET amount = ?
        WHERE key = ?
        """,
        (amount, key),
    )


async def get_user_price(user_id, key):
    title, amount = await get_price(key)

    promo_row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (f"active_promo:{user_id}",),
        fetchone=True,
    )

    if not promo_row:
        return title, amount, None, 0

    promo = await db_execute(
        """
        SELECT code, discount, active
        FROM promo_codes
        WHERE code = ?
        """,
        (promo_row["value"],),
        fetchone=True,
    )

    if not promo or not promo["active"]:
        return title, amount, None, 0

    discount = int(promo["discount"])

    final_amount = max(
        0,
        round(amount * (100 - discount) / 100)
    )

    return (
        title,
        final_amount,
        promo["code"],
        discount,
    )


# =========================================================
# TICKETS
# =========================================================

async def get_open_ticket(user_id):
    return await db_execute(
        """
        SELECT *
        FROM tickets
        WHERE user_id = ?
        AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
        fetchone=True,
    )


async def get_ticket(ticket_id):
    return await db_execute(
        """
        SELECT *
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
        fetchone=True,
    )


async def create_ticket(user: Message):
    await db_execute(
        """
        INSERT INTO tickets(user_id, username)
        VALUES (?, ?)
        """,
        (
            user.from_user.id,
            user.from_user.username or "",
        ),
    )


async def assign_ticket(ticket_id, admin_id):
    await db_execute(
        """
        UPDATE tickets
        SET assigned_admin = ?
        WHERE id = ?
        AND status = 'open'
        """,
        (admin_id, ticket_id),
    )

    # Запоминаем, с каким обращением сейчас работает админ.
    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"active_ticket:{admin_id}",
            str(ticket_id),
        ),
    )


async def close_ticket(ticket_id):
    ticket = await get_ticket(ticket_id)

    if not ticket:
        return

    await db_execute(
        """
        UPDATE tickets
        SET status = 'closed',
            closed_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ticket_id,),
    )

    if ticket["assigned_admin"]:
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (f"active_ticket:{ticket['assigned_admin']}",),
        )


# =========================================================
# KEYBOARDS
# =========================================================

def kb_main():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎧 Заказать Аудио",
                    callback_data="order:audio",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔧 Тех. Поддержка",
                    callback_data="support",
                ),
                InlineKeyboardButton(
                    text="🧧 Ввести промокод",
                    callback_data="promo",
                ),
                InlineKeyboardButton(
                    text="🔄 Другое",
                    callback_data="other",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Получить данные от DistroKid",
                    callback_data="distrokid",
                )
            ],
        ]
    )


def kb_back():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="home",
                )
            ]
        ]
    )


def kb_support_start():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💬 Открыть обращение",
                    callback_data="ticket:create",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="home",
                )
            ],
        ]
    )


def kb_ticket_user():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Закрыть обращение",
                    callback_data="ticket:user_close",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ В меню",
                    callback_data="home",
                )
            ],
        ]
    )


def kb_admin_ticket(ticket_id, taken=False):
    buttons = []

    if not taken:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🙋 Взять обращение",
                    callback_data=f"ticket:take:{ticket_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="🔒 Закрыть",
                callback_data=f"ticket:close:{ticket_id}",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_admin_panel():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📨 Обращения",
                    callback_data="admin:tickets",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Цены",
                    callback_data="admin:prices",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎟 Промокоды",
                    callback_data="admin:promos",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Админы / Поддержка",
                    callback_data="admin:admins",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ В меню",
                    callback_data="home",
                )
            ],
        ]
    )


def kb_admins_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Выдать админку",
                    callback_data="admin:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➖ Снять админку",
                    callback_data="admin:remove",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Список админов",
                    callback_data="admin:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="admin:panel",
                )
            ],
        ]
    )


def kb_price_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎧 Аудио",
                    callback_data="admin:price:audio",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💿 DistroKid",
                    callback_data="admin:price:distrokid",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Подписка",
                    callback_data="admin:price:subscription",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="admin:panel",
                )
            ],
        ]
    )


# =========================================================
# MESSAGE HELPERS
# =========================================================

async def delete_message_safe(message: Message):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def replace_with(
    message: Message,
    text: str,
    keyboard=None,
):
    await delete_message_safe(message)

    return await message.answer(
        text,
        reply_markup=keyboard,
    )


async def copy_message_safe(
    from_chat_id,
    to_chat_id,
    message_id,
):
    try:
        await bot.copy_message(
            chat_id=to_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )

    except Exception as error:
        logging.warning(
            "Ошибка копирования сообщения: %s",
            error,
        )


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "<b>Добро пожаловать в JOKAS Audio 🎧</b>\n\n"
        "Выберите нужный раздел:",
        reply_markup=kb_main(),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return

    await message.answer(
        "<b>🛠 Админ-панель</b>\n\n"
        "Администратор также является сотрудником поддержки.\n"
        "Здесь можно управлять обращениями, ценами, "
        "промокодами и администраторами.",
        reply_markup=kb_admin_panel(),
    )


# =========================================================
# HOME
# =========================================================

@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    await call.answer()

    await replace_with(
        call.message,
        "<b>JOKAS Audio 🎧</b>\n\n"
        "Выберите нужный раздел:",
        kb_main(),
    )


# =========================================================
# AUDIO
# =========================================================

@router.callback_query(F.data == "order:audio")
async def cb_audio(call: CallbackQuery):
    await call.answer()

    title, amount, promo_code, discount = await get_user_price(
        call.from_user.id,
        "audio",
    )

    promo_line = ""

    if promo_code:
        promo_line = (
            f"\n🎟 Промокод "
            f"<code>{promo_code}</code>: -{discount}%\n"
        )

    await replace_with(
        call.message,
        f"🟢 <b>{title}</b>\n\n"
        f"Цена: <b>{amount} ₽</b>"
        f"{promo_line}\n"
        "Оплата проходит через менеджера.\n"
        "Нажмите кнопку ниже, чтобы открыть обращение.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Оплатить через менеджера",
                        callback_data="ticket:create:payment",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# =========================================================
# DISTROKID
# =========================================================

@router.callback_query(F.data == "distrokid")
async def cb_distrokid(call: CallbackQuery):
    await call.answer()

    title, amount, promo_code, discount = await get_user_price(
        call.from_user.id,
        "distrokid",
    )

    promo_line = ""

    if promo_code:
        promo_line = (
            f"\n🎟 Промокод "
            f"<code>{promo_code}</code>: -{discount}%\n"
        )

    await replace_with(
        call.message,
        f"🔑 <b>{title}</b>\n\n"
        f"Стоимость: <b>{amount} ₽</b>"
        f"{promo_line}\n"
        "Данные выдаются после оплаты через менеджера.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Оплатить через менеджера",
                        callback_data="ticket:create:payment",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# =========================================================
# OTHER
# =========================================================

@router.callback_query(F.data == "other")
async def cb_other(call: CallbackQuery):
    await call.answer()

    await replace_with(
        call.message,
        "📦 <b>Другие товары</b>\n\n"
        "• DistroKid — дистрибуция аудио на 1 год\n"
        "• Подписка на загрузку аудио\n\n"
        "Для заказа откройте обращение с менеджером.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Написать менеджеру",
                        callback_data="ticket:create:payment",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# =========================================================
# PROMO
# =========================================================

@router.callback_query(F.data == "promo")
async def cb_promo(call: CallbackQuery):
    await call.answer()

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_promo:{call.from_user.id}",
            "1",
        ),
    )

    await replace_with(
        call.message,
        "🎟 <b>Введите промокод</b>\n\n"
        "Отправьте код отдельным сообщением.\n\n"
        "Например:\n"
        "<code>WELCOME10</code>",
        kb_back(),
    )


# =========================================================
# SUPPORT
# =========================================================

@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    await call.answer()

    ticket = await get_open_ticket(
        call.from_user.id
    )

    if ticket:

        if ticket["assigned_admin"]:
            text = (
                "💬 <b>Ваше обращение</b>\n\n"
                f"Обращение: <b>#{ticket['id']}</b>\n"
                f"Администратор: "
                f"<code>{ticket['assigned_admin']}</code>\n\n"
                "Пишите сообщения прямо сюда."
            )

        else:
            text = (
                "💬 <b>Ваше обращение</b>\n\n"
                f"Обращение: <b>#{ticket['id']}</b>\n\n"
                "Обращение отправлено всем администраторам.\n"
                "Ожидайте, пока один из них его возьмёт."
            )

        await replace_with(
            call.message,
            text,
            kb_ticket_user(),
        )

        return

    await replace_with(
        call.message,
        "🔧 <b>Тех. Поддержка</b>\n\n"
        "Здесь можно открыть обращение с администраторами.\n\n"
        "После открытия все администраторы получат уведомление.\n"
        "Первый администратор, который возьмёт обращение, "
        "станет ответственным за него.",
        kb_support_start(),
    )


async def open_ticket_for_user(
    message: Message,
    reason="support",
):

    existing = await get_open_ticket(
        message.from_user.id
    )

    if existing:
        return existing

    await create_ticket(message)

    ticket = await get_open_ticket(
        message.from_user.id
    )

    if reason == "payment":
        reason_text = (
            "💳 Пользователь хочет оплатить "
            "товар через менеджера."
        )
    else:
        reason_text = (
            "🔧 Пользователь открыл обращение "
            "в поддержку."
        )

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "без username"
    )

    admin_text = (
        f"📨 <b>Новое обращение #{ticket['id']}</b>\n\n"
        f"👤 Пользователь: {message.from_user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n\n"
        f"{reason_text}\n\n"
        "Первый администратор, который нажмёт "
        "«Взять обращение», станет ответственным."
    )

    admin_ids = await get_admin_ids()

    for admin_id in admin_ids:

        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=kb_admin_ticket(
                    ticket["id"]
                ),
            )

        except Exception as error:
            logging.warning(
                "Не удалось отправить обращение админу %s: %s",
                admin_id,
                error,
            )

    return ticket


@router.callback_query(F.data.startswith("ticket:create"))
async def cb_create_ticket(call: CallbackQuery):

    reason = (
        "payment"
        if call.data.endswith(":payment")
        else "support"
    )

    ticket = await open_ticket_for_user(
        call.message,
        reason,
    )

    await call.answer(
        "Обращение создано."
    )

    await replace_with(
        call.message,
        f"💬 <b>Обращение #{ticket['id']}</b>\n\n"
        "Готово.\n"
        "Все администраторы получили уведомление.\n\n"
        "Пишите сообщения прямо сюда.",
        kb_ticket_user(),
    )


# =========================================================
# USER CLOSE TICKET
# =========================================================

@router.callback_query(F.data == "ticket:user_close")
async def cb_user_close(call: CallbackQuery):

    ticket = await get_open_ticket(
        call.from_user.id
    )

    if not ticket:
        await call.answer(
            "Открытых обращений нет.",
            show_alert=True,
        )
        return

    await close_ticket(
        ticket["id"]
    )

    if ticket["assigned_admin"]:

        try:
            await bot.send_message(
                ticket["assigned_admin"],
                f"🔒 Пользователь закрыл обращение "
                f"#{ticket['id']}.",
            )
        except Exception:
            pass

    await call.answer(
        "Обращение закрыто."
    )

    await replace_with(
        call.message,
        "🔒 <b>Обращение закрыто.</b>\n\n"
        "Вы вернулись в главное меню.",
        kb_main(),
    )


# =========================================================
# TAKE TICKET
# =========================================================

@router.callback_query(
    F.data.startswith("ticket:take:")
)
async def cb_take_ticket(call: CallbackQuery):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    ticket_id = int(
        call.data.rsplit(":", 1)[1]
    )

    ticket = await get_ticket(
        ticket_id
    )

    if not ticket:
        await call.answer(
            "Обращение не найдено.",
            show_alert=True,
        )
        return

    if ticket["status"] != "open":
        await call.answer(
            "Обращение уже закрыто.",
            show_alert=True,
        )
        return

    if (
        ticket["assigned_admin"]
        and ticket["assigned_admin"]
        != call.from_user.id
    ):
        await call.answer(
            "Это обращение уже взял другой администратор.",
            show_alert=True,
        )
        return

    await assign_ticket(
        ticket_id,
        call.from_user.id,
    )

    await call.answer(
        "Обращение взято."
    )

    try:
        await call.message.edit_text(
            (call.message.text or "")
            + "\n\n"
            "✅ <b>Взято вами.</b>",
            reply_markup=kb_admin_ticket(
                ticket_id,
                taken=True,
            ),
        )

    except TelegramBadRequest:
        pass

    await bot.send_message(
        ticket["user_id"],
        f"👨‍💼 <b>Администратор подключился.</b>\n\n"
        f"Обращение #{ticket_id}\n\n"
        "Теперь можете писать сообщения сюда.",
        reply_markup=kb_ticket_user(),
    )


# =========================================================
# CLOSE TICKET
# =========================================================

@router.callback_query(
    F.data.startswith("ticket:close:")
)
async def cb_admin_close_ticket(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    ticket_id = int(
        call.data.rsplit(":", 1)[1]
    )

    ticket = await get_ticket(
        ticket_id
    )

    if not ticket:
        await call.answer(
            "Обращение не найдено.",
            show_alert=True,
        )
        return

    # Главный админ может закрывать любое обращение.
    # Обычный админ — только своё.
    if (
        ticket["assigned_admin"]
        not in (None, call.from_user.id)
        and not await is_main_admin(
            call.from_user.id
        )
    ):
        await call.answer(
            "Закрыть это обращение может "
            "ответственный администратор "
            "или главный администратор.",
            show_alert=True,
        )
        return

    await close_ticket(
        ticket_id
    )

    await call.answer(
        "Обращение закрыто."
    )

    try:
        await call.message.edit_text(
            (call.message.text or "")
            + "\n\n🔒 <b>Закрыто.</b>",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            ticket["user_id"],
            f"🔒 <b>Обращение #{ticket_id} закрыто.</b>\n\n"
            "Если понадобится помощь, можно открыть новое обращение.",
            reply_markup=kb_main(),
        )
    except Exception:
        pass


# =========================================================
# ADMIN PANEL
# =========================================================

@router.callback_query(
    F.data == "admin:panel"
)
async def cb_admin_panel(call: CallbackQuery):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    await call.answer()

    await replace_with(
        call.message,
        "<b>🛠 Админ-панель</b>\n\n"
        "Администратор = сотрудник поддержки.\n\n"
        "Выберите раздел:",
        kb_admin_panel(),
    )


# =========================================================
# ADMIN PRICES
# =========================================================

@router.callback_query(
    F.data == "admin:prices"
)
async def cb_admin_prices(call: CallbackQuery):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    audio = await get_price("audio")
    distro = await get_price("distrokid")
    subscription = await get_price(
        "subscription"
    )

    await call.answer()

    await replace_with(
        call.message,
        "💰 <b>Цены</b>\n\n"
        f"🎧 Аудио: <b>{audio[1]} ₽</b>\n"
        f"💿 DistroKid: <b>{distro[1]} ₽</b>\n"
        f"📦 Подписка: <b>{subscription[1]} ₽</b>\n\n"
        "Выберите товар:",
        kb_price_menu(),
    )


@router.callback_query(
    F.data.startswith("admin:price:")
)
async def cb_admin_price(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    key = call.data.rsplit(
        ":",
        1
    )[1]

    title, amount = await get_price(
        key
    )

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_price:{call.from_user.id}",
            key,
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "✏️ <b>Изменение цены</b>\n\n"
        f"{title}\n"
        f"Текущая цена: <b>{amount} ₽</b>\n\n"
        "Отправьте новую цену одним сообщением.\n\n"
        "Например:\n"
        "<code>299</code>",
        kb_back(),
    )


# =========================================================
# ADMIN / SUPPORT MANAGEMENT
# =========================================================

@router.callback_query(
    F.data == "admin:admins"
)
async def cb_admin_admins(
    call: CallbackQuery,
):

    # Только главный админ может управлять админами.
    if not await is_main_admin(
        call.from_user.id
    ):
        await call.answer(
            "Только главный администратор может "
            "управлять администраторами.",
            show_alert=True,
        )
        return

    count = await get_admin_count()

    await call.answer()

    await replace_with(
        call.message,
        "👥 <b>Админы / Поддержка</b>\n\n"
        f"Сейчас сотрудников поддержки: <b>{count}</b>\n\n"
        "Каждый выданный здесь админ автоматически "
        "получает доступ к обращениям пользователей.\n\n"
        "Главный админ может выдавать и снимать права.",
        kb_admins_menu(),
    )


# =========================================================
# ADD ADMIN
# =========================================================

@router.callback_query(
    F.data == "admin:add"
)
async def cb_admin_add(
    call: CallbackQuery,
):

    if not await is_main_admin(
        call.from_user.id
    ):
        await call.answer(
            "Только главный администратор.",
            show_alert=True,
        )
        return

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_admin_add:{call.from_user.id}",
            "1",
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "➕ <b>Выдать админку</b>\n\n"
        "Отправьте Telegram ID пользователя.\n\n"
        "Например:\n"
        "<code>123456789</code>\n\n"
        "После выдачи этот пользователь сразу "
        "станет сотрудником поддержки и будет "
        "получать новые обращения.",
        kb_back(),
    )


# =========================================================
# REMOVE ADMIN
# =========================================================

@router.callback_query(
    F.data == "admin:remove"
)
async def cb_admin_remove(
    call: CallbackQuery,
):

    if not await is_main_admin(
        call.from_user.id
    ):
        await call.answer(
            "Только главный администратор.",
            show_alert=True,
        )
        return

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_admin_remove:{call.from_user.id}",
            "1",
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "➖ <b>Снять админку</b>\n\n"
        "Отправьте Telegram ID администратора.\n\n"
        "Главного администратора снять нельзя.\n\n"
        "Если у снимаемого администратора есть "
        "открытые обращения, они вернутся в общую "
        "очередь поддержки.",
        kb_back(),
    )


# =========================================================
# ADMIN LIST
# =========================================================

@router.callback_query(
    F.data == "admin:list"
)
async def cb_admin_list(
    call: CallbackQuery,
):

    if not await is_main_admin(
        call.from_user.id
    ):
        await call.answer(
            "Только главный администратор.",
            show_alert=True,
        )
        return

    rows = await db_execute(
        """
        SELECT user_id, is_main
        FROM admins
        ORDER BY is_main DESC, user_id
        """,
        fetch=True,
    )

    lines = [
        "👥 <b>Администраторы / поддержка</b>\n"
    ]

    for row in rows:

        if row["is_main"]:
            role = "👑 Главный админ"
        else:
            role = "🛡 Админ / поддержка"

        lines.append(
            f"{role}\n"
            f"ID: <code>{row['user_id']}</code>\n"
        )

    await call.answer()

    await replace_with(
        call.message,
        "\n".join(lines),
        kb_admins_menu(),
    )


# =========================================================
# ADMIN TICKETS
# =========================================================

@router.callback_query(
    F.data == "admin:tickets"
)
async def cb_admin_tickets(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    rows = await db_execute(
        """
        SELECT *
        FROM tickets
        WHERE status = 'open'
        ORDER BY id DESC
        LIMIT 50
        """,
        fetch=True,
    )

    if not rows:

        text = (
            "📨 <b>Обращения</b>\n\n"
            "Открытых обращений нет."
        )

    else:

        lines = [
            "📨 <b>Открытые обращения</b>\n"
        ]

        for row in rows:

            if row["assigned_admin"]:
                status = (
                    "👨‍💼 "
                    f"админ <code>{row['assigned_admin']}</code>"
                )
            else:
                status = "⏳ ожидает администратора"

            lines.append(
                f"#{row['id']} — "
                f"{status}\n"
                f"Пользователь: "
                f"<code>{row['user_id']}</code>\n"
            )

        text = "\n".join(lines)

    await call.answer()

    await replace_with(
        call.message,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data="admin:panel",
                    )
                ]
            ]
        ),
    )


# =========================================================
# PROMOCODES
# =========================================================

@router.callback_query(
    F.data == "admin:promos"
)
async def cb_admin_promos(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    rows = await db_execute(
        """
        SELECT code, discount, active, uses
        FROM promo_codes
        ORDER BY code
        """,
        fetch=True,
    )

    if rows:

        lines = [
            "🎟 <b>Промокоды</b>\n"
        ]

        for row in rows:

            status = (
                "активен"
                if row["active"]
                else "выключен"
            )

            lines.append(
                f"<code>{row['code']}</code> — "
                f"{row['discount']}% — "
                f"{status} — "
                f"использований: {row['uses']}"
            )

        text = "\n".join(lines)

    else:

        text = (
            "🎟 <b>Промокоды</b>\n\n"
            "Промокодов пока нет."
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Создать промокод",
                    callback_data="admin:promo:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Включить/выключить",
                    callback_data="admin:promo:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Назад",
                    callback_data="admin:panel",
                )
            ],
        ]
    )

    await call.answer()

    await replace_with(
        call.message,
        text,
        keyboard,
    )


@router.callback_query(
    F.data == "admin:promo:add"
)
async def cb_promo_add(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_promo_create:{call.from_user.id}",
            "1",
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "➕ <b>Создание промокода</b>\n\n"
        "Отправьте:\n"
        "<code>КОД СКИДКА</code>\n\n"
        "Например:\n"
        "<code>WELCOME 10</code>",
        kb_back(),
    )


@router.callback_query(
    F.data == "admin:promo:toggle"
)
async def cb_promo_toggle(
    call: CallbackQuery,
):

    if not await is_admin(
        call.from_user.id
    ):
        await call.answer(
            "Нет доступа.",
            show_alert=True,
        )
        return

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(key, value)
        VALUES (?, ?)
        """,
        (
            f"awaiting_promo_toggle:{call.from_user.id}",
            "1",
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "🔁 <b>Включение/выключение промокода</b>\n\n"
        "Отправьте код промокода.",
        kb_back(),
    )


# =========================================================
# UNIVERSAL MESSAGE
# =========================================================

@router.message()
async def universal_message(
    message: Message,
):

    user_id = message.from_user.id

    # -----------------------------------------------------
    # ADMIN INPUT
    # -----------------------------------------------------

    if await is_admin(user_id):

        handled = await handle_admin_input(
            message
        )

        if handled:
            return

    # -----------------------------------------------------
    # USER PROMO
    # -----------------------------------------------------

    waiting_promo = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_promo:{user_id}",
        ),
        fetchone=True,
    )

    if waiting_promo and message.text:

        code = message.text.strip().upper()

        promo = await db_execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code = ?
            AND active = 1
            """,
            (code,),
            fetchone=True,
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"awaiting_promo:{user_id}",
            ),
        )

        if promo:

            await db_execute(
                """
                INSERT OR REPLACE INTO settings(key, value)
                VALUES (?, ?)
                """,
                (
                    f"active_promo:{user_id}",
                    code,
                ),
            )

            await db_execute(
                """
                UPDATE promo_codes
                SET uses = uses + 1
                WHERE code = ?
                """,
                (code,),
            )

            await message.answer(
                f"✅ Промокод <code>{code}</code> активирован.\n\n"
                f"Скидка: <b>{promo['discount']}%</b>\n\n"
                "Теперь скидка будет автоматически "
                "учитываться при оформлении заказа.",
                reply_markup=kb_main(),
            )

        else:

            await message.answer(
                "❌ Промокод не найден или выключен.",
                reply_markup=kb_main(),
            )

        return

    # -----------------------------------------------------
    # USER SUPPORT MESSAGE
    # -----------------------------------------------------

    ticket = await get_open_ticket(
        user_id
    )

    if ticket:

        await relay_user_message(
            message,
            ticket,
        )

        return


# =========================================================
# ADMIN INPUT HANDLER
# =========================================================

async def handle_admin_input(
    message: Message,
):

    user_id = message.from_user.id

    states = [
        f"awaiting_price:{user_id}",
        f"awaiting_admin_add:{user_id}",
        f"awaiting_admin_remove:{user_id}",
        f"awaiting_promo_create:{user_id}",
        f"awaiting_promo_toggle:{user_id}",
    ]

    state = None

    for key in states:

        row = await db_execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (key,),
            fetchone=True,
        )

        if row:
            state = key
            break

    # -----------------------------------------------------
    # NO ADMIN FORM STATE
    # -----------------------------------------------------

    if not state:

        # Ответ администратора идёт
        # в выбранное им активное обращение.
        active = await db_execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (
                f"active_ticket:{user_id}",
            ),
            fetchone=True,
        )

        if active:

            ticket_id = int(
                active["value"]
            )

            ticket = await get_ticket(
                ticket_id
            )

            if (
                ticket
                and ticket["status"] == "open"
                and ticket["assigned_admin"]
                == user_id
            ):

                await bot.send_message(
                    ticket["user_id"],
                    "👨‍💼 <b>Администратор:</b>",
                )

                await copy_message_safe(
                    user_id,
                    ticket["user_id"],
                    message.message_id,
                )

                return True

        return False

    # -----------------------------------------------------
    # CHANGE PRICE
    # -----------------------------------------------------

    if state == f"awaiting_price:{user_id}":

        if (
            not message.text
            or not message.text.strip().isdigit()
        ):
            await message.answer(
                "❌ Отправьте только число.\n"
                "Например: <code>299</code>"
            )
            return True

        amount = int(
            message.text.strip()
        )

        if amount <= 0:
            await message.answer(
                "❌ Цена должна быть больше нуля."
            )
            return True

        row = await db_execute(
            """
            SELECT value
            FROM settings
            WHERE key = ?
            """,
            (state,),
            fetchone=True,
        )

        if row:

            price_key = row["value"]

            await set_price(
                price_key,
                amount,
            )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (state,),
        )

        await message.answer(
            f"✅ Цена изменена на "
            f"<b>{amount} ₽</b>.",
            reply_markup=kb_admin_panel(),
        )

        return True

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    if state == f"awaiting_admin_add:{user_id}":

        if not await is_main_admin(user_id):
            return True

        if (
            not message.text
            or not message.text.strip().isdigit()
        ):
            await message.answer(
                "❌ Telegram ID должен быть числом."
            )
            return True

        target_id = int(
            message.text.strip()
        )

        if target_id == MAIN_ADMIN_ID:

            await message.answer(
                "ℹ️ Этот пользователь уже является "
                "главным администратором."
            )

            await db_execute(
                """
                DELETE FROM settings
                WHERE key = ?
                """,
                (state,),
            )

            return True

        await add_admin(
            target_id
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (state,),
        )

        await message.answer(
            "✅ <b>Админка выдана.</b>\n\n"
            f"ID: <code>{target_id}</code>\n\n"
            "Теперь этот пользователь является "
            "администратором и сотрудником поддержки.\n"
            "Он будет получать новые обращения.",
            reply_markup=kb_admin_panel(),
        )

        try:

            await bot.send_message(
                target_id,
                "🛡 <b>Вам выдали админку JOKAS Audio.</b>\n\n"
                "Теперь вы сотрудник поддержки.\n"
                "Новые обращения пользователей будут "
                "приходить вам в этот бот.\n\n"
                "Для управления используйте:\n"
                "<code>/admin</code>",
            )

        except Exception:
            pass

        return True

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    if state == f"awaiting_admin_remove:{user_id}":

        if not await is_main_admin(user_id):
            return True

        if (
            not message.text
            or not message.text.strip().isdigit()
        ):
            await message.answer(
                "❌ Telegram ID должен быть числом."
            )
            return True

        target_id = int(
            message.text.strip()
        )

        if target_id == MAIN_ADMIN_ID:

            await message.answer(
                "❌ Главного администратора снять нельзя."
            )

            return True

        existed = await is_admin(
            target_id
        )

        if not existed:

            await message.answer(
                "❌ Этот пользователь не является администратором."
            )

            await db_execute(
                """
                DELETE FROM settings
                WHERE key = ?
                """,
                (state,),
            )

            return True

        await remove_admin(
            target_id
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (state,),
        )

        await message.answer(
            "✅ <b>Админка снята.</b>\n\n"
            f"ID: <code>{target_id}</code>\n\n"
            "Пользователь больше не получает "
            "новые обращения поддержки.",
            reply_markup=kb_admin_panel(),
        )

        try:

            await bot.send_message(
                target_id,
                "ℹ️ <b>Ваша админка JOKAS Audio снята.</b>\n\n"
                "Вы больше не являетесь сотрудником поддержки.",
            )

        except Exception:
            pass

        return True

    # -----------------------------------------------------
    # CREATE PROMO
    # -----------------------------------------------------

    if state == f"awaiting_promo_create:{user_id}":

        if not message.text:
            return True

        parts = message.text.strip().split()

        if len(parts) != 2:
            await message.answer(
                "❌ Формат:\n"
                "<code>WELCOME 10</code>"
            )
            return True

        code = parts[0].upper()

        if not parts[1].isdigit():

            await message.answer(
                "❌ Скидка должна быть числом."
            )
            return True

        discount = int(
            parts[1]
        )

        if not 1 <= discount <= 100:

            await message.answer(
                "❌ Скидка должна быть от 1 до 100%."
            )
            return True

        await db_execute(
            """
            INSERT OR REPLACE INTO promo_codes(
                code,
                discount,
                active
            )
            VALUES (?, ?, 1)
            """,
            (
                code,
                discount,
            ),
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (state,),
        )

        await message.answer(
            "✅ <b>Промокод создан.</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"Скидка: <b>{discount}%</b>",
            reply_markup=kb_admin_panel(),
        )

        return True

    # -----------------------------------------------------
    # TOGGLE PROMO
    # -----------------------------------------------------

    if state == f"awaiting_promo_toggle:{user_id}":

        if not message.text:
            return True

        code = message.text.strip().upper()

        promo = await db_execute(
            """
            SELECT active
            FROM promo_codes
            WHERE code = ?
            """,
            (code,),
            fetchone=True,
        )

        if not promo:

            await message.answer(
                "❌ Промокод не найден."
            )

            return True

        new_status = (
            0
            if promo["active"]
            else 1
        )

        await db_execute(
            """
            UPDATE promo_codes
            SET active = ?
            WHERE code = ?
            """,
            (
                new_status,
                code,
            ),
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (state,),
        )

        await message.answer(
            f"✅ Промокод <code>{code}</code> "
            f"{'включён' if new_status else 'выключен'}.",
            reply_markup=kb_admin_panel(),
        )

        return True

    return False


# =========================================================
# USER -> ADMIN
# =========================================================

async def relay_user_message(
    message: Message,
    ticket,
):

    assigned_admin = ticket[
        "assigned_admin"
    ]

    if not assigned_admin:

        await message.answer(
            "⏳ <b>Обращение ещё никто не взял.</b>\n\n"
            "Ваше сообщение сохранено.\n"
            "Дождитесь администратора.",
        )

        return

    await bot.send_message(
        assigned_admin,
        f"👤 <b>Обращение #{ticket['id']}</b>\n\n"
        f"Пользователь ID: "
        f"<code>{message.from_user.id}</code>\n\n"
        "Новое сообщение:",
    )

    await copy_message_safe(
        message.from_user.id,
        assigned_admin,
        message.message_id,
    )


# =========================================================
# STARTUP
# =========================================================

async def main():

    global bot

    await init_db()

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())
