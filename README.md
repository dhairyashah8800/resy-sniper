# Resy Sniper

Polls Resy every 60 seconds and instantly books the first available slot at your target venue within a defined date range. Sends a Telegram notification on success or crash.

## Setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts — you'll receive a **bot token** like `123456789:ABCdef...`
3. Start a chat with your new bot (search its username, press Start)
4. Get your **chat ID**: visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser after sending any message to the bot. Look for `"chat": {"id": ...}` in the response.

### 2. Get your Resy credentials

Both values come from your browser's DevTools while logged into resy.com:

- **RESY_AUTH_TOKEN** — open DevTools → Network tab → make any request → find `X-Resy-Auth-Token` request header
- **RESY_PAYMENT_METHOD_ID** — found in the `/3/details` API response under `user.payment_methods[0].id`

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

To run locally:
```bash
pip install -r requirements.txt
export $(cat .env | xargs)
python resy_sniper.py
```

### 4. Deploy to Railway

**From the Railway dashboard:**

1. Go to [railway.app](https://railway.app) and create a new project
2. Choose **Deploy from GitHub repo** and connect your repository
3. Once linked, go to your service → **Variables** tab
4. Add each variable from `.env.example` with your real values:
   - `RESY_AUTH_TOKEN`
   - `RESY_PAYMENT_METHOD_ID`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
5. Railway will detect `railway.json` and deploy automatically as a **worker** (no web server needed)

**Via Railway CLI:**
```bash
npm install -g @railway/cli
railway login
railway init
railway up
railway variables set RESY_AUTH_TOKEN=... RESY_PAYMENT_METHOD_ID=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
```

### 5. Monitor logs on Railway

- Dashboard → your project → **Deployments** tab → click the active deployment → **Logs**
- Or via CLI: `railway logs`

The sniper logs every poll attempt with a timestamp. When it books, it prints `BOOKED [OK]` and sends a Telegram message, then exits (Railway will not restart it since exit code is 0).

## Target config

Hardcoded in `resy_sniper.py`:

| Setting | Value |
|---|---|
| Venue ID | 94741 |
| Dates | 2026-05-14 to 2026-05-24 |
| Party size | 4 |
| Time window | 17:00 to closing |
| Poll interval | 60 seconds |

A hard date guard prevents any booking outside the allowed range, even if the Resy API returns unexpected results.
