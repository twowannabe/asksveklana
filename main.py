import logging
import os
import random
import re
import requests
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

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')
NEWS_API_KEY = config('NEWS_API_KEY')  # API ключ для новостного сервиса

# Настройки подключения к PostgreSQL
DB_HOST = config('DB_HOST')
DB_PORT = config('DB_PORT')
DB_NAME = config('DB_NAME')
DB_USER = config('DB_USER')
DB_PASSWORD = config('DB_PASSWORD')

# Установка ключа API для OpenAI
openai.api_key = OPENAI_API_KEY

# Логирование с указанием кодировки и подробной информации
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Глобальные переменные
conversation_context = defaultdict(list)  # Контекст бесед
group_status = defaultdict(bool)  # Статус включения бота в группах
user_personalities = defaultdict(str)  # Личности бота для пользователей
user_requests = defaultdict(list)  # Для ограничения скорости запросов

# Начальная личность бота
default_personality = "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Получает последние новости из открытых источников и отправляет пользователю.
    """
    try:
        # Используем RSS-ленту BBC News
        response = requests.get('http://feeds.bbci.co.uk/news/rss.xml')
        soup = BeautifulSoup(response.content, features='xml')
        items = soup.findAll('item')[:5]  # Берем первые 5 новостей

        news_message = "Вот последние новости:\n\n"
        for item in items:
            title = escape_markdown(item.title.text, version=2)
            link = item.link.text
            news_message += f"*{title}*\n[Читать дальше]({link})\n\n"

        await update.message.reply_text(news_message, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка при получении новостей: {str(e)}")
        await update.message.reply_text("Произошла ошибка при получении новостей.")

def escape_markdown_v2(text: str) -> str:
    """
    Экранирование специальных символов для использования с Markdown V2 в Telegram.
    Исключает '*' и '_', чтобы сохранить форматирование.
    """
    escape_chars = r'[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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

def add_emojis_at_end(answer: str) -> str:
    """
    Добавляет случайные эмодзи в конец ответа с некоторой вероятностью.
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
        # Таблица для хранения личностей пользователей
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_personalities (
            user_id BIGINT PRIMARY KEY,
            personality TEXT
        )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Таблицы базы данных успешно созданы или уже существуют")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {str(e)}")

