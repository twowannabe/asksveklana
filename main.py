import re
import logging
from collections import defaultdict
from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai
import speech_recognition as sr
from pydub import AudioSegment
import moviepy.editor as mp
import os
import requests
from io import BytesIO
import random

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройка логирования для других библиотек
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')

# Установка ключа API для OpenAI
openai.api_key = OPENAI_API_KEY

# Глобальная переменная для хранения контекста бесед
conversation_context = defaultdict(list)

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты Свеклана - миллениал женского пола, который переписывается на русском языке. Ты дружелюбная и игривая девушка, использующая эмодзи в конце сообщений. Отвечай на вопросы, используя этот стиль."}
]

def add_emojis_at_end(answer: str) -> str:
    """Добавляет несколько эмодзи в конец ответа."""
    emojis = ['😊', '😉', '😄', '🎉', '✨', '👍', '😂', '😍', '😎', '🤔', '🥳', '😇', '🙌', '🌟']
    if random.choice([True, False]):
        return answer

    num_emojis = random.randint(1, 3)
    chosen_emojis = ''.join(random.choices(emojis, k=num_emojis))

    return f"{answer} {chosen_emojis}"

def clean_drawing_prompt(prompt: str) -> str:
    """Удаляет ключевые слова, такие как 'нарисуй', из текста запроса."""
    drawing_keywords = ["нарисуй", "создай", "изобрази", "сгенерируй", "покажи картинку", "сделай изображение"]
    for keyword in drawing_keywords:
        if keyword in prompt.lower():
            prompt = prompt.lower().replace(keyword, "").strip()
    return prompt

def is_drawing_request(message: str) -> bool:
    """Определяет, является ли сообщение запросом на рисование или показ изображения."""
    drawing_keywords = ["нарисуй", "создай", "изобрази", "сгенерируй", "покажи картинку", "сделай изображение", "покажи как выглядит", "покажи как они выглядели"]
    message = message.lower()
    return any(keyword in message for keyword in drawing_keywords)

def send_image(update: Update, context: CallbackContext, image_url: str) -> None:
    try:
        response = requests.get(image_url)
        image = BytesIO(response.content)
        image.name = 'image.png'  # Даем имя файлу, чтобы Telegram его распознал
        update.message.reply_photo(photo=image)
    except Exception as e:
        logger.error(f"Ошибка при отправке изображения: {str(e)}")
        update.message.reply_text(f"Ошибка при отправке изображения: {str(e)}")

def ask_chatgpt(messages) -> str:
    """Отправка сообщений в ChatGPT и получение ответа."""
    logger.info(f"Отправка запросов в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Или "gpt-3.5-turbo"
            messages=messages
        )
        answer = response.choices[0].message['content'].strip()
        logger.info(f"Ответ ChatGPT: {answer}")
        clean_answer = answer.replace(')', '').replace('(', '')
        return add_emojis_at_end(clean_answer)
    except Exception as e:
        logger.error(f"Ошибка при обращении к ChatGPT: {str(e)}")
        return f"Ошибка при обращении к ChatGPT: {str(e)}"

def generate_image(prompt: str) -> str:
    """Генерирует изображение по заданному текстовому описанию."""
    logger.info(f"Отправка запроса на генерацию изображения: {prompt}")
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        logger.info(f"Ссылка на сгенерированное изображение: {image_url}")
        return image_url
    except Exception as e:
        logger.error(f"Ошибка при создании изображения: {str(e)}")
        return f"Ошибка при создании изображения: {str(e)}"

def start(update: Update, context: CallbackContext) -> None:
    """Обработчик команды /start."""
    logger.info("Команда /start вызвана.")
    update.message.reply_text('Привет! Я - Свеклана, твоя виртуальная подруга. Давай пообщаемся! 😊')

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
    bot_username = context.bot.username

    if not message:
        return False

    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                return True

    if message.reply_to_message:
        if message.reply_to_message.from_user.username == bot_username:
            return True

    if message.reply_to_message and message.reply_to_message.voice:
        if message.entities:
            for entity in message.entities:
                if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                    return True

    return False

def get_user_identifier(update: Update) -> str:
    """Возвращает username, если он есть, иначе user_id."""
    user = update.message.from_user
    return user.username if user.username else str(user.id)

def process_voice_message(voice_message, user_id):
    """Обрабатывает голосовое сообщение и преобразует его в текст без сохранения на диск."""
    logger.info(f"Обработка голосового сообщения от пользователя {user_id}.")
    file_id = voice_message.file_id
    new_file = voice_message.get_file()

    voice_file = BytesIO()
    new_file.download(out=voice_file)
    voice_file.seek(0)

    sound = AudioSegment.from_ogg(voice_file)
    wav_io = BytesIO()
    sound.export(wav_io, format="wav")
    wav_io.seek(0)

    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_io) as source:
        audio_data = recognizer.record(source)
        try:
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            logger.info(f"Распознанный текст из голосового сообщения: {text}")
            return text
        except sr.UnknownValueError:
            logger.warning("Не удалось распознать голосовое сообщение.")
            return "Извините, я не смог распознать голосовое сообщение."
        except sr.RequestError:
            logger.error("Ошибка при распознавании голоса.")
            return "Ошибка при распознавании голоса."

def process_video_message(video_message, user_id):
    """Обрабатывает видеосообщение и извлекает его аудиодорожку для распознавания речи."""
    logger.info(f"Обработка видеосообщения от пользователя {user_id}.")

    new_file = video_message.get_file()

    video_file = BytesIO()
    new_file.download(out=video_file)
    video_file.seek(0)

    try:
        video = mp.VideoFileClip(video_file)
        audio_io = BytesIO()
        video.audio.write_audiofile(audio_io, codec='pcm_s16le')
        audio_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(audio_io) as source:
            audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language="ru-RU")
                logger.info(f"Распознанный текст из видео: {text}")
                return text
            except sr.UnknownValueError:
                logger.warning("Не удалось распознать аудио из видео.")
                return "Извините, я не смог распознать аудио из видео."
            except sr.RequestError:
                logger.error("Ошибка при распознавании аудио из видео.")
                return "Ошибка при распознавании аудио из видео."
    except Exception as e:
        logger.error(f"Ошибка при обработке видео: {str(e)}")
        return f"Ошибка при обработке видео: {str(e)}"

def handle_video(update: Update, context: CallbackContext) -> None:
    user_identifier = get_user_identifier(update)

    # Проверяем, это видео или видеозаметка (кружочек)
    if update.message.video_note:
        video_message = update.message.video_note
        logger.info(f"Обработка видеозаметки (кружочек) от пользователя {user_identifier}.")
    elif update.message.video:
        video_message = update.message.video
        logger.info(f"Обработка видеосообщения от пользователя {user_identifier}.")
    else:
        logger.info(f"Сообщение не является видео или кружочком.")
        return

    # Проверяем, упомянули ли бота в ответе на видео/кружочек
    if not should_respond(update, context):
        return

    user_message = process_video_message(video_message, user_identifier)
    if not user_message:
        return

    conversation_context[user_identifier].append({"role": "user", "content": user_message})

    messages = initial_instructions + conversation_context[user_identifier]
    reply = ask_chatgpt(messages)

    conversation_context[user_identifier].append({"role": "assistant", "content": reply})
    update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

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

    if is_drawing_request(user_message):
        prompt = clean_drawing_prompt(user_message)
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

def main():
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.video | Filters.video_note, handle_video))  # Обработка видео и кружочков

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
