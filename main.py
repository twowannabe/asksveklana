import logging
import os
import random
import re
import requests
import asyncio
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from telegram.helpers import escape_markdown
from decouple import config
import openai
import psycopg2
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from PIL import Image
import io
import pytesseract
from transformers import VisionEncoderDecoderModel, ViTImageProcessor, AutoTokenizer
import torch
from transformers import MarianMTModel, MarianTokenizer

# Load configuration from .env file
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# PostgreSQL database settings
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# RSS feed для команды news_command
NEWS_RSS_URL = config('NEWS_RSS_URL')

# Set API key for OpenAI
openai.api_key = OPENAI_API_KEY

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Reduce logging level for external libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Global variables
conversation_context = defaultdict(list)
group_status = defaultdict(bool)
user_personalities = defaultdict(str)

# Default bot personality
default_personality = "Ты Светлана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

# Инициализация моделей для описания изображений и перевода
model = VisionEncoderDecoderModel.from_pretrained("nlpconnect/vit-gpt2-image-captioning")
feature_extractor = ViTImageProcessor.from_pretrained("nlpconnect/vit-gpt2-image-captioning")
tokenizer = AutoTokenizer.from_pretrained("nlpconnect/vit-gpt2-image-captioning")

# Устройство (CPU или GPU)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)

