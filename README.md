# AskSveklana

AskSveklana is an interactive Telegram bot that serves as a military and political analyst, offering concise insights and analysis of current events. The bot is built using Python, OpenAI's GPT models, and Telegram Bot API. It also supports personalized bot personalities and user-specific interactions.

## Features
- Customizable bot personality for individual users.
- Ability to generate concise, informative responses to user queries.
- Image generation using OpenAI's image API.
- News fetching and summarizing feature (news source URL configurable via `.env` file).
- Ability to enable or disable bot functionality in group chats.
- Logging user interactions to PostgreSQL database.

## Requirements
- Python 3.11+
- A PostgreSQL database
- OpenAI API key
- Telegram bot token
- Libraries: `python-telegram-bot`, `openai`, `requests`, `beautifulsoup4`, `psycopg2`, `decouple`

## Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/twowannabe/asksveklana.git
   cd asksveklana
   ```

2. Create a virtual environment and activate it:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create a `.env` file with your configuration:
   ```
   TELEGRAM_TOKEN=your_telegram_token
   OPENAI_API_KEY=your_openai_api_key
   DB_HOST=your_db_host
   DB_PORT=your_db_port
   DB_NAME=your_db_name
   DB_USER=your_db_user
   DB_PASSWORD=your_db_password
   NEWS_RSS_URL=your_news_rss_url
   ```

## Usage
To start the bot:
```bash
python main.py
```

Make sure that the PostgreSQL database is running and accessible with the provided credentials.

## Systemd Service Setup (Optional)
You can set up a systemd service to run AskSveklana in the background:

1. Create a systemd service file (`/etc/systemd/system/asksvetlana.service`):
   ```
   [Unit]
   Description=AskSveklana Telegram Bot
   After=network.target

   [Service]
   User=your_user
   WorkingDirectory=/path/to/asksveklana
   ExecStart=/path/to/venv/bin/python /path/to/asksveklana/main.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```

2. Start and enable the service:
   ```bash
   sudo systemctl start asksveklana.service
   sudo systemctl enable asksveklana.service
   ```

## License
This project is licensed under a custom license. You may use, copy, and modify the code for personal, non-commercial purposes only, but you may not distribute or publish it. Proper attribution to the original author, Volodymyr Kozlov, is required. See the [LICENSE](LICENSE.md) file for details.

## Contributing
Feel free to submit issues or pull requests for features, bug fixes, or other improvements. Contributions are welcome, but note that modifications should not be published or distributed.


## Acknowledgements
- [OpenAI](https://openai.com) for providing the GPT models.
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) for the easy-to-use Telegram bot API wrapper.
- All contributors and open-source maintainers.
