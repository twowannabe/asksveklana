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

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты отвечаешь кратко и по делу, избегая лишних слов."}
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
    # Объединяем сообщения в один текст
    combined_messages = "\n".join(messages)

    # Формируем сообщения для отправки в ChatGPT
    chat_messages = [
        {"role": "system", "content": "Вы - помощник, который анализирует сообщения пользователей и создает их описания. Используйте токсичный шуточный тон в ответах."},
        {"role": "user", "content": f"Проанализируй следующие сообщения пользователя и опиши его личность, интересы и стиль общения.\n\nСообщения пользователя:\n{combined_messages}\n\nОписание пользователя {user_first_name}:"}
    ]

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Или "gpt-3.5-turbo", если у вас нет доступа к GPT-4
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
    update.message.reply_text(description)

def add_emojis_at_end(answer: str) -> str:
    """Добавляет несколько эмодзи в конец ответа."""
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']

    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

# Создание базы данных для логирования
# Создание базы данных для логирования
# Создание базы данных для логирования
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

# Очистка запросов для генерации изображений
def clean_drawing_prompt(prompt: str) -> str:
    drawing_keywords = ["нарисуй", "создай", "изобрази", "сгенерируй", "покажи картинку", "сделай изображение"]
    for keyword in drawing_keywords:
        if keyword in prompt.lower():
            prompt = prompt.lower().replace(keyword, "").strip()
    return prompt

def is_drawing_request(message: str) -> bool:
    drawing_keywords = ["нарисуй", "создай", "изобрази", "сгенерируй", "покажи картинку", "сделай изображение", "покажи как выглядит", "покажи как они выглядели"]
    message = message.lower()
    return any(keyword in message for keyword in drawing_keywords)

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
        # logger.info("Взаимодействие успешно записано в базу данных")
    except Exception as e:
        logger.error(f"Ошибка при записи в базу данных: {str(e)}")

# Запросы к ChatGPT
def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Или другой выбранный тобой модельный тип
            messages=messages,
            max_tokens=100,  # Ограничиваем максимальное количество токенов для краткости
            temperature=0.5,  # Низкая температура для краткости и прямоты
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

def generate_joke() -> str:
    joke_prompt = [
        {"role": "system", "content": "Ты - бот, который придумывает смешные анекдоты. Придумай короткий необидный анекдот про фембоя и лезбиянку Нину."}
    ]
    return ask_chatgpt(joke_prompt)

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
    keywords = [ "свеклана", "помоги", "вопрос", "ответь", "почему"]

    # Проверяем наличие упоминания бота
    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length].lower() == f"@{bot_username}".lower():
                logger.info(f"Бот упомянут в сообщении: {message.text}")
                return True

    # Проверяем, упомянуты ли ключевые слова
    if any(keyword in message.text.lower() for keyword in keywords):
        logger.info(f"Сообщение содержит ключевое слово: {message.text}")
        return True

    # Проверяем, является ли сообщение ответом на сообщение бота
    if message.reply_to_message and message.reply_to_message.from_user.username == bot_username:
        logger.info("Сообщение является ответом на сообщение бота")
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

    audio = AudioSegment.from_file(voice_file_path, format="ogg")
    wav_file_path = f"voice_{user_id}.wav"
    audio.export(wav_file_path, format="wav")
    logger.info(f"Конвертирован в WAV: {wav_file_path}")

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
            os.remove(voice_file_path)
            os.remove(wav_file_path)

def process_video_message(video_message, user_id):
    logger.info(f"Начало обработки видео сообщения от пользователя {user_id}")
    video_file_path = f"video_{user_id}.mp4"
    file = video_message.get_file()
    file.download(video_file_path)
    logger.info(f"Видео файл скачан: {video_file_path}")

    # Извлекаем аудио из видео
    audio_file_path = f"audio_{user_id}.wav"
    video = mp.VideoFileClip(video_file_path)
    video.audio.write_audiofile(audio_file_path)
    logger.info(f"Аудио извлечено из видео и сохранено как: {audio_file_path}")

    recognizer = sr.Recognizer()
    with sr.AudioFile(audio_file_path) as source:
        audio_data = recognizer.record(source)
        try:
            user_message = recognizer.recognize_google(audio_data, language="ru-RU")
            logger.info(f"Расшифрованное сообщение из видео: {user_message}")
            return user_message
        except sr.UnknownValueError:
            logger.error("Извините, я не смогла распознать аудио из видео.")
            return None
        except sr.RequestError as e:
            logger.error(f"Ошибка при обращении к сервису распознавания речи: {str(e)}")
            return None
        finally:
            # Удаляем временные файлы
            os.remove(video_file_path)
            os.remove(audio_file_path)

