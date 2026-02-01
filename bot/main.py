import os
import tempfile
import logging
import re
import asyncio
import subprocess
from datetime import datetime
from dataclasses import dataclass

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, Command, CommandStart, IS_NOT_MEMBER, IS_MEMBER, ADMINISTRATOR
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ClevVPN")
CLEVVPN_API_URL = os.getenv("CLEVVPN_API_URL", "http://89.111.143.90:8080")

# Админы бота
ADMIN_IDS = [586107799, 762967142]

# Путь к базе данных
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("BOT_TOKEN and GROQ_API_KEY must be set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

# Модель для пересказа, перевода и пунктуации
LLM_MODEL = "openai/gpt-oss-120b"

# Хранилище расшифровок: {message_id: text}
# Нужно чтобы при нажатии кнопки знать какой текст обрабатывать
transcriptions: dict[int, str] = {}

# Настройки retry
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2, 3]  # секунды между попытками


# ==================== Модель маркетинговых ссылок ====================

@dataclass
class MarketingLink:
    id: int
    code: str
    clicks_count: int
    paid_count: int
    created_at: datetime
    created_by: int


class AdminStates(StatesGroup):
    waiting_marketing_link_code = State()


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь админом."""
    return user_id in ADMIN_IDS


async def init_db():
    """Инициализация базы данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS marketing_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                clicks_count INTEGER DEFAULT 0,
                paid_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER NOT NULL
            )
        """)
        await db.commit()


async def get_all_marketing_links() -> list[MarketingLink]:
    """Получает все маркетинговые ссылки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM marketing_links ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                MarketingLink(
                    id=row["id"],
                    code=row["code"],
                    clicks_count=row["clicks_count"],
                    paid_count=row["paid_count"],
                    created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
                    created_by=row["created_by"],
                )
                for row in rows
            ]


