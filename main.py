import logging
import os
import re
import random
import requests
import asyncio
from collections import defaultdict
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    # Нужно для планирования работы
    CallbackContext,
    JobQueue
)
from decouple import config
import openai
import psycopg2
from bs4 import BeautifulSoup
from telegram.error import BadRequest, TelegramError

# Вероятность случайного ответа (1 из 60)
RANDOM_RESPONSE_CHANCE = 1 / 60

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
default_personality = (
    "Ты Светлана - миллениал женского пола, который переписывается на русском языке. "
    "Ты военный и политический эксперт, умеешь анализировать новости и сложные ситуации. "
    "Отвечай кратко и понятно, избегай длинных и сложных предложений."
)

def get_db_connection():
    """Устанавливает соединение с базой данных PostgreSQL."""
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def init_db():
    """Инициализирует таблицы базы данных, если они не существуют."""
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
    """Логирует взаимодействие пользователя с ботом в базу данных."""
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
    """Экранирует специальные символы для Markdown V2."""
    escape_chars = r'_[]()~>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def is_bot_enabled(chat_id: int) -> bool:
    """Проверяет, включён ли бот в данной группе."""
    return group_status.get(chat_id, False)

async def ask_chatgpt(messages) -> str:
    """
    Отправляет сообщения к OpenAI API и возвращает ответ.
    (Для модели "o1-mini", чтобы не ломалось, используем только roles user/assistant)
    """
    logger.info(f"Sending messages to OpenAI: {messages}")
    try:
        # Обратите внимание: "o1-mini" — это не публичная OpenAI модель,
        # но раз у вас она раньше работала, оставим так.
        response = await asyncio.wait_for(
            openai.ChatCompletion.acreate(
                model="o1-mini",
                messages=messages,
                max_completion_tokens=5000,
                n=1
            ),
            timeout=120  # Таймаут в 120 секунд
        )
        logger.info(f"Full OpenAI response: {response}")

        if 'choices' in response and len(response.choices) > 0:
            choice = response.choices[0]
            if hasattr(choice, 'message') and 'content' in choice.message:
                answer = choice.message['content'].strip()
                logger.info(f"OpenAI response: {answer}")
                return answer
            else:
                logger.warning("No 'content' in the first choice's message.")
                return None
        else:
            logger.warning("No choices returned in the OpenAI response.")
            return None
    except asyncio.TimeoutError:
        logger.error("Запрос к OpenAI превысил лимит времени")
        return "Извините, я не смог ответить на ваш запрос вовремя. Пожалуйста, попробуйте еще раз."
    except openai.error.InvalidRequestError as e:
        error_msg = f"Ошибка запроса к OpenAI API: {str(e)}"
        logger.error(error_msg)
        return None
    except openai.OpenAIError as e:
        error_msg = f"Ошибка OpenAI API: {str(e)}"
        logger.error(error_msg)
        return None
    except Exception as e:
        logger.error("Неизвестная ошибка при обращении к OpenAI", exc_info=True)
        return None

