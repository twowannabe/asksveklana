import logging
import re
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime
from io import BytesIO

from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import speech_recognition as sr
from pydub import AudioSegment
import moviepy.editor as mp
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
            "Ты Свеклана — молодая девушка, которая переписывается на русском языке. "
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

    # Определяем, нужно ли добавлять эмодзи в это сообщение
    if random.choice([True, False]):
        return answer

    # Определяем количество эмодзи
    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def format_markdown(answer: str) -> str:
    """Форматирует текст ответа, заменяя заголовки на markdown-разметку и экранируя специальные символы."""
    # Заменяем заголовки '#### ' на '**' для жирного текста
    answer = re.sub(r'^#### (.+)$', r'*\1*', answer, flags=re.MULTILINE)
    # Заменяем заголовки '### ' на '*' для курсива
    answer = re.sub(r'^### (.+)$', r'_\1_', answer, flags=re.MULTILINE)
    # Убираем лишние пустые строки
    answer = re.sub(r'\n{2,}', '\n', answer)
    # Экранируем специальные символы для Markdown V2
    answer = escape_markdown(answer, version=2)
    return answer

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

def clean_drawing_prompt(prompt: str) -> str:
    """Удаляет ключевые слова, такие как 'нарисуй', из текста запроса."""
    drawing_keywords = ["нарисуй", "создай", "изобрази", "сгенерируй", "покажи картинку", "сделай изображение"]
    for keyword in drawing_keywords:
        if keyword in prompt.lower():
            prompt = prompt.lower().replace(keyword, "").strip()
    return prompt

def is_drawing_request(message: str) -> bool:
    """Определяет, является ли сообщение запросом на рисование или показ изображения."""
    drawing_keywords = [
        "нарисуй", "создай", "изобрази", "сгенерируй",
        "покажи картинку", "сделай изображение",
        "покажи как выглядит", "покажи как они выглядели"
    ]
    message = message.lower()
    return any(keyword in message for keyword in drawing_keywords)

def send_image(update: Update, context: CallbackContext, image_url: str) -> None:
    try:
        response = requests.get(image_url)
        image = BytesIO(response.content)
        image.name = 'image.png'  # Даем имя файлу, чтобы Telegram его распознал
        update.message.reply_photo(photo=image)
    except Exception as e:
        error_msg = f"Ошибка при отправке изображения: {str(e)}"
        logger.error(error_msg)
        update.message.reply_text(error_msg)

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
            model="gpt-4o",
            messages=messages,
            max_tokens=250,  # Увеличено с 150 до 250
            temperature=0.7,   # Снижено с 0.5 до 0.3
            presence_penalty=0.5,
            frequency_penalty=0.5
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


def generate_joke() -> str:
    """Генерирует анекдот про слона."""
    joke_prompt = [
        {
            "role": "system",
            "content": (
                "Ты — бот, который придумывает смешные анекдоты. "
                "Придумай короткий необидный анекдот про аклоголичку Инну."
            )
        }
    ]
    return ask_chatgpt(joke_prompt)

# Функция для генерации изображений
def generate_image(prompt: str) -> str:
    """Генерирует изображение по заданному текстовому описанию."""
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

    # 5. Если ответили на видео сообщение и упомянули бота
    if message.reply_to_message and message.reply_to_message.video:
        if message.entities:
            for entity in message.entities:
                if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                    logger.info(f"Бот упомянут в ответе на видео сообщение: {message.text}")
                    return True

    return False

def process_voice_message(voice_message, user_id):
    """Обрабатывает голосовое сообщение и возвращает его текст"""
    voice_file_path = f"voice_{user_id}.ogg"
    file = voice_message.get_file()
    file.download(voice_file_path)
    logger.info(f"Скачан голосовой файл: {voice_file_path}")

    # Конвертируем OGG в WAV для распознавания
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
            # Удаляем временные файлы
            os.remove(voice_file_path)
            os.remove(wav_file_path)

def process_video_message(video_message, user_id):
    """Обрабатывает видео сообщение и возвращает текст из него"""
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

    if should_respond(update, context):
        user_id = update.message.from_user.id
        user_message = process_voice_message(update.message.voice, user_id)

        if user_message:
            # Проверяем, если ответили на чьё-то голосовое сообщение и упомянули бота
            if update.message.reply_to_message:
                update.message.text = user_message
                handle_message(update, context, is_voice=True)
            else:
                update.message.reply_text(user_message)

def handle_video(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    if should_respond(update, context):
        user_id = update.message.from_user.id
        logger.info(f"Обработка видео сообщения от пользователя {user_id}")
        user_message = process_video_message(update.message.video, user_id)

        if user_message:
            logger.info(f"Расшифрованное видео сообщение: {user_message}")
            # Проверяем, если ответили на чьё-то видео сообщение и упомянули бота
            if update.message.reply_to_message:
                update.message.text = user_message
                handle_message(update, context, is_video=True)
            else:
                update.message.reply_text(user_message)

# Обработчик текстовых сообщений
def handle_message(update: Update, context: CallbackContext, is_voice=False, is_video=False) -> None:
    if not update.message:
        return

    user_id = update.message.from_user.id
    user_message = extract_text_from_message(update.message)

    if is_voice:
        user_message = process_voice_message(update.message.reply_to_message.voice, user_id)
        if not user_message:
            return

    if is_video:
        user_message = process_video_message(update.message.reply_to_message.video, user_id)
        if not user_message:
            return

    # Проверка на наличие слова "геи" для генерации шутки
    if "геи" in user_message.lower():
        joke = generate_joke()
        update.message.reply_text(joke)
        return

    # Если сообщение содержит запрос на рисование
    if is_drawing_request(user_message):
        # Извлекаем текст после ключевого слова для создания изображения
        prompt = clean_drawing_prompt(user_message)
        image_url = generate_image(prompt)
        send_image(update, context, image_url)
        return

    if not is_voice and not is_video and not should_respond(update, context):
        return

    # Если сообщение является ответом и содержит упоминание бота, обрабатываем оригинальное сообщение
    if update.message.reply_to_message and not is_voice and not is_video:
        original_message = extract_text_from_message(update.message.reply_to_message)
        if not original_message and update.message.reply_to_message.voice:
            original_message = process_voice_message(update.message.reply_to_message.voice, user_id)
        if not original_message and update.message.reply_to_message.video:
            original_message = process_video_message(update.message.reply_to_message.video, user_id)
        if not original_message:
            return
        user_message = f"{original_message} {user_message}"

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
    # Создаем апдейтера и диспетчера
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    # Инициализируем базу данных
    init_db()

    # Регистрируем обработчики
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice))
    dispatcher.add_handler(MessageHandler(Filters.video, handle_video))

    # Запуск бота
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