async def get_marketing_link_by_id(link_id: int) -> MarketingLink | None:
    """Получает ссылку по ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM marketing_links WHERE id = ?", (link_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return MarketingLink(
                    id=row["id"],
                    code=row["code"],
                    clicks_count=row["clicks_count"],
                    paid_count=row["paid_count"],
                    created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
                    created_by=row["created_by"],
                )
            return None


async def get_marketing_link_by_code(code: str) -> MarketingLink | None:
    """Получает ссылку по коду."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM marketing_links WHERE code = ?", (code,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return MarketingLink(
                    id=row["id"],
                    code=row["code"],
                    clicks_count=row["clicks_count"],
                    paid_count=row["paid_count"],
                    created_at=datetime.fromisoformat(row["created_at"]) if isinstance(row["created_at"], str) else row["created_at"],
                    created_by=row["created_by"],
                )
            return None


async def create_marketing_link(code: str, created_by: int) -> MarketingLink:
    """Создает новую маркетинговую ссылку."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO marketing_links (code, created_by) VALUES (?, ?)",
            (code, created_by),
        )
        await db.commit()
        link_id = cursor.lastrowid
        return MarketingLink(
            id=link_id,
            code=code,
            clicks_count=0,
            paid_count=0,
            created_at=datetime.now(),
            created_by=created_by,
        )


async def increment_marketing_link_clicks(code: str) -> bool:
    """Увеличивает счетчик кликов по ссылке."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE marketing_links SET clicks_count = clicks_count + 1 WHERE code = ?",
            (code,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_marketing_link(link_id: int) -> bool:
    """Удаляет маркетинговую ссылку."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM marketing_links WHERE id = ?", (link_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


def is_group_chat(message: Message) -> bool:
    """Проверяет, является ли чат групповым."""
    return message.chat.type in ("group", "supergroup")


async def safe_send_message(
    target: Message | CallbackQuery,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    """
    Безопасная отправка сообщения с retry логикой.

    При ошибке парсинга Markdown - пробует без форматирования.
    При сетевых ошибках - делает до 3 попыток с задержками.
    """
    message_target = target.message if isinstance(target, CallbackQuery) else target

    for attempt in range(MAX_RETRIES):
        try:
            return await message_target.answer(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except TelegramBadRequest as e:
            error_text = str(e).lower()
            # Ошибка парсинга Markdown - пробуем без форматирования
            if "can't parse entities" in error_text or "parse entities" in error_text:
                logger.warning(f"Markdown parse error, retrying without formatting: {e}")
                try:
                    return await message_target.answer(
                        text,
                        parse_mode=None,
                        reply_markup=reply_markup
                    )
                except Exception as fallback_error:
                    logger.error(f"Fallback send also failed: {fallback_error}")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAYS[attempt])
                        continue
            else:
                # Другая ошибка Telegram - retry
                logger.warning(f"Telegram error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    continue
        except Exception as e:
            logger.warning(f"Send error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue

    # Все попытки исчерпаны
    try:
        return await message_target.answer(
            "⚠️ Не удалось отправить сообщение. Попробуйте через минуту."
        )
    except Exception:
        logger.error("Failed to send error message to user")
        return None


async def safe_edit_message(
    message: Message,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: InlineKeyboardMarkup | None = None
) -> Message | bool | None:
    """
    Безопасное редактирование сообщения с retry логикой.
    """
    for attempt in range(MAX_RETRIES):
        try:
            return await message.edit_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except TelegramBadRequest as e:
            error_text = str(e).lower()
            # Ошибка парсинга Markdown - пробуем без форматирования
            if "can't parse entities" in error_text or "parse entities" in error_text:
                logger.warning(f"Markdown parse error on edit, retrying without formatting: {e}")
                try:
                    return await message.edit_text(
                        text,
                        parse_mode=None,
                        reply_markup=reply_markup
                    )
                except Exception as fallback_error:
                    logger.error(f"Fallback edit also failed: {fallback_error}")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAYS[attempt])
                        continue
            else:
                logger.warning(f"Telegram edit error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAYS[attempt])
                    continue
        except Exception as e:
            logger.warning(f"Edit error (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAYS[attempt])
                continue

    # Все попытки исчерпаны
    try:
        return await message.edit_text(
            "⚠️ Не удалось обновить сообщение. Попробуйте через минуту."
        )
    except Exception:
        logger.error("Failed to send error message to user")
        return None


MAX_MESSAGE_LENGTH = 4000  # Оставляем запас от лимита 4096


def split_text(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Разбивает длинный текст на части, не превышающие max_length.
    Старается разбивать по абзацам, затем по предложениям, затем по словам.
    """
    if len(text) <= max_length:
        return [text]

    parts = []
    current_part = ""

    # Разбиваем по абзацам
    paragraphs = text.split('\n\n')

    for paragraph in paragraphs:
        # Если абзац сам по себе слишком длинный
        if len(paragraph) > max_length:
            # Сначала добавляем то что накопили
            if current_part:
                parts.append(current_part.strip())
                current_part = ""

            # Разбиваем длинный абзац по предложениям
            sentences = re.split(r'(?<=[.!?])\s+', paragraph)
            for sentence in sentences:
                if len(sentence) > max_length:
                    # Даже предложение слишком длинное - режем по словам
                    words = sentence.split()
                    for word in words:
                        if len(current_part) + len(word) + 1 > max_length:
                            parts.append(current_part.strip())
                            current_part = word
                        else:
                            current_part += " " + word if current_part else word
                elif len(current_part) + len(sentence) + 1 > max_length:
                    parts.append(current_part.strip())
                    current_part = sentence
                else:
                    current_part += " " + sentence if current_part else sentence
        elif len(current_part) + len(paragraph) + 2 > max_length:
            parts.append(current_part.strip())
            current_part = paragraph
        else:
            current_part += "\n\n" + paragraph if current_part else paragraph

    if current_part.strip():
        parts.append(current_part.strip())

    return parts


def detect_language(text: str) -> str:
    """
    Определяет язык текста по наличию кириллицы.
    Возвращает 'ru' если >= 30% букв кириллица, иначе 'en'.

    Простая эвристика: считаем процент кириллических символов
    среди всех буквенных символов в тексте.
    """
    # Находим все буквы (любого алфавита)
    letters = re.findall(r'[a-zA-Zа-яА-ЯёЁ]', text)
    if not letters:
        return 'ru'  # По умолчанию русский

    # Считаем кириллицу
    cyrillic = re.findall(r'[а-яА-ЯёЁ]', text)
    cyrillic_ratio = len(cyrillic) / len(letters)

    return 'ru' if cyrillic_ratio >= 0.3 else 'en'


async def check_channel_subscription(user_id: int) -> bool:
    """
    Проверяет, подписан ли пользователь на канал.
    Возвращает True если подписан, False если нет.
    """
    if not CHANNEL_ID:
        return True  # Если канал не настроен, пропускаем проверку

    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception:
        logger.exception("Error checking subscription")
        return False


async def check_clevvpn_bot_started(user_id: int) -> bool:
    if not CLEVVPN_API_URL:
        return True
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{CLEVVPN_API_URL}/api/user/exists",
                params={"telegram_id": user_id},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("exists", False)
                return False
    except Exception:
        logger.exception("Error checking ClevVPN bot started")
        return False


async def check_all_requirements(user_id: int) -> tuple[bool, bool]:
    channel_ok = await check_channel_subscription(user_id)
    bot_ok = await check_clevvpn_bot_started(user_id)
    return channel_ok, bot_ok


def get_requirements_message(channel_ok: bool, bot_ok: bool) -> str:
    step1 = "✅" if channel_ok else "❌"
    step2 = "✅" if bot_ok else "❌"
    return (
        "Чтобы бот всегда оставался бесплатным и работал стабильно, "
        "необходимо выполнить два пункта:\n\n"
        f"{step1} Шаг 1: Подпишитесь на наш [канал](https://t.me/ClevVPN)\n\n"
        f"{step2} Шаг 2: Зайдите в [бота](https://t.me/ClevVPN_bot) и нажмите «Старт»\n\n"
        "И всё — вы в деле! Спасибо, что вы с нами! ❤️"
    )


def get_requirements_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал", url="https://t.me/ClevVPN")],
        [InlineKeyboardButton(text="🤖 Бот ClevVPN", url="https://t.me/ClevVPN_bot")],
        [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_requirements")]
    ])


async def send_requirements_message(message: Message) -> None:
    channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
    text = get_requirements_message(channel_ok, bot_ok)
    keyboard = get_requirements_keyboard()
    await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)


