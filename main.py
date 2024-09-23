import logging
import re
from collections import defaultdict
from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import speech_recognition as sr
from pydub import AudioSegment
import moviepy.editor as mp
import os
import psycopg2
from datetime import datetime
import requests
from io import BytesIO
import random

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# Настройки подключения к PostgreSQL
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# Установка ключа API для OpenAI
openai.api_key = OPENAI_API_KEY

# Логирование с указанием кодировки
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO,
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
for handler in logger.handlers:
    handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
    handler.setLevel(logging.INFO)
    handler.setStream(open(os.sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1))

# Глобальная переменная для хранения контекста бесед
conversation_context = defaultdict(list)

# Список пользователей, которым разрешено управлять ботом
ALLOWED_USER_IDS = [6122780749, 530674302]  # Добавь сюда ID пользователей, которым разрешено управлять ботом

# Словарь для хранения статуса включения бота по chat_id групп
group_status = defaultdict(bool)

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты воеенный и политический эксперт, умеешь анализировать новости и сложные ситуации."}
]

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def clean_messages(messages: list) -> list:
    """Очищает сообщения от потенциально нежелательного контента."""
    cleaned_messages = []
    for msg in messages:
        # Здесь можно добавить логику очистки текста, если необходимо
        cleaned_messages.append(msg)
    return cleaned_messages

def generate_user_description(messages: list, user_first_name: str) -> str:
    """Генерирует описание пользователя на основе его сообщений."""
    combined_messages = "\n".join(messages)

    chat_messages = [
        {"role": "system", "content": "Вы - помощник, который анализирует сообщения пользователей и создает их описания. Используйте токсичный шуточный тон в ответах."},
        {"role": "user", "content": f"Проанализируй следующие сообщения пользователя и опиши его личность, интересы и стиль общения.\n\nСообщения пользователя:\n{combined_messages}\n\nОписание пользователя {user_first_name}:"}
    ]

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=chat_messages,
            max_tokens=200,
            n=1,
            temperature=0.7,
        )
        description = response.choices[0].message['content'].strip()
        return description
    except Exception as e:
        logger.error(f"Ошибка при генерации описания пользователя: {str(e)}")
        return "Извините, не удалось создать описание."

def get_user_messages(user_id: int, limit=50) -> list:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT text FROM messages WHERE user_id = %s ORDER BY date DESC LIMIT %s
    ''', (user_id, limit))

    messages = [row[0] for row in cursor.fetchall()]
    conn.close()
    return messages

def describe_user(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    user_first_name = update.message.from_user.first_name

    user_messages = get_user_messages(user_id, limit=50)

    if not user_messages:
        update.message.reply_text("У вас нет сообщений для анализа.")
        return

    user_messages = clean_messages(user_messages)

    description = generate_user_description(user_messages, user_first_name)

    update.message.reply_text(description)

def add_emojis_at_end(answer: str) -> str:
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def init_db():
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
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
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Таблица askgbt_logs успешно создана или уже существует")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {str(e)}")

def log_interaction(user_id, user_username, user_message, gpt_reply):
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        cursor = conn.cursor()
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
        INSERT INTO askgbt_logs (user_id, user_username, user_message, gpt_reply, timestamp)
        VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, user_username, user_message, gpt_reply, timestamp))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при записи в базу данных: {str(e)}")

def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages,
            max_tokens=100,
            temperature=0.5,
            n=1,
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")

        clean_answer = answer.replace(')', '').replace('(', '')

        return add_emojis_at_end(clean_answer)
    except Exception as e:
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        logger.error(error_msg)
        return error_msg

def generate_image(prompt: str) -> str:
    logger.info(f"Отправка запроса на создание изображения с описанием: {prompt}")
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        logger.info(f"Получена ссылка на изображение: {image_url}")
        return image_url
    except Exception as e:
        error_msg = f"Ошибка при создании изображения: {str(e)}"
        logger.error(error_msg)
        return error_msg

def send_image(update: Update, context: CallbackContext, image_url: str) -> None:
    try:
        response = requests.get(image_url)
        image = BytesIO(response.content)
        image.name = 'image.png'
        update.message.reply_photo(photo=image)
    except Exception as e:
        error_msg = f"Ошибка при отправке изображения: {str(e)}"
        logger.error(error_msg)
        update.message.reply_text(error_msg)

# Функция старта бота
def start(update: Update, context: CallbackContext) -> None:
    """Приветственное сообщение при запуске бота."""
    update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

# Проверка, является ли пользователь разрешённым
def is_user_allowed(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS

# Включение бота для конкретной группы
def enable_bot(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat.id

    if is_user_allowed(user_id):
        group_status[chat_id] = True
        update.message.reply_text("Бот включен в этой группе!")
    else:
        update.message.reply_text("У вас нет прав для выполнения этой команды.")

# Отключение бота для конкретной группы
def disable_bot(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    chat_id = update.message.chat.id

    if is_user_allowed(user_id):
        group_status[chat_id] = False
        update.message.reply_text("Бот отключен в этой группе!")
    else:
        update.message.reply_text("У вас нет прав для выполнения этой команды.")

# Проверка, включен ли бот в группе
def is_bot_enabled(chat_id: int) -> bool:
    return group_status.get(chat_id, False)

# Обработка текстовых сообщений
def handle_message(update: Update, context: CallbackContext, is_voice=False, is_video=False) -> None:
    chat_id = update.message.chat.id

    if not is_bot_enabled(chat_id):
        return  # Если бот отключен, не отвечаем

    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    user_message = update.message.text.strip()

    conversation_context[user_id].append({"role": "user", "content": user_message})

    messages = initial_instructions + conversation_context[user_id]

    reply = ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    log_interaction(user_id, user_username, user_message, reply)

def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    init_db()

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("describe_me", describe_user))
    dispatcher.add_handler(CommandHandler("enable", enable_bot))
    dispatcher.add_handler(CommandHandler("disable", disable_bot))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
