import os
import uuid
import logging
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey,
    UniqueConstraint, inspect, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship


def new_uuid() -> str:
    """Immutable, collision-resistant identifier for a bot instance."""
    return uuid.uuid4().hex

logger = logging.getLogger("alphabot.db")

# We are using PostgreSQL via environment variable
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in Railway variables.")

# ── Railway / Postgres compliance ────────────────────────────────────
# Railway (and Heroku-style providers) hand out URLs prefixed with the legacy
# "postgres://" scheme, which SQLAlchemy 2.x + psycopg2 no longer recognize.
# Normalize it to the canonical "postgresql+psycopg2://" form.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://") and "+psycopg2" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

_is_sqlite = DATABASE_URL.startswith("sqlite")

# Pool settings tuned for Railway: pre_ping recovers from connections the
# managed Postgres drops while idle; recycle keeps connections fresh. SQLite
# (used for local/dev/testing) needs check_same_thread disabled instead.
if _is_sqlite:
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True) # Added Name field
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    verification_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    trading_mode = Column(String, default="paper")
    active_broker = Column(String, default="alpaca")
    total_deposited = Column(Float, default=0.0)
    total_withdrawn = Column(Float, default=0.0)
    # ── Legacy single-set key columns (kept for backward compatibility) ──
    alpaca_key = Column(String, nullable=True)
    alpaca_secret = Column(String, nullable=True)
    okx_key = Column(String, nullable=True)
    okx_secret = Column(String, nullable=True)
    okx_pass = Column(String, nullable=True)

    # ── Per-mode credentials (paper vs live) ──
    alpaca_key_paper = Column(String, nullable=True)
    alpaca_secret_paper = Column(String, nullable=True)
    alpaca_key_live = Column(String, nullable=True)
    alpaca_secret_live = Column(String, nullable=True)
    okx_key_paper = Column(String, nullable=True)
    okx_secret_paper = Column(String, nullable=True)
    okx_pass_paper = Column(String, nullable=True)
    okx_key_live = Column(String, nullable=True)
    okx_secret_live = Column(String, nullable=True)
    okx_pass_live = Column(String, nullable=True)

    bots = relationship("Bot", back_populates="owner")
    trades = relationship("Trade", back_populates="owner")
    logs = relationship("ActivityLog", back_populates="owner") # Added relationship

class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    # Immutable, globally-unique identity for this bot instance. Execution logic
    # binds to this UUID so a bot can never act on another bot's position even
    # if integer ids are ever reused/reordered.
    uuid = Column(String(32), unique=True, index=True, nullable=False, default=new_uuid)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    ticker = Column(String, nullable=True)          # null = fully autonomous (engine picks the asset)
    broker = Column(String, default="alpaca")
    # Account the bot is assigned to: "paper" or "live". Set at creation from the
    # user's trading mode. Used for the Bots-tab view filter + LIVE/PAPER badge.
    mode = Column(String, default="paper")
    timeframe = Column(String, default="1h")        # candle interval the bot analyzes
    buy_limit = Column(Float, nullable=True)
    sell_limit = Column(Float, nullable=True)
    min_profit_pct = Column(Float, nullable=True)   # min % gain before a discretionary sell
    is_auto = Column(Boolean, default=True) # Added is_auto column to handle layout modes
    auto_select = Column(Boolean, default=False)  # True = engine picks the asset (no fixed ticker)
    in_position = Column(Boolean, default=False)
    shares_held = Column(Float, default=0)
    avg_entry_price = Column(Float, nullable=True)
    trade_count = Column(Integer, default=0)
    realized_pnl = Column(Float, default=0)
    funds_allocated = Column(Float, default=0.0)
    running = Column(Boolean, default=False)

    # ── First-buy price gate (optional manual entry trigger) ──
    first_buy_price = Column(Float, nullable=True)
    first_buy_done = Column(Boolean, default=False)

    # ── Risk-management state (trailing stop / adaptive take-profit) ──
    peak_price = Column(Float, nullable=True)        # highest close seen since entry
    stop_price = Column(Float, nullable=True)        # current trailing stop level
    take_profit_price = Column(Float, nullable=True) # adaptive take-profit target
    last_signal = Column(String, nullable=True)      # last pattern signal action
    last_analysis_at = Column(DateTime, nullable=True)
    last_pattern_summary = Column(String, nullable=True)  # human-readable status for UI

    owner = relationship("User", back_populates="bots")

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=True)
    # Denormalized immutable link to the bot that placed this trade. Even if a
    # bot row is later deleted/recreated, the ledger preserves exactly which
    # bot instance executed the order (audit / attribution integrity).
    bot_uuid = Column(String(32), index=True, nullable=True)
    ticker = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Float, nullable=True)
    notional = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    broker = Column(String, default="alpaca")
    mode = Column(String, default="paper")
    broker_order_id = Column(String, nullable=True)
    status = Column(String, default="submitted")
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="trades")


