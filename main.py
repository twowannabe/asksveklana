import logging
import random
import re
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from decouple import config
import openai
import asyncpg
from bs4 import BeautifulSoup
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import aiohttp

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

# Global variables
conversation_context = defaultdict(list)  # Conversation contexts
group_status = defaultdict(bool)  # Bot activation status in groups
user_personalities = defaultdict(str)  # User-specific bot personalities

# Default bot personality
default_personality = "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации."

async def init_db():
    """
    Initializes the database and necessary tables.
    """
    try:
        conn = await asyncpg.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        await conn.execute('''
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
        await conn.execute('''
        CREATE TABLE IF NOT EXISTS user_personalities (
            user_id BIGINT PRIMARY KEY,
            personality TEXT
        )
        ''')
        await conn.close()
        logger.info("Database tables created or already exist")
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")

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

def escape_markdown_v2(text):
    """
    Escapes special characters for MarkdownV2 formatting.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def log_interaction(user_id, user_username, user_message, gpt_reply):
    """
    Logs the user's interaction with the bot in the database.
    """
    try:
        conn = await asyncpg.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        timestamp = datetime.now()
        await conn.execute('''
        INSERT INTO askgbt_logs (user_id, user_username, user_message, gpt_reply, timestamp)
        VALUES ($1, $2, $3, $4, $5)
        ''', user_id, user_username, user_message, gpt_reply, timestamp)
        await conn.close()
    except Exception as e:
        logger.error(f"Error writing to database: {str(e)}")

async def ask_chatgpt(messages) -> str:
    """
    Sends messages to OpenAI ChatGPT and returns the response.
    """
    logger.info(f"Sending messages to ChatGPT: {messages}")
    try:
        # Add system message to control response length
        messages_with_formatting = [
            {"role": "system", "content": "Пожалуйста, делай ответы краткими и не более 3500 символов."}
        ] + messages

        # Check messages for empty content
        for message in messages_with_formatting:
            if not message.get("content"):
                logger.error(f"Empty content in message: {message}")
                return "Произошла ошибка: одно из сообщений было пустым."

        # Use asynchronous OpenAI API
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=messages_with_formatting,
            max_tokens=700,
            temperature=0.2,
            n=1
        )

        answer = response.choices[0].message['content'].strip()
        logger.info(f"ChatGPT response: {answer}")

        answer = add_emojis_at_end(answer)

        # Ensure the message does not exceed Telegram's maximum length
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
        logger.error("An error occurred while accessing ChatGPT", exc_info=True)
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        return error_msg

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sends a welcome message when the bot is started.
    """
    await update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sends a message with available commands and instructions.
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

async def generate_image(prompt: str) -> str:
    """
    Generates an image based on the user's description using OpenAI's API.
    """
    logger.info(f"Requesting image generation with prompt: {prompt}")
    try:
        response = await openai.Image.acreate(
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
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    image = BytesIO(await resp.read())
                    image.name = 'image.png'
                    await update.message.reply_photo(photo=image)
                else:
                    error_msg = f"Ошибка при получении изображения: статус {resp.status}"
                    logger.error(error_msg)
                    await update.message.reply_text(error_msg)
    except Exception as e:
        error_msg = f"Ошибка при отправке изображения: {str(e)}"
        logger.error(error_msg)
        await update.message.reply_text(error_msg)

async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Generates an image based on the user's description.
    """
    user_input = ' '.join(context.args)
    if not user_input:
        await update.message.reply_text("Пожалуйста, укажите описание изображения после команды /image.")
        return

    await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)

    image_url = await generate_image(user_input)
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
        conn = await asyncpg.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        await conn.execute('''
        INSERT INTO user_personalities (user_id, personality)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET personality = $2
        ''', user_id, personality)
        await conn.close()
    except Exception as e:
        logger.error(f"Error saving personality to database: {str(e)}")

    await update.message.reply_text(f"Личность бота установлена: {personality}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = context.bot.id

    logger.info(f"Received new message: {update.message}")

    if update.message is None:
        logger.info("Received empty message, ignoring.")
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    bot_username = context.bot.username

    message_text = update.message.text.strip() if update.message.text else update.message.caption
    message_text = message_text.strip() if message_text else ""

    logger.info(f"Message from user {user_id} in chat {chat_id}: {message_text}")

    should_respond = False
    reply_to_message_id = None
    text_to_process = None

    if update.message.chat.type != 'private':  # Group chat
        if not is_bot_enabled(chat_id):
            logger.info(f"Bot is disabled in chat {chat_id}. group_status={group_status}")
            return

        if f'@{bot_username}' in message_text:
            should_respond = True
            text_to_process = message_text.replace(f'@{bot_username}', '').strip()
            reply_to_message_id = update.message.message_id
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            should_respond = True
            text_to_process = message_text
            reply_to_message_id = update.message.message_id
    else:  # Private chat
        should_respond = True
        text_to_process = message_text

    if not should_respond or not text_to_process:
        logger.info(f"Bot decided not to respond. should_respond={should_respond}, text_to_process={text_to_process}")
        return

    logger.info(f"Processing text: {text_to_process}")

    personality = user_personalities.get(user_id, default_personality)

    initial_instructions = [
        {"role": "system", "content": personality},
        {"role": "system", "content": "Пожалуйста, всегда отвечай на вопросы, которые адресованы тебе напрямую, независимо от темы."}
    ]

    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]

    messages = initial_instructions + conversation_context[user_id]

    try:
        await update.message.chat.send_action(action=ChatAction.TYPING)
        reply = await ask_chatgpt(messages)
    except Exception as e:
        logger.error(f"Error accessing OpenAI: {e}")
        await update.message.reply_text("Произошла ошибка при обращении к OpenAI. Пожалуйста, повторите запрос.")
        return

    escaped_reply = escape_markdown_v2(reply)

    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    if reply_to_message_id:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=reply_to_message_id)
    else:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)

    await log_interaction(user_id, user_username, text_to_process, reply)

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Retrieves the latest news from the RSS feed and sends it to the user.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://lenta.ru/rss/news') as response:
                if response.status == 200:
                    content = await response.text()
                    soup = BeautifulSoup(content, features='xml')
                    items = soup.findAll('item')[:5]

                    news_message = "Вот последние новости:\n\n"
                    for item in items:
                        title = escape_markdown_v2(item.title.text)
                        link = item.link.text
                        news_message += f"*{title}*\n[Читать дальше]({link})\n\n"

                    await update.message.reply_text(
                        news_message,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True
                    )
                else:
                    await update.message.reply_text("Не удалось получить новости.")
    except Exception as e:
        logger.error(f"Error retrieving news: {str(e)}")
        await update.message.reply_text("Произошла ошибка при получении новостей.")

async def main():
    """
    Starts the bot.
    """
    await init_db()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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
    await application.run_polling()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
