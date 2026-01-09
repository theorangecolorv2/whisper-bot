import os
import tempfile
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ClevVPN")

if not BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("BOT_TOKEN and GROQ_API_KEY must be set")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

# Модель для пересказа и перевода
LLM_MODEL = "llama-3.3-70b-versatile"

# Хранилище расшифровок: {message_id: text}
# Нужно чтобы при нажатии кнопки знать какой текст обрабатывать
transcriptions: dict[int, str] = {}


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


async def check_subscription(user_id: int) -> bool:
    """
    Проверяет, подписан ли пользователь на канал.
    Возвращает True если подписан, False если нет.
    """
    if not CHANNEL_ID:
        return True  # Если канал не настроен, пропускаем проверку

    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["creator", "administrator", "member"]
    except Exception as e:
        logger.exception("Error checking subscription")
        return False


async def send_subscription_required(message: Message) -> None:
    """Отправляет сообщение о необходимости подписки."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Подписаться на канал", url="https://t.me/ClevVPN")],
        [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_sub")]
    ])
    await message.answer(
        "❌ Для использования бота необходимо подписаться на [канал](https://t.me/ClevVPN)\n\n"
        "После подписки нажмите кнопку проверить:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


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


@dp.message(F.content_type == "voice")
async def handle_voice(message: Message) -> None:
    """Handle voice messages and transcribe them using Whisper."""
    # Проверяем подписку на канал
    if not await check_subscription(message.from_user.id):
        await send_subscription_required(message)
        return

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

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    # Текст умещается в одно сообщение
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await status_msg.edit_text(
                        f"**Расшифровка:**\n\n{text}",
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                else:
                    # Текст слишком длинный - отправляем частями
                    await status_msg.edit_text(
                        f"**Расшифровка (часть 1/{len(parts)}):**\n\n{parts[0]}",
                        parse_mode="Markdown"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            # Последняя часть - с кнопками
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await message.answer(
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                parse_mode="Markdown",
                                reply_markup=keyboard
                            )
                        else:
                            await message.answer(
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                parse_mode="Markdown"
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
    # Проверяем подписку на канал
    if not await check_subscription(message.from_user.id):
        await send_subscription_required(message)
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

            text = transcription.text.strip()
            if text:
                # Сохраняем текст для последующих действий
                transcriptions[status_msg.message_id] = text

                # Разбиваем текст на части если он слишком длинный
                parts = split_text(text)

                if len(parts) == 1:
                    # Текст умещается в одно сообщение
                    keyboard = build_keyboard(text, status_msg.message_id)
                    await status_msg.edit_text(
                        f"**Расшифровка:**\n\n{text}",
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                else:
                    # Текст слишком длинный - отправляем частями
                    await status_msg.edit_text(
                        f"**Расшифровка (часть 1/{len(parts)}):**\n\n{parts[0]}",
                        parse_mode="Markdown"
                    )
                    for i, part in enumerate(parts[1:], start=2):
                        if i == len(parts):
                            # Последняя часть - с кнопками
                            keyboard = build_keyboard(text, status_msg.message_id)
                            await message.answer(
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                parse_mode="Markdown",
                                reply_markup=keyboard
                            )
                        else:
                            await message.answer(
                                f"**Часть {i}/{len(parts)}:**\n\n{part}",
                                parse_mode="Markdown"
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

        # Разбиваем на части если пересказ длинный
        parts = split_text(summary)

        for i, part in enumerate(parts, start=1):
            if len(parts) == 1:
                await callback.message.answer(
                    f"**Краткий пересказ:**\n\n{part}",
                    parse_mode="Markdown"
                )
            else:
                await callback.message.answer(
                    f"**Краткий пересказ (часть {i}/{len(parts)}):**\n\n{part}",
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

        # Разбиваем на части если перевод длинный
        parts = split_text(translation)
        lang_label = "русский" if target_lang == "ru" else "английский"

        for i, part in enumerate(parts, start=1):
            if len(parts) == 1:
                await callback.message.answer(
                    f"**Перевод на {lang_label}:**\n\n{part}",
                    parse_mode="Markdown"
                )
            else:
                await callback.message.answer(
                    f"**Перевод на {lang_label} (часть {i}/{len(parts)}):**\n\n{part}",
                    parse_mode="Markdown"
                )

    except Exception as e:
        logger.exception("Error translating")
        await callback.message.answer(f"Ошибка при переводе: {e}")


@dp.message(F.text == "/start")
async def handle_start(message: Message) -> None:
    """Handle /start command."""
    # Проверяем подписку
    if await check_subscription(message.from_user.id):
        # Пользователь подписан - показываем приветственное сообщение
        await message.answer(
            "Привет! Я бот для расшифровки голосовых сообщений.\n\n"
            "Отправьте мне голосовое сообщение или аудиофайл, и я расшифрую его в текст.\n\n"
            "Также я могу сделать краткий пересказ или перевести текст."
        )
    else:
        # Пользователь не подписан - показываем сообщение о необходимости подписки
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Канал", url="https://t.me/ClevVPN")],
            [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_sub")]
        ])
        await message.answer(
            "Для использования бота необходимо подписаться на [канал](https://t.me/ClevVPN)\n\n"
            "После подписки нажмите кнопку проверить:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )


@dp.callback_query(F.data == "check_sub")
async def handle_check_sub(callback: CallbackQuery) -> None:
    """Handle subscription check button."""
    if not CHANNEL_ID:
        await callback.answer("Канал не настроен", show_alert=True)
        return

    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=callback.from_user.id)
        if member.status in ["creator", "administrator", "member"]:
            await callback.answer("✅ Подписка подтверждена!", show_alert=True)
            await callback.message.answer(
                "Спасибо за подписку! Теперь отправьте мне голосовое сообщение, и я расшифрую его в текст."
            )
        else:
            await callback.answer("❌ Вы не подписаны на канал!", show_alert=True)
    except Exception as e:
        logger.exception("Error checking subscription")
        await callback.answer("Ошибка проверки подписки", show_alert=True)


@dp.message()
async def handle_unknown(message: Message) -> None:
    """Handle all other messages."""
    await message.answer(
        "Я понимаю только голосовые сообщения и команду /start. Пришлите мне голосовое сообщение и я его расшифрую."
    )


async def main() -> None:
    logger.info("Starting bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
