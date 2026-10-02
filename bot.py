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

# =========================================================
# CONFIG
# =========================================================

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
                full_name TEXT,
                reason TEXT DEFAULT 'support',
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

        await db.execute(
            """
            INSERT OR REPLACE INTO admins(user_id, is_main)
            VALUES (?, 1)
            """,
            (MAIN_ADMIN_ID,),
        )

        prices = [
            (
                "audio",
                "Заказать Аудио",
                249,
            ),
            (
                "distrokid",
                "DistroKid: дистрибуция аудио - 1 год",
                1649,
            ),
            (
                "subscription",
                "Подписка на загрузку аудио",
                1000,
            ),
        ]

        for key, title, amount in prices:
            await db.execute(
                """
                INSERT OR IGNORE INTO prices(
                    key,
                    title,
                    amount
                )
                VALUES (?, ?, ?)
                """,
                (
                    key,
                    title,
                    amount,
                ),
            )

        await db.commit()

    raw_admins = os.getenv("ADMIN_IDS", "")

    for value in raw_admins.split(","):
        value = value.strip()

        if value.isdigit():
            await db_execute(
                """
                INSERT OR IGNORE INTO admins(
                    user_id,
                    is_main
                )
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

    return [
        int(row["user_id"])
        for row in rows
    ]


async def add_admin(user_id: int):
    await db_execute(
        """
        INSERT OR REPLACE INTO admins(
            user_id,
            is_main
        )
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

    await db_execute(
        """
        UPDATE tickets
        SET assigned_admin = NULL
        WHERE assigned_admin = ?
        AND status = 'open'
        """,
        (user_id,),
    )

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

    return (
        row["title"],
        int(row["amount"]),
    )


async def set_price(key, amount):
    await db_execute(
        """
        UPDATE prices
        SET amount = ?
        WHERE key = ?
        """,
        (
            amount,
            key,
        ),
    )


async def get_user_price(user_id, key):
    title, amount = await get_price(key)

    promo_row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"active_promo:{user_id}",
        ),
        fetchone=True,
    )

    if not promo_row:
        return (
            title,
            amount,
            None,
            0,
        )

    promo = await db_execute(
        """
        SELECT code, discount, active
        FROM promo_codes
        WHERE code = ?
        """,
        (
            promo_row["value"],
        ),
        fetchone=True,
    )

    if not promo or not promo["active"]:
        return (
            title,
            amount,
            None,
            0,
        )

    discount = int(promo["discount"])

    final_amount = max(
        0,
        round(
            amount
            * (100 - discount)
            / 100
        ),
    )

    return (
        title,
        final_amount,
        promo["code"],
        discount,
    )


# =========================================================
# PROMO
# =========================================================

async def activate_promo(
    user_id: int,
    code: str,
):
    code = code.strip().upper()

    row = await db_execute(
        """
        SELECT code, discount, active
        FROM promo_codes
        WHERE code = ?
        """,
        (code,),
        fetchone=True,
    )

    if not row:
        return False, "Промокод не найден."

    if not row["active"]:
        return False, "Этот промокод выключен."

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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

    return (
        True,
        f"Промокод активирован: скидка {row['discount']}%.",
    )


# =========================================================
# TICKETS
# =========================================================

async def get_open_ticket(user_id: int):
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


async def get_ticket(ticket_id: int):
    return await db_execute(
        """
        SELECT *
        FROM tickets
        WHERE id = ?
        """,
        (ticket_id,),
        fetchone=True,
    )


async def create_ticket(
    user_id: int,
    username: str,
    full_name: str,
    reason: str,
):
    await db_execute(
        """
        INSERT INTO tickets(
            user_id,
            username,
            full_name,
            reason
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            full_name,
            reason,
        ),
    )


async def assign_ticket(
    ticket_id: int,
    admin_id: int,
):
    ticket = await get_ticket(ticket_id)

    if not ticket:
        return False

    if ticket["status"] != "open":
        return False

    if (
        ticket["assigned_admin"]
        and ticket["assigned_admin"] != admin_id
    ):
        return False

    await db_execute(
        """
        UPDATE tickets
        SET assigned_admin = ?
        WHERE id = ?
        AND status = 'open'
        """,
        (
            admin_id,
            ticket_id,
        ),
    )

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
        VALUES (?, ?)
        """,
        (
            f"active_ticket:{admin_id}",
            str(ticket_id),
        ),
    )

    return True


