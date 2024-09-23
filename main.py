import logging
from collections import defaultdict
from decouple import config
from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import os
import psycopg2
from datetime import datetime
import requests
from io import BytesIO
import random
import markdown
from bs4 import BeautifulSoup

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
ALLOWED_USER_IDS = [6122780749, 530674302, 459816251]  # Добавьте сюда ID пользователей, которым разрешено управлять ботом

# Словарь для хранения статуса включения бота по chat_id групп
group_status = defaultdict(bool)

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."}
]

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def add_emojis_at_end(answer: str) -> str:
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

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
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Таблица askgbt_logs успешно создана или уже существует")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {str(e)}")

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
        logger.error(f"Ошибка при записи в базу данных: {str(e)}")

def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        # Добавляем системное сообщение для контроля длины ответа
        messages_with_formatting = [
            {"role": "system", "content": "Пожалуйста, делай ответы краткими и не более 3500 символов."}
        ] + messages
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages_with_formatting,
            max_tokens=700,  # Регулируйте значение при необходимости
            temperature=0.5,
            n=1,
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")

        answer = add_emojis_at_end(answer)

        # Проверка на максимальную длину сообщения в Telegram
        max_length = 4096
        if len(answer) > max_length:
            # Обрезаем по последнему пробелу перед ограничением
            answer = answer[:max_length]
            answer = answer.rsplit(' ', 1)[0] + '...'

        return answer
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
def handle_message(update: Update, context: CallbackContext) -> None:
    if update.message is None:
        return  # Игнорируем обновления без сообщения

    chat_id = update.message.chat.id

    if not is_bot_enabled(chat_id):
        return  # Если бот отключен, не отвечаем

    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    user_message = update.message.text.strip()

    conversation_context[user_id].append({"role": "user", "content": user_message})

    # Оставляем только последние 10 сообщений
    conversation_context[user_id] = conversation_context[user_id][-10:]

    messages = initial_instructions + conversation_context[user_id]

    reply = ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Преобразование Markdown в HTML
    html_reply = markdown.markdown(reply)

    # Удаление неподдерживаемых тегов и обработка <p>
    allowed_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'a']

    soup = BeautifulSoup(html_reply, 'html.parser')
    for tag in soup.find_all():
        if tag.name == 'p':
            tag.replace_with(f'{tag.get_text()}\n')  # Заменяем <p> содержимым и переносом строки
        elif tag.name not in allowed_tags:
            tag.unwrap()  # Удаляем тег, сохраняя содержимое

    clean_html_reply = str(soup)

    # Отправляем сообщение с указанием parse_mode
    update.message.reply_text(clean_html_reply, parse_mode=ParseMode.HTML)

    log_interaction(user_id, user_username, user_message, reply)

def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    init_db()

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("enable", enable_bot))
    dispatcher.add_handler(CommandHandler("disable", disable_bot))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