# Модель для перевода с английского на русский
translation_model_name = 'Helsinki-NLP/opus-mt-en-ru'
translation_tokenizer = MarianTokenizer.from_pretrained(translation_model_name)
translation_model = MarianMTModel.from_pretrained(translation_model_name).to(device)

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def escape_markdown_v2(text):
    escape_chars = r'[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def convert_markdown_to_telegram(text):
    text = text.replace('**', '*')
    return text

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS askgbt_logs (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            user_username TEXT,
            user_message TEXT,
            gpt_reply TEXT,
            timestamp TIMESTAMP
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_personalities (
            user_id BIGINT PRIMARY KEY,
            personality TEXT
        )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Database tables created or already exist")
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")

def log_interaction(user_id, user_username, user_message, gpt_reply):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        timestamp = datetime.now()
        cursor.execute('''
        INSERT INTO askgbt_logs (user_id, user_username, user_message, gpt_reply, timestamp)
        VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, user_username, user_message, gpt_reply, timestamp))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error writing to database: {str(e)}")

async def ask_chatgpt(messages) -> str:
    logger.info(f"Sending messages to ChatGPT: {messages}")
    try:
        messages_with_formatting = [
            {"role": "system", "content": "Keep responses concise and no more than 3500 characters."}
        ] + messages

        for message in messages_with_formatting:
            if not message.get("content"):
                logger.error(f"Empty content in message: {message}")
                return "An error occurred: one of the messages was empty."

        response = await openai.ChatCompletion.acreate(
            model="gpt-4o-mini",
            messages=messages_with_formatting,
            max_tokens=700,
            temperature=0.2,
            n=1
        )

        answer = response.choices[0].message['content'].strip()
        logger.info(f"ChatGPT response: {answer}")

        return answer
    except openai.error.RateLimitError:
        error_msg = "Превышен лимит запросов к OpenAI API. Пожалуйста, попробуйте позже."
        logger.error(error_msg)
        return error_msg
    except openai.error.InvalidRequestError as e:
        error_msg = f"Ошибка запроса к OpenAI API: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        logger.error("Error contacting ChatGPT", exc_info=True)
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        return error_msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Я твоя виртуальная подруга Светлана. Давай общаться!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Доступные команды:\n"
        "/start - Начать беседу\n"
        "/help - Показать это сообщение\n"
        "/enable - Включить бота в этой группе (только для администраторов)\n"
        "/disable - Выключить бота в этой группе (только для администраторов)\n"
        "/image [описание] - Сгенерировать изображение\n"
        "/reset - Сбросить историю диалога\n"
        "/set_personality [описание] - Установить личность бота\n"
        "/news - Получить последние новости\n"
    )
    await update.message.reply_text(help_text)

async def is_user_admin(update: Update) -> bool:
    user_status = await update.effective_chat.get_member(update.effective_user.id)
    return user_status.status in ['administrator', 'creator']

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = True
        await update.message.reply_text("Бот включен в этой группе!")
    else:
        await update.message.reply_text("Только администраторы могут выполнять эту команду.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = False
        await update.message.reply_text("Бот отключен в этой группе!")
    else:
        await update.message.reply_text("Только администраторы могут выполнять эту команду.")

def is_bot_enabled(chat_id: int) -> bool:
    return group_status.get(chat_id, False)

def generate_image(prompt: str) -> str:
    logger.info(f"Requesting image generation with prompt: {prompt}")
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        return response['data'][0]['url']
    except Exception as e:
        error_msg = f"Error generating image: {str(e)}"
        logger.error(error_msg)
        return error_msg

async def send_image(update: Update, context: ContextTypes.DEFAULT_TYPE, image_url: str) -> None:
    try:
        response = requests.get(image_url)
        image = BytesIO(response.content)
        image.name = 'image.png'
        await update.message.reply_photo(photo=image)
    except Exception as e:
        logger.error(f"Error sending image: {str(e)}")
        await update.message.reply_text(f"Error sending image: {str(e)}")

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_input = ' '.join(context.args)
    if not user_input:
        await update.message.reply_text("Пожалуйста, укажите описание после команды /image.")
        return
    image_url = generate_image(user_input)
    if image_url.startswith("Error"):
        await update.message.reply_text(image_url)
    else:
        await send_image(update, context, image_url)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    conversation_context[user_id] = []
    await update.message.reply_text("История диалога сброшена.")

async def set_personality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    personality = ' '.join(context.args)
    if not personality:
        await update.message.reply_text("Пожалуйста, предоставьте описание личности после команды /set_personality.")
        return
    user_id = update.message.from_user.id
    user_personalities[user_id] = personality
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO user_personalities (user_id, personality)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET personality = %s
        ''', (user_id, personality, personality))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving personality to database: {str(e)}")
    await update.message.reply_text(f"Личность бота установлена: {personality}")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        response = requests.get(NEWS_RSS_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, features='xml')
        items = soup.findAll('item')[:5]

        news_message = "Последние новости:\n\n"
        for item in items:
            title = escape_markdown_v2(item.title.text)
            link = item.link.text
            news_message += f"*{title}*\n[Читать далее]({link})\n\n"

        await update.message.reply_text(
            news_message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error retrieving news: {str(e)}")
        await update.message.reply_text("Произошла ошибка при получении новостей.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        logger.warning("Received an update without a message. Ignoring.")
        return

    bot_id = context.bot.id
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    bot_username = context.bot.username
    message_text = update.message.text.strip() if update.message.text else ""

    # Проверяем, упомянут ли бот в сообщении
    is_bot_mentioned = f'@{bot_username}' in message_text

    # Проверяем, является ли сообщение ответом на другое сообщение
    is_reply = update.message.reply_to_message is not None

    should_respond = False
    reply_to_message_id = None
    text_to_process = None

    # Если бот упомянут в ответе на сообщение, используем текст из исходного сообщения
    if is_reply and is_bot_mentioned:
        original_message = update.message.reply_to_message.text or ""
        text_to_process = original_message
        should_respond = True
        reply_to_message_id = update.message.message_id

    # Личная переписка (не группа)
    if update.message.chat.type == 'private':
        should_respond = True
        text_to_process = message_text

    # Проверка на включение бота в группе
    if not should_respond or not text_to_process:
        return

    # Личность бота
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [
        {"role": "system", "content": personality},
        {"role": "system", "content": "Всегда отвечай на вопросы, адресованные тебе."}
    ]
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Сохраняем последние 10 сообщений
    messages = initial_instructions + conversation_context[user_id]

    try:
        reply = await ask_chatgpt(messages)
    except Exception as e:
        logger.error(f"Error contacting OpenAI: {e}")
        await update.message.reply_text("Произошла ошибка при обращении к OpenAI. Попробуйте еще раз.")
        return

    formatted_reply = convert_markdown_to_telegram(reply)
    escaped_reply = escape_markdown_v2(formatted_reply)

    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    if reply_to_message_id:
        await update.message.reply_text(
            escaped_reply,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=reply_to_message_id
        )
    else:
        await update.message.reply_text(
            escaped_reply,
            parse_mode=ParseMode.MARKDOWN_V2
        )

    user_username = update.message.from_user.username if update.message.from_user.username else ''
    log_interaction(user_id, user_username, text_to_process, reply)

def translate_text(text):
    tokens = translation_tokenizer([text], return_tensors='pt', padding=True).to(device)
    translation = translation_model.generate(**tokens)
    translated_text = translation_tokenizer.batch_decode(translation, skip_special_tokens=True)[0]
    return translated_text

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    init_db()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("set_personality", set_personality))
    application.add_handler(CommandHandler("news", news_command))  # Добавляем этот обработчик
    # application.add_handler(MessageHandler(filters.PHOTO, handle_photo))  # Удаляем этот обработчик
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
