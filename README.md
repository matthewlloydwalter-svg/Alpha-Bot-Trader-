# AlphaBot Backend — Full Stack

## Always-on, event-driven architecture (new)

The trading engine is fully decoupled from the frontend. Two background worker
loops (APScheduler, thread-backed) run independently of any HTTP request and are
started/stopped via the FastAPI lifespan:

- **Market-data worker** (`app/scheduler.py: poll_market_data`) fetches live data
  24/7 and continuously upserts it into Postgres (`market_quotes`), then streams
  each update over SSE. This is the database-of-record for prices, so values
  never freeze waiting on a page refresh.
- **Bot-evaluation worker** (`evaluate_bots`) re-evaluates every running bot
  against the stored market state and executes trades automatically.

**Live streaming:** the browser subscribes once to `GET /stream/updates`
(Server-Sent Events). Market quotes are broadcast; trade/portfolio events are
scoped to the authenticated user. No more manual refresh.

**Integrity:** every `Bot` has an immutable `uuid`; every `Trade` records the
exact `qty`, `notional`, `price`, `created_at`, the linking `bot_id` AND the
immutable `bot_uuid`. Cross-bot capital rotation is **off by default**
(`BOT_CAPITAL_ROTATION=1` to enable) so a bot only ever manages its own position.

### Multi-tenant API-key separation (strict)

| Concern | Credentials used | Source |
| --- | --- | --- |
| Market data (prices, bars, charts, universal DB state) | **Global data keys** | `ALPACA_DATA_KEY` / `ALPACA_DATA_SECRET` (env). OKX data is public. |
| Order placement & balance/portfolio checks | **The specific user's keys** | Postgres, pulled dynamically at execution time. |

- `app/credentials.py::resolve_data_credentials()` returns the global keys and is
  the ONLY thing the poller/chart endpoints use. The global keys are never
  passed to an order or balance call.
- `app/credentials.py::resolve_trading_credentials()` returns the user's own keys
  and is the ONLY thing trade execution / balance lookups use.
- User trading keys are stored in dedicated columns `users.alpaca_trading_key` /
  `alpaca_trading_secret`, **encrypted at rest** (Fernet) when `APP_ENCRYPTION_KEY`
  is set (`app/crypto.py`). Per-mode (paper/live) columns remain as a mode-correct
  fallback for Alpaca's separate key pairs.

### Micro-transaction trading strategy

Bots no longer go all-in. Each entry deploys a small slice of the bot's allocated
funds and exits are tight, so capital churns through many small, risk-controlled
trades 24/7 (independent of any login):

- Position size per trade: `BOT_TRADE_FRACTION` (default `0.15` = 15%, clamped 5–35%).
- Take profit fast: sell at `+BOT_MICRO_TP_PCT`% (default `0.75`).
- Cut losses fast: sell at `-BOT_MICRO_SL_PCT`% (default `0.50`).
- ATR trailing stop / take-profit remain as additional backstops.

**Admin AI assistant:** `templates/admin.html` exposes a secure, admin-only chat
box. `POST /admin/ai/audit` lets an LLM (Anthropic/OpenAI) scan & read the repo
and return a unified-diff preview of proposed edits; nothing is written until an
admin explicitly hits Approve (`/admin/ai/approve`) — Deny discards.

### Relevant environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ENGINE_ENABLED` | `1` | Master switch for the background engine. |
| `MARKET_POLL_INTERVAL` | `30` | Seconds between market-data polls. |
| `BOT_SCAN_INTERVAL` | `30` | Seconds between bot evaluation cycles. |
| `MARKET_WATCHLIST_LIMIT` | `20` | Symbols/broker polled even without a bot. |
| `ALPACA_DATA_KEY` / `ALPACA_DATA_SECRET` | — | **Global, data-only** Alpaca keys (prices/bars). Never used for trades. |
| `APP_ENCRYPTION_KEY` | — | Passphrase enabling at-rest encryption of user trading keys. |
| `BOT_TRADE_FRACTION` | `0.15` | Fraction of allocated funds deployed per trade (5–35%). |
| `BOT_MICRO_TP_PCT` | `0.75` | Take-profit threshold (% gain). |
| `BOT_MICRO_SL_PCT` | `0.50` | Stop-loss threshold (% loss). |
| `BOT_CAPITAL_ROTATION` | `0` | Allow a bot to liquidate another bot's position. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | LLM provider for the Admin AI assistant. |



## Does this match the standard FastAPI-on-Railway structure?

Yes — your pro-tip layout is exactly right, with one addition (a `.env`
file, which Railway replaces with its own "Variables" tab):

```
alphabot-backend/
├── main.py              ← routes: auth, brokers, bots, admin
├── database.py          ← SQLite models (User, Bot, Trade)
├── auth.py               ← password hashing, sessions, email sending
├── brokers.py             ← Alpaca + OKX unified interface
├── bot_engine.py          ← the bot "brain" (Claude decision + order placement)
├── templates/
│   ├── index.html         ← your main dashboard (replace with V9 frontend)
│   └── admin.html         ← your admin page (replace with V9 admin tab)
├── static/
│   ├── css/style.css
│   └── js/app.js, admin.js
├── requirements.txt
├── Procfile
└── .env.example          ← copy to .env locally; on Railway, paste into "Variables"
```

