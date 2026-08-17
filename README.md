# Kyrgyzstan Residents Search — Telegram Bot

A standalone Telegram bot that searches the [API service](https://github.com/zusty-debug/Kyrgyz-data) and replies in JSON.

## What it does

- `/start`, `/help` — welcome + examples
- `/search <query>` — search, results in JSON code block, paginated with **Next 5 →** buttons
- `/quota`, `/myinfo` — usage info
- `/grant_admin <user_id>` — owner-only
- `/revoke_admin <user_id>` — owner-only
- `/grant_premium <user_id>`, `/revoke_premium <user_id>` — owner + admins
- `/users` — list all users by role (owner + admins only)

Every response ends with `Developer - @xzusty`.

## Roles

| Role | Quota | Who |
|---|---|---|
| 👑 Owner | Unlimited | You (the Telegram ID configured in `OWNER_TELEGRAM_ID`) |
| 🛡️ Admin | Unlimited | Promoted by owner |
| ⭐ Premium | Unlimited | Granted by owner or admin |
| 👤 Regular | 5/day, resets midnight UTC | Everyone else |

## Architecture

```
Telegram user
   │ (HTTPS to api.telegram.org)
   ▼
Your bot (Render web service, free tier)
   ├── bot_users table on shared Postgres (login/role persistence)
   ├── bot_daily_usage table (5/day quota tracking)
   └── HTTPS GET to https://kyrgyz-data-api.onrender.com/api/search
        (using API_KEY header — search-only master-less key)
```

The bot shares the SAME Postgres instance (`kyrgyz-data-db`) as the API. It only uses that DB for its own two tables (`bot_users`, `bot_daily_usage`). It never reads or writes the people data.

## Deploy on Render (free tier)

This is a separate repo from the API. Two services total:

1. **API service**: deployed from `zusty-debug/Kyrgyz-data` repo (existing)
2. **Bot service**: deployed from this repo (new)

### Step 1 — Push the repo (already done if you got this README)

### Step 2 — Connect Render to this repo

1. Open <https://dashboard.render.com> on desktop or phone.
2. Top right → **+ New** → **Blueprint**.
3. Pick **GitHub** as the source.
4. Search for and select `zusty-debug/Kyrgyz-tg-bot`.
5. Render reads `render.yaml`, names the service `kyrgyz-tg-bot`, plan Free, runtime Docker, etc.
6. Tap **Apply**.

### Step 3 — Configure the env vars (the part only YOU can do)

After Render creates the service, go to its **Environment** tab and paste these values:

| Key | Value |
|---|---|
| `DATABASE_URL` | Go to your existing `kyrgyz-data-db` service in Render → top-right **Connect** → **Internal Connection String** → paste |
| `API_KEY` | The `mk_…` key you created via the API's `/admin` (search-only). This is NOT your master key. |
| `TELEGRAM_BOT_TOKEN` | The token `@BotFather` gave you, e.g. `1234567890:AAHxxx...` |
| `OWNER_TELEGRAM_ID` | Your personal Telegram user ID (you'll get this by messaging `@userinfobot` on Telegram) |

The other env vars (`API_URL`, `DEV_HANDLE`, `DAILY_QUOTA`, `PER_PAGE`) are pre-filled by the Blueprint — no action needed.

Hit **Save Changes** → Render restarts the worker.

### Step 4 — Verify

1. Open the bot's **Logs** tab. Within ~2 minutes you should see:
   ```
   Database tables ready (bot_users, bot_daily_usage).
   Bot ready. Owner=<your id>. Calling API: https://kyrgyz-data-api.onrender.com
   Starting bot polling...
   ```
2. On Telegram, open your bot, tap **Start**, type `/myinfo`. You should see `👑 Owner`.

### Step 5 — Keep-alive (recommended for free tier)

The bot service spins down after 15 minutes idle. To stay 24/7, set up a free UptimeRobot monitor:

1. Sign up at <https://uptimerobot.com>.
2. Add monitor: HTTP(s), URL `https://kyrgyz-tg-bot.onrender.com/health`, interval 5 minutes.

The bot URL (you'll see on Render after deploy) responds with `bot ok` to any GET request.

## Local testing

```bash
export DATABASE_URL=sqlite:///bot_state.db
export TELEGRAM_BOT_TOKEN=...
export API_KEY=...
export API_URL=http://localhost:8000
export OWNER_TELEGRAM_ID=...
pip install -r requirements.txt
python -m bot.bot
```

## Files

- `bot/bot.py` — all handler logic + DB models + Telegram setup
- `scripts/start.sh` — tiny HTTP healthcheck + bot polling loop
- `Dockerfile` — Python 3.11-slim container
- `render.yaml` — Render Blueprint
- `requirements.txt` — pinned deps

## Credits

Made by `@xzusty`.