# --- Обработчики команд ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /start."""
    await update.message.reply_text("Привет! Я твоя виртуальная подруга Светлана. Давай общаться!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду /help."""
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
    """Проверяет, является ли пользователь администратором."""
    try:
        user_status = await update.effective_chat.get_member(update.effective_user.id)
        return user_status.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"Error checking admin status: {str(e)}")
        return False

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает бота в группе (только для администраторов)."""
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = True
        await update.message.reply_text("Бот включён в этой группе!")
    else:
        await update.message.reply_text("Только администраторы могут выполнять эту команду.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает бота в группе (только для администраторов)."""
    chat_id = update.message.chat.id
    if await is_user_admin(update):
        group_status[chat_id] = False
        await update.message.reply_text("Бот отключен в этой группе!")
    else:
        await update.message.reply_text("Только администраторы могут выполнять эту команду.")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает историю диалога пользователя."""
    user_id = update.message.from_user.id
    conversation_context[user_id] = []
    await update.message.reply_text("История диалога сброшена.")

async def set_personality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Устанавливает личность бота для пользователя."""
    personality = ' '.join(context.args)
    if not personality:
        await update.message.reply_text(
            "Пожалуйста, укажите описание личности после команды /set_personality."
        )
        return
    user_id = update.message.from_user.id
    user_personalities[user_id] = personality
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO user_personalities (user_id, personality)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET personality = EXCLUDED.personality
        ''', (user_id, personality))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving personality to database: {str(e)}")
    await update.message.reply_text(f"Личность бота установлена: {personality}")

async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет последние новости из RSS-ленты."""
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

# --- Обработчик текстовых сообщений ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает текстовые сообщения от пользователей."""
    if update.message is None:
        logger.warning("Получено обновление без сообщения. Игнорируем.")
        return

    bot_username = context.bot.username
    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    message_text = update.message.text.strip() if update.message.text else ""

    # Определяем, упомянут ли бот
    is_bot_mentioned = f'@{bot_username}' in message_text
    is_reply = update.message.reply_to_message is not None
    is_reply_to_bot = is_reply and update.message.reply_to_message.from_user.id == context.bot.id

    should_respond = False
    text_to_process = None
    reply_to_message_id = None
    is_random_response = False

    # 1) Упомянут бот
    if is_bot_mentioned and not is_reply:
        should_respond = True
        text_to_process = message_text.replace(f'@{bot_username}', '').strip()
        reply_to_message_id = update.message.message_id

    # 2) Ответ на сообщение бота
    elif is_reply_to_bot:
        should_respond = True
        text_to_process = message_text
        reply_to_message_id = update.message.message_id

    # 3) Сообщение-ответ + упоминание бота
    elif is_reply and is_bot_mentioned:
        original_message = (
            update.message.reply_to_message.text or
            update.message.reply_to_message.caption
        )
        if original_message:
            should_respond = True
            text_to_process = original_message
            reply_to_message_id = update.message.message_id
        else:
            await update.message.reply_text(
                "Извините, я не вижу текста в исходном сообщении, не могу ответить."
            )
            return

    # Случайный ответ
    if random.random() < RANDOM_RESPONSE_CHANCE and not should_respond:
        should_respond = True
        text_to_process = message_text
        reply_to_message_id = update.message.message_id
        is_random_response = True

    # Если это групповой чат, бот должен быть "включён"
    if update.message.chat.type != 'private' and not is_bot_enabled(chat_id):
        return

    if should_respond and text_to_process:
        if is_random_response:
            # Отправляем случайную реакцию (например, аудио)
            random_choice = random.choice(['audio'])
            if random_choice == 'audio':
                random_audio_files = [
                    'inna_voice_2.ogg',
                    'inna_voice_3.ogg',
                    'inna_voice_4.ogg',
                    'inna_voice_5.ogg'
                ]
                chosen_audio_file = random.choice(random_audio_files)
                audio_path = os.path.join(os.path.dirname(__file__), chosen_audio_file)

                if os.path.exists(audio_path):
                    try:
                        with open(audio_path, 'rb') as audio_file:
                            await update.message.reply_voice(
                                voice=audio_file,
                                reply_to_message_id=reply_to_message_id
                            )
                        logger.info(f"Отправлен аудиофайл {chosen_audio_file}")
                        user_username = update.message.from_user.username or ''
                        log_interaction(user_id, user_username, text_to_process,
                                        f"Отправлен аудиофайл {chosen_audio_file}")
                    except TelegramError as e:
                        logger.error(f"Ошибка при отправке аудиофайла: {e}")
                        await update.message.reply_text(
                            "Произошла ошибка при отправке аудиофайла.",
                            reply_to_message_id=reply_to_message_id
                        )
                else:
                    logger.error(f"Аудиофайл {audio_path} не найден.")
                    await update.message.reply_text(
                        "Извините, аудиофайл не найден.",
                        reply_to_message_id=reply_to_message_id
                    )
            else:
                # Случайное текстовое сообщение
                random_text = "А тебе какая разница?"
                try:
                    await update.message.reply_text(
                        random_text,
                        reply_to_message_id=reply_to_message_id
                    )
                    logger.info("Отправлено случайное текстовое сообщение.")
                    user_username = update.message.from_user.username or ''
                    log_interaction(user_id, user_username, text_to_process, random_text)
                except Exception as e:
                    logger.error(f"Ошибка при отправке случайного текста: {e}")
                    await update.message.reply_text(
                        "Произошла ошибка при отправке сообщения.",
                        reply_to_message_id=reply_to_message_id
                    )
            return  # Уже ответили

        # Обычный ответ через OpenAI
        personality = user_personalities.get(user_id, default_personality)

        # Первая реплика — добавляем personality в начало
        if not conversation_context[user_id]:
            combined_user_message = (
                f"{personality}\n"
                "Отвечай кратко и по существу.\n"
                f"Пользователь: {text_to_process}"
            )
        else:
            combined_user_message = text_to_process

        conversation_context[user_id].append({"role": "user", "content": combined_user_message})
        conversation_context[user_id] = conversation_context[user_id][-10:]  # Оставляем последние 10 сообщений

        messages = conversation_context[user_id]

        try:
            reply = await ask_chatgpt(messages)
        except Exception as e:
            logger.error(f"Ошибка при обращении к OpenAI: {e}")
            await update.message.reply_text(
                "Произошла ошибка при обращении к OpenAI. Попробуйте ещё раз."
            )
            return

        if not reply or reply.strip() == "":
            logger.warning("Пустой ответ от OpenAI.")
            await update.message.reply_text(
                "Извините, я не смог сформулировать ответ на ваш запрос. Попробуйте переформулировать."
            )
            return

        conversation_context[user_id].append({"role": "assistant", "content": reply})
        conversation_context[user_id] = conversation_context[user_id][-10:]  # Снова обрезаем

        escaped_reply = escape_markdown_v2(reply)
        max_length = 4096
        if len(escaped_reply) > max_length:
            escaped_reply = escaped_reply[:max_length]

        try:
            await update.message.reply_text(
                escaped_reply,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_to_message_id=reply_to_message_id
            )
        except BadRequest as e:
            logger.error(f"Ошибка Telegram API: {e.message}")
            await update.message.reply_text(
                reply,
                reply_to_message_id=reply_to_message_id
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения в Telegram: {e}")
            await update.message.reply_text(
                "Произошла ошибка при отправке сообщения.",
                reply_to_message_id=reply_to_message_id
            )

        user_username = update.message.from_user.username or ''
        log_interaction(user_id, user_username, text_to_process, reply)


# --- Обработчик ошибок ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит и логирует ошибки, возникающие при обработке обновлений."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text("Произошла ошибка при обработке вашего запроса.")
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")

# -------------------------------------------------------------------
#   ДОБАВЛЯЕМ ФУНКЦИЮ ДЛЯ ПЕРИОДИЧЕСКОЙ РАССЫЛКИ ИСТОРИЙ
# -------------------------------------------------------------------

async def post_regular_story(context: CallbackContext) -> None:
    """
    Функция для рассылки выдуманной истории из жизни «Свекланы»
    каждые 30 минут во все группы, где бот включён.
    Модель "o1-mini" не любит роль "system", поэтому отправим короткий промпт в role="user".
    """
    # Придумываем промпт, который попросит сгенерировать историю
    # про Свеклану (айтишницу, любит выпить, живёт в Питере и т.д.)
    story_prompt = (
        "Ты Свеклана, айтишница, живёшь в Питере, хочешь в Москву, любишь выпить много вина, но потом ничего не помнишь,"
        "влюблена в Андрея, Диму и Пилата, но они живут в Москве, а ты девушка лёгкого поведения. "
        "Расскажи короткую забавную историю, как прошёл твой день (3-5 предложений)."
    )

    messages = [
        {"role": "user", "content": story_prompt}
    ]

    # Запрашиваем историю у OpenAI
    try:
        story = await ask_chatgpt(messages)
        if not story:
            story = "Слушай, сегодня я не в настроении что-то рассказывать."
    except Exception as e:
        logger.error(f"Ошибка при получении истории от OpenAI: {e}")
        story = "Извините, сегодня что-то пошло не так с историей..."

    # Рассылаем историю во все группы, где бот включён
    for chat_id, is_enabled in group_status.items():
        if is_enabled:
            try:
                await context.bot.send_message(chat_id=chat_id, text=story)
            except Exception as e:
                logger.error(f"Не удалось отправить историю в чат {chat_id}: {e}")


def main():
    """Запускает Telegram бота."""
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).read_timeout(60).build()

    # Инициализация базы данных
    init_db()

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("enable", enable_bot))
    application.add_handler(CommandHandler("disable", disable_bot))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("set_personality", set_personality))
    application.add_handler(CommandHandler("news", news_command))

    # Регистрируем обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    # ---------------------
    # Планируем периодическую рассылку историй
    # ---------------------
    job_queue = application.job_queue
    # Каждые 1800 секунд (30 минут) вызываем post_regular_story
    job_queue.run_repeating(
        post_regular_story,
        interval=28800,  # 30 минут
        first=10        # Первый раз через 10 секунд после старта
    )

    logger.info("Starting the bot...")
    application.run_polling()


if __name__ == '__main__':
    main()
