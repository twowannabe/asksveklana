import logging
import os
import random
import re
import requests
import asyncio
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from decouple import config
import openai
import psycopg2
from bs4 import BeautifulSoup
from telegram import Update, ParseMode  # Добавлены необходимые импорты
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes  # Добавлены необходимые импорты
)
import html

# Load configuration from .env file
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# PostgreSQL database settings
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# Set API key for OpenAI
openai.api_key = OPENAI_API_KEY

# Configure logging with reduced verbosity for external libraries
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,  # Main logger level set to INFO
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Reduce logging level for external libraries
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Global variables
conversation_context = defaultdict(list)  # Conversation contexts
group_status = defaultdict(bool)  # Bot activation status in groups
user_personalities = defaultdict(str)  # User-specific bot personalities
user_requests = defaultdict(list)  # For rate limiting

# Default bot personality
default_personality = "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

def get_db_connection():
    """
    Establishes a connection to the PostgreSQL database.
    """
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def add_emojis_at_end(answer: str) -> str:
    """
    Randomly adds emojis to the end of the assistant's reply.
    """
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def init_db():
    """
    Initializes the database and necessary tables.
    """
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
        # Table for storing user personalities
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

async def ask_chatgpt(messages) -> str:
    """
    Sends messages to OpenAI ChatGPT and returns the reply.
    """
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        # Add a system message to control the response length
        messages_with_formatting = [
            {"role": "system", "content": "Пожалуйста, делай ответы краткими и не более 3500 символов."}
        ] + messages

        response = await openai.ChatCompletion.acreate(
            model="gpt-4o-mini",
            messages=messages_with_formatting,
            max_tokens=700,
            temperature=0.5,
            n=1
        )

        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")

        return add_emojis_at_end(answer)
    except Exception as e:
        logger.error("Произошла ошибка при обращении к ChatGPT", exc_info=True)
        return f"Ошибка при обращении к ChatGPT: {str(e)}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Check if the message is text only
    if update.message is None or update.message.text is None:
        logger.info("Received a non-text message, ignoring it.")
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    message_text = update.message.text.strip()
    bot_username = context.bot.username

    logger.info(f"Received text message from user {user_id} in chat {chat_id}: {message_text}")

    text_to_process = message_text

    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            logger.info(f"Bot is disabled in chat {chat_id}")
            return

        # Handle forwarded messages
        if update.message.forward_date:
            text_to_process = update.message.text

        elif f'@{bot_username}' in message_text:
            if update.message.reply_to_message and update.message.reply_to_message.text:
                text_to_process = update.message.reply_to_message.text
            else:
                text_to_process = message_text.replace(f'@{bot_username}', '').strip()

        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            if message_text:
                text_to_process = message_text
            else:
                await update.message.reply_text("Пожалуйста, отправьте текст вашего сообщения.")
                return
        else:
            return
    else:
        text_to_process = message_text

    if not text_to_process:
        await update.message.reply_text("Похоже, вы отправили пустое сообщение. Пожалуйста, отправьте текст.")
        return

    # Get bot's personality for the user
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]

    messages = initial_instructions + conversation_context[user_id]

    reply = await ask_chatgpt(messages)

    escaped_reply = escape_markdown(reply, version=2)
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    init_db()

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