def log_interaction(user_id, user_username, user_message, gpt_reply):
    """
    Логирует взаимодействие пользователя с ботом в базу данных.
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
        logger.error(f"Ошибка при записи в базу данных: {str(e)}")

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

        # Выполняем блокирующий вызов OpenAI API в отдельном потоке
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, openai.ChatCompletion.create,
                                              model="gpt-3.5-turbo",
                                              messages=messages_with_formatting,
                                              max_tokens=700,
                                              temperature=0.5,
                                              n=1)

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
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        logger.error(error_msg)
        return error_msg

def generate_image(prompt: str) -> str:
    """
    Генерирует изображение на основе описания пользователя с помощью OpenAI API.
    """
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

async def send_image(update: Update, context: ContextTypes.DEFAULT_TYPE, image_url: str) -> None:
    """
    Отправляет сгенерированное изображение пользователю.
    """
    try:
        response = requests.get(image_url)
        image = BytesIO(response.content)
        image.name = 'image.png'
        await update.message.reply_photo(photo=image)
    except Exception as e:
        error_msg = f"Ошибка при отправке изображения: {str(e)}"
        logger.error(error_msg)
        await update.message.reply_text(error_msg)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Приветственное сообщение при запуске бота.
    """
    await update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет сообщение с доступными командами и инструкциями.
    """
    help_text = (
        "Доступные команды:\n"
        "/start - Начать общение с ботом\n"
        "/help - Показать это сообщение помощи\n"
        "/enable - Включить бота в этой группе (только для администраторов)\n"
        "/disable - Отключить бота в этой группе (только для администраторов)\n"
        "/image [запрос] - Сгенерировать изображение по описанию\n"
        "/reset - Сбросить историю диалога\n"
        "/set_personality [описание] - Установить личность бота\n"
        "/news - Получить последние новости\n"
    )
    await update.message.reply_text(help_text)

async def is_user_admin(update: Update) -> bool:
    """
    Проверяет, является ли пользователь администратором в чате.
    """
    user_status = await update.effective_chat.get_member(update.effective_user.id)
    return user_status.status in ['administrator', 'creator']

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Включает бота в группе.
    """
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = True
        await update.message.reply_text("Бот включен в этой группе!")
    else:
        await update.message.reply_text("Только администратор может выполнять эту команду.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отключает бота в группе.
    """
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = False
        await update.message.reply_text("Бот отключен в этой группе!")
    else:
        await update.message.reply_text("Только администратор может выполнять эту команду.")

def is_bot_enabled(chat_id: int) -> bool:
    """
    Проверяет, включен ли бот в данном чате.
    """
    return group_status.get(chat_id, False)

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

    # Проверка на упоминание бота в группе
    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            return  # Бот отключен в этой группе
        if f'@{bot_username}' not in message_text:
            return  # Бот не упомянут
        message_text = message_text.replace(f'@{bot_username}', '').strip()

    # Ограничение скорости запросов (Rate Limiting)
    current_time = datetime.now()
    user_requests[user_id] = [req_time for req_time in user_requests[user_id] if (current_time - req_time).seconds < 60]
    if len(user_requests[user_id]) >= 5:
        await update.message.reply_text("Вы слишком часто отправляете запросы. Пожалуйста, подождите немного.")
        return
    user_requests[user_id].append(current_time)

    # Получение личности бота для пользователя
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]

    # Обновление контекста беседы
    conversation_context[user_id].append({"role": "user", "content": message_text})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Храним последние 10 сообщений

    messages = initial_instructions + conversation_context[user_id]

    reply = await ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Экранирование специальных символов для Markdown V2
    escaped_reply = escape_markdown(reply, version=2)

    # Проверяем длину сообщения
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Отправляем ответ
    try:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {str(e)}")
        await update.message.reply_text("Произошла ошибка при отправке сообщения.")

    log_interaction(user_id, user_username, message_text, reply)
    logger.info(f"User ID: {user_id}, Chat ID: {chat_id}, Message ID: {update.message.message_id}")

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Генерирует изображение по описанию пользователя.
    """
    user_input = ' '.join(context.args)
    if not user_input:
        await update.message.reply_text("Пожалуйста, укажите описание изображения после команды /image.")
        return
    image_url = generate_image(user_input)
    if image_url.startswith("Ошибка"):
        await update.message.reply_text(image_url)
    else:
        await send_image(update, context, image_url)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Сбрасывает историю диалога с пользователем.
    """
    user_id = update.message.from_user.id
    conversation_context[user_id] = []
    await update.message.reply_text("История диалога сброшена.")

async def set_personality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Позволяет пользователю установить личность бота.
    """
    personality = ' '.join(context.args)
    if not personality:
        await update.message.reply_text("Пожалуйста, укажите желаемую личность бота после команды /set_personality.")
        return

    user_id = update.message.from_user.id
    user_personalities[user_id] = personality

    # Сохраняем личность в базе данных
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
        logger.error(f"Ошибка при сохранении личности в базу данных: {str(e)}")

    await update.message.reply_text(f"Личность бота установлена: {personality}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает входящие изображения.
    """
    await update.message.reply_text("Спасибо за изображение! Но я пока не умею обрабатывать изображения.")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Получает последние новости и отправляет пользователю.
    """
    try:
        response = requests.get(
            'https://newsapi.org/v2/top-headlines',
            params={'country': 'ru', 'apiKey': NEWS_API_KEY}
        )
        news_data = response.json()
        if news_data['status'] == 'ok':
            articles = news_data['articles'][:5]
            news_message = "Вот последние новости:\n\n"
            for article in articles:
                title = escape_markdown(article['title'], version=2)
                description = escape_markdown(article.get('description', ''), version=2)
                url = article['url']
                news_message += f"*{title}*\n{description}\n[Читать дальше]({url})\n\n"
            await update.message.reply_text(news_message, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
        else:
            await update.message.reply_text("Не удалось получить новости.")
    except Exception as e:
        logger.error(f"Ошибка при получении новостей: {str(e)}")
        await update.message.reply_text("Произошла ошибка при получении новостей.")

def main():
    """
    Запуск бота.
    """
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    init_db()

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("set_personality", set_personality))
    application.add_handler(CommandHandler("news", news_command))

    # Обработчики сообщений и фотографий
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
