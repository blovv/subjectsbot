import asyncio
import html
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "study_bot.sqlite3")
BOT_OWNER_ID_RAW = os.getenv("BOT_OWNER_ID", "").strip()
BOT_OWNER_ID = int(BOT_OWNER_ID_RAW) if BOT_OWNER_ID_RAW.isdigit() else None

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Создай .env и укажи BOT_TOKEN=...")

router = Router()


class AddSubjectStates(StatesGroup):
    waiting_name = State()
    waiting_paragraphs = State()
    waiting_points = State()


class AddBotAdminStates(StatesGroup):
    waiting_user_id = State()


class MenuTextStates(StatesGroup):
    waiting_text = State()


class MenuTitleStates(StatesGroup):
    waiting_title = State()


class MenuCommandStates(StatesGroup):
    waiting_command = State()


class MenuDeleteCustomStates(StatesGroup):
    waiting_value = State()


class RenameSubjectStates(StatesGroup):
    waiting_name = State()


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                max_per_user INTEGER,
                UNIQUE(chat_id, name)
            )
        """)

        # Миграция со старой версии.
        cur = await db.execute("PRAGMA table_info(subjects)")
        columns = {row[1] for row in await cur.fetchall()}
        if "max_per_user" not in columns:
            await db.execute("ALTER TABLE subjects ADD COLUMN max_per_user INTEGER")
        if "next_paragraph_number" not in columns:
            await db.execute("ALTER TABLE subjects ADD COLUMN next_paragraph_number INTEGER")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS paragraphs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                UNIQUE(subject_id, title),
                FOREIGN KEY(subject_id) REFERENCES subjects(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paragraph_id INTEGER NOT NULL,
                number INTEGER NOT NULL,
                claimed_by INTEGER,
                claimed_name TEXT,
                claimed_username TEXT,
                UNIQUE(paragraph_id, number),
                FOREIGN KEY(paragraph_id) REFERENCES paragraphs(id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS known_groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                menu_delete_seconds INTEGER NOT NULL DEFAULT 300,
                menu_text TEXT NOT NULL DEFAULT 'Выбери предмет, затем параграф и нужный пункт.',
                menu_title TEXT NOT NULL DEFAULT '📚 Предметы',
                menu_title_html TEXT,
                menu_command TEXT NOT NULL DEFAULT '/menu'
            )
        """)

        # Миграция со старых версий: по умолчанию меню удаляется через 5 минут.
        cur = await db.execute("PRAGMA table_info(known_groups)")
        known_group_columns = {row[1] for row in await cur.fetchall()}
        if "menu_delete_seconds" not in known_group_columns:
            await db.execute(
                "ALTER TABLE known_groups "
                "ADD COLUMN menu_delete_seconds INTEGER NOT NULL DEFAULT 300"
            )
        if "menu_text" not in known_group_columns:
            await db.execute(
                "ALTER TABLE known_groups "
                "ADD COLUMN menu_text TEXT NOT NULL "
                "DEFAULT 'Выбери предмет, затем параграф и нужный пункт.'"
            )
        if "menu_title" not in known_group_columns:
            await db.execute(
                "ALTER TABLE known_groups "
                "ADD COLUMN menu_title TEXT NOT NULL DEFAULT '📚 Предметы'"
            )
        if "menu_title_html" not in known_group_columns:
            await db.execute(
                "ALTER TABLE known_groups ADD COLUMN menu_title_html TEXT"
            )
        if "menu_command" not in known_group_columns:
            await db.execute(
                "ALTER TABLE known_groups "
                "ADD COLUMN menu_command TEXT NOT NULL DEFAULT '/menu'"
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS menu_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            )
        """)

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_menu_messages_delete_at
            ON menu_messages(delete_at)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT,
                username TEXT,
                added_by INTEGER,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Для старых предметов фиксируем следующий номер параграфа один раз.
        # После этого удаление §1, §2 и т.д. уже не откатывает счётчик назад.
        cur = await db.execute("""
            SELECT id
            FROM subjects
            WHERE next_paragraph_number IS NULL
        """)
        for (subject_id,) in await cur.fetchall():
            cur2 = await db.execute("""
                SELECT COALESCE(MAX(CAST(title AS INTEGER)), 0)
                FROM paragraphs
                WHERE subject_id=?
                  AND title GLOB '[0-9]*'
            """, (subject_id,))
            max_number = (await cur2.fetchone())[0] or 0
            await db.execute(
                "UPDATE subjects SET next_paragraph_number=? WHERE id=?",
                (max_number + 1, subject_id),
            )

        # Если обновляемся со старой версии, хотя бы сохраним группы,
        # для которых уже есть предметы.
        await db.execute("""
            INSERT OR IGNORE INTO known_groups(chat_id, title)
            SELECT DISTINCT chat_id, 'Группа ' || chat_id
            FROM subjects
        """)

        await db.commit()


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }
    except Exception:
        return False


def is_owner(user_id: int) -> bool:
    return BOT_OWNER_ID is not None and user_id == BOT_OWNER_ID


async def is_bot_admin_user(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM bot_admins WHERE user_id=?",
            (user_id,),
        )
        return await cur.fetchone() is not None


async def can_manage_group(bot: Bot, chat_id: int, user_id: int) -> bool:
    # Главный админ и добавленные им админы бота могут управлять
    # зарегистрированными группами независимо от роли в Telegram-группе.
    if is_owner(user_id):
        return True
    if await is_bot_admin_user(user_id):
        return True

    # Сохраняем прежнее поведение для обычных Telegram-админов.
    return await is_admin(bot, chat_id, user_id)


async def get_bot_admins():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT user_id, display_name, username, added_by, added_at
            FROM bot_admins
            ORDER BY COALESCE(display_name, ''), user_id
        """)
        return await cur.fetchall()


async def add_bot_admin(
    user_id: int,
    display_name: Optional[str],
    username: Optional[str],
    added_by: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO bot_admins(user_id, display_name, username, added_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                display_name=excluded.display_name,
                username=excluded.username,
                added_by=excluded.added_by
        """, (user_id, display_name, username, added_by))
        await db.commit()


async def remove_bot_admin(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM bot_admins WHERE user_id=?",
            (user_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def register_group(chat_id: int, title: Optional[str]):
    title = (title or f"Группа {chat_id}").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO known_groups(chat_id, title)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
        """, (chat_id, title))
        await db.commit()


async def get_known_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT chat_id, title
            FROM known_groups
            ORDER BY title COLLATE NOCASE
        """)
        return await cur.fetchall()


async def get_group_title(chat_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT title FROM known_groups WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def unregister_group(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM known_groups WHERE chat_id=?", (chat_id,))
        await db.execute("DELETE FROM menu_messages WHERE chat_id=?", (chat_id,))
        await db.commit()


def _utf16_offset_to_py_index(value: str, offset_units: int) -> int:
    """Переводит Telegram UTF-16 offset в Python-индекс."""
    if offset_units <= 0:
        return 0

    used = 0
    for index, ch in enumerate(value):
        units = len(ch.encode("utf-16-le")) // 2
        if used + units > offset_units:
            return index
        used += units
        if used == offset_units:
            return index + 1

    return len(value)


def build_custom_emoji_html(value: str, entities) -> str:
    """
    Сохраняет Telegram custom emoji как HTML:
    <tg-emoji emoji-id="...">fallback</tg-emoji>
    """
    source = value or ""
    clean = source.strip()
    if not clean:
        return ""

    left_trim = len(source) - len(source.lstrip())
    right_bound = len(source.rstrip())

    custom = []
    for entity in entities or []:
        entity_type = getattr(
            getattr(entity, "type", None),
            "value",
            getattr(entity, "type", None),
        )
        custom_id = getattr(entity, "custom_emoji_id", None)

        if entity_type != "custom_emoji" or not custom_id:
            continue

        start = _utf16_offset_to_py_index(source, int(entity.offset))
        end = _utf16_offset_to_py_index(
            source,
            int(entity.offset + entity.length),
        )

        if start < left_trim or end > right_bound or start >= end:
            continue

        custom.append((
            start - left_trim,
            end - left_trim,
            str(custom_id),
        ))

    if not custom:
        return html.escape(clean)

    custom.sort(key=lambda item: item[0])

    out = []
    cursor = 0

    for start, end, custom_id in custom:
        if start < cursor:
            continue

        out.append(html.escape(clean[cursor:start]))
        fallback = html.escape(clean[start:end])
        out.append(
            f'<tg-emoji emoji-id="{html.escape(custom_id)}">'
            f'{fallback}</tg-emoji>'
        )
        cursor = end

    out.append(html.escape(clean[cursor:]))
    return "".join(out)


def message_custom_emoji_html(message: Message) -> str:
    return build_custom_emoji_html(
        message.text or "",
        message.entities or [],
    )


async def get_menu_command(chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT menu_command FROM known_groups WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()

    if not row or not row[0]:
        return "/menu"

    return row[0]


async def set_menu_command(chat_id: int, command: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE known_groups SET menu_command=? WHERE chat_id=?",
            (command, chat_id),
        )
        await db.commit()


def normalize_menu_command(raw: str) -> str:
    value = (raw or "").strip()

    if not value:
        raise ValueError("Пустая команда")

    if not value.startswith("/"):
        value = "/" + value

    # Команда должна быть одним токеном без пробелов.
    if any(ch.isspace() for ch in value):
        raise ValueError("В команде не должно быть пробелов")

    # Ограничиваем длину, но разрешаем кириллицу/Unicode:
    # пользователь может вручную вводить /предметы.
    if len(value) < 2 or len(value) > 32:
        raise ValueError("Команда должна быть длиной 2–32 символа")

    # Запрещаем символ @, чтобы не путать с /cmd@botusername.
    if "@" in value:
        raise ValueError("Символ @ не поддерживается")

    return value


async def get_menu_title(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT menu_title, menu_title_html
            FROM known_groups
            WHERE chat_id=?
        """, (chat_id,))
        row = await cur.fetchone()

    if not row:
        return "📚 Предметы", html.escape("📚 Предметы")

    title = row["menu_title"] or "📚 Предметы"
    title_html = row["menu_title_html"] or html.escape(title)
    return title, title_html


