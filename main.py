import logging
import re
from collections import defaultdict, Counter
from decouple import config
from telegram import Update, ParseMode, Message
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import openai

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

# Функция для отправки сообщения в ChatGPT и получения ответа
def ask_chatgpt(messages) -> str:
    logger.info(f"Отправка сообщений в ChatGPT: {messages}")
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
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
    update.message.reply_text('Привет! Я бот, который передает ваши сообщения в ChatGPT.')

def extract_text_from_message(message: Message) -> str:
    """Извлекает текст из сообщения, если текст доступен."""
    if message.text:
        return message.text.strip()
    if message.caption:
        return message.caption.strip()
    return ""

# Обработчик текстовых сообщений
def handle_message(update: Update, context: CallbackContext) -> None:
    message = update.message
    chat_type = message.chat.type
    user_id = message.from_user.id
    logger.info(f"Получено сообщение: {message.text} в чате типа {chat_type}")
    bot_username = f"@{context.bot.username}"
    user_message = extract_text_from_message(message)

    if chat_type in ['group', 'supergroup']:
        # Проверяем, есть ли упоминание бота или это ответ на сообщение бота
        if (user_message and re.search(bot_username, user_message)) or (message.reply_to_message and message.reply_to_message.from_user.username == context.bot.username):
            logger.info(f"Получено сообщение с упоминанием или ответом на сообщение бота: {user_message}")

            # Проверяем, на какое сообщение отвечают
            if message.reply_to_message:
                original_message = extract_text_from_message(message.reply_to_message)
                if original_message:
                    conversation_context[user_id].append({"role": "user", "content": original_message})

            # Обновляем счетчик повторений вопроса
            question_counters[user_id][user_message] += 1

            # Проверяем, не повторяет ли пользователь один и тот же вопрос более трех раз
            if question_counters[user_id][user_message] > 3:
                user_message += " (Давайте попробуем обсудить что-то новое!)"
                question_counters[user_id][user_message] = 0  # Сбросить счетчик после изменения темы

            # Сохраняем сообщение в контексте беседы
            conversation_context[user_id].append({"role": "user", "content": user_message})

            # Отправляем всю историю сообщений в ChatGPT и получаем ответ
            reply = ask_chatgpt(conversation_context[user_id])
            logger.info(f"Отправка ответа: {reply}")

            # Сохраняем ответ бота в контексте беседы
            conversation_context[user_id].append({"role": "assistant", "content": reply})

            try:
                update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Ошибка при отправке ответа: {str(e)}")
                update.message.reply_text(reply)
        else:
            logger.info("Упоминание бота или ответ на сообщение бота не обнаружено")
    else:
        # Обработка личных сообщений
        logger.info(f"Получено личное сообщение: {user_message}")

        # Обновляем счетчик повторений вопроса
        question_counters[user_id][user_message] += 1

        # Проверяем, не повторяет ли пользователь один и тот же вопрос более трех раз
        if question_counters[user_id][user_message] > 3:
            user_message += " (Давайте попробуем обсудить что-то новое!)"
            question_counters[user_id][user_message] = 0  # Сбросить счетчик после изменения темы

        # Сохраняем сообщение в контексте беседы
        conversation_context[user_id].append({"role": "user", "content": user_message})

        # Отправляем всю историю сообщений в ChatGPT и получаем ответ
        reply = ask_chatgpt(conversation_context[user_id])
        logger.info(f"Отправка ответа: {reply}")

        # Сохраняем ответ бота в контексте беседы
        conversation_context[user_id].append({"role": "assistant", "content": reply})

        try:
            update.message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Ошибка при отправке ответа: {str(e)}")
            update.message.reply_text(reply)

# Основная функция для запуска бота
def main() -> None:
    updater = Updater(TELEGRAM_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.reply, handle_message))
    dispatcher.add_handler(MessageHandler(Filters.photo, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