async def get_active_admin_ticket(
    admin_id: int,
):
    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"active_ticket:{admin_id}",
        ),
        fetchone=True,
    )

    if not row:
        return None

    try:
        ticket_id = int(row["value"])
    except ValueError:
        return None

    ticket = await get_ticket(ticket_id)

    if not ticket:
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"active_ticket:{admin_id}",
            ),
        )
        return None

    if ticket["status"] != "open":
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"active_ticket:{admin_id}",
            ),
        )
        return None

    if ticket["assigned_admin"] != admin_id:
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"active_ticket:{admin_id}",
            ),
        )
        return None

    return ticket


async def close_ticket(ticket_id: int):
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
            (
                f"active_ticket:{ticket['assigned_admin']}",
            ),
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


def kb_admin_ticket(
    ticket_id: int,
    taken=False,
):
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

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


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


def kb_promo_admin():
    return InlineKeyboardMarkup(
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


# =========================================================
# MESSAGE HELPERS
# =========================================================

async def delete_message_safe(
    message: Message,
):
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception:
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
        return True

    except Exception as error:
        logging.warning(
            "Ошибка копирования сообщения: %s",
            error,
        )
        return False


# =========================================================
# START
# =========================================================

@router.message(CommandStart())
async def cmd_start(
    message: Message,
):
    await message.answer(
        "<b>Добро пожаловать в JOKAS Audio 🎧</b>\n\n"
        "Выберите нужный раздел:",
        reply_markup=kb_main(),
    )


# =========================================================
# ADMIN COMMAND
# =========================================================

@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
):
    if not await is_admin(
        message.from_user.id
    ):
        await message.answer(
            "❌ У вас нет доступа к админ-панели."
        )
        return

    await message.answer(
        "<b>🛠 Админ-панель</b>\n\n"
        "Администратор также является сотрудником поддержки.\n"
        "Выберите раздел:",
        reply_markup=kb_admin_panel(),
    )


# =========================================================
# HOME
# =========================================================

@router.callback_query(
    F.data == "home"
)
async def cb_home(
    call: CallbackQuery,
):
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