def build_keyboard(text: str, message_id: int) -> InlineKeyboardMarkup:
    """
    Создает клавиатуру с кнопками под расшифровкой.

    Кнопка перевода зависит от языка:
    - Русский текст -> "Перевести на английский"
    - Английский текст -> "Перевести на русский"

    Кнопка пересказа показывается только если в тексте >= 20 слов.
    """
    lang = detect_language(text)

    # callback_data содержит действие и message_id через двоеточие
    # Формат: "action:message_id"
    translate_text = "Перевести на английский" if lang == 'ru' else "Перевести на русский"
    target_lang = "en" if lang == 'ru' else "ru"

    buttons = []

    # Кнопка пересказа только для текстов с 20+ слов
    word_count = len(text.split())
    if word_count >= 20:
        buttons.append([InlineKeyboardButton(text="Краткий пересказ", callback_data=f"summary:{message_id}")])

    buttons.append([InlineKeyboardButton(text=translate_text, callback_data=f"translate:{target_lang}:{message_id}")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def summarize_text(text: str) -> str:
    """
    Создает краткий пересказ текста через LLM.
    Промпт просит сохранить все важное, но сделать текст короче.
    """
    response = groq_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Ты помощник для создания кратких пересказов. Сделай краткий пересказ текста, сохранив все важные детали, имена, даты, цифры и ключевые мысли. Пиши на том же языке что и оригинал. Выводи только пересказ, без пояснений."
            },
            {
                "role": "user",
                "content": text
            }
        ],
        temperature=0.3  # Низкая температура для более точного пересказа
    )
    return response.choices[0].message.content


async def translate_text(text: str, target_lang: str) -> str:
    """
    Переводит текст на указанный язык через LLM.
    target_lang: 'ru' или 'en'
    """
    lang_name = "русский" if target_lang == "ru" else "английский"

    response = groq_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": f"Ты переводчик. Переведи текст на {lang_name} язык. Сохрани смысл и стиль оригинала. Выводи только перевод, без пояснений."
            },
            {
                "role": "user",
                "content": text
            }
        ],
        temperature=0.3
    )
    return response.choices[0].message.content


async def fix_punctuation(text: str) -> str:
    """
    Исправляет пунктуацию в тексте через LLM.
    Добавляет точки, запятые, заглавные буквы где нужно.
    """
    response = groq_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Исправь пунктуацию в тексте: добавь точки, запятые, вопросительные и восклицательные знаки, заглавные буквы в начале предложений. Не меняй слова, не добавляй и не удаляй текст — только расставь знаки препинания. Выводи только исправленный текст."
            },
            {
                "role": "user",
                "content": text
            }
        ],
        temperature=0.1
    )
    return response.choices[0].message.content


