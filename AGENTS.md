# AGENTS.md

## Cursor Cloud specific instructions

AlphaBotix Trading is a single **FastAPI** backend (entrypoint `main.py`, app package in `app/`)
that also serves the dashboard UI from `templates/` + `static/`. There is no separate
frontend service. Standard run instructions live in `README.md` ("Local setup"); the notes
below cover only non-obvious caveats for this environment.

### Environment / config
- The Python venv lives in `venv/` (Python 3.12). Activate with `. venv/bin/activate` before
  running anything. The startup update script creates the venv and installs `requirements.txt`.
- `app/database.py` **raises `ValueError` at import time** if `DATABASE_URL` is unset, so the
  app (and any script that imports `main`) cannot start without it. A gitignored dev `.env`
  (loaded via `python-dotenv` in `main.py`) supplies it. The update script creates `.env` with
  dev defaults if missing:
  ```
  DATABASE_URL=sqlite:///./alphabot.db
  JWT_SECRET=dev-local-secret-key-change-me
  ADMIN_EMAILS=admin@alphabot.dev
  PLATFORM_NAME=AlphaBotix Trading
  ENGINE_ENABLED=1
  RESEND_API_KEY=
  ```
  Postgres is used in production; SQLite is fully supported for local dev (the code branches on
  the URL scheme). The SQLite file `alphabot.db` is created automatically on first run.
- Broker keys (Alpaca/OKX) and `RESEND_API_KEY` are **optional** for dev. Without them:
  signup/login, bot CRUD, and OKX crypto market data all work; Alpaca equity data and
  email verification do not. From address: `AlphaBotix Trading <updates@alphabotixtrading.com>`.
- **Live trading** requires verified email + saved Live broker keys (enforced by
  `POST /broker/trading-mode`). Simulated broker fills are paper-only.
- Production should set a strong `JWT_SECRET` (≥24 chars). Weak/missing secrets fail closed
  when `ENV=production` or Railway is detected. Set `FRONTEND_ORIGIN` for CORS if needed;
  `/docs` is disabled in production unless `DOCS_ENABLED=1`.

### Running
- Dev server: `. venv/bin/activate && uvicorn main:app --reload --port 8000` (serves UI + API
  at http://127.0.0.1:8000; interactive API docs at `/docs`).
- Public landing page is `/` (`templates/landing.html`). Auth is at `/login` and
  `/signup`. The trading app lives under `/dashboard/*` (Portfolio default:
  `/dashboard/portfolio`; also `/dashboard/markets`, `/bots`, `/news`, `/history`,
  `/assets`, `/account`). Legacy `/app` redirects to `/dashboard/portfolio`.
  Subscription upgrades: `/upgrade-plans` (Stripe Checkout). Legal: `/terms`,
  `/privacy`. Health: `/health`.
  AdSense client script is in the `<head>` of every HTML page (landing, app,
  legal, admin) so Google can auto-place ads sitewide.
- New signups start on the **Starter** plan (1 bot). Admins are unlimited. Paid
  tiers map to Growth 5 / Pro 10 / Enterprise 25 bots via Stripe on `/upgrade-plans`.
  Checkout: `POST /billing/checkout` with `{price_id}` only (server builds
  `mode=subscription` line items). Portal: `POST /billing/portal`.
  Webhooks: `POST /webhooks/stripe` (alias `/billing/webhook`). Success page:
  `/checkout/success?session_id={CHECKOUT_SESSION_ID}`.
  Set `STRIPE_API_KEY` (or `STRIPE_SECRET_KEY`), `STRIPE_WEBHOOK_SECRET`,
  `STRIPE_ENVIRONMENT=live|test`, and `PUBLIC_BASE_URL` in prod.
  Live vs test Price IDs live in `app/plans.py` (`STRIPE_PRICE_IDS`).
  Support: `plan_level` on users; Account mailto aliases via
  `app/support_routing.py`; lookup `GET /api/support/lookup?email=`.
- On startup an **always-on APScheduler background engine** launches (see `app/scheduler.py`):
  it polls market data every 30s and evaluates running bots every 60s. The first OKX poll fetches
  live BTC/ETH/SOL candles with no keys required. Set `ENGINE_ENABLED=0` to disable it.
- The Alpaca watchlist poller is silently skipped unless `ALPACA_DATA_KEY` / `ALPACA_DATA_SECRET`
  are set — this is expected, not an error.

### Lint / test / build
- There are **no automated tests and no lint config** in this repo. Use
  `python -m compileall -q main.py app/` as a quick syntax/build sanity check.
- An admin account is any email listed in `ADMIN_EMAILS`; sign up with that email to reach `/admin`.
