import os
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

logger = logging.getLogger("alphabot.db")

# We are using PostgreSQL via environment variable
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in Railway variables.")

# Postgres setup
engine = create_engine(DATABASE_URL)
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
    alpaca_key = Column(String, nullable=True)
    alpaca_secret = Column(String, nullable=True)
    okx_key = Column(String, nullable=True)
    okx_secret = Column(String, nullable=True)
    okx_pass = Column(String, nullable=True)
    
    bots = relationship("Bot", back_populates="owner")
    trades = relationship("Trade", back_populates="owner")
    logs = relationship("ActivityLog", back_populates="owner") # Added relationship

class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    ticker = Column(String, nullable=False)
    broker = Column(String, default="alpaca")
    timeframe = Column(String, default="1h")        # candle interval the bot analyzes
    buy_limit = Column(Float, nullable=True)
    sell_limit = Column(Float, nullable=True)
    min_profit_pct = Column(Float, nullable=True)   # min % gain before a discretionary sell
    is_auto = Column(Boolean, default=True) # Added is_auto column to handle layout modes
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
        "timeframe": "VARCHAR DEFAULT '1h'",
        "min_profit_pct": "FLOAT",
        "first_buy_price": "FLOAT",
        "first_buy_done": "BOOLEAN DEFAULT FALSE",
        "peak_price": "FLOAT",
        "stop_price": "FLOAT",
        "take_profit_price": "FLOAT",
        "last_signal": "VARCHAR",
        "last_analysis_at": "TIMESTAMP",
        "last_pattern_summary": "VARCHAR",
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
    except Exception as e:
        logger.warning("Additive migration skipped: %s", e)


def init_db():
    Base.metadata.create_all(bind=engine)
    _run_additive_migrations()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