@dp.message(F.content_type == "voice")
async def handle_voice(message: Message) -> None:
    """Handle voice messages and transcribe them using Whisper."""
    in_group = is_group_chat(message)

    # В личных чатах проверяем требования, в группах - пропускаем
    if not in_group:
        channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
        if not channel_ok or not bot_ok:
            await send_requirements_message(message)
            return

    # Отправляем сообщение и сохраняем его, чтобы потом отредактировать
    # В группах отвечаем реплаем на исходное сообщение
    status_msg = await message.answer(
        "Расшифровываю...",
        reply_to_message_id=message.message_id if in_group else None
    )

    try:
        # Download voice file
        file = await bot.get_file(message.voice.file_id)
        file_bytes = await bot.download_file(file.file_path)

        # Save to temp file (Groq needs a file path)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(file_bytes.read())
            tmp_path = tmp.name

        try:
            # Transcribe with Whisper via Groq
            with open(tmp_path, "rb") as audio_file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(tmp_path, audio_file.read()),
                    model="whisper-large-v3",
                )

            raw_text = transcription.text.strip()
            if raw_text:
                # Исправляем пунктуацию через LLM
                text = await fix_punctuation(raw_text)

                # Сохраняем текст для последующих действий (пересказ/перевод)
                transcriptions[status_msg.message_id] = text

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    # Текст умещается в одно сообщение
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка:**\n\n{text}",
                        reply_markup=keyboard
                    )
                else:
                    # Текст слишком длинный - отправляем частями
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка (часть 1/{len(parts)}):**\n\n{parts[0]}"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            # Последняя часть - с кнопками
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                reply_markup=keyboard
                            )
                        else:
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}"
                            )
            else:
                await status_msg.edit_text("Не удалось распознать речь.")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error transcribing voice message")
        await safe_edit_message(status_msg, "⚠️ Ошибка при расшифровке. Попробуйте через минуту.", parse_mode=None)


@dp.message(F.content_type == "audio")
async def handle_audio(message: Message) -> None:
    """Handle audio files."""
    # В групповых чатах игнорируем аудиофайлы (только voice и video_note)
    if is_group_chat(message):
        return

    # Проверяем все требования
    channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
    if not channel_ok or not bot_ok:
        await send_requirements_message(message)
        return

    status_msg = await message.answer("Расшифровываю аудио...")

    try:
        file = await bot.get_file(message.audio.file_id)
        file_bytes = await bot.download_file(file.file_path)

        # Get file extension from mime type or default to mp3
        ext = ".mp3"
        if message.audio.mime_type:
            ext_map = {"audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/wav": ".wav"}
            ext = ext_map.get(message.audio.mime_type, ".mp3")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(file_bytes.read())
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as audio_file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(tmp_path, audio_file.read()),
                    model="whisper-large-v3",
                )

            raw_text = transcription.text.strip()
            if raw_text:
                # Исправляем пунктуацию через LLM
                text = await fix_punctuation(raw_text)

                # Сохраняем текст для последующих действий
                transcriptions[status_msg.message_id] = text

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    # Текст умещается в одно сообщение
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка:**\n\n{text}",
                        reply_markup=keyboard
                    )
                else:
                    # Текст слишком длинный - отправляем частями
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка (часть 1/{len(parts)}):**\n\n{parts[0]}"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            # Последняя часть - с кнопками
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                reply_markup=keyboard
                            )
                        else:
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}"
                            )
            else:
                await status_msg.edit_text("Не удалось распознать речь.")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error transcribing audio")
        await safe_edit_message(status_msg, "⚠️ Ошибка при расшифровке. Попробуйте через минуту.", parse_mode=None)


