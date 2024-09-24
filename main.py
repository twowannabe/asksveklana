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
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.helpers import escape_markdown

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

def log_interaction(user_id, user_username, user_message, gpt_reply):
    """
    Logs the user's interaction with the bot in the database.
    """
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
    """
    Отправляет сообщения в OpenAI ChatGPT и возвращает ответ.
    """
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        # Добавляем системное сообщение для контроля длины ответа
        messages_with_formatting = [
            {"role": "system", "content": "Пожалуйста, делай ответы краткими и не более 3500 символов."}
        ] + messages

        # Проверяем сообщения на наличие пустых строк
        for message in messages_with_formatting:
            if not message.get("content"):
                logger.error(f"Empty content in message: {message}")
                return "Произошла ошибка: одно из сообщений было пустым."

        # Используем асинхронный метод OpenAI API
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=messages_with_formatting,
            max_tokens=700,
            temperature=0.5,
            n=1
        )

        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")

        answer = add_emojis_at_end(answer)

        # Проверка на максимальную длину сообщения в Telegram
        max_length = 4096
        if len(answer) > max_length:
            answer = answer[:max_length]
            answer = answer.rsplit(' ', 1)[0] + '...'

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
        logger.error("Произошла ошибка при обращении к ChatGPT", exc_info=True)
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        return error_msg

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает входящие текстовые сообщения и генерирует ответ с помощью OpenAI.
    """
    if update.message is None:
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    message_text = update.message.text.strip()
    bot_username = context.bot.username

    # Переменная для хранения текста, который бот должен обработать
    text_to_process = message_text

    # Проверяем условия в групповых чатах
    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            return  # Бот отключен в этой группе

        # Если бот упомянут по никнейму
        if f'@{bot_username}' in message_text:
            # Если это ответ на другое сообщение
            if update.message.reply_to_message:
                # Используем текст сообщения, на которое был ответ
                text_to_process = update.message.reply_to_message.text
            else:
                # Убираем упоминание бота из сообщения
                text_to_process = message_text.replace(f'@{bot_username}', '').trim()
        # Если сообщение является ответом на сообщение бота
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
            # Используем текст сообщения пользователя
            text_to_process = message_text
        # Если сообщение является ответом на другое сообщение и содержит упоминание бота
        elif update.message.reply_to_message and f'@{bot_username}' in message_text:
            # Используем текст сообщения, на которое был ответ
            text_to_process = update.message.reply_to_message.text
        else:
            return  # Бот не должен реагировать на это сообщение
    else:
        # В личных сообщениях используем текст как есть
        text_to_process = message_text

    # Проверка на наличие текста для обработки
    if not text_to_process:
        await update.message.reply_text("Похоже, вы отправили пустое сообщение. Пожалуйста, отправьте текст.")
        logger.error(f"Received empty message from user {user_id}")
        return

    # Ограничение частоты запросов
    current_time = datetime.now()
    user_requests[user_id] = [
        req_time for req_time in user_requests[user_id]
        if (current_time - req_time).seconds < 60
    ]
    if len(user_requests[user_id]) >= 5:
        await update.message.reply_text("Вы слишком часто отправляете запросы. Пожалуйста, подождите немного.")
        return
    user_requests[user_id].append(current_time)

    # Получаем личность бота для пользователя
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]

    # Обновляем контекст разговора
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Храним последние 10 сообщений

    messages = initial_instructions + conversation_context[user_id]

    # Проверка на наличие пустых сообщений перед отправкой в ChatGPT
    for message in messages:
        if not message.get("content"):
            logger.error(f"Empty content in conversation context: {message}")
            await update.message.reply_text("Произошла ошибка при обработке контекста. Пожалуйста, попробуйте снова.")
            return

    reply = await ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Экранируем специальные символы для Markdown V2
    escaped_reply = escape_markdown(reply, version=2)

    # Проверяем, что сообщение не превышает лимит Telegram
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Отправляем ответ
    try:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {str(e)}")
        await update.message.reply_text("Произошла ошибка при отправке сообщения.")

    log_interaction(user_id, user_username, text_to_process, reply)
    logger.info(f"User ID: {user_id}, Chat ID: {chat_id}, Message ID: {update.message.message_id}")

def is_bot_enabled(chat_id: int) -> bool:
    """
    Checks if the bot is enabled in the given chat.
    """
    return group_status.get(chat_id, False)

def main():
    """
    Starts the bot.
    """
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    init_db()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("set_personality", set_personality))
    application.add_handler(CommandHandler("news", news_command))

    # Add message handlers
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