class MarketQuote(Base):
    """
    Continuously-refreshed market state written by the background market-data
    worker (see ``app/scheduler.py``). This is the database-of-record for live
    prices so reads never depend on a frontend HTTP request and never go stale
    (the bug where MSFT froze at $385). One row per (broker, symbol).
    """
    __tablename__ = "market_quotes"
    __table_args__ = (UniqueConstraint("broker", "symbol", name="uq_market_quote_broker_symbol"),)

    id = Column(Integer, primary_key=True, index=True)
    broker = Column(String, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    price = Column(Float, nullable=True)
    signal_action = Column(String, nullable=True)      # last computed BUY/SELL/HOLD
    signal_strength = Column(Float, nullable=True)
    candle_ts = Column(Integer, nullable=True)         # epoch seconds of source candle
    updated_at = Column(DateTime, default=datetime.utcnow, index=True)

class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message = Column(String, nullable=False)
    level = Column(String, default="INFO") 
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="logs")

# SQL column types for additive migrations on pre-existing tables.
# create_all() only CREATEs missing tables — it never ALTERs existing ones,
# so we add any newly-introduced columns here in an idempotent way.
_MIGRATIONS = {
    "bots": {
        # Identity / config columns added after initial schema
        "uuid": "VARCHAR(32)",
        "mode": "VARCHAR DEFAULT 'paper'",
        "timeframe": "VARCHAR DEFAULT '1h'",
        "auto_select": "BOOLEAN DEFAULT FALSE",
        "is_auto": "BOOLEAN DEFAULT TRUE",
        "min_profit_pct": "FLOAT",
        "buy_limit": "FLOAT",
        "sell_limit": "FLOAT",
        # Position-state columns
        "shares_held": "FLOAT DEFAULT 0",
        "avg_entry_price": "FLOAT",
        "trade_count": "INTEGER DEFAULT 0",
        "realized_pnl": "FLOAT DEFAULT 0",
        "running": "BOOLEAN DEFAULT FALSE",
        # Entry price gate
        "first_buy_price": "FLOAT",
        "first_buy_done": "BOOLEAN DEFAULT FALSE",
        # Risk-management state
        "peak_price": "FLOAT",
        "stop_price": "FLOAT",
        "take_profit_price": "FLOAT",
        "last_signal": "VARCHAR",
        "last_analysis_at": "TIMESTAMP",
        "last_pattern_summary": "VARCHAR",
    },
    "trades": {
        "bot_uuid": "VARCHAR(32)",
        "broker": "VARCHAR DEFAULT 'alpaca'",
        "mode": "VARCHAR DEFAULT 'paper'",
        "broker_order_id": "VARCHAR",
        "status": "VARCHAR DEFAULT 'submitted'",
        "notional": "FLOAT",
        "qty": "FLOAT",
    },
    "users": {
        "name": "VARCHAR",
        "trading_mode": "VARCHAR DEFAULT 'paper'",
        "active_broker": "VARCHAR DEFAULT 'alpaca'",
        "total_deposited": "FLOAT DEFAULT 0",
        "total_withdrawn": "FLOAT DEFAULT 0",
        "email_verified": "BOOLEAN DEFAULT FALSE",
        "verification_code": "VARCHAR",
        "alpaca_key": "VARCHAR",
        "alpaca_secret": "VARCHAR",
        "okx_key": "VARCHAR",
        "okx_secret": "VARCHAR",
        "okx_pass": "VARCHAR",
        "alpaca_key_paper": "VARCHAR",
        "alpaca_secret_paper": "VARCHAR",
        "alpaca_key_live": "VARCHAR",
        "alpaca_secret_live": "VARCHAR",
        "okx_key_paper": "VARCHAR",
        "okx_secret_paper": "VARCHAR",
        "okx_pass_paper": "VARCHAR",
        "okx_key_live": "VARCHAR",
        "okx_secret_live": "VARCHAR",
        "okx_pass_live": "VARCHAR",
    },
}