@dp.message(F.content_type == "video")
async def handle_video(message: Message) -> None:
    """Handle video files - extract audio and transcribe."""
    # В групповых чатах игнорируем обычные видео (только voice и video_note)
    if is_group_chat(message):
        return

    # Проверяем все требования
    channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
    if not channel_ok or not bot_ok:
        await send_requirements_message(message)
        return

    status_msg = await message.answer("Извлекаю аудио из видео...")

    try:
        try:
            file = await bot.get_file(message.video.file_id)
        except TelegramBadRequest as e:
            if "file is too big" in str(e).lower():
                await safe_edit_message(
                    status_msg,
                    "⚠️ Видео слишком большое (максимум 20 МБ). Попробуйте отправить видео меньшего размера.",
                    parse_mode=None
                )
                return
            raise
        file_bytes = await bot.download_file(file.file_path)

        # Сохраняем видео во временный файл
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_video:
            tmp_video.write(file_bytes.read())
            video_path = tmp_video.name

        # Путь для извлечённого аудио
        audio_path = video_path.replace(".mp4", ".ogg")

        try:
            # Извлекаем аудио через ffmpeg
            result = subprocess.run(
                [
                    "ffmpeg", "-i", video_path,
                    "-vn",  # без видео
                    "-acodec", "libopus",  # кодек opus для ogg
                    "-b:a", "64k",  # битрейт
                    "-y",  # перезаписать если существует
                    audio_path
                ],
                capture_output=True,
                text=True,
                timeout=120  # таймаут 2 минуты
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg error: {result.stderr}")
                await safe_edit_message(status_msg, "⚠️ Не удалось извлечь аудио из видео.", parse_mode=None)
                return

            await safe_edit_message(status_msg, "Расшифровываю...", parse_mode=None)

            # Транскрибируем аудио
            with open(audio_path, "rb") as audio_file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(audio_path, audio_file.read()),
                    model="whisper-large-v3",
                )

            raw_text = transcription.text.strip()
            if raw_text:
                # Исправляем пунктуацию через LLM
                text = await fix_punctuation(raw_text)

                # Сохраняем текст для последующих действий
                transcriptions[status_msg.message_id] = text

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка видео:**\n\n{text}",
                        reply_markup=keyboard
                    )
                else:
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка видео (часть 1/{len(parts)}):**\n\n{parts[0]}"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                reply_markup=keyboard
                            )
                        else:
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}"
                            )
            else:
                await status_msg.edit_text("Не удалось распознать речь в видео.")
        finally:
            # Удаляем временные файлы
            if os.path.exists(video_path):
                os.unlink(video_path)
            if os.path.exists(audio_path):
                os.unlink(audio_path)

    except Exception as e:
        logger.exception("Error transcribing video")
        await safe_edit_message(status_msg, "⚠️ Ошибка при расшифровке видео. Попробуйте через минуту.", parse_mode=None)


