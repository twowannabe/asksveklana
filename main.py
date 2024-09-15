import logging
import re
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from decouple import config
from telegram import Update, Message, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import requests

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

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

# Начальная инструкция для ChatGPT
initial_instructions = [
    {
        "role": "system",
        "content": (
            "Ты Светлана — молодая девушка, которая переписывается на русском языке. "
            "Ты дружелюбная и игривая, используешь эмодзи в конце сообщений. "
            "Отвечай понятно и интересно, используя markdown-разметку для выделения текста (жирный, курсив). "
            "Например:\n"
            "- *О, это очень интересно!*\n"
            "- **Согласна с тобой полностью!**\n"
            "- Почему бы нам не обсудить это подробнее? 😊"
        )
    }
]

def add_emojis_at_end(answer: str) -> str:
    """Добавляет несколько эмодзи в конец ответа."""
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']
    if random.choice([True, False]):
        return answer
    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))
    return f"{answer} {chosen_emojis}"

def format_markdown(answer: str) -> str:
    # Экранируем специальные символы для Markdown V1
    escape_chars = r'_*[]()~`>#+-=|{}.!'

    def escape_special_chars(text):
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

    # Преобразование к правильному Markdown-формату
    answer = re.sub(r'(\d+)\.', r'\1\\.', answer)  # Экранирование чисел в списках
    answer = escape_special_chars(answer)  # Экранируем все специальные символы
    return answer

def split_message(message: str, max_length: int = 4096) -> list:
    """Разбивает длинное сообщение на несколько частей."""
    return [message[i:i + max_length] for i in range(0, len(message), max_length)]

# Создание базы данных для логирования
def init_db():
    conn = sqlite3.connect('chatgpt_logs.db')
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        user_message TEXT,
        gpt_reply TEXT,
        timestamp TEXT
    )
    ''')
    conn.commit()
    conn.close()

def log_interaction(user_id, user_message, gpt_reply):
    conn = sqlite3.connect('chatgpt_logs.db')
    cursor = conn.cursor()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
    INSERT INTO logs (user_id, user_message, gpt_reply, timestamp)
    VALUES (?, ?, ?, ?)
    ''', (user_id, user_message, gpt_reply, timestamp))
    conn.commit()
    conn.close()

# Функция для отправки сообщения в ChatGPT и получения ответа
def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages,
            max_tokens=500,
            temperature=0.7
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")

        # Форматируем ответ
        formatted_answer = format_markdown(answer)

        # Удаляем только скобочки перед добавлением эмодзи
        clean_answer = formatted_answer.replace(')', '').replace('(', '')

        return add_emojis_at_end(clean_answer)
    except Exception as e:
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        logger.error(error_msg)
        return error_msg

# Обработчик команды /start
def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('Привет! Я — Светлана, твоя виртуальная подруга. Давай пообщаемся! 😊')

def extract_text_from_message(message: Message) -> str:
    """Извлекает текст из сообщения, если текст доступен."""
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""

# Обработчик текстовых сообщений
def handle_message(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    user_id = update.message.from_user.id
    user_message = extract_text_from_message(update.message)

    # Добавляем сообщение пользователя в контекст
    conversation_context[user_id].append({"role": "user", "content": user_message})

    # Подготавливаем сообщения для отправки в ChatGPT
    messages = initial_instructions + conversation_context[user_id]

    # Получаем ответ от ChatGPT
    reply = ask_chatgpt(messages)

    # Добавляем ответ ChatGPT в контекст
    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Разделяем длинные сообщения и отправляем по частям
    messages = split_message(reply)
    for msg in messages:
        update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # Логирование взаимодействия
    log_interaction(user_id, user_message, reply)

def main():
    # Создаем апдейтера и диспетчера
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    # Инициализируем базу данных
    init_db()

    # Регистрируем обработчики
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Запуск бота
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