async def set_menu_title(
    chat_id: int,
    title: str,
    title_html: str,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE known_groups
            SET menu_title=?, menu_title_html=?
            WHERE chat_id=?
        """, (title, title_html, chat_id))
        await db.commit()


async def get_menu_text(chat_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT menu_text FROM known_groups WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
        if not row or row[0] is None:
            return "Выбери предмет, затем параграф и нужный пункт."
        return str(row[0])


async def set_menu_text(chat_id: int, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE known_groups SET menu_text=? WHERE chat_id=?",
            (value, chat_id),
        )
        await db.commit()


def parse_duration_input(raw: str) -> int:
    value = (raw or "").strip().lower().replace(" ", "")
    if not value:
        raise ValueError

    if value in {"0", "off", "нет", "никогда"}:
        return 0

    units = {
        "с": 1, "сек": 1, "секунд": 1, "s": 1, "sec": 1,
        "м": 60, "мин": 60, "минут": 60, "m": 60, "min": 60,
        "ч": 3600, "час": 3600, "часов": 3600, "h": 3600, "hr": 3600,
    }

    if value.isdigit():
        seconds = int(value)
    else:
        match = re.fullmatch(r"(\d+)([a-zа-яё]+)", value)
        if not match:
            raise ValueError
        amount = int(match.group(1))
        unit = match.group(2)
        if unit not in units:
            raise ValueError
        seconds = amount * units[unit]

    if not 0 <= seconds <= 7 * 24 * 3600:
        raise ValueError
    return seconds


async def get_menu_delete_seconds(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT menu_delete_seconds FROM known_groups WHERE chat_id=?",
            (chat_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 300


async def set_menu_delete_seconds(chat_id: int, seconds: int):
    seconds = max(0, int(seconds))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE known_groups SET menu_delete_seconds=? WHERE chat_id=?",
            (seconds, chat_id),
        )
        await db.commit()


def format_delete_delay(seconds: int) -> str:
    if seconds <= 0:
        return "не удалять"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} ч"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} мин"
    return f"{seconds} сек"


async def schedule_menu_message_delete(chat_id: int, message_id: int, seconds: int):
    if seconds <= 0:
        return

    delete_at = int(time.time()) + int(seconds)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO menu_messages(chat_id, message_id, delete_at)
            VALUES (?, ?, ?)
        """, (chat_id, message_id, delete_at))
        await db.commit()


async def delete_due_menu_messages(bot: Bot):
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT chat_id, message_id
            FROM menu_messages
            WHERE delete_at <= ?
            ORDER BY delete_at
            LIMIT 100
        """, (now,))
        rows = await cur.fetchall()

    for row in rows:
        chat_id = row["chat_id"]
        message_id = row["message_id"]

        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            # Сообщение могло быть удалено вручную или стать недоступным.
            pass
        finally:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    DELETE FROM menu_messages
                    WHERE chat_id=? AND message_id=?
                """, (chat_id, message_id))
                await db.commit()


