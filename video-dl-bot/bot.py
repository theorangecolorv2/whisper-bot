import os
import asyncio
import logging
import time
import subprocess
import json
import re
from datetime import datetime
from dataclasses import dataclass
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import aiosqlite
import yt_dlp
from yt_dlp.utils import sanitize_filename

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [586107799, 762967142, 6682411163]

CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_URL = "https://t.me/ClevVPN"
PARTNER_BOT_URL = "https://t.me/ClevVPN_bot"
CLEVVPN_API_URL = os.getenv("CLEVVPN_API_URL", "http://89.111.143.90:8080")

# Docker paths
BASE_DIR = "/app/data"
DOWNLOAD_PATH = os.path.join(BASE_DIR, "downloads")
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
DB_PATH = os.path.join(BASE_DIR, "bot.db")

if not os.path.exists(DOWNLOAD_PATH):
    os.makedirs(DOWNLOAD_PATH)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Local Bot API Server (up to 2GB file limit)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

BOT_API_URL = os.getenv("BOT_API_URL", "http://telegram-bot-api:8081")
local_server = TelegramAPIServer.from_base(BOT_API_URL)
session = AiohttpSession(api=local_server)
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()


@dataclass
class MarketingLink:
    id: int
    code: str
    clicks_count: int
    paid_count: int
    created_at: datetime
    created_by: int


@dataclass
class User:
    id: int
    telegram_id: int
    username: str | None
    marketing_link_id: int | None
    created_at: datetime


