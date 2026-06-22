import os
from fastapi import FastAPI, Depends, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from jose import jwt, JWTError

# Import your finalized modules
from database import init_db, get_db, User, Bot, Trade
from auth import hash_password, verify_password, create_session_token, JWT_SECRET, JWT_ALGORITHM
import brokers
import bot_engine

# Initialize database tables on startup
file_path = os.path.join(os.getcwd(), "templates", "index.html")
with open(file_path, "r") as f:
    content = f.read()
return HTMLResponse(content=content)

init_db()

app = FastAPI(title="AlphaBot Trading System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve JavaScript and styling files from a 'static' directory
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── AUTHENTICATION DEPENDENCY ─────────────────────────────────────
def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid session token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid session token")
        
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ── FRONTEND PAGE ROUTE ───────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """Serves the main index.html dashboard file."""
   
    return templates.TemplateResponse("index.html", {"request": request})
        


# ── AUTH ENDPOINTS ────────────────────────────────────────────────
@app.post("/signup")
def signup(data: dict, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Missing email or password")
        
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
        
    hashed = hash_password(password)
    new_user = User(email=email, hashed_password=hashed)
    db.add(new_user)
    db.commit()
    return {"status": "success", "message": "Account created successfully"}


@app.post("/login")
def login(data: dict, response: Response, db: Session = Depends(get_db)):
    email = data.get("email")
    password = data.get("password")
    
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Invalid email or password")
        
    token = create_session_token(user_id=user.id, email=user.email)
    response.set_cookie(key="session_token", value=token, httponly=True)
    return {"status": "success", "message": "Logged in successfully"}


@app.post("/logout")
def logout(response: Response):
    response.delete_cookie("session_token")
    return {"status": "success", "message": "Logged out successfully"}


# ── TRADING & PORTFOLIO ENDPOINTS ─────────────────────────────────
@app.get("/account")
def get_user_account(current_user: User = Depends(get_current_user)):
    """Fetches real-time balances using user credentials from the database."""
    broker_name = getattr(current_user, "trading_mode", "alpaca") 
    
    try:
        account_info = brokers.get_account_info(
            broker=broker_name,
            alpaca_key=getattr(current_user, "alpaca_key", None),
            alpaca_secret=getattr(current_user, "alpaca_secret", None),
            okx_key=getattr(current_user, "okx_key", None),
            okx_secret=getattr(current_user, "okx_secret", None),
            okx_passphrase=getattr(current_user, "okx_passphrase", None),
            paper=(getattr(current_user, "trading_mode", "paper") == "paper")
        )
        return account_info
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Broker integration error: {str(e)}")


@app.get("/positions")
def get_user_positions(current_user: User = Depends(get_current_user)):
    """Returns an array of current open holdings for the dashboard views."""
    return []


# ── BOT SCHEDULER TRIGGER RUNNER ──────────────────────────────────
@app.post("/bots/{bot_id}/run-cycle")
def run_bot_cycle(bot_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Manually executes a trading evaluation cycle using the bot engine."""
    bot = db.query(Bot).filter(Bot.id == bot_id, Bot.owner_id == current_user.id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot configuration not found")
        
    # Placeholder metrics parameters for your bot engine processing rules
    current_price = 100.0
    recent_prices = [98.0, 99.0, 100.0]
    news_summary = "Market conditions stable."
    
    try:
        # FIXED: Argument order perfectly matches bot_engine.py signature (db first, then bot)
        result = bot_engine.run_bot_cycle(db, bot, current_price, recent_prices, news_summary)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