async def menu_cleanup_worker(bot: Bot):
    while True:
        try:
            await delete_due_menu_messages(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Ошибка очистки меню: {exc}")

        await asyncio.sleep(1)


async def get_subjects(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, name, max_per_user
            FROM subjects
            WHERE chat_id=?
            ORDER BY name COLLATE NOCASE
        """, (chat_id,))
        return await cur.fetchall()


async def get_subject(subject_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, name, max_per_user
            FROM subjects
            WHERE id=? AND chat_id=?
        """, (subject_id, chat_id))
        return await cur.fetchone()


async def get_subject_by_name(chat_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, name, max_per_user
            FROM subjects
            WHERE chat_id=? AND name=?
        """, (chat_id, name))
        return await cur.fetchone()


def parse_paragraph_range(raw: str):
    """
    '3-7' / '3–7' -> (3, 7)
    '5' -> (1, 5) для совместимости со старым вариантом.
    """
    value = (raw or "").strip().replace("–", "-").replace("—", "-")

    if "-" in value:
        parts = [p.strip() for p in value.split("-", 1)]
        if len(parts) != 2:
            raise ValueError
        start = int(parts[0])
        end = int(parts[1])
    else:
        count = int(value)
        start = 1
        end = count

    if not (1 <= start <= end <= 999):
        raise ValueError
    if end - start + 1 > 100:
        raise ValueError

    return start, end


async def create_subject_structure(
    chat_id: int,
    name: str,
    paragraph_start: int,
    paragraph_end: int,
    points_count: int,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cur = await db.execute(
            "SELECT id FROM subjects WHERE chat_id=? AND name=?",
            (chat_id, name),
        )
        if await cur.fetchone():
            return None

        cur = await db.execute("""
            INSERT INTO subjects(chat_id, name, next_paragraph_number)
            VALUES (?, ?, ?)
        """, (chat_id, name, paragraph_end + 1))
        subject_id = cur.lastrowid

        sort_order = 1
        for paragraph_number in range(paragraph_start, paragraph_end + 1):
            cur = await db.execute("""
                INSERT INTO paragraphs(subject_id, title, sort_order)
                VALUES (?, ?, ?)
            """, (subject_id, str(paragraph_number), sort_order))
            paragraph_id = cur.lastrowid
            sort_order += 1

            for n in range(1, points_count + 1):
                await db.execute(
                    "INSERT INTO points(paragraph_id, number) VALUES (?, ?)",
                    (paragraph_id, n),
                )

        await db.commit()
        return subject_id


async def delete_subject(subject_id: int, chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cur = await db.execute(
            "DELETE FROM subjects WHERE id=? AND chat_id=?",
            (subject_id, chat_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_paragraphs(subject_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                p.id,
                p.title,
                p.sort_order,
                COUNT(pt.id) AS total,
                SUM(CASE WHEN pt.claimed_by IS NOT NULL THEN 1 ELSE 0 END) AS claimed
            FROM paragraphs p
            LEFT JOIN points pt ON pt.paragraph_id = p.id
            WHERE p.subject_id=?
            GROUP BY p.id
            ORDER BY p.sort_order, p.id
        """, (subject_id,))
        return await cur.fetchall()


async def get_paragraph_info(paragraph_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                p.id,
                p.title,
                p.sort_order,
                s.id AS subject_id,
                s.name AS subject_name,
                s.max_per_user
            FROM paragraphs p
            JOIN subjects s ON s.id = p.subject_id
            WHERE p.id=? AND s.chat_id=?
        """, (paragraph_id, chat_id))
        return await cur.fetchone()


async def get_paragraph_by_title(subject_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, title, sort_order
            FROM paragraphs
            WHERE subject_id=? AND title=?
        """, (subject_id, title))
        return await cur.fetchone()


async def get_points(paragraph_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, number, claimed_by, claimed_name, claimed_username
            FROM points
            WHERE paragraph_id=?
            ORDER BY number
        """, (paragraph_id,))
        return await cur.fetchall()


async def count_user_subject_claims(subject_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*)
            FROM points pt
            JOIN paragraphs p ON p.id = pt.paragraph_id
            WHERE p.subject_id=? AND pt.claimed_by=?
        """, (subject_id, user_id))
        return (await cur.fetchone())[0]


async def add_points(paragraph_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(MAX(number), 0) FROM points WHERE paragraph_id=?",
            (paragraph_id,),
        )
        start = (await cur.fetchone())[0] + 1

        for number in range(start, start + amount):
            await db.execute(
                "INSERT INTO points(paragraph_id, number) VALUES (?, ?)",
                (paragraph_id, number),
            )

        await db.commit()
        return start, start + amount - 1


async def remove_points(paragraph_id: int, amount: int):
    """
    Удаляет последние N пунктов параграфа.

    Возвращает:
      ("removed", first_number, last_number)
      ("not_enough", total_points)
      ("occupied", [numbers...])
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute("""
            SELECT id, number, claimed_by
            FROM points
            WHERE paragraph_id=?
            ORDER BY number DESC
        """, (paragraph_id,))
        rows = await cur.fetchall()

        total = len(rows)
        if amount > total:
            return ("not_enough", total)

        to_remove = rows[:amount]
        occupied = sorted(
            row["number"] for row in to_remove
            if row["claimed_by"] is not None
        )

        if occupied:
            return ("occupied", occupied)

        ids = [row["id"] for row in to_remove]
        numbers = sorted(row["number"] for row in to_remove)

        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM points WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()

        return ("removed", numbers[0], numbers[-1])


async def create_next_paragraph(subject_id: int, points_count: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute("""
            SELECT next_paragraph_number
            FROM subjects
            WHERE id=?
        """, (subject_id,))
        row = await cur.fetchone()
        if not row:
            await db.rollback()
            return None, None

        number = row[0]
        if number is None:
            cur = await db.execute("""
                SELECT COALESCE(MAX(CAST(title AS INTEGER)), 0) + 1
                FROM paragraphs
                WHERE subject_id=?
                  AND title GLOB '[0-9]*'
            """, (subject_id,))
            number = (await cur.fetchone())[0] or 1

        # На случай старых/ручных данных не создаём дубликат.
        while True:
            cur = await db.execute(
                "SELECT 1 FROM paragraphs WHERE subject_id=? AND title=?",
                (subject_id, str(number)),
            )
            if await cur.fetchone() is None:
                break
            number += 1

        cur = await db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM paragraphs WHERE subject_id=?",
            (subject_id,),
        )
        sort_order = (await cur.fetchone())[0]

        title = str(number)
        cur = await db.execute("""
            INSERT INTO paragraphs(subject_id, title, sort_order)
            VALUES (?, ?, ?)
        """, (subject_id, title, sort_order))
        paragraph_id = cur.lastrowid

        for n in range(1, points_count + 1):
            await db.execute(
                "INSERT INTO points(paragraph_id, number) VALUES (?, ?)",
                (paragraph_id, n),
            )

        await db.execute(
            "UPDATE subjects SET next_paragraph_number=? WHERE id=?",
            (number + 1, subject_id),
        )

        await db.commit()
        return paragraph_id, title


async def toggle_claim(
    point_id: int,
    chat_id: int,
    user_id: int,
    full_name: str,
    username: Optional[str],
):
    """
    Возвращает:
      ("claimed", paragraph_id)
      ("released", paragraph_id)
      ("busy", paragraph_id, claimed_name)
      ("limit", paragraph_id, limit)
      ("missing", None)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute("""
            SELECT
                pt.id,
                pt.paragraph_id,
                pt.claimed_by,
                pt.claimed_name,
                p.subject_id,
                s.max_per_user
            FROM points pt
            JOIN paragraphs p ON p.id = pt.paragraph_id
            JOIN subjects s ON s.id = p.subject_id
            WHERE pt.id=? AND s.chat_id=?
        """, (point_id, chat_id))
        row = await cur.fetchone()

        if not row:
            await db.rollback()
            return ("missing", None)

        # Свой пункт можно освободить.
        if row["claimed_by"] == user_id:
            await db.execute("""
                UPDATE points
                SET claimed_by=NULL, claimed_name=NULL, claimed_username=NULL
                WHERE id=?
            """, (point_id,))
            await db.commit()
            return ("released", row["paragraph_id"])

        # Чужой пункт нельзя забрать.
        if row["claimed_by"] is not None:
            await db.rollback()
            return (
                "busy",
                row["paragraph_id"],
                row["claimed_name"] or "другим участником",
            )

        # Лимит по предмету.
        limit = row["max_per_user"]
        if limit is not None and limit > 0:
            cur = await db.execute("""
                SELECT COUNT(*)
                FROM points pt
                JOIN paragraphs p ON p.id = pt.paragraph_id
                WHERE p.subject_id=? AND pt.claimed_by=?
            """, (row["subject_id"], user_id))
            current = (await cur.fetchone())[0]

            if current >= limit:
                await db.rollback()
                return ("limit", row["paragraph_id"], limit)

        await db.execute("""
            UPDATE points
            SET claimed_by=?, claimed_name=?, claimed_username=?
            WHERE id=?
        """, (user_id, full_name, username, point_id))

        await db.commit()
        return ("claimed", row["paragraph_id"])


# =========================================================
# ОБЫЧНОЕ МЕНЮ
# =========================================================

def person_text(user_id: int, name: str, username: Optional[str]) -> str:
    safe_name = html.escape(name or "Участник")
    if username:
        return f'<a href="https://t.me/{html.escape(username)}">{safe_name}</a>'
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


async def subjects_screen(chat_id: int):
    subjects = await get_subjects(chat_id)
    menu_text = await get_menu_text(chat_id)
    _, menu_title_html = await get_menu_title(chat_id)

    text = f"<b>{menu_title_html}</b>" if menu_title_html else ""
    if menu_text.strip():
        if text:
            text += "\n\n"
        text += html.escape(menu_text.strip())

    rows = []
    for s in subjects:
        limit = s["max_per_user"]
        suffix = f" · максимум {limit}" if limit else ""
        rows.append([
            InlineKeyboardButton(
                text=f"📘 {s['name']}{suffix}",
                callback_data=f"sub:{s['id']}",
            )
        ])

    if not subjects:
        text += "\n\nПока предметов нет."

    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="home")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def subject_screen(chat_id: int, subject_id: int, user_id: int):
    subject = await get_subject(subject_id, chat_id)
    if not subject:
        return None

    paragraphs = await get_paragraphs(subject_id)
    current = await count_user_subject_claims(subject_id, user_id)
    limit = subject["max_per_user"]

    if limit:
        user_line = f"👤 У тебя занято: <b>{current}/{limit}</b>"
    else:
        user_line = f"👤 У тебя занято: <b>{current}</b> · лимита нет"

    text = (
        f"📘 <b>{html.escape(subject['name'])}</b>\n"
        f"{user_line}\n\n"
        "Выбери параграф:"
    )

    rows = []
    for p in paragraphs:
        total = p["total"] or 0
        claimed = p["claimed"] or 0
        free = total - claimed

        rows.append([
            InlineKeyboardButton(
                text=f"§ {p['title']} — свободно {free}/{total}",
                callback_data=f"par:{p['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="home")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def paragraph_screen(chat_id: int, paragraph_id: int, user_id: int):
    info = await get_paragraph_info(paragraph_id, chat_id)
    if not info:
        return None

    points = await get_points(paragraph_id)
    current = await count_user_subject_claims(info["subject_id"], user_id)
    limit = info["max_per_user"]

    if limit:
        limit_line = f"Твой лимит по предмету: <b>{current}/{limit}</b>"
    else:
        limit_line = f"У тебя занято по предмету: <b>{current}</b> · лимита нет"

    lines = [
        f"📘 <b>{html.escape(info['subject_name'])}</b>",
        f"📖 <b>§ {html.escape(info['title'])}</b>",
        limit_line,
        "",
        "Нажми на пункт, чтобы занять его.",
        "Повторное нажатие на свой пункт освобождает его.",
        "",
    ]

    rows = []
    for pt in points:
        if pt["claimed_by"] is None:
            lines.append(f"🟢 <b>Пункт {pt['number']}</b> — свободен")
            button_text = f"🟢 {pt['number']} — занять"
        else:
            who = person_text(
                pt["claimed_by"],
                pt["claimed_name"] or "Участник",
                pt["claimed_username"],
            )
            lines.append(f"🔴 <b>Пункт {pt['number']}</b> — {who}")
            short_name = (pt["claimed_name"] or "занят")[:22]
            button_text = f"🔴 {pt['number']} — {short_name}"

        rows.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"pt:{pt['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"par:{paragraph_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"sub:{info['subject_id']}",
        )
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# =========================================================
# АДМИН-ПАНЕЛЬ В ЛИЧКЕ
# =========================================================

async def admin_groups_screen(bot: Bot, user_id: int):
    groups = await get_known_groups()

    available = []
    has_global_access = is_owner(user_id) or await is_bot_admin_user(user_id)

    for group in groups:
        chat_id = group["chat_id"]

        if has_global_access or await is_admin(bot, chat_id, user_id):
            title = group["title"]
            try:
                chat = await bot.get_chat(chat_id)
                title = chat.title or title
                await register_group(chat_id, title)
            except Exception:
                pass
            available.append((chat_id, title))

    text = (
        "⚙️ <b>Админ-панель</b>\n\n"
        "Выбери группу, которую хочешь настроить."
    )

    rows = []

    if is_owner(user_id):
        rows.append([
            InlineKeyboardButton(
                text="👤 Админы бота",
                callback_data="botadmins_home",
            )
        ])

    for chat_id, title in available:
        rows.append([
            InlineKeyboardButton(
                text=f"👥 {title}",
                callback_data=f"adm_group:{chat_id}",
            )
        ])

    if not available:
        text += (
            "\n\nЯ пока не нашёл доступных групп.\n"
            "Открой нужную группу и один раз используй там <code>/menu</code>, "
            "после этого вернись сюда и снова введи /admin."
        )

    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_groups")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def bot_admins_screen():
    admins = await get_bot_admins()

    text = (
        "👤 <b>Админы бота</b>\n\n"
        "Эти люди могут менять предметы, лимиты, параграфы и пункты "
        "во всех зарегистрированных группах.\n\n"
        "Они <b>не могут</b> добавлять/удалять админов бота и не имеют доступа к /backup."
    )

    rows = [[
        InlineKeyboardButton(
            text="➕ Добавить админа",
            callback_data="botadmins_add",
        )
    ]]

    for admin in admins:
        name = admin["display_name"] or f"ID {admin['user_id']}"
        if admin["username"]:
            name += f" (@{admin['username']})"
        rows.append([
            InlineKeyboardButton(
                text=f"👤 {name}",
                callback_data=f"botadmins_delask:{admin['user_id']}",
            )
        ])

    if not admins:
        text += "\n\nПока дополнительных админов нет."

    rows.append([
        InlineKeyboardButton(
            text="⬅️ К группам",
            callback_data="adm_groups",
        )
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_home_screen(chat_id: int, user_id: Optional[int] = None):
    subjects = await get_subjects(chat_id)
    group_title = await get_group_title(chat_id) or f"Группа {chat_id}"
    delete_seconds = await get_menu_delete_seconds(chat_id)

    text = (
        f"⚙️ <b>{html.escape(group_title)}</b>\n\n"
        "Здесь можно добавлять и удалять предметы, менять лимиты и структуру.\n"
        f"Автоудаление /menu: <b>{format_delete_delay(delete_seconds)}</b>"
    )

    rows = []

    # Бренд-настройки доступны только главному админу.
    if user_id is not None and is_owner(user_id):
        rows.extend([
            [
                InlineKeyboardButton(
                    text="⌨️ Команда меню",
                    callback_data=f"adm_menucommand:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Заголовок /menu",
                    callback_data=f"adm_menutitle:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Текст /menu",
                    callback_data=f"adm_menutext:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🧹 Меню: {format_delete_delay(delete_seconds)}",
                    callback_data=f"adm_menudel:{chat_id}",
                )
            ],
        ])

    rows.append([
        InlineKeyboardButton(
            text="➕ Добавить предмет",
            callback_data=f"adm_add_subject:{chat_id}",
        )
    ])


    for s in subjects:
        limit = s["max_per_user"]
        suffix = f" · лимит {limit}" if limit else " · без лимита"
        rows.append([
            InlineKeyboardButton(
                text=f"📘 {s['name']}{suffix}",
                callback_data=f"adm_sub:{chat_id}:{s['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"adm_group:{chat_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ К группам",
            callback_data="adm_groups",
        )
    ])

    if not subjects:
        text += "\n\nПредметов пока нет. Нажми «➕ Добавить предмет»."

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_menu_command_screen(chat_id: int):
    group_title = await get_group_title(chat_id) or f"Группа {chat_id}"
    command = await get_menu_command(chat_id)

    text = (
        f"⌨️ <b>Команда меню</b>\n"
        f"Группа: <b>{html.escape(group_title)}</b>\n\n"
        f"Сейчас: <code>{html.escape(command)}</code>\n\n"
        "Можно поставить свою команду, например <code>/предметы</code>.\n"
        "Пользователи должны будут вводить именно её."
    )

    rows = [
        [InlineKeyboardButton(
            text="✏️ Изменить команду",
            callback_data=f"adm_menucommandedit:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Вернуть /menu",
            callback_data=f"adm_menucommanddefault:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"adm_group:{chat_id}",
        )],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_menu_title_screen(chat_id: int):
    group_title = await get_group_title(chat_id) or f"Группа {chat_id}"
    _, title_html = await get_menu_title(chat_id)

    text = (
        f"🏷 <b>Заголовок /menu</b>\n"
        f"Группа: <b>{html.escape(group_title)}</b>\n\n"
        f"Сейчас:\n<b>{title_html}</b>\n\n"
        "Можно поставить свой заголовок, в том числе с Telegram "
        "custom emoji / анимированным эмодзи."
    )

    rows = [
        [InlineKeyboardButton(
            text="✏️ Изменить заголовок",
            callback_data=f"adm_menutitleedit:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="🧹 Убрать заголовок",
            callback_data=f"adm_menutitleclear:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Вернуть 📚 Предметы",
            callback_data=f"adm_menutitledefault:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"adm_group:{chat_id}",
        )],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_menu_text_screen(chat_id: int):
    group_title = await get_group_title(chat_id) or f"Группа {chat_id}"
    current = await get_menu_text(chat_id)

    shown = html.escape(current) if current.strip() else "<i>текст убран</i>"
    text = (
        f"📝 <b>Текст /menu</b>\n"
        f"Группа: <b>{html.escape(group_title)}</b>\n\n"
        f"Сейчас:\n{shown}\n\n"
        "Можно заменить подпись под заголовком «📚 Предметы» или убрать её полностью."
    )

    rows = [
        [InlineKeyboardButton(
            text="✏️ Изменить текст",
            callback_data=f"adm_menutextedit:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="🧹 Убрать текст",
            callback_data=f"adm_menutextclear:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Вернуть стандартный",
            callback_data=f"adm_menutextdefault:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"adm_group:{chat_id}",
        )],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_menu_delete_screen(chat_id: int):
    group_title = await get_group_title(chat_id) or f"Группа {chat_id}"
    current = await get_menu_delete_seconds(chat_id)

    text = (
        f"🧹 <b>Автоудаление меню</b>\n"
        f"Группа: <b>{html.escape(group_title)}</b>\n\n"
        f"Сейчас: <b>{format_delete_delay(current)}</b>\n\n"
        "Через сколько удалять сообщение бота, которое появляется после /menu?"
    )

    presets = [
        (30, "30 сек"),
        (60, "1 мин"),
        (300, "5 мин"),
        (600, "10 мин"),
        (1800, "30 мин"),
        (3600, "1 час"),
    ]

    rows = []
    for seconds, label in presets:
        prefix = "✅ " if current == seconds else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{label}",
                callback_data=f"adm_menudelset:{chat_id}:{seconds}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text=("✅ " if current == 0 else "") + "♾ Не удалять",
            callback_data=f"adm_menudelset:{chat_id}:0",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⏱ Своё время",
            callback_data=f"adm_menudelcustom:{chat_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"adm_group:{chat_id}",
        )
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_subject_screen(chat_id: int, subject_id: int):
    subject = await get_subject(subject_id, chat_id)
    if not subject:
        return None

    paragraphs = await get_paragraphs(subject_id)
    limit = subject["max_per_user"]

    text = [
        f"⚙️ <b>{html.escape(subject['name'])}</b>",
        "",
        f"Лимит на человека: <b>{limit if limit else 'нет'}</b>",
        "",
        "Можно изменить лимит, добавить новый параграф",
        "или открыть параграф и изменить количество пунктов.",
    ]

    rows = [
        [
            InlineKeyboardButton(
                text="✏️ Переименовать предмет",
                callback_data=f"adm_rename_sub:{chat_id}:{subject_id}",
            )
        ],
        [
            InlineKeyboardButton(text="1", callback_data=f"adm_limit:{chat_id}:{subject_id}:1"),
            InlineKeyboardButton(text="2", callback_data=f"adm_limit:{chat_id}:{subject_id}:2"),
            InlineKeyboardButton(text="3", callback_data=f"adm_limit:{chat_id}:{subject_id}:3"),
            InlineKeyboardButton(text="4", callback_data=f"adm_limit:{chat_id}:{subject_id}:4"),
        ],
        [
            InlineKeyboardButton(text="5", callback_data=f"adm_limit:{chat_id}:{subject_id}:5"),
            InlineKeyboardButton(text="6", callback_data=f"adm_limit:{chat_id}:{subject_id}:6"),
            InlineKeyboardButton(text="∞ Без лимита", callback_data=f"adm_limit:{chat_id}:{subject_id}:0"),
        ],
        [
            InlineKeyboardButton(
                text="➕ Новый параграф (5 пунктов)",
                callback_data=f"adm_newpar:{chat_id}:{subject_id}",
            )
        ],
    ]

    for p in paragraphs:
        total = p["total"] or 0
        rows.append([
            InlineKeyboardButton(
                text=f"⚙️ §{p['title']} · {total} пункт(ов)",
                callback_data=f"adm_par:{chat_id}:{p['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="🗑 Удалить предмет",
            callback_data=f"adm_delask:{chat_id}:{subject_id}",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="⬅️ К предметам",
            callback_data=f"adm_group:{chat_id}",
        )
    ])

    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=rows)


async def admin_paragraph_screen(chat_id: int, paragraph_id: int):
    info = await get_paragraph_info(paragraph_id, chat_id)
    if not info:
        return None

    points = await get_points(paragraph_id)
    total = len(points)
    occupied = sum(1 for p in points if p["claimed_by"] is not None)

    text = (
        f"⚙️ <b>{html.escape(info['subject_name'])} → §{html.escape(info['title'])}</b>\n\n"
        f"Всего пунктов: <b>{total}</b>\n"
        f"Занято: <b>{occupied}</b>\n"
        f"Свободно: <b>{total - occupied}</b>\n\n"
        "Добавляй или удаляй пункты кнопками ниже.\n"
        "При удалении убираются последние пункты по номеру. "
        "Занятые пункты бот удалить не даст."
    )

    rows = [
        [
            InlineKeyboardButton(text="➕ 1", callback_data=f"adm_addpts:{chat_id}:{paragraph_id}:1"),
            InlineKeyboardButton(text="➕ 3", callback_data=f"adm_addpts:{chat_id}:{paragraph_id}:3"),
            InlineKeyboardButton(text="➕ 5", callback_data=f"adm_addpts:{chat_id}:{paragraph_id}:5"),
            InlineKeyboardButton(text="➕ 10", callback_data=f"adm_addpts:{chat_id}:{paragraph_id}:10"),
        ],
        [
            InlineKeyboardButton(text="➖ 1", callback_data=f"adm_delpts:{chat_id}:{paragraph_id}:1"),
            InlineKeyboardButton(text="➖ 3", callback_data=f"adm_delpts:{chat_id}:{paragraph_id}:3"),
            InlineKeyboardButton(text="➖ 5", callback_data=f"adm_delpts:{chat_id}:{paragraph_id}:5"),
            InlineKeyboardButton(text="➖ 10", callback_data=f"adm_delpts:{chat_id}:{paragraph_id}:10"),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Удалить параграф",
                callback_data=f"adm_delparask:{chat_id}:{paragraph_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"adm_sub:{chat_id}:{info['subject_id']}",
            )
        ],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def delete_paragraph(paragraph_id: int, chat_id: int):
    """
    Полностью удаляет параграф и все его пункты.
    Благодаря ON DELETE CASCADE записи points удаляются автоматически.
    Возвращает (subject_id, paragraph_title) или None.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        cur = await db.execute("""
            SELECT p.id, p.title, p.subject_id
            FROM paragraphs p
            JOIN subjects s ON s.id = p.subject_id
            WHERE p.id=? AND s.chat_id=?
        """, (paragraph_id, chat_id))
        row = await cur.fetchone()

        if not row:
            return None

        await db.execute(
            "DELETE FROM paragraphs WHERE id=?",
            (paragraph_id,),
        )
        await db.commit()

        return row["subject_id"], row["title"]


async def require_owner_callback(callback: CallbackQuery) -> bool:
    if not is_owner(callback.from_user.id):
        await callback.answer(
            "Эта настройка доступна только главному админу.",
            show_alert=True,
        )
        return False
    return True


async def require_admin_callback(
    callback: CallbackQuery,
    bot: Bot,
    group_id: int,
) -> bool:
    if callback.message.chat.type != "private":
        await callback.answer(
            "Админ-панель теперь работает только в личке с ботом.",
            show_alert=True,
        )
        return False

    if not await can_manage_group(bot, group_id, callback.from_user.id):
        await callback.answer(
            "У тебя нет доступа к настройкам этой группы.",
            show_alert=True,
        )
        return False

    return True


def make_database_backup() -> Path:
    """
    Создаёт консистентную копию SQLite через штатный backup API.
    Это безопаснее, чем просто копировать файл во время работы бота.
    """
    temp_dir = Path(tempfile.mkdtemp(prefix="study_bot_backup_"))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = temp_dir / f"study_bot_backup_{timestamp}.sqlite3"

    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    return backup_path




# =========================================================
# АВТОМАТИЧЕСКАЯ РЕГИСТРАЦИЯ ГРУПП
# =========================================================

@router.my_chat_member()
async def remember_group_membership(event: ChatMemberUpdated):
    """
    Группа автоматически попадает в known_groups сразу после добавления
    бота или выдачи ему прав администратора. Никакой /menu для регистрации
    группы не требуется.
    """
    if event.chat.type not in {"group", "supergroup"}:
        return

    status = event.new_chat_member.status

    if status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    }:
        await register_group(event.chat.id, event.chat.title)
        print(
            f"Зарегистрирована группа: "
            f"{event.chat.title or event.chat.id} ({event.chat.id})"
        )

    elif status in {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    }:
        await unregister_group(event.chat.id)
        print(
            f"Бот удалён из группы: "
            f"{event.chat.title or event.chat.id} ({event.chat.id})"
        )


# =========================================================
# КОМАНДЫ
# =========================================================

@router.message(CommandStart())
async def start_cmd(message: Message, bot: Bot, state: FSMContext):
    payload = message.text.partition(" ")[2].strip()

    if message.chat.type == "private" and payload == "admin":
        await state.clear()
        text, kb = await admin_groups_screen(bot, message.from_user.id)
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if message.chat.type == "private":
        await message.answer(
            "Привет! Для учеников используй /menu в учебной группе.\n"
            "Для администраторов здесь доступна команда /admin.\n""Группа появляется в админке автоматически после добавления в неё бота."
        )
        return

    await register_group(message.chat.id, message.chat.title)
    text, kb = await subjects_screen(message.chat.id)
    sent = await message.answer(
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    delete_seconds = await get_menu_delete_seconds(message.chat.id)
    await schedule_menu_message_delete(
        message.chat.id,
        sent.message_id,
        delete_seconds,
    )


async def send_group_menu(message: Message):
    # Если у бота есть право «Удалять сообщения», команда пользователя
    # исчезнет из группы и в чате останется только само меню.
    try:
        await message.delete()
    except Exception:
        pass

    await register_group(message.chat.id, message.chat.title)

    text, kb = await subjects_screen(message.chat.id)
    sent = await message.answer(
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

    delete_seconds = await get_menu_delete_seconds(message.chat.id)
    await schedule_menu_message_delete(
        message.chat.id,
        sent.message_id,
        delete_seconds,
    )


@router.message(Command("menu"))
async def menu_cmd(message: Message):
    if message.chat.type == "private":
        await message.answer("Используй команду меню в учебной группе.")
        return

    configured = await get_menu_command(message.chat.id)
    if configured != "/menu":
        # /menu больше не является публичной командой этой группы.
        return

    await send_group_menu(message)


@router.message(Command("admin"))
async def admin_cmd(message: Message, bot: Bot, state: FSMContext):
    await state.clear()

    if message.chat.type != "private":
        await register_group(message.chat.id, message.chat.title)
        me = await bot.get_me()
        await message.answer(
            "⚙️ Админ-панель теперь работает в личке с ботом.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть админ-панель",
                    url=f"https://t.me/{me.username}?start=admin",
                )
            ]]),
        )
        return

    text, kb = await admin_groups_screen(bot, message.from_user.id)
    await message.answer(
        text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("cancel"))
async def cancel_admin_action(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        return
    await state.clear()
    await message.answer("❌ Действие отменено.")


@router.message(AddBotAdminStates.waiting_user_id)
async def add_bot_admin_user_id(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    if not is_owner(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Только главный админ может управлять админами бота.")
        return

    raw = (message.text or "").strip()
    try:
        user_id = int(raw)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "Отправь числовой Telegram ID пользователя. "
            "Он может узнать его командой /id."
        )
        return

    if user_id == BOT_OWNER_ID:
        await message.answer("Это ID главного админа — добавлять его не нужно.")
        return

    display_name = None
    username = None
    try:
        chat = await bot.get_chat(user_id)
        display_name = " ".join(
            x for x in [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]
            if x
        ) or getattr(chat, "title", None)
        username = getattr(chat, "username", None)
    except Exception:
        # Добавление по ID всё равно разрешаем.
        pass

    await add_bot_admin(
        user_id=user_id,
        display_name=display_name,
        username=username,
        added_by=message.from_user.id,
    )
    await state.clear()

    name = display_name or f"ID {user_id}"
    await message.answer(
        f"✅ <b>{html.escape(name)}</b> добавлен в админы бота.",
        parse_mode=ParseMode.HTML,
    )

    screen_text, kb = await bot_admins_screen()
    await message.answer(screen_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(MenuCommandStates.waiting_command)
async def menu_command_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if message.chat.type != "private":
        await state.clear()
        return

    if not is_owner(message.from_user.id):
        await state.clear()
        await message.answer(
            "⛔ Только главный админ может менять команду меню."
        )
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")

    if not group_id:
        await state.clear()
        await message.answer("Не выбрана группа.")
        return

    raw = (message.text or "").strip()

    if raw.lower() == "/default":
        command = "/menu"
    else:
        try:
            command = normalize_menu_command(raw)
        except ValueError as exc:
            await message.answer(
                f"Некорректная команда: {html.escape(str(exc))}\n\n"
                "Например: <code>/предметы</code> или <code>/menu</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

    await set_menu_command(group_id, command)
    await state.clear()

    await message.answer(
        f"✅ Команда меню теперь: <code>{html.escape(command)}</code>",
        parse_mode=ParseMode.HTML,
    )

    screen_text, kb = await admin_menu_command_screen(group_id)
    await message.answer(
        screen_text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@router.message(MenuTitleStates.waiting_title)
async def menu_title_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
):
    if message.chat.type != "private":
        await state.clear()
        return

    if not is_owner(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Эта настройка доступна только главному админу.")
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")

    if (
        not group_id
        or not await can_manage_group(
            bot,
            group_id,
            message.from_user.id,
        )
    ):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    raw = (message.text or "").strip()

    if raw.lower() == "/default":
        title = "📚 Предметы"
        title_html = html.escape(title)
    elif raw.lower() == "/empty":
        title = ""
        title_html = ""
    else:
        if not raw:
            await message.answer(
                "Отправь новый заголовок, /default или /empty."
            )
            return

        if len(raw) > 100:
            await message.answer(
                "Заголовок слишком длинный. Максимум 100 символов."
            )
            return

        title = raw
        title_html = message_custom_emoji_html(message)

    await set_menu_title(group_id, title, title_html)
    await state.clear()

    await message.answer(
        "✅ Заголовок /menu обновлён."
        if title
        else "✅ Заголовок /menu убран."
    )

    screen_text, kb = await admin_menu_title_screen(group_id)
    await message.answer(
        screen_text,
        reply_markup=kb,
        parse_mode=ParseMode.HTML,
    )


@router.message(MenuTextStates.waiting_text)
async def menu_text_input(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    if not is_owner(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Эта настройка доступна только главному админу.")
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    if not group_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/empty":
        raw = ""
    elif not raw:
        await message.answer("Отправь текст или /empty, чтобы убрать его полностью.")
        return

    if len(raw) > 1000:
        await message.answer("Текст слишком длинный. Максимум 1000 символов.")
        return

    await set_menu_text(group_id, raw)
    await state.clear()
    await message.answer("✅ Текст /menu обновлён." if raw else "✅ Текст под заголовком /menu убран.")

    screen_text, kb = await admin_menu_text_screen(group_id)
    await message.answer(screen_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(MenuDeleteCustomStates.waiting_value)
async def menu_delete_custom_input(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    if not group_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    try:
        seconds = parse_duration_input(message.text or "")
    except ValueError:
        await message.answer(
            "Не понял время. Примеры: <code>19</code>, <code>19с</code>, "
            "<code>2мин</code>, <code>1ч</code> или <code>0</code> для отключения.",
            parse_mode=ParseMode.HTML,
        )
        return

    await set_menu_delete_seconds(group_id, seconds)
    await state.clear()
    await message.answer(
        "✅ Автоудаление отключено." if seconds == 0
        else f"✅ Меню будет удаляться через {format_delete_delay(seconds)}."
    )

    screen_text, kb = await admin_menu_delete_screen(group_id)
    await message.answer(screen_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(RenameSubjectStates.waiting_name)
async def rename_subject_input(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    subject_id = data.get("target_subject_id")
    if not group_id or not subject_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    name = (message.text or "").strip()
    if not name or len(name) > 80:
        await message.answer("Название должно быть от 1 до 80 символов.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cur = await db.execute(
                "UPDATE subjects SET name=? WHERE id=? AND chat_id=?",
                (name, subject_id, group_id),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await message.answer("Предмет с таким названием уже существует.")
            return

    if cur.rowcount == 0:
        await state.clear()
        await message.answer("Предмет не найден.")
        return

    await state.clear()
    await message.answer(f"✅ Предмет переименован в <b>{html.escape(name)}</b>.", parse_mode=ParseMode.HTML)
    screen = await admin_subject_screen(group_id, subject_id)
    if screen:
        screen_text, kb = screen
        await message.answer(screen_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(AddSubjectStates.waiting_name)
async def add_subject_name(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    if not group_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    name = (message.text or "").strip()
    if not name or len(name) > 50:
        await message.answer("Название должно быть от 1 до 50 символов. Введи ещё раз или /cancel.")
        return

    if await get_subject_by_name(group_id, name):
        await message.answer("Такой предмет уже есть. Введи другое название или /cancel.")
        return

    await state.update_data(subject_name=name)
    await state.set_state(AddSubjectStates.waiting_paragraphs)
    await message.answer(
        f"📘 Предмет: <b>{html.escape(name)}</b>\n\n"
        "Какие параграфы создать?\n"
        "Например: <code>3-7</code> создаст §3, §4, §5, §6, §7.\n"
        "Можно отправить просто <code>5</code> — тогда будут §1–§5.",
        parse_mode=ParseMode.HTML,
    )


@router.message(AddSubjectStates.waiting_paragraphs)
async def add_subject_paragraphs(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    if not group_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    try:
        paragraph_start, paragraph_end = parse_paragraph_range(message.text or "")
    except (ValueError, TypeError):
        await message.answer(
            "Неверный диапазон. Например: <code>3-7</code> или <code>5</code>. "
            "Максимум 100 параграфов за раз.",
            parse_mode=ParseMode.HTML,
        )
        return

    await state.update_data(
        paragraph_start=paragraph_start,
        paragraph_end=paragraph_end,
    )
    await state.set_state(AddSubjectStates.waiting_points)
    await message.answer(
        f"Будут созданы §{paragraph_start}–§{paragraph_end}.\n"
        "Сколько пунктов создать в каждом параграфе? Отправь число от 1 до 100."
    )


@router.message(AddSubjectStates.waiting_points)
async def add_subject_points(message: Message, state: FSMContext, bot: Bot):
    if message.chat.type != "private":
        await state.clear()
        return

    data = await state.get_data()
    group_id = data.get("target_chat_id")
    if not group_id or not await can_manage_group(bot, group_id, message.from_user.id):
        await state.clear()
        await message.answer("⛔ Нет доступа к выбранной группе.")
        return

    try:
        points_count = int((message.text or "").strip())
        if not 1 <= points_count <= 100:
            raise ValueError
    except ValueError:
        await message.answer("Отправь число от 1 до 100 или /cancel.")
        return

    name = data["subject_name"]
    paragraph_start = data["paragraph_start"]
    paragraph_end = data["paragraph_end"]

    subject_id = await create_subject_structure(
        group_id,
        name,
        paragraph_start,
        paragraph_end,
        points_count,
    )
    await state.clear()

    if subject_id is None:
        await message.answer("Такой предмет уже существует.")
        return

    await message.answer(
        f"✅ Создан предмет <b>{html.escape(name)}</b>: "
        f"§{paragraph_start}–§{paragraph_end}, по {points_count} пунктов.",
        parse_mode=ParseMode.HTML,
    )

    screen = await admin_subject_screen(group_id, subject_id)
    if screen:
        screen_text, kb = screen
        await message.answer(
            screen_text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("my"))
async def my_claims(message: Message):
    if message.chat.type == "private":
        await message.answer("Используй /my в учебной группе.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT
                s.name AS subject_name,
                p.title AS paragraph_title,
                pt.number
            FROM points pt
            JOIN paragraphs p ON p.id = pt.paragraph_id
            JOIN subjects s ON s.id = p.subject_id
            WHERE s.chat_id=? AND pt.claimed_by=?
            ORDER BY s.name COLLATE NOCASE, p.sort_order, pt.number
        """, (message.chat.id, message.from_user.id))
        rows = await cur.fetchall()

    if not rows:
        await message.answer("У тебя пока нет занятых пунктов.")
        return

    lines = ["🙋 <b>Мои пункты:</b>", ""]
    for r in rows:
        lines.append(
            f"• {html.escape(r['subject_name'])} — "
            f"§{html.escape(r['paragraph_title'])}, пункт {r['number']}"
        )

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("setup"))
async def setup_subject(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    await register_group(message.chat.id, message.chat.title)

    raw = message.text.partition(" ")[2].strip()

    try:
        name, paragraph_range_raw, points_raw = [x.strip() for x in raw.split("|")]
        paragraph_start, paragraph_end = parse_paragraph_range(paragraph_range_raw)
        points_count = int(points_raw)
        if not 1 <= points_count <= 100:
            raise ValueError
    except Exception:
        await message.answer(
            "Формат:\n"
            "<code>/setup История | 3-7 | 5</code>\n"
            "создаст §3–§7 по 5 пунктов.\n\n"
            "Старый вариант тоже работает:\n"
            "<code>/setup История | 5 | 5</code> → §1–§5.",
            parse_mode=ParseMode.HTML,
        )
        return

    subject_id = await create_subject_structure(
        message.chat.id,
        name,
        paragraph_start,
        paragraph_end,
        points_count,
    )

    if subject_id is None:
        await message.answer("Такой предмет уже существует.")
        return

    await message.answer(
        f"✅ Создан предмет <b>{html.escape(name)}</b>: "
        f"§{paragraph_start}–§{paragraph_end}, по {points_count} пунктов.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("limit"))
async def limit_cmd(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    await register_group(message.chat.id, message.chat.title)

    raw = message.text.partition(" ")[2].strip()

    try:
        subject_name, limit_raw = [x.strip() for x in raw.split("|")]
        limit = int(limit_raw)
        if not (0 <= limit <= 100):
            raise ValueError
    except Exception:
        await message.answer(
            "Формат:\n"
            "<code>/limit История | 4</code>\n"
            "<code>/limit История | 0</code> — убрать лимит",
            parse_mode=ParseMode.HTML,
        )
        return

    subject = await get_subject_by_name(message.chat.id, subject_name)
    if not subject:
        await message.answer("Такого предмета нет.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subjects SET max_per_user=? WHERE id=?",
            (None if limit == 0 else limit, subject["id"]),
        )
        await db.commit()

    if limit == 0:
        await message.answer(
            f"✅ Лимит для <b>{html.escape(subject_name)}</b> отключён.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"✅ По <b>{html.escape(subject_name)}</b> теперь максимум "
            f"<b>{limit}</b> пункт(а/ов) на человека.",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("addparagraph"))
async def add_paragraph_cmd(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    await register_group(message.chat.id, message.chat.title)

    raw = message.text.partition(" ")[2].strip()

    try:
        subject_name, paragraph_title, points_count = [x.strip() for x in raw.split("|")]
        points_count = int(points_count)
        if not (1 <= points_count <= 100):
            raise ValueError
    except Exception:
        await message.answer(
            "Формат:\n"
            "<code>/addparagraph История | 4 | 8</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    subject = await get_subject_by_name(message.chat.id, subject_name)
    if not subject:
        await message.answer("Такого предмета нет.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

        cur = await db.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM paragraphs WHERE subject_id=?",
            (subject["id"],),
        )
        sort_order = (await cur.fetchone())[0]

        await db.execute("""
            INSERT OR IGNORE INTO paragraphs(subject_id, title, sort_order)
            VALUES (?, ?, ?)
        """, (subject["id"], paragraph_title, sort_order))

        cur = await db.execute(
            "SELECT id FROM paragraphs WHERE subject_id=? AND title=?",
            (subject["id"], paragraph_title),
        )
        paragraph_id = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT COALESCE(MAX(number), 0) FROM points WHERE paragraph_id=?",
            (paragraph_id,),
        )
        current_max = (await cur.fetchone())[0]

        for n in range(current_max + 1, points_count + 1):
            await db.execute(
                "INSERT OR IGNORE INTO points(paragraph_id, number) VALUES (?, ?)",
                (paragraph_id, n),
            )

        await db.commit()

    await message.answer(
        f"✅ Добавлен/обновлён §{html.escape(paragraph_title)} "
        f"в предмете <b>{html.escape(subject_name)}</b>.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("addpoints"))
async def add_points_cmd(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    await register_group(message.chat.id, message.chat.title)

    raw = message.text.partition(" ")[2].strip()

    try:
        subject_name, paragraph_title, amount_raw = [x.strip() for x in raw.split("|")]
        amount = int(amount_raw)
        if not (1 <= amount <= 100):
            raise ValueError
    except Exception:
        await message.answer(
            "Формат:\n"
            "<code>/addpoints История | 2 | 3</code>\n\n"
            "Добавит ещё 3 пункта в §2.",
            parse_mode=ParseMode.HTML,
        )
        return

    subject = await get_subject_by_name(message.chat.id, subject_name)
    if not subject:
        await message.answer("Такого предмета нет.")
        return

    paragraph = await get_paragraph_by_title(subject["id"], paragraph_title)
    if not paragraph:
        await message.answer("Такого параграфа нет.")
        return

    first, last = await add_points(paragraph["id"], amount)

    await message.answer(
        f"✅ В <b>{html.escape(subject_name)}</b>, §{html.escape(paragraph_title)} "
        f"добавлены пункты {first}–{last}.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("reset_subject"))
async def reset_subject_cmd(message: Message, bot: Bot):
    if message.chat.type == "private":
        return

    if not await is_admin(bot, message.chat.id, message.from_user.id):
        return

    await register_group(message.chat.id, message.chat.title)

    subject_name = message.text.partition(" ")[2].strip()
    if not subject_name:
        await message.answer(
            "Формат: <code>/reset_subject История</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE points
            SET claimed_by=NULL, claimed_name=NULL, claimed_username=NULL
            WHERE paragraph_id IN (
                SELECT p.id
                FROM paragraphs p
                JOIN subjects s ON s.id = p.subject_id
                WHERE s.chat_id=? AND s.name=?
            )
        """, (message.chat.id, subject_name))
        await db.commit()

    await message.answer(
        f"♻️ Все пункты предмета <b>{html.escape(subject_name)}</b> освобождены.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("id"))
async def id_cmd(message: Message):
    if message.chat.type != "private":
        return

    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>\n\n"
        "Его можно указать в <code>BOT_OWNER_ID</code> в файле .env.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("backup"))
async def backup_cmd(message: Message):
    if message.chat.type != "private":
        return

    if BOT_OWNER_ID is None:
        await message.answer(
            "⚠️ Команда /backup ещё не настроена.\n\n"
            "1. Напиши мне <code>/id</code>.\n"
            "2. Скопируй свой Telegram ID.\n"
            "3. Добавь в <code>.env</code> строку:\n"
            "<code>BOT_OWNER_ID=твой_id</code>\n"
            "4. Перезапусти бота.",
            parse_mode=ParseMode.HTML,
        )
        return

    if message.from_user.id != BOT_OWNER_ID:
        await message.answer("⛔ У тебя нет доступа к резервной копии базы.")
        return

    backup_path = None
    try:
        backup_path = await asyncio.to_thread(make_database_backup)

        await message.answer_document(
            document=FSInputFile(
                backup_path,
                filename=backup_path.name,
            ),
            caption=(
                "💾 <b>Резервная копия базы данных</b>\n"
                f"Создана: <code>{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}</code>\n\n"
                "Сохрани этот файл на всякий случай."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await message.answer(
            "❌ Не удалось создать резервную копию базы.\n"
            f"<code>{html.escape(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if backup_path is not None:
            try:
                backup_path.unlink(missing_ok=True)
                backup_path.parent.rmdir()
            except Exception:
                pass


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "📚 <b>Для всех:</b>\n"
        "/menu или настроенная команда — открыть общее меню\n"
        "/my — мои занятые пункты\n\n"
        "⚙️ <b>Для админов:</b>\n"
        "/admin — админ-панель в личке с ботом\n"        "Главный админ может добавлять помощников через «👤 Админы бота»\n"
        "/backup — скачать резервную копию БД (только владелец)\n"
        "/id — узнать свой Telegram ID\n"
        "<code>/setup История | 3-7 | 5</code>\n"
        "<code>/limit История | 4</code>\n"
        "<code>/addparagraph История | 4 | 8</code>\n"
        "<code>/addpoints История | 2 | 3</code>\n"
        "<code>/reset_subject История</code>",
        parse_mode=ParseMode.HTML,
    )



# =========================================================
# НАСТРАИВАЕМАЯ КОМАНДА МЕНЮ
# ВАЖНО: этот catch-all стоит ПОСЛЕ всех системных команд,
# чтобы не перехватывать /admin, /backup, /help и т.д.
# =========================================================

@router.message(F.text.startswith("/"))
async def custom_menu_command(message: Message):
    if message.chat.type == "private":
        return

    configured = await get_menu_command(message.chat.id)
    incoming = (message.text or "").strip()

    # Telegram может прислать /команда@имя_бота
    base = incoming.split("@", 1)[0]

    if base != configured:
        return

    await send_group_menu(message)


# =========================================================
# CALLBACKS ОБЫЧНОГО МЕНЮ
# =========================================================

@router.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery):
    text, kb = await subjects_screen(callback.message.chat.id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("sub:"))
async def cb_subject(callback: CallbackQuery):
    subject_id = int(callback.data.split(":")[1])
    screen = await subject_screen(
        callback.message.chat.id,
        subject_id,
        callback.from_user.id,
    )

    if not screen:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    text, kb = screen

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("par:"))
async def cb_paragraph(callback: CallbackQuery):
    paragraph_id = int(callback.data.split(":")[1])
    screen = await paragraph_screen(
        callback.message.chat.id,
        paragraph_id,
        callback.from_user.id,
    )

    if not screen:
        await callback.answer("Параграф не найден.", show_alert=True)
        return

    text, kb = screen

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("pt:"))
async def cb_point(callback: CallbackQuery):
    point_id = int(callback.data.split(":")[1])

    result = await toggle_claim(
        point_id=point_id,
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        full_name=callback.from_user.full_name,
        username=callback.from_user.username,
    )

    if result[0] == "missing":
        await callback.answer("Пункт не найден.", show_alert=True)
        return

    if result[0] == "busy":
        await callback.answer(
            f"Этот пункт уже занял(а): {result[2]}",
            show_alert=True,
        )
        return

    if result[0] == "limit":
        await callback.answer(
            f"Достигнут лимит: максимум {result[2]} пункт(а/ов) по этому предмету.",
            show_alert=True,
        )
        return

    paragraph_id = result[1]
    screen = await paragraph_screen(
        callback.message.chat.id,
        paragraph_id,
        callback.from_user.id,
    )

    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(
                text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    if result[0] == "claimed":
        await callback.answer("✅ Пункт занят")
    else:
        await callback.answer("🟢 Пункт освобождён")


# =========================================================
# CALLBACKS АДМИНКИ В ЛИЧКЕ
# =========================================================

@router.callback_query(F.data == "adm_groups")
async def cb_admin_groups(callback: CallbackQuery, bot: Bot, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Открой админку в личке с ботом.", show_alert=True)
        return

    await state.clear()
    text, kb = await admin_groups_screen(bot, callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "botadmins_home")
async def cb_bot_admins_home(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Открой админку в личке.", show_alert=True)
        return
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный админ.", show_alert=True)
        return

    await state.clear()
    text, kb = await bot_admins_screen()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "botadmins_add")
async def cb_bot_admins_add(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer("Открой админку в личке.", show_alert=True)
        return
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный админ.", show_alert=True)
        return

    await state.clear()
    await state.set_state(AddBotAdminStates.waiting_user_id)
    await callback.message.answer(
        "➕ <b>Добавление админа бота</b>\n\n"
        "Отправь Telegram ID человека.\n"
        "Он может узнать его, написав этому боту <code>/id</code>.\n\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("botadmins_delask:"))
async def cb_bot_admins_delete_ask(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer("Открой админку в личке.", show_alert=True)
        return
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный админ.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, display_name, username FROM bot_admins WHERE user_id=?",
            (user_id,),
        )
        admin = await cur.fetchone()

    if not admin:
        await callback.answer("Админ уже удалён.", show_alert=True)
        return

    name = admin["display_name"] or f"ID {user_id}"
    text = (
        f"🗑 Убрать <b>{html.escape(name)}</b> из админов бота?\n\n"
        "После этого человек потеряет глобальный доступ к настройкам."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Да, убрать",
            callback_data=f"botadmins_del:{user_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Нет, назад",
            callback_data="botadmins_home",
        )],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("botadmins_del:"))
async def cb_bot_admins_delete(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer("Открой админку в личке.", show_alert=True)
        return
    if not is_owner(callback.from_user.id):
        await callback.answer("Только главный админ.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    deleted = await remove_bot_admin(user_id)

    text, kb = await bot_admins_screen()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass

    await callback.answer("✅ Доступ удалён" if deleted else "Уже удалён")


@router.callback_query(F.data.startswith("adm_group:"))
async def cb_admin_group(callback: CallbackQuery, bot: Bot, state: FSMContext):
    group_id = int(callback.data.split(":")[1])
    if not await require_admin_callback(callback, bot, group_id):
        return

    await state.clear()

    try:
        chat = await bot.get_chat(group_id)
        await register_group(group_id, chat.title)
    except Exception:
        pass

    text, kb = await admin_home_screen(group_id, callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_menucommand:"))
async def cb_admin_menu_command(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await require_owner_callback(callback):
        return

    group_id = int(callback.data.split(":")[1])
    await state.clear()

    text, kb = await admin_menu_command_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("adm_menucommandedit:"))
async def cb_admin_menu_command_edit(
    callback: CallbackQuery,
    state: FSMContext,
):
    if not await require_owner_callback(callback):
        return

    group_id = int(callback.data.split(":")[1])

    await state.clear()
    await state.update_data(target_chat_id=group_id)
    await state.set_state(MenuCommandStates.waiting_command)

    await callback.message.answer(
        "⌨️ Отправь новую команду меню.\n\n"
        "Например: <code>/предметы</code>\n"
        "<code>/default</code> — вернуть /menu\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_menucommanddefault:"))
async def cb_admin_menu_command_default(
    callback: CallbackQuery,
):
    if not await require_owner_callback(callback):
        return

    group_id = int(callback.data.split(":")[1])
    await set_menu_command(group_id, "/menu")

    text, kb = await admin_menu_command_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer("✅ Команда снова /menu")


@router.callback_query(F.data.startswith("adm_menutitle:"))
async def cb_admin_menu_title(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return

    if not await require_admin_callback(
        callback,
        bot,
        group_id,
    ):
        return

    await state.clear()
    text, kb = await admin_menu_title_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("adm_menutitleedit:"))
async def cb_admin_menu_title_edit(
    callback: CallbackQuery,
    bot: Bot,
    state: FSMContext,
):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return

    if not await require_admin_callback(
        callback,
        bot,
        group_id,
    ):
        return

    await state.clear()
    await state.update_data(target_chat_id=group_id)
    await state.set_state(MenuTitleStates.waiting_title)

    await callback.message.answer(
        "🏷 Отправь новый заголовок /menu одним сообщением.\n\n"
        "Можно использовать Telegram custom emoji / "
        "анимированный эмодзи.\n\n"
        "Например: <code>📚 Наши предметы</code>\n"
        "<code>/default</code> — вернуть «📚 Предметы»\n"
        "<code>/empty</code> — убрать заголовок\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_menutitleclear:"))
async def cb_admin_menu_title_clear(
    callback: CallbackQuery,
    bot: Bot,
):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return

    if not await require_admin_callback(
        callback,
        bot,
        group_id,
    ):
        return

    await set_menu_title(group_id, "", "")
    text, kb = await admin_menu_title_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer("✅ Заголовок убран")


@router.callback_query(F.data.startswith("adm_menutitledefault:"))
async def cb_admin_menu_title_default(
    callback: CallbackQuery,
    bot: Bot,
):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return

    if not await require_admin_callback(
        callback,
        bot,
        group_id,
    ):
        return

    title = "📚 Предметы"
    await set_menu_title(
        group_id,
        title,
        html.escape(title),
    )

    text, kb = await admin_menu_title_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer("✅ Стандартный заголовок восстановлен")


@router.callback_query(F.data.startswith("adm_menutext:"))
async def cb_admin_menu_text(callback: CallbackQuery, bot: Bot, state: FSMContext):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return
    if not await require_admin_callback(callback, bot, group_id):
        return
    await state.clear()
    text, kb = await admin_menu_text_screen(group_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_menutextedit:"))
async def cb_admin_menu_text_edit(callback: CallbackQuery, bot: Bot, state: FSMContext):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return
    if not await require_admin_callback(callback, bot, group_id):
        return
    await state.clear()
    await state.update_data(target_chat_id=group_id)
    await state.set_state(MenuTextStates.waiting_text)
    await callback.message.answer(
        "📝 Отправь новый текст для /menu.\n\n"
        "Чтобы убрать текст полностью, отправь <code>/empty</code>.\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_menutextclear:"))
async def cb_admin_menu_text_clear(callback: CallbackQuery, bot: Bot):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return
    if not await require_admin_callback(callback, bot, group_id):
        return
    await set_menu_text(group_id, "")
    text, kb = await admin_menu_text_screen(group_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer("✅ Текст убран")


@router.callback_query(F.data.startswith("adm_menutextdefault:"))
async def cb_admin_menu_text_default(callback: CallbackQuery, bot: Bot):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return
    if not await require_admin_callback(callback, bot, group_id):
        return
    await set_menu_text(group_id, "Выбери предмет, затем параграф и нужный пункт.")
    text, kb = await admin_menu_text_screen(group_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer("✅ Стандартный текст восстановлен")


@router.callback_query(F.data.startswith("adm_menudel:"))
async def cb_admin_menu_delete(callback: CallbackQuery, bot: Bot):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return

    if not await require_admin_callback(callback, bot, group_id):
        return

    text, kb = await admin_menu_delete_screen(group_id)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("adm_menudelset:"))
async def cb_admin_menu_delete_set(callback: CallbackQuery, bot: Bot):
    _, group_raw, seconds_raw = callback.data.split(":")
    group_id = int(group_raw)

    if not await require_owner_callback(callback):
        return
    seconds = int(seconds_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    allowed = {0, 30, 60, 300, 600, 1800, 3600}
    if seconds not in allowed:
        await callback.answer("Некорректное значение.", show_alert=True)
        return

    await set_menu_delete_seconds(group_id, seconds)

    text, kb = await admin_menu_delete_screen(group_id)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer(
        "✅ Автоудаление отключено"
        if seconds == 0
        else f"✅ Меню будет удаляться через {format_delete_delay(seconds)}"
    )


@router.callback_query(F.data.startswith("adm_menudelcustom:"))
async def cb_admin_menu_delete_custom(callback: CallbackQuery, bot: Bot, state: FSMContext):
    group_id = int(callback.data.split(":")[1])

    if not await require_owner_callback(callback):
        return
    if not await require_admin_callback(callback, bot, group_id):
        return
    await state.clear()
    await state.update_data(target_chat_id=group_id)
    await state.set_state(MenuDeleteCustomStates.waiting_value)
    await callback.message.answer(
        "⏱ Отправь своё время автоудаления.\n\n"
        "Примеры: <code>19</code> = 19 секунд, <code>45с</code>, "
        "<code>2мин</code>, <code>1ч</code>.\n"
        "<code>0</code> — не удалять. Максимум 7 дней.\n\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_add_subject:"))
async def cb_admin_add_subject(callback: CallbackQuery, bot: Bot, state: FSMContext):
    group_id = int(callback.data.split(":")[1])
    if not await require_admin_callback(callback, bot, group_id):
        return

    await state.clear()
    await state.update_data(target_chat_id=group_id)
    await state.set_state(AddSubjectStates.waiting_name)

    group_title = await get_group_title(group_id) or f"Группа {group_id}"
    await callback.message.answer(
        f"➕ <b>Добавление предмета</b>\n"
        f"Группа: <b>{html.escape(group_title)}</b>\n\n"
        "Отправь название предмета, например: <code>История</code>\n\n"
        "Для отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rename_sub:"))
async def cb_admin_rename_subject(callback: CallbackQuery, bot: Bot, state: FSMContext):
    _, group_raw, subject_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)
    if not await require_admin_callback(callback, bot, group_id):
        return
    subject = await get_subject(subject_id, group_id)
    if not subject:
        await callback.answer("Предмет не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(target_chat_id=group_id, target_subject_id=subject_id)
    await state.set_state(RenameSubjectStates.waiting_name)
    await callback.message.answer(
        f"✏️ Текущее название: <b>{html.escape(subject['name'])}</b>\n\n"
        "Отправь новое название предмета.\nДля отмены: /cancel",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm_delask:"))
async def cb_admin_delete_ask(callback: CallbackQuery, bot: Bot):
    _, group_raw, subject_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    subject = await get_subject(subject_id, group_id)
    if not subject:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    text = (
        f"🗑 <b>Удалить предмет «{html.escape(subject['name'])}»?</b>\n\n"
        "Будут удалены все его параграфы, пункты и занятые места. "
        "Это действие нельзя отменить."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🗑 Да, удалить",
            callback_data=f"adm_delconfirm:{group_id}:{subject_id}",
        )],
        [InlineKeyboardButton(
            text="↩️ Нет, назад",
            callback_data=f"adm_sub:{group_id}:{subject_id}",
        )],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_delconfirm:"))
async def cb_admin_delete_confirm(callback: CallbackQuery, bot: Bot):
    _, group_raw, subject_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    subject = await get_subject(subject_id, group_id)
    if not subject:
        await callback.answer("Предмет уже удалён.", show_alert=True)
        return

    name = subject["name"]
    deleted = await delete_subject(subject_id, group_id)
    if not deleted:
        await callback.answer("Не удалось удалить предмет.", show_alert=True)
        return

    text, kb = await admin_home_screen(group_id, callback.from_user.id)
    try:
        await callback.message.edit_text(
            f"✅ Предмет <b>{html.escape(name)}</b> удалён.\n\n" + text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass
    await callback.answer("🗑 Предмет удалён")


@router.callback_query(F.data.startswith("adm_sub:"))
async def cb_admin_subject(callback: CallbackQuery, bot: Bot):
    _, group_raw, subject_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    screen = await admin_subject_screen(group_id, subject_id)
    if not screen:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    text, kb = screen
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_limit:"))
async def cb_admin_limit(callback: CallbackQuery, bot: Bot):
    _, group_raw, subject_raw, limit_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)
    limit = int(limit_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE subjects SET max_per_user=? WHERE id=? AND chat_id=?",
            (None if limit == 0 else limit, subject_id, group_id),
        )
        await db.commit()

    if cur.rowcount == 0:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    screen = await admin_subject_screen(group_id, subject_id)
    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass

    await callback.answer("✅ Лимит убран" if limit == 0 else f"✅ Лимит: {limit}")


@router.callback_query(F.data.startswith("adm_newpar:"))
async def cb_admin_new_paragraph(callback: CallbackQuery, bot: Bot):
    _, group_raw, subject_raw = callback.data.split(":")
    group_id = int(group_raw)
    subject_id = int(subject_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    subject = await get_subject(subject_id, group_id)
    if not subject:
        await callback.answer("Предмет не найден.", show_alert=True)
        return

    _, title = await create_next_paragraph(subject_id, 5)

    screen = await admin_subject_screen(group_id, subject_id)
    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass

    await callback.answer(f"✅ Создан §{title} с 5 пунктами")


@router.callback_query(F.data.startswith("adm_par:"))
async def cb_admin_paragraph(callback: CallbackQuery, bot: Bot):
    _, group_raw, paragraph_raw = callback.data.split(":")
    group_id = int(group_raw)
    paragraph_id = int(paragraph_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    screen = await admin_paragraph_screen(group_id, paragraph_id)
    if not screen:
        await callback.answer("Параграф не найден.", show_alert=True)
        return

    text, kb = screen
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm_delparask:"))
async def cb_admin_delete_paragraph_ask(callback: CallbackQuery, bot: Bot):
    _, group_raw, paragraph_raw = callback.data.split(":")
    group_id = int(group_raw)
    paragraph_id = int(paragraph_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    info = await get_paragraph_info(paragraph_id, group_id)
    if not info:
        await callback.answer("Параграф не найден.", show_alert=True)
        return

    points = await get_points(paragraph_id)
    occupied = sum(1 for p in points if p["claimed_by"] is not None)

    text = (
        f"🗑 <b>Удалить §{html.escape(info['title'])}?</b>\n\n"
        f"Предмет: <b>{html.escape(info['subject_name'])}</b>\n"
        f"Пунктов: <b>{len(points)}</b>\n"
        f"Занятых пунктов: <b>{occupied}</b>\n\n"
        "Будет удалён весь параграф вместе со всеми его пунктами "
        "и всеми текущими занятиями. Это действие нельзя отменить."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🗑 Да, удалить параграф",
                callback_data=f"adm_delpar:{group_id}:{paragraph_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="↩️ Нет, назад",
                callback_data=f"adm_par:{group_id}:{paragraph_id}",
            )
        ],
    ])

    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest:
        pass

    await callback.answer()


@router.callback_query(F.data.startswith("adm_delpar:"))
async def cb_admin_delete_paragraph(callback: CallbackQuery, bot: Bot):
    _, group_raw, paragraph_raw = callback.data.split(":")
    group_id = int(group_raw)
    paragraph_id = int(paragraph_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    result = await delete_paragraph(paragraph_id, group_id)
    if not result:
        await callback.answer("Параграф уже удалён или не найден.", show_alert=True)
        return

    subject_id, paragraph_title = result

    screen = await admin_subject_screen(group_id, subject_id)
    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(
                text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            pass

    await callback.answer(f"🗑 §{paragraph_title} удалён")


@router.callback_query(F.data.startswith("adm_addpts:"))
async def cb_admin_add_points(callback: CallbackQuery, bot: Bot):
    _, group_raw, paragraph_raw, amount_raw = callback.data.split(":")
    group_id = int(group_raw)
    paragraph_id = int(paragraph_raw)
    amount = int(amount_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    info = await get_paragraph_info(paragraph_id, group_id)
    if not info:
        await callback.answer("Параграф не найден.", show_alert=True)
        return

    first, last = await add_points(paragraph_id, amount)

    screen = await admin_paragraph_screen(group_id, paragraph_id)
    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass

    await callback.answer(f"✅ Добавлены пункты {first}–{last}")


@router.callback_query(F.data.startswith("adm_delpts:"))
async def cb_admin_delete_points(callback: CallbackQuery, bot: Bot):
    _, group_raw, paragraph_raw, amount_raw = callback.data.split(":")
    group_id = int(group_raw)
    paragraph_id = int(paragraph_raw)
    amount = int(amount_raw)

    if not await require_admin_callback(callback, bot, group_id):
        return

    info = await get_paragraph_info(paragraph_id, group_id)
    if not info:
        await callback.answer("Параграф не найден.", show_alert=True)
        return

    result = await remove_points(paragraph_id, amount)

    if result[0] == "not_enough":
        total = result[1]
        await callback.answer(
            f"Нельзя удалить {amount}: сейчас всего {total} пункт(а/ов).",
            show_alert=True,
        )
        return

    if result[0] == "occupied":
        numbers = ", ".join(map(str, result[1]))
        await callback.answer(
            f"Нельзя удалить: среди последних пунктов заняты № {numbers}. "
            "Сначала освободите их.",
            show_alert=True,
        )
        return

    _, first, last = result

    screen = await admin_paragraph_screen(group_id, paragraph_id)
    if screen:
        text, kb = screen
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass

    if first == last:
        await callback.answer(f"🗑 Удалён пункт {first}")
    else:
        await callback.answer(f"🗑 Удалены пункты {first}–{last}")


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    await init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    cleanup_task = asyncio.create_task(menu_cleanup_worker(bot))

    print("Study Claim Bot v16 запущен")
    print(
        "Используемые Telegram updates:",
        ", ".join(dp.resolve_used_update_types()),
    )

    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
