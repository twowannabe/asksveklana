import logging
import re
from collections import defaultdict
from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import psycopg2
from datetime import datetime
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

# Максимальная длина для ответов Telegram (с запасом для форматирования и emoji)
MAX_TELEGRAM_MESSAGE_LENGTH = 3900

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты дружелюбная и игривая девушка, использующая эмодзи в конце сообщений. Отвечай на вопросы, используя этот стиль."}
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
        cleaned_messages.append(msg)
    return cleaned_messages

def generate_user_description(messages: list, user_first_name: str) -> str:
    """Генерирует короткое описание пользователя на основе его сообщений с ограничением на 2-3 предложения."""
    # Объединяем сообщения в один текст
    combined_messages = "\n".join(messages)

    # Формируем сообщения для отправки в ChatGPT
    chat_messages = [
        {"role": "system", "content": "Вы - помощник, который анализирует сообщения пользователей и создает их краткие описания. Описание должно быть лаконичным, содержащим всего 2-3 предложения."},
        {"role": "user", "content": f"Проанализируй следующие сообщения пользователя и опиши его личность и стиль общения кратко, используя не более 2-3 предложений.\n\nСообщения пользователя:\n{combined_messages}\n\nОписание пользователя {user_first_name}:"}
    ]

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Или "gpt-4", если доступно
            messages=chat_messages,
            max_tokens=100,  # Ограничиваем количество токенов для краткости
            temperature=0.7,
        )
        description = response.choices[0].message['content'].strip()

        # Ограничиваем длину текста для Telegram
        if len(description) > MAX_TELEGRAM_MESSAGE_LENGTH:
            description = description[:MAX_TELEGRAM_MESSAGE_LENGTH] + "..."

        return description
    except Exception as e:
        logger.error(f"Ошибка при генерации описания пользователя: {str(e)}")
        return "Извините, не удалось создать описание."

def get_user_messages(user_id: int, limit=50) -> list:
    """Получает последние сообщения пользователя из базы данных."""
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

    # Получаем сообщения пользователя из базы данных
    user_messages = get_user_messages(user_id, limit=50)

    if not user_messages:
        update.message.reply_text("У вас нет сообщений для анализа.")
        return

    # Очищаем сообщения
    user_messages = clean_messages(user_messages)

    # Генерируем описание пользователя на основе его сообщений
    description = generate_user_description(user_messages, user_first_name)

    # Отправляем описание пользователю
    update.message.reply_text(description, parse_mode=ParseMode.MARKDOWN)

def add_emojis_at_end(answer: str) -> str:
    """Добавляет несколько эмодзи в конец ответа."""
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def escape_markdown(text: str) -> str:
    """Экранирует специальные символы для Markdown."""
    return re.sub(r'([_*\[\]()~`>#+-=|{}.!])', r'\\\1', text)

def clean_gpt_response(response: str) -> str:
    """Очищает или заменяет неподдерживаемые символы и корректирует формат."""
    response = response.replace('**', '*')  # Исправление двойных звездочек, если используются
    return response

# Логирование взаимодействия
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

# Функция старта бота
def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

def extract_text_from_message(message: Message) -> str:
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""

def should_respond(update: Update, context: CallbackContext) -> bool:
    message = update.message

    if not message:
        return False

    bot_username = context.bot.username

    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                logger.info(f"Бот упомянут в сообщении: {message.text}")
                return True

    if message.reply_to_message:
        if message.reply_to_message.from_user.username == bot_username:
            logger.info("Сообщение является ответом на сообщение бота")
            return True

        if message.reply_to_message.video:
            logger.info("Сообщение является ответом на видеосообщение")
            return True

    return False

def process_voice_message(voice_message, user_id):
    if voice_message is None:
        logger.error("Голосовое сообщение не найдено")
        return None

    voice_file_path = f"voice_{user_id}.ogg"
    file = voice_message.get_file()
    file.download(voice_file_path)
    logger.info(f"Скачан голосовой файл: {voice_file_path}")

    # Преобразование OGG в WAV для распознавания
    from pydub import AudioSegment
    audio = AudioSegment.from_file(voice_file_path, format="ogg")
    wav_file_path = f"voice_{user_id}.wav"
    audio.export(wav_file_path, format="wav")
    logger.info(f"Конвертирован в WAV: {wav_file_path}")

    # Распознавание речи
    import speech_recognition as sr
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_file_path) as source:
        audio_data = recognizer.record(source)
        try:
            user_message = recognizer.recognize_google(audio_data, language="ru-RU")
            logger.info(f"Расшифрованное сообщение: {user_message}")
            return user_message
        except sr.UnknownValueError:
            logger.error("Не удалось распознать голосовое сообщение.")
            return None
        except sr.RequestError as e:
            logger.error(f"Ошибка при обращении к Google API: {str(e)}")
            return None

def handle_voice(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    if should_respond(update, context):
        user_id = update.message.from_user.id
        voice_message = update.message.voice

        if voice_message:
            user_message = process_voice_message(voice_message, user_id)

            if user_message:
                update.message.text = user_message
                handle_message(update, context, is_voice=True)
            else:
                update.message.reply_text("Извините, я не смогла распознать ваше голосовое сообщение.")
        else:
            logger.error("Голосовое сообщение не найдено")

# Основная функция обработки текстовых сообщений
def handle_message(update: Update, context: CallbackContext, is_voice=False, is_video=False) -> None:
    if not update.message:
        return

    user_id = update.message.from_user.id
    user_username = update.message.from_user.username  # Получаем имя пользователя
    user_message = extract_text_from_message(update.message)

    if not should_respond(update, context):
        return

    conversation_context[user_id].append({"role": "user", "content": user_message})

    messages = initial_instructions + conversation_context[user_id]

    try:
        # Запрашиваем ответ у ChatGPT
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=400,  # Ограничение на использование токенов
            temperature=0.7
        )

        reply = response.choices[0].message['content'].strip()

        # Очищаем и экранируем ответ
        reply = clean_gpt_response(reply)
        reply = escape_markdown(reply)
        reply = add_emojis_at_end(reply)

        # Отправляем сообщение пользователю
        update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

        # Логируем ответ
        conversation_context[user_id].append({"role": "assistant", "content": reply})
        log_interaction(user_id, user_username, user_message, reply)

    except Exception as e:
        logger.error(f"Ошибка при запросе к OpenAI: {str(e)}")
        update.message.reply_text("Извините, произошла ошибка при обработке запроса.")

# Основная функция для запуска бота
def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("describe_me", describe_user))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
