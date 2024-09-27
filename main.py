async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = context.bot.id
    bot_username = context.bot.username

    # Проверка, что сообщение текстовое
    if update.message is None or update.message.text is None:
        logger.info("Получено не текстовое сообщение, игнорируем его.")
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    message_text = update.message.text.strip()

    logger.info(f"Получено текстовое сообщение от пользователя {user_id} в чате {chat_id}: {message_text}")

    # Основной текст, который будем обрабатывать
    text_to_process = message_text

    # Обработка групповых чатов (если не в личном чате)
    if update.message.chat.type != 'private':
        # Проверка активации бота в чате
        if not is_bot_enabled(chat_id):
            logger.info(f"Бот отключен в чате {chat_id}")
            return

        # Если сообщение содержит упоминание бота
        if f'@{bot_username}' in message_text:
            # Если это ответ на другое сообщение
            if update.message.reply_to_message:
                # Обрабатываем текст того сообщения, на которое идет ссылка
                text_to_process = update.message.reply_to_message.text
            elif update.message.entities:
                # Если в сообщении есть упоминания других сообщений, проверяем их
                for entity in update.message.entities:
                    if entity.type == 'mention' and entity.user and entity.user.username == bot_username:
                        # Если упомянут наш бот, смотрим, на что ссылается
                        text_to_process = update.message.reply_to_message.text if update.message.reply_to_message else message_text.replace(f'@{bot_username}', '').strip()

        # Если сообщение является ответом на сообщение бота
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            text_to_process = message_text

        # В других случаях бот не отвечает
        else:
            return
    else:
        # Если это личный чат, просто обрабатываем текст сообщения
        text_to_process = message_text

    # Проверяем, что у нас есть текст для обработки
    if not text_to_process:
        await update.message.reply_text("Похоже, вы отправили пустое сообщение. Пожалуйста, отправьте текст.")
        return

    # Формируем контекст разговора для пользователя
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Ограничение на последние 10 сообщений

    # Отправка запроса к OpenAI и получение ответа
    messages = initial_instructions + conversation_context[user_id]
    reply = await ask_chatgpt(messages)

    # Экранирование текста Markdown и проверка на длину
    escaped_reply = escape_markdown(reply, version=2)
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Ответ на исходное сообщение с использованием reply_to_message_id
    await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2, reply_to_message_id=update.message.message_id)