@dp.message(F.content_type == "video_note")
async def handle_video_note(message: Message) -> None:
    """Handle video notes (круглые видеосообщения) - extract audio and transcribe."""
    in_group = is_group_chat(message)

    # В личных чатах проверяем требования, в группах - пропускаем
    if not in_group:
        channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
        if not channel_ok or not bot_ok:
            await send_requirements_message(message)
            return

    # В группах отвечаем реплаем на исходное сообщение
    status_msg = await message.answer(
        "Расшифровываю видеосообщение...",
        reply_to_message_id=message.message_id if in_group else None
    )

    try:
        try:
            file = await bot.get_file(message.video_note.file_id)
        except TelegramBadRequest as e:
            if "file is too big" in str(e).lower():
                await safe_edit_message(
                    status_msg,
                    "⚠️ Видеосообщение слишком большое (максимум 20 МБ).",
                    parse_mode=None
                )
                return
            raise
        file_bytes = await bot.download_file(file.file_path)

        # Сохраняем видео во временный файл
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_video:
            tmp_video.write(file_bytes.read())
            video_path = tmp_video.name

        # Путь для извлечённого аудио
        audio_path = video_path.replace(".mp4", ".ogg")

        try:
            # Извлекаем аудио через ffmpeg
            result = subprocess.run(
                [
                    "ffmpeg", "-i", video_path,
                    "-vn",
                    "-acodec", "libopus",
                    "-b:a", "64k",
                    "-y",
                    audio_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.error(f"FFmpeg error: {result.stderr}")
                await safe_edit_message(status_msg, "⚠️ Не удалось извлечь аудио из видеосообщения.", parse_mode=None)
                return

            # Транскрибируем аудио
            with open(audio_path, "rb") as audio_file:
                transcription = groq_client.audio.transcriptions.create(
                    file=(audio_path, audio_file.read()),
                    model="whisper-large-v3",
                )

            raw_text = transcription.text.strip()
            if raw_text:
                # Исправляем пунктуацию через LLM
                text = await fix_punctuation(raw_text)

                # Сохраняем текст для последующих действий
                transcriptions[status_msg.message_id] = text

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка:**\n\n{text}",
                        reply_markup=keyboard
                    )
                else:
                    await safe_edit_message(
                        status_msg,
                        f"**Расшифровка (часть 1/{len(parts)}):**\n\n{parts[0]}"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                reply_markup=keyboard
                            )
                        else:
                            await safe_send_message(
                                message,
                                f"**Часть {i}/{len(parts)}:**\n\n{part}"
                            )
            else:
                await status_msg.edit_text("Не удалось распознать речь.")
        finally:
            if os.path.exists(video_path):
                os.unlink(video_path)
            if os.path.exists(audio_path):
                os.unlink(audio_path)

    except Exception as e:
        logger.exception("Error transcribing video note")
        await safe_edit_message(status_msg, "⚠️ Ошибка при расшифровке. Попробуйте через минуту.", parse_mode=None)


@dp.callback_query(F.data.startswith("summary:"))
async def handle_summary_callback(callback: CallbackQuery) -> None:
    """
    Обработчик нажатия кнопки 'Краткий пересказ'.

    callback.data имеет формат "summary:message_id"
    Извлекаем message_id, находим текст в словаре, делаем пересказ.
    """
    # Сразу отвечаем на callback чтобы убрать "часики" на кнопке
    await callback.answer("Создаю пересказ...")

    try:
        # Извлекаем message_id из callback_data
        message_id = int(callback.data.split(":")[1])

        # Получаем оригинальный текст
        text = transcriptions.get(message_id)
        if not text:
            await safe_send_message(callback, "Текст не найден. Возможно, бот был перезапущен.", parse_mode=None)
            return

        # Делаем пересказ через LLM
        summary = await summarize_text(text)

        # Разбиваем на части если пересказ длинный
        parts = split_text(summary)

        for i, part in enumerate(parts, start=1):
            if len(parts) == 1:
                await safe_send_message(
                    callback,
                    f"**Краткий пересказ:**\n\n{part}"
                )
            else:
                await safe_send_message(
                    callback,
                    f"**Краткий пересказ (часть {i}/{len(parts)}):**\n\n{part}"
                )

    except Exception as e:
        logger.exception("Error creating summary")
        await safe_send_message(callback, "⚠️ Ошибка при создании пересказа. Попробуйте через минуту.", parse_mode=None)


@dp.callback_query(F.data.startswith("translate:"))
async def handle_translate_callback(callback: CallbackQuery) -> None:
    """
    Обработчик нажатия кнопки перевода.

    callback.data имеет формат "translate:target_lang:message_id"
    target_lang - язык на который переводим ('ru' или 'en')
    """
    await callback.answer("Перевожу...")

    try:
        # Парсим callback_data: "translate:ru:12345" или "translate:en:12345"
        parts = callback.data.split(":")
        target_lang = parts[1]
        message_id = int(parts[2])

        # Получаем оригинальный текст
        text = transcriptions.get(message_id)
        if not text:
            await safe_send_message(callback, "Текст не найден. Возможно, бот был перезапущен.", parse_mode=None)
            return

        # Переводим через LLM
        translation = await translate_text(text, target_lang)

        # Разбиваем на части если перевод длинный
        parts = split_text(translation)
        lang_label = "русский" if target_lang == "ru" else "английский"

        for i, part in enumerate(parts, start=1):
            if len(parts) == 1:
                await safe_send_message(
                    callback,
                    f"**Перевод на {lang_label}:**\n\n{part}"
                )
            else:
                await safe_send_message(
                    callback,
                    f"**Перевод на {lang_label} (часть {i}/{len(parts)}):**\n\n{part}"
                )

    except Exception as e:
        logger.exception("Error translating")
        await safe_send_message(callback, "⚠️ Ошибка при переводе. Попробуйте через минуту.", parse_mode=None)


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> (IS_MEMBER | ADMINISTRATOR)))
async def handle_bot_added_to_group(event: ChatMemberUpdated) -> None:
    """Обработчик добавления бота в группу."""
    if event.chat.type in ("group", "supergroup"):
        await bot.send_message(
            event.chat.id,
            "Привет! Я бот для расшифровки голосовых и видеосообщений⚡️\n"
            "Я буду расшифровывать все голосовые и кружки в чате💛"
        )


# ==================== Админ-панель: маркетинговые ссылки ====================

