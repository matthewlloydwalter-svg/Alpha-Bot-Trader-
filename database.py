import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

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
    buy_limit = Column(Float, nullable=True)
    sell_limit = Column(Float, nullable=True)
    in_position = Column(Boolean, default=False)
    shares_held = Column(Float, default=0)
    avg_entry_price = Column(Float, nullable=True)
    trade_count = Column(Integer, default=0)
    realized_pnl = Column(Float, default=0)
    
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

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()    broker_order_id = Column(String, nullable=True)
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