def _run_additive_migrations():
    """Add any missing columns to existing tables (Postgres/SQLite safe)."""
    try:
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())
        with engine.begin() as conn:
            for table, columns in _MIGRATIONS.items():
                if table not in existing_tables:
                    continue
                present = {c["name"] for c in inspector.get_columns(table)}
                for col, ddl in columns.items():
                    if col not in present:
                        logger.info("[MIGRATION] Adding %s.%s", table, col)
                        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'))

            # Fully-autonomous bots need a NULLable ticker. SQLite can't ALTER a
            # constraint in place (and recreates aren't worth it for dev DBs),
            # so only relax this on Postgres where it matters for Railway.
            if "bots" in existing_tables and not _is_sqlite:
                try:
                    conn.execute(text('ALTER TABLE bots ALTER COLUMN ticker DROP NOT NULL'))
                    logger.info("[MIGRATION] Relaxed bots.ticker NOT NULL constraint")
                except Exception as e:
                    logger.debug("ticker NOT NULL relax skipped: %s", e)
    except Exception as e:
        logger.warning("Additive migration skipped: %s", e)


def _backfill_integrity():
    """
    One-time data hygiene so the new integrity guarantees apply to rows that
    pre-date this refactor:

      1. Assign an immutable UUID to every existing bot that lacks one.
      2. Stamp each trade's bot_uuid from its linked bot (attribution).
      3. Reconstruct the exact ``qty`` for legacy BUY trades that only stored a
         notional (the History tab showed '0' for these).
    """
    try:
        with engine.begin() as conn:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())

            if "bots" in tables:
                rows = conn.execute(text("SELECT id FROM bots WHERE uuid IS NULL OR uuid = ''")).fetchall()
                for (bid,) in rows:
                    conn.execute(text("UPDATE bots SET uuid = :u WHERE id = :i"),
                                 {"u": new_uuid(), "i": bid})
                if rows:
                    logger.info("[BACKFILL] Assigned UUIDs to %d legacy bot(s)", len(rows))

            if "bots" in tables and "users" in tables:
                # Backfill bot.mode from owner's trading_mode for pre-existing bots
                conn.execute(text(
                    "UPDATE bots SET mode = ("
                    "  SELECT u.trading_mode FROM users u WHERE u.id = bots.owner_id"
                    ") WHERE mode = 'paper' AND owner_id IS NOT NULL"
                ))

            if "trades" in tables and "bots" in tables:
                conn.execute(text(
                    "UPDATE trades SET bot_uuid = ("
                    "  SELECT b.uuid FROM bots b WHERE b.id = trades.bot_id"
                    ") WHERE bot_uuid IS NULL AND bot_id IS NOT NULL"
                ))
                # Reconstruct qty for buys that only recorded a notional + price.
                conn.execute(text(
                    "UPDATE trades SET qty = notional / price "
                    "WHERE (qty IS NULL OR qty = 0) AND notional IS NOT NULL "
                    "AND price IS NOT NULL AND price > 0"
                ))
    except Exception as e:
        logger.warning("Integrity backfill skipped: %s", e)


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_additive_migrations()
    _backfill_integrity()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