@router.callback_query(
    F.data == "order:audio"
)
async def cb_audio(
    call: CallbackQuery,
):
    await call.answer()

    title, amount, promo_code, discount = (
        await get_user_price(
            call.from_user.id,
            "audio",
        )
    )

    promo_line = ""

    if promo_code:
        promo_line = (
            f"\n🎟 Промокод "
            f"<code>{promo_code}</code>: "
            f"-{discount}%\n"
        )

    await replace_with(
        call.message,
        f"🟢 <b>{title}</b>\n\n"
        f"Цена: <b>{amount} ₽</b>"
        f"{promo_line}\n"
        "Оплата проходит через менеджера.\n"
        "Нажмите кнопку ниже:",
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

@router.callback_query(
    F.data == "distrokid"
)
async def cb_distrokid(
    call: CallbackQuery,
):
    await call.answer()

    title, amount, promo_code, discount = (
        await get_user_price(
            call.from_user.id,
            "distrokid",
        )
    )

    promo_line = ""

    if promo_code:
        promo_line = (
            f"\n🎟 Промокод "
            f"<code>{promo_code}</code>: "
            f"-{discount}%\n"
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

@router.callback_query(
    F.data == "other"
)
async def cb_other(
    call: CallbackQuery,
):
    await call.answer()

    await replace_with(
        call.message,
        "📦 <b>Другие товары</b>\n\n"
        "• DistroKid - дистрибуция аудио на 1 год\n"
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
# PROMO USER
# =========================================================

@router.callback_query(
    F.data == "promo"
)
async def cb_promo(
    call: CallbackQuery,
):
    await call.answer()

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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

@router.callback_query(
    F.data == "support"
)
async def cb_support(
    call: CallbackQuery,
):
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
                "Обращение отправлено администраторам.\n"
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
        "станет ответственным.",
        kb_support_start(),
    )


# =========================================================
# CREATE TICKET
# =========================================================

async def open_ticket_for_user(
    user_id: int,
    username: str,
    full_name: str,
    reason: str = "support",
):
    # ВАЖНО:
    # user_id приходит именно от пользователя,
    # а НЕ из call.message.from_user.

    existing = await get_open_ticket(
        user_id
    )

    if existing:
        return existing

    await create_ticket(
        user_id=user_id,
        username=username,
        full_name=full_name,
        reason=reason,
    )

    ticket = await get_open_ticket(
        user_id
    )

    if not ticket:
        raise RuntimeError(
            "Не удалось создать тикет."
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

    username_text = (
        f"@{username}"
        if username
        else "без username"
    )

    full_name_text = (
        full_name
        if full_name
        else "Без имени"
    )

    admin_text = (
        f"📨 <b>Новое обращение #{ticket['id']}</b>\n\n"
        f"👤 <b>Пользователь:</b> "
        f"{full_name_text}\n"
        f"🔗 <b>Username:</b> "
        f"{username_text}\n"
        f"🆔 <b>ID:</b> "
        f"<code>{user_id}</code>\n\n"
        f"{reason_text}\n\n"
        "Первый администратор, который нажмёт "
        "«Взять обращение», станет ответственным."
    )

    admin_ids = await get_admin_ids()

    sent_count = 0

    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id,
                admin_text,
                reply_markup=kb_admin_ticket(
                    ticket["id"]
                ),
            )

            sent_count += 1

        except Exception as error:
            logging.warning(
                "Не удалось отправить тикет "
                "#%s админу %s: %s",
                ticket["id"],
                admin_id,
                error,
            )

    logging.info(
        "Создан тикет #%s. "
        "user_id=%s username=%s "
        "admins=%s/%s",
        ticket["id"],
        user_id,
        username,
        sent_count,
        len(admin_ids),
    )

    return ticket


@router.callback_query(
    F.data.startswith("ticket:create")
)
async def cb_create_ticket(
    call: CallbackQuery,
):
    reason = (
        "payment"
        if call.data.endswith(":payment")
        else "support"
    )

    # ВАЖНО:
    # call.from_user - реальный пользователь,
    # который нажал кнопку.
    ticket = await open_ticket_for_user(
        user_id=call.from_user.id,
        username=call.from_user.username or "",
        full_name=call.from_user.full_name or "",
        reason=reason,
    )

    await call.answer(
        "Обращение создано."
    )

    await replace_with(
        call.message,
        f"💬 <b>Обращение #{ticket['id']}</b>\n\n"
        "Готово.\n"
        "Администраторы получили уведомление.\n\n"
        "Пишите сообщения прямо сюда.",
        kb_ticket_user(),
    )


# =========================================================
# USER CLOSE TICKET
# =========================================================

@router.callback_query(
    F.data == "ticket:user_close"
)
async def cb_user_close(
    call: CallbackQuery,
):
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
                f"🔒 Пользователь закрыл "
                f"обращение #{ticket['id']}.",
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
async def cb_take_ticket(
    call: CallbackQuery,
):
    admin_id = call.from_user.id

    if not await is_admin(admin_id):
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
        and ticket["assigned_admin"] != admin_id
    ):
        await call.answer(
            "Это обращение уже взял другой администратор.",
            show_alert=True,
        )
        return

    # Если админ уже ведёт другой тикет,
    # переключаем активный тикет на новый.
    old_ticket = await get_active_admin_ticket(
        admin_id
    )

    if old_ticket and old_ticket["id"] != ticket_id:
        await call.answer(
            "Сначала закройте текущее активное обращение.",
            show_alert=True,
        )
        return

    success = await assign_ticket(
        ticket_id,
        admin_id,
    )

    if not success:
        await call.answer(
            "Не удалось взять обращение.",
            show_alert=True,
        )
        return

    await call.answer(
        "Обращение взято."
    )

    try:
        old_text = call.message.text or ""

        await call.message.edit_text(
            old_text
            + "\n\n"
            "✅ <b>Взято вами.</b>\n\n"
            "Теперь отправляйте сообщения "
            "обычным текстом в этот чат с ботом.",
            reply_markup=kb_admin_ticket(
                ticket_id,
                taken=True,
            ),
        )

    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            ticket["user_id"],
            f"👨‍💼 <b>Администратор подключился.</b>\n\n"
            f"Обращение #{ticket_id}\n\n"
            "Теперь можете писать сообщения сюда.",
            reply_markup=kb_ticket_user(),
        )
    except Exception as error:
        logging.warning(
            "Не удалось уведомить пользователя "
            "%s о назначении тикета: %s",
            ticket["user_id"],
            error,
        )


# =========================================================
# CLOSE TICKET ADMIN
# =========================================================

