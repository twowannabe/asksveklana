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
import requests
from io import BytesIO
import random

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

    # Определяем, нужно ли добавлять эмодзи в это сообщение
    if random.choice([True, False]):
        return answer

    # Определяем количество эмодзи
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
        error_msg = f"Ошибка при отправке изображения: {str(e)}"
        update.message.reply_text(error_msg)

# Функция для отправки сообщения в ChatGPT и получения ответа
def ask_chatgpt(messages) -> str:
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",  # Или "gpt-3.5-turbo"
            messages=messages
        )
        answer = response.choices[0].message['content'].strip()

        # Удаляем только скобочки перед добавлением эмодзи
        clean_answer = answer.replace(')', '').replace('(', '')

        return add_emojis_at_end(clean_answer)
    except Exception as e:
        error_msg = f"Ошибка при обращении к ChatGPT: {str(e)}"
        return error_msg

# Функция для генерации изображений
def generate_image(prompt: str) -> str:
    """Генерирует изображение по заданному текстовому описанию."""
    try:
        response = openai.Image.create(
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = response['data'][0]['url']
        return image_url
    except Exception as e:
        error_msg = f"Ошибка при создании изображения: {str(e)}"
        return error_msg

# Обработчик команды /start
def start(update: Update, context: CallbackContext) -> None:
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

    if not message:
        return False

    bot_username = context.bot.username

    # 1. Если упомянули никнейм бота
    if message.entities:
        for entity in message.entities:
            if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                return True

    # 2. Если ответили на сообщение бота
    if message.reply_to_message:
        if message.reply_to_message.from_user.username == bot_username:
            return True

    # 3. Если упомянули бота и ответили на чьё-то сообщение
    if message.reply_to_message:
        if message.entities:
            for entity in message.entities:
                if entity.type == 'mention' and message.text[entity.offset:entity.offset + entity.length] == f"@{bot_username}":
                    return True

    return False

# Функция для обработки голосовых сообщений
def process_voice_message(voice_message, user_id):
    """Обрабатывает голосовое сообщение и преобразует его в текст."""
    file_id = voice_message.file_id
    new_file = voice_message.get_file()

    # Скачиваем файл
    file_path = f"voice_{user_id}.ogg"
    new_file.download(file_path)

    # Преобразование OGG в WAV для распознавания
    sound = AudioSegment.from_ogg(file_path)
    wav_path = f"voice_{user_id}.wav"
    sound.export(wav_path, format="wav")

    # Распознавание речи
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
        try:
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            os.remove(file_path)  # Удаляем временные файлы
            os.remove(wav_path)
            return text
        except sr.UnknownValueError:
            return "Извините, я не смог распознать голосовое сообщение."
        except sr.RequestError:
            return "Ошибка при распознавании голоса."

# Функция для обработки видеосообщений
def process_video_message(video_message, user_id):
    """Обрабатывает видеосообщение и извлекает его аудиодорожку для распознавания речи."""
    file_id = video_message.file_id
    new_file = video_message.get_file()

    # Скачиваем видеофайл
    file_path = f"video_{user_id}.mp4"
    new_file.download(file_path)

    # Извлечение аудио из видео
    try:
        video = mp.VideoFileClip(file_path)
        audio_path = f"audio_from_video_{user_id}.wav"
        video.audio.write_audiofile(audio_path)

        # Распознавание речи с аудиодорожки
        recognizer = sr.Recognizer()
        with sr.AudioFile(audio_path) as source:
            audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language="ru-RU")
                os.remove(file_path)  # Удаляем временные файлы
                os.remove(audio_path)
                return text
            except sr.UnknownValueError:
                return "Извините, я не смог распознать аудио из видео."
            except sr.RequestError:
                return "Ошибка при распознавании аудио из видео."
    except Exception as e:
        return f"Ошибка при обработке видео: {str(e)}"

# Обработчик голосовых сообщений
def handle_voice(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    voice_message = update.message.voice

    # Обрабатываем голосовое сообщение и получаем текст
    user_message = process_voice_message(voice_message, user_id)
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

# Обработчик видеосообщений
def handle_video(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    video_message = update.message.video

    # Обрабатываем видеосообщение и получаем текст
    user_message = process_video_message(video_message, user_id)
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

    # Если сообщение содержит запрос на рисование
    if is_drawing_request(user_message):
        # Здесь можно извлечь текст после ключевого слова для создания изображения
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

def main():
    # Создаем апдейтера и диспетчера
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

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