@dp.message(Command("admin"))
async def handle_admin_command(message: Message) -> None:
    """Команда /admin для входа в админ-панель."""
    if not is_admin(message.from_user.id):
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Ссылки для блогеров", callback_data="admin_marketing_links"))

    await message.answer(
        "🔐 <b>Админ-панель</b>\n\n"
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "admin_back")
async def handle_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    """Возврат в главное меню админки."""
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Ссылки для блогеров", callback_data="admin_marketing_links"))

    await callback.message.edit_text(
        "🔐 <b>Админ-панель</b>\n\n"
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_marketing_links")
async def handle_admin_marketing_links(callback: CallbackQuery) -> None:
    """Список маркетинговых ссылок."""
    if not is_admin(callback.from_user.id):
        return

    links = await get_all_marketing_links()

    builder = InlineKeyboardBuilder()
    for link in links[:20]:
        builder.row(InlineKeyboardButton(
            text=f"🔗 {link.code} ({link.clicks_count}/{link.paid_count})",
            callback_data=f"admin_mlink_{link.id}",
        ))
    builder.row(InlineKeyboardButton(text="➕ Создать ссылку", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))

    await callback.message.edit_text(
        "🔗 <b>Ссылки для блогеров</b>\n\n"
        "Формат: название (переходы/оплаты)\n\n"
        "Выберите ссылку для просмотра статистики или создайте новую:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_mlink_create")
async def handle_mlink_create(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало создания ссылки."""
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_marketing_link_code)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_marketing_links"))

    bot_info = await callback.bot.get_me()

    await callback.message.edit_text(
        "🔗 <b>Создание ссылки для блогера</b>\n\n"
        "Введите название ссылки (только латиница, без пробелов):\n\n"
        f"Например: <code>mamix</code>\n"
        f"Диплинк будет: <code>t.me/{bot_info.username}?start=mamix</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.message(AdminStates.waiting_marketing_link_code)
async def handle_mlink_code_input(message: Message, state: FSMContext) -> None:
    """Обработка ввода кода ссылки."""
    if not is_admin(message.from_user.id):
        return

    code = message.text.strip().lower()

    if not re.match(r'^[a-z0-9_-]+$', code):
        await message.answer(
            "❌ Название может содержать только латинские буквы, цифры, _ и -\n\n"
            "Попробуйте ещё раз:"
        )
        return

    existing = await get_marketing_link_by_code(code)
    if existing:
        await message.answer(
            f"❌ Ссылка с названием <code>{code}</code> уже существует.\n\n"
            "Введите другое название:",
            parse_mode="HTML",
        )
        return

    link = await create_marketing_link(code=code, created_by=message.from_user.id)

    await state.clear()

    bot_info = await message.bot.get_me()
    deeplink = f"https://t.me/{bot_info.username}?start={code}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать ещё", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="📋 К списку", callback_data="admin_marketing_links"))
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_back"))

    await message.answer(
        f"✅ <b>Ссылка создана!</b>\n\n"
        f"📝 Название: <code>{link.code}</code>\n"
        f"🔗 Диплинк:\n<code>{deeplink}</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(
    F.data.startswith("admin_mlink_")
    & ~F.data.in_(["admin_mlink_create"])
    & ~F.data.startswith("admin_mlink_delete_")
    & ~F.data.startswith("admin_mlink_confirm_")
)
async def handle_mlink_detail(callback: CallbackQuery) -> None:
    """Детали ссылки."""
    if not is_admin(callback.from_user.id):
        return

    link_id = int(callback.data.replace("admin_mlink_", ""))

    link = await get_marketing_link_by_id(link_id)

    if not link:
        await callback.answer("Ссылка не найдена", show_alert=True)
        return

    bot_info = await callback.bot.get_me()
    deeplink = f"https://t.me/{bot_info.username}?start={link.code}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_mlink_delete_{link.id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_marketing_links"))

    await callback.message.edit_text(
        f"🔗 <b>Ссылка: {link.code}</b>\n\n"
        f"👥 Переходов: <b>{link.clicks_count}</b>\n"
        f"💰 Оплат: <b>{link.paid_count}</b>\n"
        f"📅 Создана: {link.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"🔗 Диплинк:\n<code>{deeplink}</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_mlink_delete_"))
async def handle_mlink_delete_confirm(callback: CallbackQuery) -> None:
    """Подтверждение удаления ссылки."""
    if not is_admin(callback.from_user.id):
        return

    link_id = int(callback.data.replace("admin_mlink_delete_", ""))

    link = await get_marketing_link_by_id(link_id)

    if not link:
        await callback.answer("Ссылка не найдена", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin_mlink_confirm_{link.id}"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_mlink_{link.id}"))

    await callback.message.edit_text(
        f"🗑 <b>Удаление ссылки</b>\n\n"
        f"Вы уверены, что хотите удалить ссылку <code>{link.code}</code>?\n\n"
        f"Статистика будет потеряна:\n"
        f"• Переходов: {link.clicks_count}\n"
        f"• Оплат: {link.paid_count}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_mlink_confirm_"))
async def handle_mlink_delete(callback: CallbackQuery) -> None:
    """Удаление ссылки."""
    if not is_admin(callback.from_user.id):
        return

    link_id = int(callback.data.replace("admin_mlink_confirm_", ""))

    deleted = await delete_marketing_link(link_id)

    if deleted:
        await callback.answer("✅ Ссылка удалена", show_alert=True)
    else:
        await callback.answer("❌ Ошибка удаления", show_alert=True)
        return

    links = await get_all_marketing_links()

    builder = InlineKeyboardBuilder()
    for link in links[:20]:
        builder.row(InlineKeyboardButton(
            text=f"🔗 {link.code} ({link.clicks_count}/{link.paid_count})",
            callback_data=f"admin_mlink_{link.id}",
        ))
    builder.row(InlineKeyboardButton(text="➕ Создать ссылку", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back"))

    await callback.message.edit_text(
        "🔗 <b>Ссылки для блогеров</b>\n\n"
        "Формат: название (переходы/оплаты)\n\n"
        "Выберите ссылку для просмотра статистики или создайте новую:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


# ==================== Команда /start ====================

@dp.message(CommandStart(deep_link=True))
async def handle_start_with_deeplink(message: Message) -> None:
    """Handle /start command with deep link parameter."""
    # В групповых чатах не отвечаем на /start
    if is_group_chat(message):
        return

    # Извлекаем параметр из /start <param>
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        ref_code = args[1].strip().lower()
        # Увеличиваем счетчик переходов если такая ссылка существует
        await increment_marketing_link_clicks(ref_code)

    # Проверяем все требования
    channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
    if not channel_ok or not bot_ok:
        await send_requirements_message(message)
        return

    # Все требования выполнены - показываем приветственное сообщение
    await message.answer(
        "Привет! Я бот для расшифровки голосовых сообщений.\n\n"
        "Отправьте мне голосовое сообщение или аудиофайл, и я расшифрую его в текст.\n\n"
        "Также я могу сделать краткий пересказ или перевести текст."
    )


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Handle /start command without parameters."""
    # В групповых чатах не отвечаем на /start (приветствие при добавлении)
    if is_group_chat(message):
        return

    # Проверяем все требования
    channel_ok, bot_ok = await check_all_requirements(message.from_user.id)
    if not channel_ok or not bot_ok:
        await send_requirements_message(message)
        return

    # Все требования выполнены - показываем приветственное сообщение
    await message.answer(
        "Привет! Я бот для расшифровки голосовых сообщений.\n\n"
        "Отправьте мне голосовое сообщение или аудиофайл, и я расшифрую его в текст.\n\n"
        "Также я могу сделать краткий пересказ или перевести текст."
    )


@dp.callback_query(F.data == "check_requirements")
async def handle_check_requirements(callback: CallbackQuery) -> None:
    channel_ok, bot_ok = await check_all_requirements(callback.from_user.id)
    if channel_ok and bot_ok:
        await callback.answer("✅ Все условия выполнены!", show_alert=True)
        await callback.message.answer("Спасибо! Теперь отправьте мне голосовое сообщение, и я расшифрую его в текст.")
    else:
        text = get_requirements_message(channel_ok, bot_ok)
        keyboard = get_requirements_keyboard()
        if not channel_ok and not bot_ok:
            await callback.answer("❌ Выполните оба шага!", show_alert=True)
        elif not channel_ok:
            await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
        else:
            await callback.answer("❌ Нажмите /start в боте ClevVPN!", show_alert=True)
        try:
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass


@dp.message()
async def handle_unknown(message: Message) -> None:
    """Handle all other messages."""
    # В групповых чатах не отвечаем на неизвестные сообщения
    if is_group_chat(message):
        return

    await message.answer(
        "Отправьте мне голосовое сообщение, аудио или видео — и я расшифрую его в текст."
    )


async def main() -> None:
    logger.info("Starting bot...")
    await init_db()
    logger.info("Database initialized")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
