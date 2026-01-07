import os
import tempfile
import logging
import re

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HTTP_PROXY = os.getenv("HTTP_PROXY")  # Прокси для Groq (опционально)

if not BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("BOT_TOKEN and GROQ_API_KEY must be set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Настраиваем Groq клиент с прокси если он указан
# httpx.Client используется Groq под капотом для HTTP запросов
if HTTP_PROXY:
    logger.info(f"Using HTTP proxy: {HTTP_PROXY}")
    http_client = httpx.Client(proxy=HTTP_PROXY)
    groq_client = Groq(api_key=GROQ_API_KEY, http_client=http_client)
else:
    groq_client = Groq(api_key=GROQ_API_KEY)

# Модель для пересказа и перевода
LLM_MODEL = "llama-3.3-70b-versatile"

# Хранилище расшифровок: {message_id: text}
# Нужно чтобы при нажатии кнопки знать какой текст обрабатывать
transcriptions: dict[int, str] = {}


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


def build_keyboard(text: str, message_id: int) -> InlineKeyboardMarkup:
    """
    Создает клавиатуру с кнопками под расшифровкой.

    Кнопка перевода зависит от языка:
    - Русский текст -> "Перевести на английский"
    - Английский текст -> "Перевести на русский"
    """
    lang = detect_language(text)

    # callback_data содержит действие и message_id через двоеточие
    # Формат: "action:message_id"
    translate_text = "Перевести на английский" if lang == 'ru' else "Перевести на русский"
    target_lang = "en" if lang == 'ru' else "ru"

    buttons = [
        [InlineKeyboardButton(text="Краткий пересказ", callback_data=f"summary:{message_id}")],
        [InlineKeyboardButton(text=translate_text, callback_data=f"translate:{target_lang}:{message_id}")]
    ]

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


@dp.message(F.content_type == "voice")
async def handle_voice(message: Message) -> None:
    """Handle voice messages and transcribe them using Whisper."""
    # Отправляем сообщение и сохраняем его, чтобы потом отредактировать
    status_msg = await message.answer("Расшифровываю...")

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

            text = transcription.text.strip()
            if text:
                # Сохраняем текст для последующих действий (пересказ/перевод)
                transcriptions[status_msg.message_id] = text

                # Создаем клавиатуру с кнопками
                keyboard = build_keyboard(text, status_msg.message_id)

                # Редактируем сообщение вместо отправки нового
                await status_msg.edit_text(
                    f"**Расшифровка:**\n\n{text}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await status_msg.edit_text("Не удалось распознать речь.")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error transcribing voice message")
        await status_msg.edit_text(f"Ошибка при расшифровке: {e}")


@dp.message(F.content_type == "audio")
async def handle_audio(message: Message) -> None:
    """Handle audio files."""
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

            text = transcription.text.strip()
            if text:
                # Сохраняем текст для последующих действий
                transcriptions[status_msg.message_id] = text

                # Создаем клавиатуру с кнопками
                keyboard = build_keyboard(text, status_msg.message_id)

                # Редактируем сообщение вместо отправки нового
                await status_msg.edit_text(
                    f"**Расшифровка:**\n\n{text}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                await status_msg.edit_text("Не удалось распознать речь.")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error transcribing audio")
        await status_msg.edit_text(f"Ошибка при расшифровке: {e}")


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
            await callback.message.answer("Текст не найден. Возможно, бот был перезапущен.")
            return

        # Делаем пересказ через LLM
        summary = await summarize_text(text)

        # Отправляем пересказ отдельным сообщением (не редактируем оригинал)
        await callback.message.answer(
            f"**Краткий пересказ:**\n\n{summary}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.exception("Error creating summary")
        await callback.message.answer(f"Ошибка при создании пересказа: {e}")


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
            await callback.message.answer("Текст не найден. Возможно, бот был перезапущен.")
            return

        # Переводим через LLM
        translation = await translate_text(text, target_lang)

        # Отправляем перевод отдельным сообщением
        lang_label = "русский" if target_lang == "ru" else "английский"
        await callback.message.answer(
            f"**Перевод на {lang_label}:**\n\n{translation}",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.exception("Error translating")
        await callback.message.answer(f"Ошибка при переводе: {e}")


@dp.message(F.text == "/start")
async def handle_start(message: Message) -> None:
    """Handle /start command."""
    await message.answer(
        "Привет! Отправь мне голосовое сообщение, и я расшифрую его в текст."
    )


async def main() -> None:
    logger.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