@router.callback_query(
    F.data.startswith("ticket:close:")
)
async def cb_admin_close_ticket(
    call: CallbackQuery,
):
    admin_id = call.from_user.id

    if not await is_admin(admin_id):
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
        not in (
            None,
            admin_id,
        )
        and not await is_main_admin(admin_id)
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
async def cb_admin_panel(
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
async def cb_admin_prices(
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
        1,
    )[1]

    title, amount = await get_price(
        key
    )

    await db_execute(
        """
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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
# ADMIN MANAGEMENT
# =========================================================

@router.callback_query(
    F.data == "admin:admins"
)
async def cb_admin_admins(
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

    count = await get_admin_count()

    await call.answer()

    await replace_with(
        call.message,
        "👥 <b>Админы / Поддержка</b>\n\n"
        f"Сейчас сотрудников поддержки: <b>{count}</b>\n\n"
        "Каждый выданный здесь админ "
        "получает новые обращения.\n\n"
        "Главный админ может выдавать "
        "и снимать права.",
        kb_admins_menu(),
    )


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
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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
        "После выдачи пользователь станет "
        "администратором и сотрудником поддержки.",
        kb_back(),
    )


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
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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
        "Главного администратора снять нельзя.",
        kb_back(),
    )


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
# ADMIN TICKETS LIST
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
                    "👨‍💼 админ "
                    f"<code>{row['assigned_admin']}</code>"
                )
            else:
                status = "⏳ ожидает администратора"

            username = (
                f"@{row['username']}"
                if row["username"]
                else "без username"
            )

            lines.append(
                f"#{row['id']} - {status}\n"
                f"👤 {row['full_name'] or 'Без имени'}\n"
                f"🔗 {username}\n"
                f"🆔 <code>{row['user_id']}</code>\n"
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
# PROMO ADMIN
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
                f"<code>{row['code']}</code> - "
                f"{row['discount']}% - "
                f"{status} - "
                f"использований: {row['uses']}"
            )

        text = "\n".join(lines)
    else:
        text = (
            "🎟 <b>Промокоды</b>\n\n"
            "Промокодов пока нет."
        )

    await call.answer()

    await replace_with(
        call.message,
        text,
        kb_promo_admin(),
    )


@router.callback_query(
    F.data == "admin:promo:add"
)
async def cb_admin_promo_add(
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
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
        VALUES (?, ?)
        """,
        (
            f"awaiting_promo_add:{call.from_user.id}",
            "1",
        ),
    )

    await call.answer()

    await replace_with(
        call.message,
        "➕ <b>Создание промокода</b>\n\n"
        "Отправьте промокод и скидку через пробел.\n\n"
        "Например:\n"
        "<code>WELCOME10 10</code>\n\n"
        "Это создаст код WELCOME10 "
        "со скидкой 10%.",
        kb_back(),
    )


@router.callback_query(
    F.data == "admin:promo:toggle"
)
async def cb_admin_promo_toggle(
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
        INSERT OR REPLACE INTO settings(
            key,
            value
        )
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
        "🔁 <b>Включение / выключение</b>\n\n"
        "Отправьте код промокода.\n\n"
        "Например:\n"
        "<code>WELCOME10</code>",
        kb_back(),
    )


# =========================================================
# TEXT MESSAGE ROUTER
# =========================================================

async def handle_promo_input(
    message: Message,
):
    user_id = message.from_user.id

    row = await db_execute(
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

    if not row:
        return False

    await db_execute(
        """
        DELETE FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_promo:{user_id}",
        ),
    )

    success, result = await activate_promo(
        user_id,
        message.text or "",
    )

    await message.answer(
        (
            "✅ "
            if success
            else "❌ "
        )
        + result,
        reply_markup=kb_main(),
    )

    return True


async def handle_admin_state(
    message: Message,
):
    admin_id = message.from_user.id

    if not await is_admin(admin_id):
        return False

    text = (message.text or "").strip()

    # -----------------------------------------------------
    # ADD ADMIN
    # -----------------------------------------------------

    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_admin_add:{admin_id}",
        ),
        fetchone=True,
    )

    if row:
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"awaiting_admin_add:{admin_id}",
            ),
        )

        if not await is_main_admin(admin_id):
            await message.answer(
                "❌ Только главный администратор "
                "может выдавать админку."
            )
            return True

        if not text.isdigit():
            await message.answer(
                "❌ ID должен состоять только из цифр."
            )
            return True

        new_admin_id = int(text)

        await add_admin(
            new_admin_id
        )

        await message.answer(
            "✅ <b>Админка выдана.</b>\n\n"
            f"ID: <code>{new_admin_id}</code>\n\n"
            "Теперь этот пользователь является "
            "администратором и сотрудником поддержки."
        )

        try:
            await bot.send_message(
                new_admin_id,
                "🛡 <b>Вам выдан доступ администратора "
                "JOKAS Audio.</b>\n\n"
                "Откройте бота и отправьте /start.\n"
                "Для панели администратора используйте /admin.",
            )
        except Exception as error:
            logging.warning(
                "Не удалось уведомить нового админа %s: %s",
                new_admin_id,
                error,
            )

        return True

    # -----------------------------------------------------
    # REMOVE ADMIN
    # -----------------------------------------------------

    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_admin_remove:{admin_id}",
        ),
        fetchone=True,
    )

    if row:
        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"awaiting_admin_remove:{admin_id}",
            ),
        )

        if not await is_main_admin(admin_id):
            await message.answer(
                "❌ Только главный администратор."
            )
            return True

        if not text.isdigit():
            await message.answer(
                "❌ ID должен состоять только из цифр."
            )
            return True

        remove_id = int(text)

        if remove_id == MAIN_ADMIN_ID:
            await message.answer(
                "❌ Главного администратора снять нельзя."
            )
            return True

        removed = await remove_admin(
            remove_id
        )

        if removed:
            await message.answer(
                "✅ <b>Админка снята.</b>\n\n"
                f"ID: <code>{remove_id}</code>"
            )
        else:
            await message.answer(
                "❌ Не удалось снять админку."
            )

        return True

    # -----------------------------------------------------
    # PRICE
    # -----------------------------------------------------

    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_price:{admin_id}",
        ),
        fetchone=True,
    )

    if row:
        key = row["value"]

        if not text.isdigit():
            await message.answer(
                "❌ Цена должна быть числом."
            )
            return True

        amount = int(text)

        if amount < 0:
            await message.answer(
                "❌ Цена не может быть отрицательной."
            )
            return True

        await set_price(
            key,
            amount,
        )

        await db_execute(
            """
            DELETE FROM settings
            WHERE key = ?
            """,
            (
                f"awaiting_price:{admin_id}",
            ),
        )

        await message.answer(
            "✅ Цена изменена.\n\n"
            f"Новая цена: <b>{amount} ₽</b>",
            reply_markup=kb_admin_panel(),
        )

        return True

    # -----------------------------------------------------
    # PROMO ADD
    # -----------------------------------------------------

    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_promo_add:{admin_id}",
        ),
        fetchone=True,
    )

    if row:
        parts = text.split()

        if len(parts) != 2:
            await message.answer(
                "❌ Формат:\n"
                "<code>CODE 10</code>"
            )
            return True

        code = parts[0].upper()

        try:
            discount = int(parts[1])
        except ValueError:
            await message.answer(
                "❌ Скидка должна быть числом."
            )
            return True

        if not 0 <= discount <= 100:
            await message.answer(
                "❌ Скидка должна быть от 0 до 100."
            )
            return True

        await db_execute(
            """
            INSERT OR REPLACE INTO promo_codes(
                code,
                discount,
                active,
                uses
            )
            VALUES (?, ?, 1, 0)
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
            (
                f"awaiting_promo_add:{admin_id}",
            ),
        )

        await message.answer(
            "✅ <b>Промокод создан.</b>\n\n"
            f"Код: <code>{code}</code>\n"
            f"Скидка: <b>{discount}%</b>",
            reply_markup=kb_admin_panel(),
        )

        return True

    # -----------------------------------------------------
    # PROMO TOGGLE
    # -----------------------------------------------------

    row = await db_execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (
            f"awaiting_promo_toggle:{admin_id}",
        ),
        fetchone=True,
    )

    if row:
        code = text.upper()

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
            (
                f"awaiting_promo_toggle:{admin_id}",
            ),
        )

        status_text = (
            "включён"
            if new_status
            else "выключен"
        )

        await message.answer(
            "✅ Промокод "
            f"<code>{code}</code> {status_text}."
        )

        return True

    return False


