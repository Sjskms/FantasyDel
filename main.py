# -*- coding: utf-8 -*-
import asyncio
import io
import hashlib
import logging
import sqlite3
import sys
from PIL import Image

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

# ================= НАСТРОЙКИ БОТА =================
BOT_TOKEN = "8966599826:AAHNuHjB31pPN7qaKsk3ExObJWm7W0Jgpws"  # Токен от @BotFather
ADMIN_ID = 7916504148         # Ваш Telegram ID для отправки логов/оповещений о дубликатах
DB_PATH = "data/duplicate_detector.db"
# Допустимый порог различия для картинок (0 - точная копия, чем больше число, тем больше размыта схожесть).
# Оптимально: от 0 до 5.
HAMMING_DISTANCE_THRESHOLD = 4 
# ==================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- Работа с Базой Данных (SQLite) ---
def init_db():
    """Инициализация базы данных."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            post_type TEXT, -- 'text', 'photo', 'video'
            content_hash TEXT,
            meta_info TEXT,  -- доп. инфо (например, file_unique_id или размер видео)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def add_post_record(chat_id: int, message_id: int, post_type: str, content_hash: str, meta_info: str = ""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO channel_posts (chat_id, message_id, post_type, content_hash, meta_info) VALUES (?, ?, ?, ?, ?)",
        (chat_id, message_id, post_type, content_hash, meta_info)
    )
    conn.commit()
    conn.close()


def get_existing_hashes(chat_id: int, post_type: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT message_id, content_hash, meta_info FROM channel_posts WHERE chat_id = ? AND post_type = ?",
        (chat_id, post_type)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# --- Хеширование контента ---

def get_text_hash(text: str) -> str:
    """Генерирует SHA-256 хеш текста, нормализуя пробелы и регистр."""
    if not text:
        return ""
    normalized = " ".join(text.strip().lower().split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def calculate_average_hash(image_bytes: bytes) -> str:
    """
    Вычисляет Average Hash (aHash) картинки.
    Позволяет находить дубликаты даже при сжатии или изменении разрешения.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert('L').resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / 64
        # Генерируем 64-битную строку из 0 и 1
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception as e:
        logging.error(f"Ошибка при расчете хеша картинки: {e}")
        return ""


def get_hamming_distance(hash1: str, hash2: str) -> int:
    """Вычисляет расстояние Хэмминга (количество различающихся бит)."""
    if len(hash1) != len(hash2):
        return 999
    return sum(c1 != c2 for c1, c2 in zip(hash1, hash2))


# --- Логика детекции дубликатов ---

async def check_and_process_duplicate(message: Message, post_type: str, current_hash: str, meta_info: str = "") -> bool:
    """
    Проверяет, является ли сообщение дубликатом. 
    Если да — удаляет его, оповещает админа и возвращает True.
    """
    chat_id = message.chat.id
    message_id = message.message_id
    records = get_existing_hashes(chat_id, post_type)

    is_duplicate = False
    original_msg_id = None

    if post_type == "text":
        # Для текста ищем точное совпадение хешей
        for orig_id, orig_hash, _ in records:
            if orig_hash == current_hash:
                is_duplicate = True
                original_msg_id = orig_id
                break

    elif post_type == "photo":
        # Для фото сравниваем расстояние Хэмминга
        for orig_id, orig_hash, _ in records:
            distance = get_hamming_distance(orig_hash, current_hash)
            if distance <= HAMMING_DISTANCE_THRESHOLD:
                is_duplicate = True
                original_msg_id = orig_id
                break

    elif post_type == "video":
        # Для видео проверяем file_unique_id ИЛИ связку (длительность + размер файла)
        for orig_id, orig_hash, orig_meta in records:
            # 1. Сравнение по file_unique_id
            if current_hash and current_hash == orig_hash:
                is_duplicate = True
                original_msg_id = orig_id
                break
            # 2. Сравнение по размеру и длине (если залит заново другим файлом)
            if meta_info and meta_info == orig_meta:
                is_duplicate = True
                original_msg_id = orig_id
                break

    if is_duplicate:
        try:
            # Ссылка на оригинальный пост
            chat_username = message.chat.username
            if chat_username:
                orig_link = f"https://t.me/{chat_username}/{original_msg_id}"
            else:
                # Для закрытых каналов
                clean_chat_id = str(chat_id).replace("-100", "")
                orig_link = f"https://t.me/c/{clean_chat_id}/{original_msg_id}"

            # 1. Удаляем дубликат из канала
            await bot.delete_message(chat_id, message_id)
            logging.info(f"Удален дубликат ({post_type}) в канале {chat_id}. Ссылка на оригинал: {orig_link}")

            # 2. Оповещаем администратора
            admin_text = (
                f"🚨 <b>Обнаружен и удален дубликат в канале!</b>\n\n"
                f"• <b>Тип контента:</b> {post_type.upper()}\n"
                f"• <b>Канал:</b> {message.chat.title} ({chat_id})\n"
                f"• <b>Оригинальное сообщение:</b> <a href='{orig_link}'>Перейти к оригиналу</a>"
            )
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", disable_web_page_preview=False)
            return True

        except TelegramBadRequest as e:
            logging.error(f"Не удалось удалить сообщение или отправить отчет. Возможно, у бота нет прав: {e}")
            # Пытаемся сообщить админу об ошибке прав
            try:
                await bot.send_message(
                    ADMIN_ID, 
                    f"⚠️ Бот обнаружил дубликат в канале <i>{message.chat.title}</i>, но <b>не смог его удалить</b>!\n"
                    f"Проверьте, выданы ли боту права Администратора на 'Удаление сообщений'.\n\nКод ошибки: {e}",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return False
        except Exception as e:
            logging.error(f"Произошла непредвиденная ошибка при обработке дубликата: {e}")
            return False

    # Если это уникальный контент — сохраняем в базу данных
    add_post_record(chat_id, message_id, post_type, current_hash, meta_info)
    return False


# --- Обработчики событий канала (channel_post) ---

@dp.channel_post(F.text)
async def handle_channel_text(message: Message):
    text_hash = get_text_hash(message.text)
    if text_hash:
        await check_and_process_duplicate(message, "text", text_hash)


@dp.channel_post(F.photo)
async def handle_channel_photo(message: Message):
    # Берем самую маленькую миниатюру фото (индекс 0), чтобы скачивание происходило моментально!
    # Для построения 8x8 хеша сверхвысокое качество не требуется.
    photo_size = message.photo[0]
    
    try:
        # Скачиваем миниатюру в оперативную память
        file_in_mem = io.BytesIO()
        await bot.download(photo_size.file_id, destination=file_in_mem)
        file_bytes = file_in_mem.getvalue()
        
        # Считаем перцептивный хеш изображения
        img_hash = calculate_average_hash(file_bytes)
        if img_hash:
            await check_and_process_duplicate(message, "photo", img_hash)
    except Exception as e:
        logging.error(f"Ошибка при скачивании/обработке фото: {e}")


@dp.channel_post(F.video)
async def handle_channel_video(message: Message):
    video = message.video
    
    # В качестве основного хеша берем уникальный ID файла в Telegram
    file_unique_id = video.file_unique_id
    
    # Дополнительно сохраняем метаданные: "размер_длительность" на случай перезаливки файла
    meta_info = f"{video.file_size}_{video.duration}"
    
    await check_and_process_duplicate(message, "video", file_unique_id, meta_info)


# --- Точка входа ---

async def main():
    init_db()
    logging.info("База данных успешно инициализирована.")
    
    # Сброс зависших вебхуков и запуск поллинга
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен и ожидает постов в канале...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")