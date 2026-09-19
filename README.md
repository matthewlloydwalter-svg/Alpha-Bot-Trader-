# AlphaBotix Trading

AlphaBotix Trading is a FastAPI application that serves both the trading API and the browser dashboard. Users can connect Alpaca for equities or OKX for crypto, create automated trading bots, inspect market data and news, and review portfolio and trade history from one account.

## Architecture

- `main.py` is the application entry point and owns HTTP routes, template rendering, authentication flows, broker endpoints, bot endpoints, billing, admin routes, and the SSE market update stream.
- `app/` contains the supporting modules: SQLAlchemy database models, authentication and rate limiting, broker adapters, market data and storage, bot evaluation, scheduler, email, plans and Stripe billing, AI admin tooling, and realtime events.
- `templates/` contains the landing page, authentication/dashboard shell, legal pages, pricing and checkout pages, admin page, and the shared footer partial.
- `static/` contains the dashboard and admin JavaScript, pricing JavaScript, CSS, and image assets.
- `alphabot.db` is created automatically for local SQLite development. Production deployments can use PostgreSQL.

The background engine is started by the FastAPI lifespan. APScheduler polls market data and evaluates running bots independently of browser requests. Dashboard clients subscribe to `GET /stream/updates` for live market, trade, and portfolio events.

## Features

- Signup, login, session cookies, email verification, password reset, and first-run tutorial walkthrough.
- Paper and live trading modes with separate Alpaca and OKX credentials.
- Alpaca equity trading and OKX crypto trading, including OKX public market data without API keys.
- Bot creation, risk controls, scheduled evaluation, simulated fills for paper mode, and broker order placement for live mode.
- Portfolio valuation, broker balances, markets, technical dashboards, news sentiment, trade history, and account management.
- Starter, Growth, Pro, and Enterprise plans with bot limits, Stripe Checkout, billing portal, and webhook handling.
- Admin dashboard with user controls, system metrics, logs, email tools, and an approval-based AI code assistant.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Public landing page |
| `/login`, `/signup` | Authentication entry points |
| `/dashboard/portfolio` | Portfolio dashboard |
| `/dashboard/markets` | Market universe and asset dashboards |
| `/dashboard/bots` | Bot management |
| `/dashboard/news` | Market news and sentiment |
| `/dashboard/history` | Trade ledger |
| `/dashboard/assets` | Broker funds and account details |
| `/dashboard/account` | Profile, plan, credentials, and trading mode |
| `/upgrade-plans` | Subscription plans and upgrades |
| `/terms`, `/privacy` | Legal pages |
| `/admin` | Admin controls |
| `/health` | Liveness check |
| `/docs` | FastAPI API documentation in development |

The legacy `/app` and `/dashboard` paths redirect to the portfolio dashboard.

## Local setup

Requirements: Python 3.12 and the packages in `requirements.txt`.

```bash
git clone <repository-url>
cd Alpha-Bot-Trader-
python3.12 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

Create a local `.env` file. The minimum useful development configuration is:

```dotenv
DATABASE_URL=sqlite:///./alphabot.db
JWT_SECRET=dev-local-secret-key-change-me
ADMIN_EMAILS=admin@alphabot.dev
PLATFORM_NAME=AlphaBotix Trading
ENGINE_ENABLED=1
RESEND_API_KEY=
```

Broker credentials are optional for signup, login, bot CRUD, and public OKX market data. Add Alpaca server data keys for equity watchlist polling. Add `RESEND_API_KEY` to send verification and account emails. Stripe variables are required only for subscription checkout and billing operations.

Start the development server:

```bash
. venv/bin/activate
uvicorn main:app --reload --port 8000
```

Open `http://127.0.0.1:8000`. The dashboard and API are served by the same process.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | SQLite or PostgreSQL connection URL; required |
| `JWT_SECRET` | Session signing secret; use a strong value in production |
| `ADMIN_EMAILS` | Comma-separated emails granted admin access |
| `PLATFORM_NAME` | Display name used by templates |
| `ENGINE_ENABLED` | Set to `0` to disable background market and bot workers |
| `ALPACA_DATA_KEY`, `ALPACA_DATA_SECRET` | Optional Alpaca market-data credentials |
| `RESEND_API_KEY` | Optional transactional email provider key |
| `STRIPE_API_KEY` or `STRIPE_SECRET_KEY` | Stripe server key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook verification secret |
| `STRIPE_ENVIRONMENT` | `live` or `test` Stripe price mapping |
| `PUBLIC_BASE_URL` | Public URL used for checkout redirects |
| `FRONTEND_ORIGIN` | Optional CORS origin for separate clients |

The scheduler uses a 30-second market-data interval and a 60-second bot-evaluation interval by default. Production should use PostgreSQL, a strong `JWT_SECRET`, HTTPS, and persistent storage for any SQLite deployment.

## Trading safety

Paper mode is the default. Alpaca paper keys and OKX demo keys must be stored in the corresponding paper fields. Live trading requires a verified email and saved live broker credentials; the API rejects a live-mode switch when those requirements are not met. Live broker keys can place real orders, so validate a strategy in paper mode first.

## Validation

This repository currently has no automated test suite or lint configuration. Run the available syntax check before deployment:

```bash
. venv/bin/activate
python -m compileall -q main.py app/
```

For API exploration in development, use `/docs`. For production, documentation is disabled unless `DOCS_ENABLED=1` is set.

## Deployment

The included `Procfile` runs the FastAPI application with Uvicorn. A Railway deployment should define the production environment variables, configure a persistent volume when using SQLite, and expose the public application URL through `PUBLIC_BASE_URL`. PostgreSQL is recommended for production workloads.
