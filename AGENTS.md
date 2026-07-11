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

### Running
- Dev server: `. venv/bin/activate && uvicorn main:app --reload --port 8000` (serves UI + API
  at http://127.0.0.1:8000; interactive API docs at `/docs`).
- On startup an **always-on APScheduler background engine** launches (see `app/scheduler.py`):
  it polls market data every 30s and evaluates running bots every 60s. The first OKX poll fetches
  live BTC/ETH/SOL candles with no keys required. Set `ENGINE_ENABLED=0` to disable it.
- The Alpaca watchlist poller is silently skipped unless `ALPACA_DATA_KEY` / `ALPACA_DATA_SECRET`
  are set — this is expected, not an error.

### Lint / test / build
- There are **no automated tests and no lint config** in this repo. Use
  `python -m compileall -q main.py app/` as a quick syntax/build sanity check.
- An admin account is any email listed in `ADMIN_EMAILS`; sign up with that email to reach `/admin`.
