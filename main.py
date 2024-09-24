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
# NEWS_API_KEY is no longer needed

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

def generate_image(prompt: str) -> str:
    """
    Generates an image based on the user's description using OpenAI's API.
    """
    logger.info(f"Requesting image generation with prompt: {prompt}")
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        logger.info(f"Received image URL: {image_url}")
        return image_url
    except Exception as e:
        error_msg = f"Ошибка при создании изображения: {str(e)}"
        logger.error(error_msg)
        return error_msg

async def send_image(update: Update, context: ContextTypes.DEFAULT_TYPE, image_url: str) -> None:
    """
    Sends the generated image to the user.
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
    Sends a welcome message when the bot is started.
    """
    await update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sends a message with available commands and instructions.
    """
    # Check if the command is directed at this bot in a group chat
    message_text = update.message.text
    bot_username = context.bot.username

    if update.message.chat.type != 'private':
        if not re.match(rf'^/help(@{bot_username})?$', message_text.strip()):
            return  # Not directed to this bot

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
    Checks if the user is an administrator in the chat.
    """
    user_status = await update.effective_chat.get_member(update.effective_user.id)
    return user_status.status in ['administrator', 'creator']

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Enables the bot in the group.
    """
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = True
        await update.message.reply_text("Бот включен в этой группе!")
    else:
        await update.message.reply_text("Только администратор может выполнять эту команду.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Disables the bot in the group.
    """
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = False
        await update.message.reply_text("Бот отключен в этой группе!")
    else:
        await update.message.reply_text("Только администратор может выполнять эту команду.")

def is_bot_enabled(chat_id: int) -> bool:
    """
    Checks if the bot is enabled in the given chat.
    """
    return group_status.get(chat_id, False)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles incoming text messages and generates a reply using OpenAI.
    """
    if update.message is None:
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    message_text = update.message.text.strip()
    bot_username = context.bot.username

    # Check for bot mention or reply in group chats
    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            return  # Bot is disabled in this group

        # Condition if the bot is mentioned
        if f'@{bot_username}' in message_text:
            message_text = message_text.replace(f'@{bot_username}', '').strip()
        # Condition if the message is a reply to the bot's message
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
            pass  # Proceed without modifying message_text
        else:
            return  # Bot is neither mentioned nor replied to

    # Rate limiting
    current_time = datetime.now()
    user_requests[user_id] = [req_time for req_time in user_requests[user_id] if (current_time - req_time).seconds < 60]
    if len(user_requests[user_id]) >= 5:
        await update.message.reply_text("Вы слишком часто отправляете запросы. Пожалуйста, подождите немного.")
        return
    user_requests[user_id].append(current_time)

    # Get bot personality for the user
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]

    # Update conversation context
    conversation_context[user_id].append({"role": "user", "content": message_text})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Keep last 10 messages

    messages = initial_instructions + conversation_context[user_id]

    reply = await ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Escape special characters for Markdown V2
    escaped_reply = escape_markdown(reply, version=2)

    # Ensure the message does not exceed Telegram's limit
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Send the reply
    try:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error sending message: {str(e)}")
        await update.message.reply_text("Произошла ошибка при отправке сообщения.")

    log_interaction(user_id, user_username, message_text, reply)
    logger.info(f"User ID: {user_id}, Chat ID: {chat_id}, Message ID: {update.message.message_id}")

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Generates an image based on the user's description.
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
    Resets the conversation history with the user.
    """
    user_id = update.message.from_user.id
    conversation_context[user_id] = []
    await update.message.reply_text("История диалога сброшена.")

async def set_personality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Allows the user to set the bot's personality.
    """
    personality = ' '.join(context.args)
    if not personality:
        await update.message.reply_text("Пожалуйста, укажите желаемую личность бота после команды /set_personality.")
        return

    user_id = update.message.from_user.id
    user_personalities[user_id] = personality

    # Save personality to the database
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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles incoming images.
    """
    await update.message.reply_text("Спасибо за изображение! Но я пока не умею обрабатывать изображения.")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Retrieves the latest news from the BBC RSS feed and sends it to the user.
    """
    try:
        # Use the BBC News RSS feed
        response = requests.get('http://feeds.bbci.co.uk/news/rss.xml')
        response.raise_for_status()  # Check for request errors

        # Parse the XML content
        soup = BeautifulSoup(response.content, features='xml')
        items = soup.findAll('item')[:5]  # Get the first 5 news items

        news_message = "Вот последние новости от BBC:\n\n"
        for item in items:
            title = escape_markdown(item.title.text, version=2)
            link = item.link.text
            news_message += f"*{title}*\n[Читать дальше]({link})\n\n"

        # Send the news message
        await update.message.reply_text(
            news_message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error retrieving news: {str(e)}")
        await update.message.reply_text("Произошла ошибка при получении новостей.")

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
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Run the bot
    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