# =========================================================
# MESSAGE RELAY
# =========================================================

async def relay_user_message(
    message: Message,
):
    user_id = message.from_user.id

    ticket = await get_open_ticket(
        user_id
    )

    if not ticket:
        return False

    if not ticket["assigned_admin"]:
        await message.answer(
            "⏳ <b>Обращение ещё никто не взял.</b>\n\n"
            "Ваше сообщение получено. "
            "Дождитесь администратора."
        )
        return True

    admin_id = ticket["assigned_admin"]

    header = (
        f"👤 <b>{message.from_user.full_name}</b>\n"
    )

    if message.from_user.username:
        header += (
            f"🔗 @{message.from_user.username}\n"
        )

    header += (
        f"🆔 <code>{user_id}</code>\n"
        f"🎫 Тикет #{ticket['id']}\n\n"
    )

    try:
        await bot.send_message(
            admin_id,
            header,
        )

        await copy_message_safe(
            from_chat_id=message.chat.id,
            to_chat_id=admin_id,
            message_id=message.message_id,
        )

    except Exception as error:
        logging.warning(
            "Не удалось переслать сообщение "
            "пользователя %s админу %s: %s",
            user_id,
            admin_id,
            error,
        )

        await message.answer(
            "⚠️ Не удалось отправить сообщение "
            "администратору. Попробуйте ещё раз."
        )

    return True


