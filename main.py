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
default_personality = "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def escape_markdown_v2(text):
    escape_chars = r'_*[]()~>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

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
        error_msg = "Exceeded OpenAI API request limit. Please try again later."
        logger.error(error_msg)
        return error_msg
    except openai.error.InvalidRequestError as e:
        error_msg = f"OpenAI API request error: {str(e)}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        logger.error("Error contacting ChatGPT", exc_info=True)
        error_msg = f"Error contacting ChatGPT: {str(e)}"
        return error_msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hello! I am your virtual friend Svetlana. Let's chat!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Available commands:\n"
        "/start - Start a conversation\n"
        "/help - Show this help message\n"
        "/enable - Enable bot in this group (admins only)\n"
        "/disable - Disable bot in this group (admins only)\n"
        "/image [description] - Generate an image\n"
        "/reset - Reset conversation history\n"
        "/set_personality [description] - Set bot personality\n"
        "/news - Get the latest news\n"
    )
    await update.message.reply_text(help_text)

async def is_user_admin(update: Update) -> bool:
    user_status = await update.effective_chat.get_member(update.effective_user.id)
    return user_status.status in ['administrator', 'creator']

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = True
        await update.message.reply_text("Bot enabled in this group!")
    else:
        await update.message.reply_text("Only admins can execute this command.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = False
        await update.message.reply_text("Bot disabled in this group!")
    else:
        await update.message.reply_text("Only admins can execute this command.")

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
        await update.message.reply_text("Please provide a description after the /image command.")
        return
    image_url = generate_image(user_input)
    if image_url.startswith("Error"):
        await update.message.reply_text(image_url)
    else:
        await send_image(update, context, image_url)

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    conversation_context[user_id] = []
    await update.message.reply_text("Conversation history reset.")

async def set_personality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    personality = ' '.join(context.args)
    if not personality:
        await update.message.reply_text("Please provide a personality description after the /set_personality command.")
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
    await update.message.reply_text(f"Bot personality set to: {personality}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверка, что update.message не равен None
    if update.message is None:
        logger.warning("Received an update without a message. Ignoring.")
        return

    bot_id = context.bot.id
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    bot_username = context.bot.username
    message_text = update.message.text.strip() if update.message.text else update.message.caption
    message_text = message_text.strip() if message_text else ""

    should_respond = False
    reply_to_message_id = None
    text_to_process = None

    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            return
        if f'@{bot_username}' in message_text:
            should_respond = True
            text_to_process = message_text
            reply_to_message_id = update.message.message_id
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            should_respond = True
            text_to_process = message_text
            reply_to_message_id = update.message.message_id
    else:
        should_respond = True
        text_to_process = message_text
        reply_to_message_id = update.message.message_id

    if not should_respond or not text_to_process:
        return

    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [
        {"role": "system", "content": personality},
        {"role": "system", "content": "Always answer questions addressed to you."}
    ]
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]
    messages = initial_instructions + conversation_context[user_id]

    try:
        reply = await ask_chatgpt(messages)
    except Exception as e:
        logger.error(f"Error contacting OpenAI: {e}")
        await update.message.reply_text("An error occurred while contacting OpenAI. Please try again.")
        return

    escaped_reply = escape_markdown_v2(reply)
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    if reply_to_message_id:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=reply_to_message_id)
    else:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)

    log_interaction(user_id, user_username, text_to_process, reply)

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        response = requests.get('https://www.pravda.com.ua/rus/rss/view_news/')
        response.raise_for_status()
        soup = BeautifulSoup(response.content, features='xml')
        items = soup.findAll('item')[:5]

        news_message = "Latest news from pravda.com.ua:\n\n"
        for item in items:
            title = escape_markdown_v2(item.title.text)
            link = item.link.text
            news_message += f"*{title}*\n[Read more]({link})\n\n"

        await update.message.reply_text(
            news_message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Error retrieving news: {str(e)}")
        await update.message.reply_text("An error occurred while fetching news.")

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
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