class AdminStates(StatesGroup):
    waiting_marketing_link_code = State()
    waiting_broadcast_message = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def init_db():
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                marketing_link_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (marketing_link_id) REFERENCES marketing_links(id) ON DELETE SET NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def get_all_marketing_links() -> list[MarketingLink]:
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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE marketing_links SET clicks_count = clicks_count + 1 WHERE code = ?",
            (code,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_marketing_link(link_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM marketing_links WHERE id = ?", (link_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


# ==================== Функции для работы с пользователями ====================


async def save_user(telegram_id: int, username: str | None, marketing_link_code: str | None = None) -> None:
    """Сохраняет пользователя в БД. Если уже существует — обновляет username."""
    marketing_link_id = None
    if marketing_link_code:
        link = await get_marketing_link_by_code(marketing_link_code)
        if link:
            marketing_link_id = link.id

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, marketing_link_id) VALUES (?, ?, ?)",
            (telegram_id, username, marketing_link_id),
        )
        await db.execute(
            "UPDATE users SET username = ? WHERE telegram_id = ?",
            (username, telegram_id),
        )
        if marketing_link_id:
            await db.execute(
                "UPDATE users SET marketing_link_id = ? WHERE telegram_id = ? AND marketing_link_id IS NULL",
                (marketing_link_id, telegram_id),
            )
        await db.execute(
            "INSERT OR IGNORE INTO broadcast_users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )
        await db.execute(
            "UPDATE broadcast_users SET username = ? WHERE telegram_id = ?",
            (username, telegram_id),
        )
        await db.commit()


async def get_all_broadcast_users() -> list[tuple[int, str | None]]:
    """Returns all broadcast users as (telegram_id, username) tuples."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT telegram_id, username FROM broadcast_users ORDER BY id")
        return await cursor.fetchall()


async def get_broadcast_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM broadcast_users")
        row = await cursor.fetchone()
        return row[0]


async def get_all_users() -> list[User]:
    """Возвращает всех пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [
            User(
                id=row["id"],
                telegram_id=row["telegram_id"],
                username=row["username"],
                marketing_link_id=row["marketing_link_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


async def get_users_count() -> int:
    """Возвращает количество пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0]


async def check_subscription(user_id: int) -> bool:
    try:
        logger.info(f"Checking subscription: user_id={user_id}, channel_id={CHANNEL_ID}")
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        logger.info(f"User status: {member.status}")

        if member.status in ["member", "administrator", "creator"]:
            logger.info(f"User {user_id} is subscribed")
            return True
        else:
            logger.info(f"User {user_id} is NOT subscribed (status: {member.status})")
            return False
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
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


async def show_subscription_requirement(event, channel_ok: bool = False, bot_ok: bool = False):
    step1 = "✅" if channel_ok else "❌"
    step2 = "✅" if bot_ok else "❌"
    text = (
        "Чтобы бот всегда оставался бесплатным и работал стабильно, "
        "необходимо выполнить два пункта:\n\n"
        f"{step1} Шаг 1: Подпишитесь на наш <a href='{CHANNEL_URL}'>канал</a>\n\n"
        f"{step2} Шаг 2: Зайдите в <a href='{PARTNER_BOT_URL}'>бота</a> и нажмите «Старт»\n\n"
        "И всё — вы в деле! Спасибо, что вы с нами! ❤️"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="🤖 Бот", url=PARTNER_BOT_URL)],
        [InlineKeyboardButton(text="🔄 Проверить", callback_data="check_sub")]
    ])

    try:
        if isinstance(event, types.Message):
            await event.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=False)
        elif isinstance(event, types.CallbackQuery):
            try:
                await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=False)
            except:
                await event.message.answer(text, reply_markup=kb, parse_mode="HTML", disable_web_page_preview=False)
    except Exception as e:
        logger.error(f"Error showing subscription requirement: {e}")


async def send_welcome_message(chat_id: int):
    welcome_text = (
        "Привет! Это бот для скачивания видео/аудио из YouTube, TikTok и Instagram💛\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Зайди в одну из социальных сетей.\n"
        "2. Выбери интересное видео.\n"
        "3. Нажми кнопку Скопировать ссылку.\n"
        "4. Отправь ссылку боту и получи скаченный файл!\n\n"
        "<b>Поддерживаемые платформы:</b>\n"
        "✅ YouTube\n"
        "✅ TikTok\n"
        "✅ Instagram\n\n"
        "Доступ к медиа без блокировок, VPN и других сложностей.\n\n"
        "@clev_video_bot"
    )

    await bot.send_message(chat_id, welcome_text, parse_mode="HTML")


def get_video_metas(file_path):
    """Get video metadata: width, height, duration"""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',  # Select only video stream
            '-show_entries', 'stream=width,height,duration,codec_name',
            '-of', 'json', file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        streams = data.get('streams', [])

        if not streams:
            logger.warning(f"No video stream found in {file_path}")
            return None, None, None

        stream = streams[0]
        width = stream.get('width')
        height = stream.get('height')

        # Get duration from video stream or container
        duration = stream.get('duration')
        if not duration:
            # Try to get duration from format/container
            cmd_format = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'json', file_path
            ]
            result_format = subprocess.run(cmd_format, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            format_data = json.loads(result_format.stdout)
            duration = format_data.get('format', {}).get('duration')

        return width, height, int(float(duration or 0))
    except Exception as e:
        logging.error(f"Error getting metadata: {e}")
        return None, None, None


def has_video_stream(file_path):
    """Check if file contains actual video stream (not just audio)"""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_type,codec_name,width,height',
            '-of', 'json', file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        streams = data.get('streams', [])

        if not streams:
            logger.warning(f"No video stream in file: {file_path}")
            return False

        stream = streams[0]
        # Check that it's actually video with dimensions
        width = stream.get('width', 0)
        height = stream.get('height', 0)
        codec_type = stream.get('codec_type', '')

        if codec_type != 'video' or not width or not height:
            logger.warning(f"Invalid video stream: codec_type={codec_type}, {width}x{height}")
            return False

        logger.info(f"Valid video stream found: {stream.get('codec_name')} {width}x{height}")
        return True
    except Exception as e:
        logger.error(f"Error checking video stream: {e}")
        return False


def generate_thumbnail(video_path):
    """Generate thumbnail for video at 1 second mark"""
    try:
        thumb_path = video_path.rsplit('.', 1)[0] + '_thumb.jpg'
        cmd = [
            'ffmpeg', '-i', video_path, '-ss', '00:00:01.000',
            '-vframes', '1', '-vf', 'scale=320:-1', thumb_path, '-y'
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return thumb_path if os.path.exists(thumb_path) else None
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
        return None


def get_video_codec(file_path):
    """Get video codec name from file"""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip().lower()
    except Exception as e:
        logger.error(f"Error getting video codec: {e}")
        return None


def convert_to_telegram_format(input_path, force_convert=False):
    """Convert video to Telegram-compatible format (h264 + aac)

    Args:
        input_path: Path to input video file
        force_convert: If True, always re-encode. If False, only convert if codec is not h264.
    """
    try:
        # Check current codec
        current_codec = get_video_codec(input_path)
        logger.info(f"Current video codec: {current_codec}")

        # If already h264/avc and not forcing, skip conversion
        if not force_convert and current_codec in ('h264', 'avc1', 'avc'):
            logger.info(f"Video already in h264 format, skipping conversion")
            return True

        output_path = input_path.rsplit('.', 1)[0] + '_converted.mp4'

        # Use different settings based on source codec
        # For VP9/AV1 we need full re-encode
        cmd = [
            'ffmpeg', '-i', input_path,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',  # Ensure compatible pixel format
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            '-y', output_path
        ]

        logger.info(f"Converting video from {current_codec} to h264...")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode == 0 and os.path.exists(output_path):
            # Verify the converted file has video stream
            if has_video_stream(output_path):
                os.remove(input_path)
                os.rename(output_path, input_path)
                logger.info(f"Successfully converted video to h264: {input_path}")
                return True
            else:
                logger.error(f"Converted file has no video stream!")
                if os.path.exists(output_path):
                    os.remove(output_path)
                return False
        else:
            logger.error(f"FFmpeg conversion failed: {result.stderr[:500] if result.stderr else 'unknown error'}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
    except Exception as e:
        logger.error(f"Error converting video: {e}")
        return False


def run_yt_dlp(url, mode='video', quality='1080'):
    timestamp = int(time.time())

    if mode == 'audio':
        outtmpl = os.path.join(DOWNLOAD_PATH, f'%(title)s_{timestamp}.%(ext)s')
    else:
        outtmpl = os.path.join(DOWNLOAD_PATH, f'%(id)s_{timestamp}.%(ext)s')

    ydl_opts = {
        'outtmpl': outtmpl,
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }

    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH

    if "instagram.com" in url:
        ydl_opts['cookiefile'] = COOKIES_PATH if os.path.exists(COOKIES_PATH) else None
        ydl_opts['extract_flat'] = False

    # Enable browser impersonation for TikTok
    if "tiktok.com" in url:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget
            ydl_opts['impersonate'] = ImpersonateTarget('chrome')
        except ImportError:
            logger.warning("ImpersonateTarget not available, skipping impersonation")

    if mode == 'video':
        if "tiktok.com" in url:
            # TikTok: prefer single stream with video, fallback to best combined
            ydl_opts['format'] = 'best[vcodec!=none][acodec!=none]/best[vcodec!=none]/best'
        elif "instagram.com" in url:
            # Instagram: prefer mp4 with video codec
            ydl_opts['format'] = 'best[ext=mp4][vcodec!=none]/best[vcodec!=none]/best'
        else:
            # YouTube: prefer h264 codec (avc1) for better compatibility
            # First try mp4 with h264, then any format with video, then best overall
            ydl_opts['format'] = (
                f'bestvideo[height<={quality}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={quality}][vcodec^=avc1]+bestaudio/'
                f'bestvideo[height<={quality}]+bestaudio/'
                f'best[height<={quality}][vcodec!=none]/'
                f'best[vcodec!=none]'
            )
        ydl_opts['merge_output_format'] = 'mp4'
        # Ensure ffmpeg properly merges video and audio
        ydl_opts['postprocessor_args'] = {
            'merger': ['-c:v', 'copy', '-c:a', 'aac', '-strict', 'experimental']
        }
    else:
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

        if mode == 'audio':
            base, _ = os.path.splitext(filename)
            filename = base + ".mp3"
        elif mode == 'video':
            # yt-dlp may merge into .mp4 with a different name than prepare_filename returns
            # Check if the expected file exists, otherwise look for the merged .mp4
            if not os.path.exists(filename):
                base, _ = os.path.splitext(filename)
                mp4_path = base + ".mp4"
                if os.path.exists(mp4_path):
                    filename = mp4_path
                    logger.info(f"Using merged file: {filename}")

        return filename, info.get('title', 'Video')


def get_platform(url: str) -> str:
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "YouTube"
    elif "tiktok.com" in url_lower:
        return "TikTok"
    elif "instagram.com" in url_lower:
        return "Instagram"
    else:
        return "Unknown"


def is_valid_url(url: str) -> bool:
    if not re.match(r'^https?://', url, re.IGNORECASE):
        return False

    platform = get_platform(url)
    if platform == "Unknown":
        return False

    return True


class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        if not isinstance(event, (types.Message, types.CallbackQuery)):
            return await handler(event, data)

        user_id = event.from_user.id

        if isinstance(event, types.Message):
            chat_type = event.chat.type
        else:
            chat_type = event.message.chat.type

        if chat_type != 'private':
            return await handler(event, data)

        if isinstance(event, types.Message):
            text = event.text or ""

            if text.startswith('/start'):
                return await handler(event, data)

            if text.startswith('/admin'):
                return await handler(event, data)

            if "tiktok.com" in text:
                return await handler(event, data)

            channel_ok = await check_subscription(user_id)
            bot_ok = await check_clevvpn_bot_started(user_id)
            if channel_ok and bot_ok:
                return await handler(event, data)
            else:
                await show_subscription_requirement(event, channel_ok=channel_ok, bot_ok=bot_ok)
                return

        elif isinstance(event, types.CallbackQuery):
            if event.data == "check_sub":
                return await handler(event, data)

            if event.data and event.data.startswith("admin"):
                return await handler(event, data)

            channel_ok = await check_subscription(user_id)
            bot_ok = await check_clevvpn_bot_started(user_id)
            if channel_ok and bot_ok:
                return await handler(event, data)
            else:
                await show_subscription_requirement(event, channel_ok=channel_ok, bot_ok=bot_ok)
                return


@dp.message(Command("admin"))
async def handle_admin_command(message: types.Message) -> None:
    if not is_admin(message.from_user.id):
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Ссылки для блогеров", callback_data="admin_marketing_links"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))

    await message.answer(
        "🔐 <b>Админ-панель</b>\n\n"
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "admin_back")
async def handle_admin_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return

    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Ссылки для блогеров", callback_data="admin_marketing_links"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))

    await callback.message.edit_text(
        "🔐 <b>Админ-панель</b>\n\n"
        "Выберите раздел:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_marketing_links")
async def handle_admin_marketing_links(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return

    links = await get_all_marketing_links()

    builder = InlineKeyboardBuilder()
    for link in links[:20]:
        builder.row(InlineKeyboardButton(
            text=f"{link.code} ({link.clicks_count}/{link.paid_count})",
            callback_data=f"admin_mlink_{link.id}",
        ))
    builder.row(InlineKeyboardButton(text="Создать ссылку", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin_back"))

    await callback.message.edit_text(
        "<b>Ссылки для блогеров</b>\n\n"
        "Формат: название (переходы/оплаты)\n\n"
        "Выберите ссылку для просмотра статистики или создайте новую:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_mlink_create")
async def handle_mlink_create(callback: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_marketing_link_code)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Отмена", callback_data="admin_marketing_links"))

    bot_info = await callback.bot.get_me()

    await callback.message.edit_text(
        "<b>Создание ссылки для блогера</b>\n\n"
        "Введите название ссылки (только латиница, без пробелов):\n\n"
        f"Например: <code>mamix</code>\n"
        f"Диплинк будет: <code>t.me/{bot_info.username}?start=mamix</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.message(AdminStates.waiting_marketing_link_code)
async def handle_mlink_code_input(message: types.Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return

    code = message.text.strip().lower()

    if not re.match(r'^[a-z0-9_-]+$', code):
        await message.answer(
            "Название может содержать только латинские буквы, цифры, _ и -\n\n"
            "Попробуйте ещё раз:"
        )
        return

    existing = await get_marketing_link_by_code(code)
    if existing:
        await message.answer(
            f"Ссылка с названием <code>{code}</code> уже существует.\n\n"
            "Введите другое название:",
            parse_mode="HTML",
        )
        return

    link = await create_marketing_link(code=code, created_by=message.from_user.id)

    await state.clear()

    bot_info = await message.bot.get_me()
    deeplink = f"https://t.me/{bot_info.username}?start={code}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Создать ещё", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="К списку", callback_data="admin_marketing_links"))
    builder.row(InlineKeyboardButton(text="В меню", callback_data="admin_back"))

    await message.answer(
        f"<b>Ссылка создана!</b>\n\n"
        f"Название: <code>{link.code}</code>\n"
        f"Диплинк:\n<code>{deeplink}</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(
    F.data.startswith("admin_mlink_")
    & ~F.data.in_(["admin_mlink_create"])
    & ~F.data.startswith("admin_mlink_delete_")
    & ~F.data.startswith("admin_mlink_confirm_")
)
async def handle_mlink_detail(callback: types.CallbackQuery) -> None:
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
    builder.row(InlineKeyboardButton(text="Удалить", callback_data=f"admin_mlink_delete_{link.id}"))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin_marketing_links"))

    await callback.message.edit_text(
        f"<b>Ссылка: {link.code}</b>\n\n"
        f"Переходов: <b>{link.clicks_count}</b>\n"
        f"Оплат: <b>{link.paid_count}</b>\n"
        f"Создана: {link.created_at.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"Диплинк:\n<code>{deeplink}</code>",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_mlink_delete_"))
async def handle_mlink_delete_confirm(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return

    link_id = int(callback.data.replace("admin_mlink_delete_", ""))

    link = await get_marketing_link_by_id(link_id)

    if not link:
        await callback.answer("Ссылка не найдена", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Да, удалить", callback_data=f"admin_mlink_confirm_{link.id}"))
    builder.row(InlineKeyboardButton(text="Отмена", callback_data=f"admin_mlink_{link.id}"))

    await callback.message.edit_text(
        f"<b>Удаление ссылки</b>\n\n"
        f"Вы уверены, что хотите удалить ссылку <code>{link.code}</code>?\n\n"
        f"Статистика будет потеряна:\n"
        f"Переходов: {link.clicks_count}\n"
        f"Оплат: {link.paid_count}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_mlink_confirm_"))
async def handle_mlink_delete(callback: types.CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return

    link_id = int(callback.data.replace("admin_mlink_confirm_", ""))

    deleted = await delete_marketing_link(link_id)

    if deleted:
        await callback.answer("Ссылка удалена", show_alert=True)
    else:
        await callback.answer("Ошибка удаления", show_alert=True)
        return

    links = await get_all_marketing_links()

    builder = InlineKeyboardBuilder()
    for link in links[:20]:
        builder.row(InlineKeyboardButton(
            text=f"{link.code} ({link.clicks_count}/{link.paid_count})",
            callback_data=f"admin_mlink_{link.id}",
        ))
    builder.row(InlineKeyboardButton(text="Создать ссылку", callback_data="admin_mlink_create"))
    builder.row(InlineKeyboardButton(text="Назад", callback_data="admin_back"))

    await callback.message.edit_text(
        "<b>Ссылки для блогеров</b>\n\n"
        "Формат: название (переходы/оплаты)\n\n"
        "Выберите ссылку для просмотра статистики или создайте новую:",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


# ==================== Админ-панель: рассылка ====================


@dp.callback_query(F.data == "admin_broadcast")
async def handle_admin_broadcast(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Начало рассылки."""
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_broadcast_message)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"))

    users_count = await get_broadcast_users_count()

    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        f"👥 Пользователей в базе: {users_count}\n\n"
        "Отправьте сообщение для рассылки.\n\n"
        "Поддерживается:\n"
        "• Текст (с HTML-форматированием)\n"
        "• Фото с подписью",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@dp.message(AdminStates.waiting_broadcast_message, F.photo)
async def handle_broadcast_photo(message: types.Message, state: FSMContext) -> None:
    """Получение фото для рассылки."""
    if not is_admin(message.from_user.id):
        return

    photo_id = message.photo[-1].file_id
    caption = message.caption or ""

    await state.update_data(photo_id=photo_id, caption=caption, text=None)
    await state.set_state(None)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧪 Тест (админам)", callback_data="broadcast_test"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"))

    await message.answer_photo(
        photo=photo_id,
        caption=f"{caption}\n\n<i>👆 Предпросмотр сообщения</i>" if caption else "<i>👆 Предпросмотр сообщения</i>",
        parse_mode="HTML",
    )
    await message.answer(
        "Выберите действие:",
        reply_markup=builder.as_markup(),
    )


@dp.message(AdminStates.waiting_broadcast_message, F.text)
async def handle_broadcast_text(message: types.Message, state: FSMContext) -> None:
    """Получение текста для рассылки."""
    if not is_admin(message.from_user.id):
        return

    text = message.text

    await state.update_data(text=text, photo_id=None, caption=None)
    await state.set_state(None)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧪 Тест (админам)", callback_data="broadcast_test"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"))

    await message.answer(
        f"{text}\n\n<i>👆 Предпросмотр сообщения</i>",
        parse_mode="HTML",
    )
    await message.answer(
        "Выберите действие:",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "broadcast_test")