async def relay_admin_message(
    message: Message,
):
    admin_id = message.from_user.id

    if not await is_admin(admin_id):
        return False

    ticket = await get_active_admin_ticket(
        admin_id
    )

    if not ticket:
        return False

    user_id = ticket["user_id"]

    try:
        await bot.send_message(
            user_id,
            "👨‍💼 <b>Поддержка:</b>",
        )

        await copy_message_safe(
            from_chat_id=message.chat.id,
            to_chat_id=user_id,
            message_id=message.message_id,
        )

    except Exception as error:
        logging.warning(
            "Не удалось переслать сообщение "
            "админа %s пользователю %s: %s",
            admin_id,
            user_id,
            error,
        )

        await message.answer(
            "⚠️ Не удалось отправить сообщение пользователю."
        )

    return True


# =========================================================
# ALL MESSAGE HANDLER
# =========================================================

@router.message()
async def handle_all_messages(
    message: Message,
):
    if not message.from_user:
        return

    user_id = message.from_user.id

    # -----------------------------------------------------
    # 1. ADMIN STATE
    # -----------------------------------------------------

    if await is_admin(user_id):
        handled = await handle_admin_state(
            message
        )

        if handled:
            return

    # -----------------------------------------------------
    # 2. PROMO INPUT
    # -----------------------------------------------------

    if message.text:
        handled = await handle_promo_input(
            message
        )

        if handled:
            return

    # -----------------------------------------------------
    # 3. ADMIN TICKET MESSAGE
    # -----------------------------------------------------

    if await is_admin(user_id):
        active_ticket = await get_active_admin_ticket(
            user_id
        )

        if active_ticket:
            await relay_admin_message(
                message
            )
            return

    # -----------------------------------------------------
    # 4. USER TICKET MESSAGE
    # -----------------------------------------------------

    ticket = await get_open_ticket(
        user_id
    )

    if ticket:
        await relay_user_message(
            message
        )
        return

    # -----------------------------------------------------
    # 5. DEFAULT
    # -----------------------------------------------------

    if message.text:
        await message.answer(
            "Используйте меню ниже:",
            reply_markup=kb_main(),
        )


# =========================================================
# MAIN
# =========================================================

async def main():
    global bot

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(message)s"
        ),
    )

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    # -----------------------------------------------------
    # IMPORTANT:
    # Удаляем webhook перед polling.
    # Это исправляет:
    #
    # TelegramConflictError:
    # can't use getUpdates method while webhook is active
    # -----------------------------------------------------

    try:
        await bot.delete_webhook(
            drop_pending_updates=False
        )

        logging.info(
            "Webhook удалён. Запускаем polling."
        )

    except Exception as error:
        logging.warning(
            "Не удалось удалить webhook: %s",
            error,
        )

    me = await bot.get_me()

    logging.info(
        "Бот запущен: @%s id=%s",
        me.username,
        me.id,
    )

    logging.info(
        "Главный администратор: %s",
        MAIN_ADMIN_ID,
    )

    await dp.start_polling(
        bot
    )


if __name__ == "__main__":
    asyncio.run(main())
