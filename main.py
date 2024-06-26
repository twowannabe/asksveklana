import logging
import re
import speech_recognition as sr
from collections import defaultdict, Counter
from decouple import config
from telegram import Update, ParseMode, Message, Voice
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from pydub import AudioSegment
import openai
import os

# Загрузка конфигурации из .env файла
TELEGRAM_TOKEN = config('TELEGRAM_TOKEN')
OPENAI_API_KEY = config('OPENAI_API_KEY')
# Установка ключа API для OpenAI
openai.api_key = OPENAI_API_KEY

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levellevel)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальная переменная для хранения контекста бесед и счетчиков повторений
conversation_context = defaultdict(list)
question_counters = defaultdict(Counter)

# Начальная инструкция для ChatGPT
initial_instructions = [
    {"role": "system", "content": "Ты - дружелюбная женщина-бот, которая любит заигрывать с пользователями. Отвечай на вопросы, используя нежный и игривый тон."}
]

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
    update.message.reply_text('Привет! Я - Лиза, твоя виртуальная подруга. Давай пообщаемся!')

def extract_text_from_message(message: Message) -> str:
    """Извлекает текст из сообщения, если текст доступен."""
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""

# Функция для распознавания речи из голосового сообщения
def recognize_speech_from_voice(voice: Voice, file_path: str) -> str:
    voice_file = voice.get_file()
    voice_file.download(file_path)
    audio = AudioSegment.from_ogg(file_path)
    audio.export(file_path, format="wav")

    recognizer = sr.Recognizer()
    with sr.AudioFile(file_path) as source:
        audio_data = recognizer.record(source)
        try:
            text = recognizer.recognize_google(audio_data, language="ru-RU")
            return text
        except sr.UnknownValueError:
            return "Не удалось распознать речь"
        except sr.RequestError as e:
            logger.error(f"Ошибка запроса к сервису распознавания речи: {str(e)}")
            return "Ошибка при распознавании речи"

# Обработчик текстовых сообщений
def handle_message(update: Update, context: CallbackContext) -> None:
    message = update.message
    chat_type = message.chat.type
    user_id = message.from_user.id
    bot_username = f"@{context.bot.username}"

    logger.info(f"Получено сообщение: {message.text} в чате типа {chat_type}")
    user_message = extract_text_from_message(message)

    if chat_type in ['group', 'supergroup']:
        # Проверяем, есть ли упоминание бота или это ответ на сообщение бота
        if (user_message and re.search(bot_username, user_message)) or (message.reply_to_message and message.reply_to_message.from_user.username == context.bot.username):
            process_user_message(update, context, user_message, user_id)
        else:
            logger.info("Упоминание бота или ответ на сообщение бота не обнаружено")
    else:
        # Обработка личных сообщений
        process_user_message(update, context, user_message, user_id)

def process_user_message(update: Update, context: CallbackContext, user_message: str, user_id: int) -> None:
    # Обновляем счетчик повторений вопроса
    question_counters[user_id][user_message] += 1

    # Проверяем, не повторяет ли пользователь один и тот же вопрос более трех раз
    if question_counters[user_id][user_message] > 3:
        user_message += " (Давайте попробуем обсудить что-то новое!)"
        question_counters[user_id][user_message] = 0  # Сбросить счетчик после изменения темы

    # Сохраняем сообщение в контексте беседы
    conversation_context[user_id].append({"role": "user", "content": user_message})

    # Отправляем всю историю сообщений в ChatGPT и получаем ответ
    messages = initial_instructions + conversation_context[user_id]
    reply = ask_chatgpt(messages)
    logger.info(f"Отправка ответа: {reply}")

    # Сохраняем ответ бота в контексте беседы
    conversation_context[user_id].append({"role": "assistant", "content": reply})

    try:
        update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа: {str(e)}")
        update.message.reply_text(reply)

# Обработчик голосовых сообщений
def handle_voice_message(update: Update, context: CallbackContext) -> None:
    message = update.message
    user_id = message.from_user.id
    file_path = f"voice_{user_id}.wav"
    bot_username = f"@{context.bot.username}"

    logger.info("Получено голосовое сообщение")

    # Распознаем текст из голосового сообщения
    user_message = recognize_speech_from_voice(message.voice, file_path)
    logger.info(f"Распознанное голосовое сообщение: {user_message}")

    # Проверяем, является ли это ответом на сообщение бота или упоминанием бота
    if (message.reply_to_message and message.reply_to_message.from_user.username == context.bot.username) or (message.caption and re.search(bot_username, message.caption)):
        process_user_message(update, context, user_message, user_id)

    # Удаляем временный файл
    os.remove(file_path)

# Основная функция для запуска бота
def main() -> None:
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.reply, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.photo, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.voice, handle_voice_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
