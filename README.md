# 🛒 Shop Bot

Telegram shop bot built with aiogram 3 — product catalog, cart and checkout with inline keyboards.

## Run locally
```bash
cp .env.example .env     # put your token in .env
pip install -r requirements.txt
python bot.py
```

## Deploy on Railway
1. Push this repo to GitHub.
2. Railway → New Project → Deploy from GitHub repo → select it.
3. Service → **Variables** → add `BOT_TOKEN` = your token from @BotFather.
4. Railway redeploys automatically. Check **Deployments → Logs** for `Bot started as @...`.

The bot uses long polling, so no domain or port is needed. Run only one instance per token.