def handle_voice(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    # Проверяем, нужно ли боту отвечать
    if should_respond(update, context):
        user_id = update.message.from_user.id
        voice_message = update.message.voice  # Получаем голосовое сообщение от пользователя

        if voice_message:  # Проверяем, существует ли голосовое сообщение
            # Обрабатываем голосовое сообщение
            user_message = process_voice_message(voice_message, user_id)

            if user_message:
                # Обрабатываем ответное сообщение, если оно есть
                if update.message.reply_to_message:
                    if update.message.reply_to_message.voice:
                        # Если ответ на голосовое сообщение, обрабатываем его
                        original_voice_message = update.message.reply_to_message.voice
                        original_text = process_voice_message(original_voice_message, user_id)
                        if original_text:
                            user_message = f"{original_text} {user_message}"

                    elif update.message.reply_to_message.text:
                        # Если ответ на текстовое сообщение, добавляем его к пользовательскому
                        original_text = update.message.reply_to_message.text
                        user_message = f"{original_text} {user_message}"

                # Имитация текстового сообщения, чтобы бот мог ответить как на текстовое сообщение
                update.message.text = user_message
                handle_message(update, context, is_voice=True)
            else:
                update.message.reply_text("Извините, я не смогла распознать ваше голосовое сообщение.")
        else:
            logger.error("Голосовое сообщение не найдено")

def handle_video(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    if should_respond(update, context):
        user_id = update.message.from_user.id
        logger.info(f"Обработка видео сообщения от пользователя {user_id}")
        user_message = process_video_message(update.message.video, user_id)

        if user_message:
            logger.info(f"Расшифрованное видео сообщение: {user_message}")
            if update.message.reply_to_message:
                update.message.text = user_message
                handle_message(update, context, is_video=True)
            else:
                update.message.reply_text(user_message)

# Функция обработки сообщений
def handle_message(update: Update, context: CallbackContext, is_voice=False, is_video=False) -> None:
    if not update.message:
        return

    user_id = update.message.from_user.id
    user_username = update.message.from_user.username  # Получаем имя пользователя
    user_message = extract_text_from_message(update.message)

    if is_voice:
        user_message = process_voice_message(update.message.reply_to_message.voice, user_id)
        if not user_message:
            return

    if is_video:
        user_message = process_video_message(update.message.reply_to_message.video, user_id)
        if not user_message:
            return

    if "геи" in user_message.lower():
        joke = generate_joke()
        update.message.reply_text(joke)
        return

    if is_drawing_request(user_message):
        prompt = user_message
        image_url = generate_image(prompt)
        send_image(update, context, image_url)
        return

    if not is_voice and not is_video and not should_respond(update, context):
        return

    if update.message.reply_to_message and not is_voice and not is_video:
        original_message = extract_text_from_message(update.message.reply_to_message)
        if not original_message and update.message.reply_to_message.voice:
            original_message = process_voice_message(update.message.reply_to_message.voice, user_id)
        if not original_message and update.message.reply_to_message.video:
            original_message = process_video_message(update.message.reply_to_message.video, user_id)
        if not original_message:
            return
        user_message = f"{original_message} {user_message}"

    conversation_context[user_id].append({"role": "user", "content": user_message})

    messages = initial_instructions + conversation_context[user_id]

    reply = ask_chatgpt(messages)

    conversation_context[user_id].append({"role": "assistant", "content": reply})

    update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

    # Логирование взаимодействия с учётом имени пользователя
    log_interaction(user_id, user_username, user_message, reply)

# Основная функция для запуска бота
def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    init_db()

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("describe_me", describe_user))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice))
    dispatcher.add_handler(MessageHandler(Filters.video, handle_video))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