async def handle_broadcast_test(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Тестовая отправка админам."""
    if not is_admin(callback.from_user.id):
        return

    data = await state.get_data()
    await callback.answer("Отправляю тест админам...")

    sent = 0
    failed = 0

    for admin_id in ADMIN_IDS:
        try:
            if data.get("photo_id"):
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=data["photo_id"],
                    caption=data.get("caption") or None,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=admin_id,
                    text=data.get("text", ""),
                    parse_mode="HTML",
                )
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send test to admin {admin_id}: {e}")
            failed += 1
        await asyncio.sleep(0.1)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📢 Отправить ВСЕМ", callback_data="broadcast_all"))
    builder.row(InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_broadcast"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back"))

    await callback.message.edit_text(
        f"🧪 <b>Тестовая рассылка завершена</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}\n\n"
        f"Теперь можете отправить всем пользователям.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "broadcast_all")
async def handle_broadcast_all(callback: types.CallbackQuery, state: FSMContext) -> None:
    """Отправка рассылки всем пользователям."""
    if not is_admin(callback.from_user.id):
        return

    data = await state.get_data()

    if not data.get("text") and not data.get("photo_id"):
        await callback.answer("Сначала создайте сообщение", show_alert=True)
        return

    await callback.message.edit_text(
        "📢 <b>Рассылка запущена...</b>\n\nПодождите, это может занять время.",
        parse_mode="HTML",
    )
    await callback.answer()

    broadcast_list = await get_all_broadcast_users()

    total = len(broadcast_list)
    sent = 0
    failed = 0

    for i, (telegram_id, username) in enumerate(broadcast_list):
        try:
            if data.get("photo_id"):
                await bot.send_photo(
                    chat_id=telegram_id,
                    photo=data["photo_id"],
                    caption=data.get("caption") or None,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=telegram_id,
                    text=data.get("text", ""),
                    parse_mode="HTML",
                )
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send to user {telegram_id}: {e}")
            failed += 1

        if (i + 1) % 30 == 0:
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(0.05)

    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_back"))

    await callback.message.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего: {total}\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


# ==================== Команда /start ====================


@dp.message(CommandStart(deep_link=True))
async def cmd_start_with_deeplink(message: types.Message):
    ref_code = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        ref_code = args[1].strip().lower()
        await increment_marketing_link_clicks(ref_code)

    # Сохраняем пользователя в БД
    await save_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        marketing_link_code=ref_code,
    )

    channel_ok = await check_subscription(message.from_user.id)
    bot_ok = await check_clevvpn_bot_started(message.from_user.id)

    if not channel_ok or not bot_ok:
        await show_subscription_requirement(message, channel_ok=channel_ok, bot_ok=bot_ok)
    else:
        await send_welcome_message(message.chat.id)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Сохраняем пользователя в БД
    await save_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )

    channel_ok = await check_subscription(message.from_user.id)
    bot_ok = await check_clevvpn_bot_started(message.from_user.id)

    if not channel_ok or not bot_ok:
        await show_subscription_requirement(message, channel_ok=channel_ok, bot_ok=bot_ok)
    else:
        await send_welcome_message(message.chat.id)


@dp.callback_query(F.data == "check_sub")
async def check_sub_btn(callback: types.CallbackQuery):
    try:
        channel_ok = await check_subscription(callback.from_user.id)
        bot_ok = await check_clevvpn_bot_started(callback.from_user.id)
        if channel_ok and bot_ok:
            await callback.answer("✅ Все условия выполнены!", show_alert=False)

            try:
                await callback.message.delete()
            except:
                pass

            await send_welcome_message(callback.message.chat.id)
        else:
            if not channel_ok and not bot_ok:
                await callback.answer("❌ Выполните оба шага!", show_alert=True)
            elif not channel_ok:
                await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
            else:
                await callback.answer("❌ Нажмите /start в боте ClevVPN!", show_alert=True)
            await show_subscription_requirement(callback, channel_ok=channel_ok, bot_ok=bot_ok)
    except Exception as e:
        logger.error(f"Error checking subscription by button: {e}")
        await callback.answer("Ошибка проверки. Попробуйте позже.", show_alert=True)


@dp.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()

    if not is_valid_url(url):
        await message.answer("Это не похоже на ссылку YouTube, TikTok или Instagram.\n\nПожалуйста, отправьте корректную ссылку.")
        return

    platform = get_platform(url)

    if platform == "YouTube":
        try:
            ydl_opts = {'quiet': True, 'nocheckcertificate': True}
            if os.path.exists(COOKIES_PATH):
                ydl_opts['cookiefile'] = COOKIES_PATH

            loop = asyncio.get_event_loop()
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="1080p", callback_data=f"dl|video|1080|{url[:50]}"),
                 InlineKeyboardButton(text="720p", callback_data=f"dl|video|720|{url[:50]}")],
                [InlineKeyboardButton(text="MP3 (Аудио)", callback_data=f"dl|audio|0|{url[:50]}")]
            ])
            await message.answer(
                f"<b>{info.get('title', 'Video')[:100]}</b>\n"
                f"<b>Платформа:</b> {platform}\n\n"
                "Выберите качество:",
                reply_markup=kb,
                parse_mode="HTML",
                reply_to_message_id=message.message_id
            )
        except Exception as e:
            logger.error(f"Error getting YouTube data: {e}")
            await message.answer(f"Ошибка получения данных: {str(e)[:100]}")

    elif platform == "TikTok":
        status_msg = await message.answer("Скачиваю с TikTok...")
        await process_download(message, url, 'video', '1080', platform, status_msg)

    elif platform == "Instagram":
        try:
            status_msg = await message.answer("Скачиваю с Instagram...")
            await process_download(message, url, 'video', 'best', platform, status_msg)
        except Exception as e:
            logger.error(f"Error processing Instagram: {e}")
            await message.answer(f"Ошибка обработки Instagram: {str(e)[:100]}")

    else:
        await message.answer("Я поддерживаю только ссылки на YouTube, TikTok и Instagram.")


@dp.callback_query(F.data.startswith("dl|"))
async def callback_dl(callback: types.CallbackQuery):
    try:
        if not callback.message.reply_to_message:
            return await callback.answer("Ошибка: старое сообщение", show_alert=True)

        url = callback.message.reply_to_message.text
        platform = get_platform(url)

        if platform != "TikTok":
            channel_ok = await check_subscription(callback.from_user.id)
            bot_ok = await check_clevvpn_bot_started(callback.from_user.id)
            if not channel_ok or not bot_ok:
                await callback.answer("Требуется выполнить условия!", show_alert=True)
                await show_subscription_requirement(callback, channel_ok=channel_ok, bot_ok=bot_ok)
                return

        parts = callback.data.split("|")
        if len(parts) < 4:
            mode, quality = parts[1], parts[2]
        else:
            mode, quality = parts[1], parts[2]

        await callback.message.edit_text("Скачиваю с YouTube...")
        await process_download(callback.message, url, mode, quality, platform, callback.message)

    except Exception as e:
        logger.error(f"Error in callback_dl: {e}")
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)


async def process_download(message: types.Message, url: str, mode: str, quality: str, platform: str, status_msg: types.Message):
    file_path = None
    thumb_path = None
    try:
        loop = asyncio.get_event_loop()
        file_path, title = await loop.run_in_executor(None, run_yt_dlp, url, mode, quality)

        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await status_msg.edit_text("Ошибка: Файл не скачался (пустой). Попробуйте обновить cookies.txt")
            return

        if mode == 'video':
            # Check if downloaded file has video stream
            has_video = await loop.run_in_executor(None, has_video_stream, file_path)
            if not has_video:
                logger.error(f"Downloaded file has no video stream: {file_path}")
                await status_msg.edit_text(
                    "Ошибка: скачанный файл не содержит видео. "
                    "Возможно, видео недоступно или защищено. Попробуйте другую ссылку."
                )
                return

            # Convert to Telegram-compatible format if needed
            converted = await loop.run_in_executor(None, convert_to_telegram_format, file_path)
            if not converted:
                logger.warning(f"Conversion failed, trying to send original file")

            width, height, duration = await loop.run_in_executor(None, get_video_metas, file_path)
            thumb_path = await loop.run_in_executor(None, generate_thumbnail, file_path)

            if width and height and duration:
                await message.answer_video(
                    video=FSInputFile(file_path),
                    thumbnail=FSInputFile(thumb_path) if thumb_path else None,
                    caption=f"{platform}: {title[:200]}",
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=True
                )
            else:
                await message.answer_video(
                    video=FSInputFile(file_path),
                    thumbnail=FSInputFile(thumb_path) if thumb_path else None,
                    caption=f"{platform}: {title[:200]}",
                    supports_streaming=True
                )
        else:
            await message.answer_audio(
                audio=FSInputFile(file_path),
                caption=f"{platform}: {title[:200]}"
            )

        await status_msg.delete()
    except Exception as e:
        error_str = str(e)
        logger.error(f"Error downloading: {type(e).__name__}: {error_str}")
        # Show user-friendly error messages
        if "blocked" in error_str.lower() or "IP" in error_str:
            error_text = "Видео недоступно с нашего сервера. Попробуйте позже или пришлите другую ссылку."
        elif "private" in error_str.lower() or "login" in error_str.lower():
            error_text = "Это видео приватное или требует авторизации."
        elif "not found" in error_str.lower() or "404" in error_str:
            error_text = "Видео не найдено. Проверьте ссылку."
        elif "empty media" in error_str.lower():
            error_text = "Instagram не отдаёт это видео. Попробуйте другую ссылку."
        else:
            error_text = "Ошибка при скачивании. Проверьте ссылку и попробуйте ещё раз."
        await status_msg.edit_text(error_text)
    finally:
        # Clean up video file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Deleted file: {file_path}")
            except Exception as e:
                logger.error(f"Error deleting file {file_path}: {e}")

        # Clean up thumbnail
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
                logger.info(f"Deleted thumbnail: {thumb_path}")
            except Exception as e:
                logger.error(f"Error deleting thumbnail {thumb_path}: {e}")


@dp.errors()
async def errors_handler(update, exception):
    logger.error(f"Error: {exception}", exc_info=True)
    return True


async def main():
    await init_db()

    subscription_middleware = SubscriptionMiddleware()
    dp.message.middleware(subscription_middleware)
    dp.callback_query.middleware(subscription_middleware)

    await bot.set_my_commands([
        BotCommand(command="/start", description="Перезапустить бота")
    ])

    logger.info("Bot starting...")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot startup error: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
