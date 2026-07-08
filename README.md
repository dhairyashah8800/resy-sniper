# Resy Sniper

Polls the Resy API directly for open reservation slots at target venues and books the first one that matches your date/time criteria. Supports multiple venues running in parallel, each with independent date ranges and polling rules. Sends a Telegram notification on success or crash.

## Setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts — you'll receive a **bot token** like `123456789:ABCdef...`
3. Start a chat with your new bot (search its username, press Start)
4. Get your **chat ID**: visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser after sending any message to the bot. Look for `"chat": {"id": ...}` in the response.

### 2. Get your Resy credentials

All three values come from your browser's DevTools while logged into resy.com:

- **RESY_API_KEY** — Network tab → any Resy API request → `Authorization` request header, the `api_key="..."` portion
- **RESY_AUTH_TOKEN** — Network tab → any request → `X-Resy-Auth-Token` request header
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
   - `RESY_API_KEY`
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
railway variables set RESY_API_KEY=... RESY_AUTH_TOKEN=... RESY_PAYMENT_METHOD_ID=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
```

### 5. Monitor logs on Railway

- Dashboard → your project → **Deployments** tab → click the active deployment → **Logs**
- Or via CLI: `railway logs`

Each venue runs in its own daemon thread and logs every poll attempt with a timestamp. When a venue books successfully, it prints `BOOKED [OK]`, sends a Telegram message, and that thread exits. The process as a whole exits (code 0) once all threads finish, so Railway won't restart it.

## Target config

Targets are defined as `VenueTarget` entries in `resy_sniper.py` — edit that file directly to change venues, dates, or polling behavior.

| Setting | Ambassadors Clubhouse | Bungalow |
|---|---|---|
| Venue ID | 94741 | 80201 |
| Dates | 2026-05-14 to 2026-05-24 | 2026-05-14 to 2026-05-20 (skips Tuesdays) |
| Party size | 4 | 4 |
| Time window | 17:00+ | any time |
| Poll interval | 60s | 60s, switches to 2s "drop sniper" mode 10:59:50–11:02:50 ET daily |

A hard date guard on every target prevents booking outside its allowed date range, even if the Resy API returns unexpected results. Bungalow additionally stops polling at noon ET each day and resumes the next morning.

`test_book.py` is a standalone one-shot script for manually testing the find → details → book flow against a single venue/date, useful for verifying credentials or debugging API changes outside the main polling loop.

## Notes

- This tool calls Resy's internal API directly (not the public site) using credentials pulled from an authenticated browser session. This is unofficial and likely outside Resy's Terms of Service — use at your own risk, and expect it to break if Resy changes their API or rate-limits/flags automated traffic.
- All credentials (`RESY_API_KEY`, `RESY_AUTH_TOKEN`, `RESY_PAYMENT_METHOD_ID`, Telegram tokens) are read from environment variables — never commit real values to `.env` or source files.
