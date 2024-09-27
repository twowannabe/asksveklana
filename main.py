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

# Импорты из библиотеки telegram
from telegram import Update  # Необходимый импорт Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes  # Правильный импорт ContextTypes
from telegram.helpers import escape_markdown
import html

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# Настройки базы данных PostgreSQL
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# Установка API ключа для OpenAI
openai.api_key = OPENAI_API_KEY

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
conversation_context = defaultdict(list)
group_status = defaultdict(bool)
user_personalities = defaultdict(str)
user_requests = defaultdict(list)

# Персональность бота по умолчанию
default_personality = "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

def get_db_connection():
    """
    Устанавливает соединение с базой данных PostgreSQL.
    """
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def set_group_status(chat_id: int, status: bool):
    """
    Устанавливает статус активации бота для группы в базе данных.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO group_status (chat_id, is_enabled)
            VALUES (%s, %s)
            ON CONFLICT (chat_id)
            DO UPDATE SET is_enabled = EXCLUDED.is_enabled
        ''', (chat_id, status))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при обновлении статуса группы {chat_id}: {str(e)}")

def load_group_statuses():
    """
    Загружает статусы групп из базы данных в глобальную переменную group_status.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT chat_id, is_enabled FROM group_status')
        rows = cursor.fetchall()
        for row in rows:
            group_id, status = row
            group_status[group_id] = status
        cursor.close()
        conn.close()
        logger.info("Статусы групп загружены из базы данных")
    except Exception as e:
        logger.error(f"Ошибка при загрузке статусов групп: {str(e)}")

def add_emojis_at_end(answer: str) -> str:
    """
    Случайным образом добавляет эмодзи в конец ответа ассистента.
    """
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def init_db():
    """
    Инициализирует базу данных и необходимые таблицы.
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
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_personalities (
            user_id BIGINT PRIMARY KEY,
            personality TEXT
        )
        ''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_status (
            chat_id BIGINT PRIMARY KEY,
            is_enabled BOOLEAN NOT NULL
        )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Таблицы базы данных созданы или уже существуют")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {str(e)}")

async def ask_chatgpt(messages) -> str:
    """
    Отправляет сообщения в OpenAI ChatGPT и возвращает ответ.
    """
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        messages_with_formatting = [
            {"role": "system", "content": "Пожалуйста, делай ответы краткими и не более 3500 символов."}
        ] + messages

        response = await openai.ChatCompletion.acreate(
            model="gpt-4",
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

def is_bot_enabled(chat_id: int) -> bool:
    """
    Проверяет, активирован ли бот в указанном чате.
    """
    return group_status[chat_id]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = context.bot.id

    # Проверка наличия текста в сообщении
    if update.message is None or update.message.text is None:
        logger.info("Получено не текстовое сообщение, игнорируем его.")
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    message_text = update.message.text.strip()
    bot_username = context.bot.username

    logger.info(f"Получено сообщение от пользователя {user_id} в чате {chat_id}: {message_text}")

    # По умолчанию текст для обработки - это текущий текст сообщения
    text_to_process = message_text

    # Определение условия для ответа
    should_respond = False
    reply_to_message_id = None

    if update.message.chat.type != 'private':  # Если сообщение в группе
        # Проверка, что бот активен в данном чате
        if not is_bot_enabled(chat_id):
            logger.info(f"Бот отключен в чате {chat_id}")
            return

        # Условие 1: Сообщение содержит тег бота
        if f'@{bot_username}' in message_text:
            should_respond = True

            # Если это ответ на другое сообщение, прочитать сообщение, на которое ссылаются
            if update.message.reply_to_message and update.message.reply_to_message.text:
                text_to_process = update.message.reply_to_message.text
                reply_to_message_id = update.message.reply_to_message.message_id
            else:
                text_to_process = message_text.replace(f'@{bot_username}', '').strip()

        # Условие 2: Сообщение — это ответ на сообщение бота
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            should_respond = True
            text_to_process = message_text  # Текст ответа пользователя
            reply_to_message_id = update.message.reply_to_message.message_id

    else:  # Если сообщение в личном чате
        should_respond = True
        text_to_process = message_text

    # Если бот не должен отвечать, прервать обработку
    if not should_respond or not text_to_process:
        return

    # Логирование текста для обработки
    logger.info(f"Текст для обработки: {text_to_process}")

    # Получение персональности бота для пользователя
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]

    # Добавление сообщения в контекст разговора
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Ограничение контекста последними 10 сообщениями

    # Формирование сообщений для ChatGPT
    messages = initial_instructions + conversation_context[user_id]

    # Получение ответа от ChatGPT
    reply = await ask_chatgpt(messages)

    # Экранирование текста для Markdown
    escaped_reply = escape_markdown(reply, version=2)
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Отправка ответа в чат
    if reply_to_message_id:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=reply_to_message_id)
    else:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    group_status[chat_id] = True
    set_group_status(chat_id, True)
    await update.message.reply_text("Бот активирован в этом чате.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    group_status[chat_id] = False
    set_group_status(chat_id, False)
    await update.message.reply_text("Бот деактивирован в этом чате.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    init_db()
    load_group_statuses()

    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
