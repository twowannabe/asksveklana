import logging
import re
from collections import defaultdict, Counter
from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import speech_recognition as sr
from pydub import AudioSegment
import os
import sqlite3
from datetime import datetime

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# Установка ключа API для OpenAI
openai.api_key = OPENAI_API_KEY

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальная переменная для хранения контекста бесед и счетчиков повторений
conversation_context = defaultdict(list)
question_counters = defaultdict(Counter)

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты - дружелюбная женщина-бот, которая любит заигрывать с пользователями (иногда). Отвечай на вопросы, используя нежный и игривый тон, но не всегда."}
]

# Создание базы данных для логирования
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

def get_sqlite_connection():
    conn = getattr(threading.current_thread(), "sqlite_connection", None)
    if conn is None:
        conn = connect('chatgpt_logs.db')
        setattr(threading.current_thread(), "sqlite_connection", conn)
    return conn.cursor()

# Updated log_interaction function to use thread-local cursor
def log_interaction(user_id, user_message, gpt_reply):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor = get_sqlite_connection()
    try:
        cursor.execute('''
        INSERT INTO logs (user_id, user_message, gpt_reply, timestamp)
        VALUES (?, ?, ?, ?)
        ''', (user_id, user_message, gpt_reply, timestamp))
        cursor.connection.commit()
    except Error as e:
        print(f"Error logging interaction: {e}")

# Функция для отправки сообщения в ChatGPT и получения ответа
def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=messages
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")
        return answer
    except Exception as e:
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        logger.error(error_msg)
        return error_msg

# Обработчик команды /start
def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text('Привет! Я - Джессика, твоя виртуальная подруга. Давай пообщаемся!')

def extract_text_from_message(message: Message) -> str:
    """Извлекает текст из сообщения, если текст доступен."""
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""

def should_respond(update: Update, context: CallbackContext) -> bool:
    """Проверяет, должен ли бот отвечать на сообщение."""
    message = update.message

    if not message:
        return False

    bot_username = context.bot.username

    # 1. Если упомянули никнейм бота
    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                logger.info(f"Бот упомянут в сообщении: {message.text}")
                return True

    # 2. Если ответили на сообщение бота
    if message.reply_to_message:
        if message.reply_to_message.from_user.username == bot_username:
            logger.info("Сообщение является ответом на сообщение бота")
            return True

    # 3. Если упомянули бота и ответили на чьё-то сообщение
    if message.reply_to_message:
        if message.entities:
            for entity in message.entities:
                if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                    logger.info(f"Бот упомянут в ответе на сообщение: {message.text}")
                    return True

    # 4. Если ответили на голосовое сообщение и упомянули бота
    if message.reply_to_message and message.reply_to_message.voice:
        if message.entities:
            for entity in message.entities:
                if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                    logger.info(f"Бот упомянут в ответе на голосовое сообщение: {message.text}")
                    return True

    return False

def process_voice_message(voice_message, user_id):
    """Обрабатывает голосовое сообщение и возвращает его текст"""
    voice_file_path = f"voice_{user_id}.ogg"
    voice_message.download(voice_file_path)

    # Конвертируем OGG в WAV для распознавания
    audio = AudioSegment.from_file(voice_file_path, format="ogg")
    wav_file_path = f"voice_{user_id}.wav"
    audio.export(wav_file_path, format="wav")

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_file_path) as source:
        audio_data = recognizer.record(source)
        try:
            user_message = recognizer.recognize_google(audio_data, language="ru-RU")
            logger.info(f"Расшифрованное сообщение: {user_message}")
            return user_message
        except sr.UnknownValueError:
            logger.error("Извините, я не смогла распознать голосовое сообщение.")
            return None
        except sr.RequestError as e:
            logger.error(f"Ошибка при обращении к сервису распознавания речи: {str(e)}")
            return None
        finally:
            # Удаляем временные файлы
            os.remove(voice_file_path)
            os.remove(wav_file_path)

def handle_voice(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    user_id = update.message.from_user.id
    user_message = process_voice_message(update.message.voice, user_id)

    if user_message:
        # Проверяем, если ответили на чьё-то голосовое сообщение и упомянули бота
        if update.message.reply_to_message and should_respond(update, context):
            update.message.text = user_message
            handle_message(update, context, is_voice=True)
        else:
            update.message.reply_text(user_message)

# Обработчик текстовых сообщений
def handle_message(update: Update, context: CallbackContext, is_voice=False) -> None:
    if not update.message:
        return

    if not should_respond(update, context) and not is_voice:
        return

    user_id = update.message.from_user.id
    user_message = extract_text_from_message(update.message)

    if is_voice:
        user_message = process_voice_message(update.message.reply_to_message.voice, user_id)
        if not user_message:
            return

    # Проверка на повторяющиеся вопросы
    question_counters[user_id][user_message] += 1
    if question_counters[user_id][user_message] > 3:
        update.message.reply_text("Вы уже спрашивали об этом несколько раз. Пожалуйста, задайте другой вопрос.")
        return

    # Если сообщение является ответом и содержит упоминание бота, обрабатываем оригинальное сообщение
    if update.message.reply_to_message and not is_voice:
        user_message = extract_text_from_message(update.message.reply_to_message)
        if not user_message:
            return

    # Добавляем сообщение пользователя в контекст
    conversation_context[user_id].append({"role": "user", "content": user_message})

    # Подготавливаем сообщения для отправки в ChatGPT
    messages = initial_instructions + conversation_context[user_id]

    # Получаем ответ от ChatGPT
    reply = ask_chatgpt(messages)

    # Добавляем ответ ChatGPT в контекст
    conversation_context[user_id].append({"role": "assistant", "content": reply})

    # Отправляем ответ пользователю
    update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # Логирование взаимодействия
    log_interaction(user_id, user_message, reply)

def main():
    # Create a SQLite connection for the main thread
    conn = sqlite3.connect('chatgpt_logs.db')  # Use sqlite3.connect() to create a connection
    cursor = conn.cursor()

    # Function to stop and close SQLite connection
    def stop_and_close_sqlite_connection():
        try:
            conn.close()
        except Error as e:
            print(f"Error closing SQLite connection: {e}")

    # Ensure the SQLite connection is closed properly on program exit
    import atexit
    atexit.register(stop_and_close_sqlite_connection)

    # Создаем апдейтера и диспетчера
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    # Регистрируем обработчики
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice))

    # Запуск бота
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