I split the single `main.py` you asked for into five small files
(`main.py`, `database.py`, `auth.py`, `brokers.py`, `bot_engine.py`)
rather than one giant file. Functionally this is one backend — Railway
runs `main.py` exactly the same way — but a single file mixing user
auth, two brokers' APIs, and an AI decision engine would be hundreds
of lines that are hard to debug later. Each file does one job.

## What's real vs. what needs your input

**Real and working as written:**
- User signup/login with hashed passwords and session cookies
- Email verification gate (codes sent via your SMTP settings)
- SQLite database that persists users, bots, and trade history
- Broker switching (Alpaca ↔ OKX) with per-user stored credentials
- `/bots/{id}/run-cycle` — asks Claude for a decision, places a real
  order via Alpaca or OKX if the decision clears your bot's limits
- Admin routes — user list with deposit/withdrawal/profit numbers,
  platform-wide email sending, gated to emails in `ADMIN_EMAILS`

**Needs your input before going live:**
- `templates/index.html` and `admin.html` are placeholders. Your real
  V9 dashboard UI needs to be translated into calls against these
  routes — see "Connecting your frontend" below.
- Bot key encryption: keys are currently stored as plain text columns
  in SQLite for simplicity. Before opening this to *other* people
  (not just you), encrypt those columns — I noted exactly where in
  `database.py` and `main.py`.
- The profit calculation in `/admin/users` is a rough estimate from
  trade history, not a true mark-to-market P&L (that requires pulling
  live position values from the broker per user, which is a further
  build once you have real trade volume to test against).

## 1. Local setup

```bash
cd alphabot-backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in every value in `.env` — see the comments in that file. At
minimum you need `JWT_SECRET`, `ADMIN_EMAILS`, and `ANTHROPIC_API_KEY`
to start the server at all; SMTP and broker keys can be added later.

```bash
uvicorn main:app --reload --port 8000
```

Visit `http://localhost:8000` — you should see the placeholder page.
Visit `http://localhost:8000/docs` for the interactive API explorer.

## 2. Connecting your real frontend

Your V9 frontend currently calls `fetch('https://api.anthropic.com/...')`
directly from the browser (fine for a prototype, not for production
since it can't hold secrets). Now it should call **your own backend**
instead, which holds the real keys server-side. The pattern is:

```javascript
// Login
await fetch('/auth/login', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  credentials: 'include',   // sends/receives the session cookie
  body: JSON.stringify({email, password})
});

// Create a bot
await fetch('/bots', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  credentials: 'include',
  body: JSON.stringify({name: 'My Bot', ticker: 'AAPL', funds_allocated: 500, is_auto: true})
});

// Run one decision cycle for a bot (call this on a timer from the frontend)
await fetch(`/bots/${botId}/run-cycle`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  credentials: 'include',
  body: JSON.stringify({current_price: 183.40, recent_prices: [180, 181, 183.4]})
});
```

Move your existing HTML/CSS/JS from the V9 artifact into
`templates/index.html` (the HTML) and `static/js/app.js` (the
JavaScript), swapping every place it faked data locally for a fetch
call to one of these routes instead.

## 3. Deploying to Railway

1. Push this folder to a GitHub repo.
2. In Railway: New Project → Deploy from GitHub repo → select it.
3. Railway auto-detects Python and reads your `Procfile`.
4. Go to your service's **Variables** tab and add every value from
   `.env.example` (your real values, not the placeholders).
5. **Attach a Volume** (Railway → your service → Settings → Volumes)
   mounted at `/app` or wherever your working directory is, so
   `alphabot.db` (the SQLite file) survives redeploys. Without this,
   every deploy wipes your user database.
6. Deploy. Railway gives you a permanent `https://yourapp.up.railway.app`
   URL — use this as `FRONTEND_ORIGIN` if your frontend is hosted
   separately, or serve the frontend from the same app via the
   `templates/` folder.

## 4. Testing the live trading path safely

Before flipping any real user to live mode:

1. Use Alpaca's **paper** keys first (`/broker/alpaca/keys`, then
   leave `trading_mode` as `paper`).
2. Create a bot, manually call `/bots/{id}/run-cycle` a few times with
   realistic price data, and confirm orders show up in your Alpaca
   paper dashboard.
3. Check your alert email actually arrives (send_email failures are
   logged, not silent, but confirm anyway).
4. Only then save live keys and call `/broker/trading-mode` with
   `{"mode": "live"}` — which the backend will refuse unless the
   email is verified and live keys are saved, exactly as you asked.

## 5. OKX paper trading note

OKX's "paper" equivalent is their **demo trading** feature, which
requires generating a *separate* set of demo API keys from OKX's demo
trading section (not your regular keys with a flag). `brokers.py`
calls `exchange.set_sandbox_mode(True)` when your stored
`trading_mode` is `paper` — make sure the keys you save are the demo
ones if you want OKX paper trading to actually use fake funds.
