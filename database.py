"""
database.py — SQLite database setup and ORM models.

Using SQLite because it requires zero external setup and persists to a
file on disk, which works fine on Railway as long as you attach a
Railway Volume (see README) so the file survives restarts/redeploys.
If you outgrow SQLite later, swap DATABASE_URL for a Postgres URL —
SQLAlchemy makes that a one-line change.
"""

import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./alphabot.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    verification_code = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Per-broker connection state. Real keys are never stored in plaintext
    # here in a real deployment — see the note in main.py's save_broker_keys
    # about encrypting these columns at rest before going live with real users.
    alpaca_key = Column(String, nullable=True)
    alpaca_secret = Column(String, nullable=True)
    alpaca_paper = Column(Boolean, default=True)

    okx_key = Column(String, nullable=True)
    okx_secret = Column(String, nullable=True)
    okx_passphrase = Column(String, nullable=True)

    active_broker = Column(String, default="alpaca")  # "alpaca" or "okx"
    trading_mode = Column(String, default="paper")    # "paper" or "live"

    total_deposited = Column(Float, default=0.0)
    total_withdrawn = Column(Float, default=0.0)

    bots = relationship("Bot", back_populates="owner", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="owner", cascade="all, delete-orphan")


class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)
    ticker = Column(String, nullable=True)   # null = fully autonomous, AI picks
    broker = Column(String, default="alpaca")
    funds_allocated = Column(Float, default=0.0)
    is_auto = Column(Boolean, default=True)
    running = Column(Boolean, default=True)

    buy_limit = Column(Float, nullable=True)
    sell_limit = Column(Float, nullable=True)
    min_profit_pct = Column(Float, nullable=True)
    first_buy_price = Column(Float, nullable=True)
    first_buy_done = Column(Boolean, default=False)

    in_position = Column(Boolean, default=False)
    shares_held = Column(Float, default=0.0)
    avg_entry_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)
    trade_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="bots")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=True)
    ticker = Column(String, nullable=False)
    side = Column(String, nullable=False)  # "buy" or "sell"
    qty = Column(Float, nullable=True)
    notional = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    broker = Column(String, default="alpaca")
    mode = Column(String, default="paper")  # "paper" or "live"
    broker_order_id = Column(String, nullable=True)
    status = Column(String, default="submitted")
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="trades")


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
# Add these to the bottom of database.py

def save_verification_code(email: str, code: str, db):
    user = db.query(User).filter(User.email == email).first()
    if user:
        user.verification_code = code
        db.commit()

def verify_user_code(email: str, input_code: str, db):
    user = db.query(User).filter(User.email == email).first()
    if user and user.verification_code == input_code:
        user.email_verified = True
        user.verification_code = None  # Clear code after successful use
        db.commit()
        return True
    return False
