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

# Загрузка конфигурации из файла .env
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# Настройки базы данных PostgreSQL
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# RSS-лента для команды news_command
NEWS_RSS_URL = config('NEWS_RSS_URL')

# Установка API-ключа для OpenAI
openai.api_key = OPENAI_API_KEY

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Уменьшение уровня логирования для внешних библиотек
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Глобальные переменные
conversation_context = defaultdict(list)
group_status = defaultdict(bool)
user_personalities = defaultdict(str)

# Личность бота по умолчанию
default_personality = "Ты Светлана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

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

def escape_markdown_v2(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def convert_markdown_to_telegram(text):
    text = text.replace('**', '*')
    return text

def is_bot_enabled(chat_id: int) -> bool:
    return group_status.get(chat_id, False)

async def ask_chatgpt(messages) -> str:
    logger.info(f"Sending messages to OpenAI: {messages}")
    try:
        response = await openai.ChatCompletion.acreate(
            model="o1-mini",
            messages=messages,
            max_completion_tokens=700,
            # temperature=0.2,
            n=1
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"OpenAI response: {answer}")
        return answer
    except openai.error.InvalidRequestError as e:
        error_msg = f"Ошибка запроса к OpenAI API: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except openai.OpenAIError as e:
        error_msg = f"Ошибка OpenAI API: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        logger.error("Неизвестная ошибка при обращении к OpenAI", exc_info=True)
        error_msg = f"Неизвестная ошибка: {str(e)}"
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
    bot_username = context.bot.username
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    message_text = update.message.text.strip() if update.message.text else ""

    # Определяем, упомянут ли бот в сообщении
    is_bot_mentioned = f'@{bot_username}' in message_text
    # Проверяем, является ли сообщение ответом на другое сообщение
    is_reply = update.message.reply_to_message is not None
    # Проверяем, является ли сообщение ответом на сообщение бота
    is_reply_to_bot = is_reply and update.message.reply_to_message.from_user.id == bot_id

    should_respond = False
    text_to_process = None
    reply_to_message_id = None

    if is_bot_mentioned and not is_reply:
        # Сценарий 1: Бот упомянут в сообщении
        should_respond = True
        # Удаляем упоминание бота из текста сообщения
        text_to_process = message_text.replace(f'@{bot_username}', '').strip()
        reply_to_message_id = update.message.message_id

    elif is_reply_to_bot:
        # Сценарий 2: Сообщение является ответом на сообщение бота
        should_respond = True
        text_to_process = message_text
        reply_to_message_id = update.message.message_id

    elif is_reply and is_bot_mentioned:
        # Сценарий 3: Сообщение является ответом на другое сообщение и упоминает бота
        # Получаем текст оригинального сообщения
        original_message = update.message.reply_to_message.text
        if original_message:
            should_respond = True
            text_to_process = original_message
            reply_to_message_id = update.message.message_id
        else:
            # Если оригинальное сообщение не содержит текст
            await update.message.reply_text("Извините, я не могу прочитать сообщение, на которое вы ответили.")
            return

    # Проверяем, включён ли бот в группе
    if update.message.chat.type != 'private' and not is_bot_enabled(chat_id):
        return

    # Обрабатываем сообщение, если должны ответить и есть текст для обработки
    if should_respond and text_to_process:
        personality = user_personalities.get(user_id, default_personality)
        instructions = "Всегда отвечай на вопросы, адресованные тебе."

        # Обновляем контекст переписки
        conversation_context[user_id].append({"role": "user", "content": text_to_process})
        conversation_context[user_id] = conversation_context[user_id][-10:]  # Сохраняем последние 10 сообщений

        # Формируем сообщения для отправки в API
        messages = []

        # Добавляем личность и инструкции в первое сообщение пользователя
        initial_message = f"{personality}\n{instructions}\n\n{conversation_context[user_id][0]['content']}"
        messages.append({"role": "user", "content": initial_message})

        # Добавляем остальные сообщения из контекста
        for message in conversation_context[user_id][1:]:
            messages.append(message)

        try:
            reply = await ask_chatgpt(messages)
        except Exception as e:
            logger.error(f"Error contacting OpenAI: {e}")
            await update.message.reply_text("Произошла ошибка при обращении к OpenAI. Попробуйте еще раз.")
            return

        # Добавляем ответ бота в контекст переписки
        conversation_context[user_id].append({"role": "assistant", "content": reply})
        conversation_context[user_id] = conversation_context[user_id][-10:]  # Сохраняем последние 10 сообщений

        # Отправляем ответ пользователю
        formatted_reply = convert_markdown_to_telegram(reply)
        escaped_reply = escape_markdown_v2(formatted_reply)

        max_length = 4096
        if len(escaped_reply) > max_length:
            escaped_reply = escaped_reply[:max_length]

        await update.message.reply_text(
            escaped_reply,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=reply_to_message_id
        )

        user_username = update.message.from_user.username if update.message.from_user.username else ''
        log_interaction(user_id, user_username, text_to_process, reply)

def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Инициализация базы данных
    init_db()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("set_personality", set_personality))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запускаем бота
    application.run_polling()

if __name__ == '__main__':
    main()
