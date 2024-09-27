async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверяем, что сообщение содержит только текст
    if update.message is None or update.message.text is None:
        logger.info("Received a non-text message, ignoring it.")
        return

    chat_id = update.message.chat.id
    user_id = update.message.from_user.id
    user_username = update.message.from_user.username
    message_text = update.message.text.strip()
    bot_username = context.bot.username

    # Получение ID бота
    bot_me = await context.bot.get_me()
    bot_id = bot_me.id

    # Логирование информации о сообщении
    logger.info(f"Received text message from user {user_id} in chat {chat_id}: {message_text}")

    # Variable to store the text to be processed by the bot
    text_to_process = message_text

    # Check conditions in group chats
    if update.message.chat.type != 'private':
        if not is_bot_enabled(chat_id):
            logger.info(f"Bot is disabled in chat {chat_id}")
            return  # Bot is disabled in this group

        # Handle forwarded messages (пересланные сообщения)
        if update.message.forward_date:
            text_to_process = update.message.text
            logger.info(f"Processing forwarded message text: {text_to_process}")

        # Check if the bot is mentioned by @username
        elif f'@{bot_username}' in message_text:
            if update.message.reply_to_message and update.message.reply_to_message.text:
                text_to_process = update.message.reply_to_message.text
                logger.info(f"Processing replied message text: {text_to_process}")
            else:
                text_to_process = message_text.replace(f'@{bot_username}', '').strip()
                logger.info(f"Processing direct mention text: {text_to_process}")

        # If the message is a reply to a message sent by the bot itself
        elif update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_id:
            if message_text:
                text_to_process = message_text
                logger.info(f"Processing reply to bot's message: {text_to_process}")
            else:
                await update.message.reply_text("Пожалуйста, отправьте текст вашего сообщения.")
                logger.warning(f"Empty reply from user {user_id} to bot's message")
                return
        else:
            logger.info("Message does not mention the bot or reply to bot's message. Ignoring.")
            return  # The bot should not respond to this message
    else:
        # In personal messages, use the text as is
        text_to_process = message_text
        logger.info(f"Processing private message: {text_to_process}")

    # Check if there's text to process
    if not text_to_process:
        await update.message.reply_text("Похоже, вы отправили пустое сообщение. Пожалуйста, отправьте текст.")
        logger.error(f"Received empty message from user {user_id}")
        return

    # Rate limiting logic
    current_time = datetime.now()
    user_requests[user_id] = [
        req_time for req_time in user_requests[user_id]
        if (current_time - req_time).seconds < 60
    ]
    if len(user_requests[user_id]) >= 5:
        await update.message.reply_text("Вы слишком часто отправляете запросы. Пожалуйста, подождите немного.")
        logger.warning(f"User {user_id} exceeded rate limit.")
        return
    user_requests[user_id].append(current_time)

    # Get bot's personality for the user
    personality = user_personalities.get(user_id, default_personality)
    initial_instructions = [{"role": "system", "content": personality}]

    # Update the conversation context
    conversation_context[user_id].append({"role": "user", "content": text_to_process})
    conversation_context[user_id] = conversation_context[user_id][-10:]  # Keep only the last 10 messages

    messages = initial_instructions + conversation_context[user_id]

    # Check for empty messages before sending to ChatGPT
    for message in messages:
        if not message.get("content"):
            logger.error(f"Empty content in conversation context: {message}")
            await update.message.reply_text("Произошла ошибка при обработке контекста. Пожалуйста, попробуйте снова.")
            return

    # Generate response
    reply = await ask_chatgpt(messages)

    if not isinstance(reply, str):
        logger.error("Ответ от ChatGPT не является строкой.")
        await update.message.reply_text("Произошла ошибка при обработке ответа от ChatGPT.")
        return

    # Escape special characters for Markdown V2
    escaped_reply = escape_markdown(reply, version=2)

    # Ensure message doesn't exceed Telegram's character limit
    max_length = 4096
    if len(escaped_reply) > max_length:
        escaped_reply = escaped_reply[:max_length]

    # Send the response
    try:
        await update.message.reply_text(escaped_reply, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info(f"Sent reply to user {user_id}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения: {str(e)}")
        await update.message.reply_text("Произошла ошибка при отправке сообщения.")
