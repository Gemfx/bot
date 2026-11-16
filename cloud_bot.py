import os
import re
import uuid
import time
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from dotenv import load_dotenv
import requests

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, PreCheckoutQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from telethon import TelegramClient
from telethon.sessions import StringSession

# --- FASTAPI BACKEND IMPORTS ---
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn
# -------------------------------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "my_secure_dashboard_key").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS_RAW.split(",") if x.strip()]

# --- TELETHON MINING USERBOT CONFIG ---
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "1234567"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "f72565d820fde421d56172f2261dadd4")

MINING_BOTS_RAW = os.getenv("MINING_BOT_USERNAME", "@UltrawalletTrade_Bot,@ATF_AIRDROP_bot")
MINING_BOT_USERNAMES = [b.strip() for b in MINING_BOTS_RAW.split(",") if b.strip()]

STATUS_COMMAND = "/claim"
# --------------------------------------

ACTIVE_ALERTS = []
MINING_STATUS_STORE = {}

# --- WEB & MINI-APP STUDIO STATE ---
TEMPLATES_DIR = "templates"
GENERATED_SITES_DIR = "generated_sites"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(GENERATED_SITES_DIR, exist_ok=True)
USER_WEB_PROJECTS = {}  # {user_id: {"site_id": str, "html": str, "active": bool}}
# -----------------------------------

DYNAMIC_CONFIG = {
    "polling_interval": 900,
    "auto_claim_enabled": True,
    "active_bots": list(MINING_BOT_USERNAMES),
    "ready_keywords": ["ready", "complete", "harvest", "claim available", "limit reached", "balance: 0"],
    "not_ready_keywords": ["remaining", "hrs", "mins", "sec"],
    "ready_valid_hours": 12,
    "cycle_hours": {}
}

telethon_client_one = None
tg_app_global = None

TICKER_MAP = {
    "usdt": "tether", "btc": "bitcoin", "eth": "ethereum",
    "sol": "solana", "bnb": "binancecoin", "xrp": "ripple",
    "doge": "dogecoin", "ada": "cardano"
}

def fetch_pair_price(symbol: str, asset_type: str = "crypto") -> float | None:
    symbol_clean = symbol.lower().strip()
    try:
        if asset_type == "crypto":
            symbol_clean = TICKER_MAP.get(symbol_clean, symbol_clean)
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol_clean}&vs_currencies=usd"
            res = requests.get(url, timeout=5).json()
            if symbol_clean in res:
                return float(res[symbol_clean]["usd"])
    except Exception as e:
        print(f"[!] Price fetch error: {e}")
    return None

async def scan_account_bots(telethon_client, account_label="Account-1"):
    current_bots = DYNAMIC_CONFIG.get("active_bots", MINING_BOT_USERNAMES)
    ready_count = 0
    results = {}
    
    for bot_username in current_bots:
        try:
            bot_entity = await telethon_client.get_entity(bot_username)
            await telethon_client.send_message(bot_entity, STATUS_COMMAND)
            await asyncio.sleep(6)
             
            messages = await telethon_client.get_messages(bot_entity, limit=2)
            bot_status = "NO MESSAGES"
            last_text = ""
            msg_time = time.time()
            
            for latest_msg in messages:
                if not latest_msg.message:
                    continue
                last_text = latest_msg.message
                if hasattr(latest_msg, 'date') and latest_msg.date:
                    msg_time = latest_msg.date.timestamp()
                
                message_text = latest_msg.message.lower()
                
                ready_keywords = DYNAMIC_CONFIG.get("ready_keywords", ["ready", "complete", "harvest", "claim available", "limit reached", "balance: 0"])
                not_ready_keywords = DYNAMIC_CONFIG.get("not_ready_keywords", ["remaining", "hrs", "mins", "sec"])
                
                is_ready = any(k in message_text for k in ready_keywords)
                if any(nr in message_text for nr in not_ready_keywords):
                    is_ready = False
                
                bot_status = "READY TO CLAIM" if is_ready else "MINING IN PROGRESS"
                break
            
            if account_label not in MINING_STATUS_STORE:
                MINING_STATUS_STORE[account_label] = {}
                
            MINING_STATUS_STORE[account_label][bot_username] = {
                "status": bot_status,
                "last_response": last_text,
                "message_time": msg_time,
                "timestamp": time.time(),
                "mode": "message"
            }
            results[bot_username] = bot_status
            if bot_status == "READY TO CLAIM":
                ready_count += 1
                
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[-] [{account_label}] Error scanning {bot_username}: {e}")
            results[bot_username] = "NO REPLY"
            if account_label not in MINING_STATUS_STORE:
                MINING_STATUS_STORE[account_label] = {}
            MINING_STATUS_STORE[account_label][bot_username] = {
                "status": "NO REPLY",
                "last_response": str(e),
                "message_time": time.time(),
                "timestamp": time.time(),
                "mode": "message"
            }
    return results

async def auto_claimer_loop(telethon_client, account_label="Account-1"):
    print(f"[+] Autonomous Mini-App Radar started for [{account_label}]")
    await asyncio.sleep(10)
    
    while True:
        if DYNAMIC_CONFIG.get("auto_claim_enabled", True):
            await scan_account_bots(telethon_client, account_label)
        poll_interval = DYNAMIC_CONFIG.get("polling_interval", 900)
        await asyncio.sleep(poll_interval)

def verify_dashboard_access(x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    if x_api_key and x_api_key == DASHBOARD_API_KEY:
        return True
    if x_telegram_init_data or not DASHBOARD_API_KEY:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized dashboard access")

# --- FASTAPI LIFESPAN & BACKEND SETUP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app_global, telethon_client_one
    
    tg_app_global = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()
     
    handlers = {
        "help": tg_help,
        "dashboard": tg_dashboard,
        "crypto": tg_crypto,
    }
    for name, handler in handlers.items():
        tg_app_global.add_handler(CommandHandler([name, name.upper()], handler))

    await tg_app_global.initialize()
    await tg_app_global.start()
     
    tg_menu = [
        BotCommand("dashboard", "Open Mining Dashboard"),
        BotCommand("crypto", "Crypto Price Lookup"),
        BotCommand("help", "Show Commands")
    ]
    await tg_app_global.bot.set_my_commands(tg_menu)
    await tg_app_global.updater.start_polling(drop_pending_updates=True)

    session_one = os.getenv("SESSION_STRING_ONE", "").strip()
    if session_one:
        try:
            telethon_client_one = TelegramClient(StringSession(session_one), TELEGRAM_API_ID, TELEGRAM_API_HASH)
            await telethon_client_one.start()
            asyncio.create_task(auto_claimer_loop(telethon_client_one, "Account-1"))
        except Exception as e:
            print(f"[!] Failed Account-1: {e}")

    print("[+] Cloud Bot, FastAPI Server & Dashboard API successfully started.")
    yield
    
    if tg_app_global:
        await tg_app_global.stop()
        await tg_app_global.shutdown()

api_app = FastAPI(title="Cloud Bot & Mining Dashboard API", lifespan=lifespan)

@api_app.get("/", response_class=HTMLResponse)
def root():
    index_file = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"status": "online", "service": "Mining Radar Dashboard Active"}

@api_app.get("/api/mining-status")
async def get_mining_status(x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    return {"status": "online", "accounts": MINING_STATUS_STORE}

@api_app.get("/api/settings")
async def get_settings(x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    return {"status": "success", "settings": DYNAMIC_CONFIG}

@api_app.post("/api/settings")
async def update_settings(request: Request, x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    global DYNAMIC_CONFIG, MINING_BOT_USERNAMES
    try:
        payload = await request.json()
        if "polling_interval" in payload:
            DYNAMIC_CONFIG["polling_interval"] = int(payload["polling_interval"])
        if "auto_claim_enabled" in payload:
            DYNAMIC_CONFIG["auto_claim_enabled"] = bool(payload["auto_claim_enabled"])
        if "active_bots" in payload and isinstance(payload["active_bots"], list):
            clean_bots = [b.strip() for b in payload["active_bots"] if b.strip()]
            if clean_bots:
                DYNAMIC_CONFIG["active_bots"] = clean_bots
                MINING_BOT_USERNAMES = clean_bots
        if "ready_keywords" in payload:
            DYNAMIC_CONFIG["ready_keywords"] = payload["ready_keywords"]
        if "not_ready_keywords" in payload:
            DYNAMIC_CONFIG["not_ready_keywords"] = payload["not_ready_keywords"]
        if "ready_valid_hours" in payload:
            DYNAMIC_CONFIG["ready_valid_hours"] = float(payload["ready_valid_hours"])
        if "cycle_hours" in payload:
            DYNAMIC_CONFIG["cycle_hours"] = payload["cycle_hours"]
            
        return {"status": "success", "message": "Settings updated!", "settings": DYNAMIC_CONFIG}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@api_app.post("/api/action/{account_id}/force_check")
async def force_check_account(account_id: int, x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    global telethon_client_one
    if not telethon_client_one:
        raise HTTPException(status_code=400, detail="Telethon client not active on this instance.")
    
    label = f"Account-{account_id}" if account_id > 0 else "Account-1"
    results = await scan_account_bots(telethon_client_one, label)
    return {"status": "success", "results": results}

@api_app.post("/api/mark-claimed")
async def mark_claimed(request: Request, x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    try:
        payload = await request.json()
        account = payload.get("account")
        bot = payload.get("bot")
        if account in MINING_STATUS_STORE and bot in MINING_STATUS_STORE[account]:
            MINING_STATUS_STORE[account][bot]["status"] = "CLAIMED"
        return {"status": "success", "message": f"{bot} marked as claimed"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@api_app.post("/api/create-invoice")
async def create_invoice(x_api_key: str = Header(None), x_telegram_init_data: str = Header(None)):
    verify_dashboard_access(x_api_key, x_telegram_init_data)
    return {"status": "success", "invoice_link": "https://t.me/share/url?url=ProPassSubscription"}

# --- TELEGRAM COMMAND HANDLERS ---
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **Cloud Mining Radar Bot**\n\n"
        "📊 `/dashboard` — Open Mining Control Center\n"
        "💵 `/crypto <ticker>` — Crypto Spot Price\n"
        "ℹ️ `/help` — Show commands"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def tg_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    app_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
    keyboard = [[InlineKeyboardButton("📊 Open Mining Dashboard", web_app={"url": app_url})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🚀 **Autonomous Mining Control Center**", reply_markup=reply_markup, parse_mode="Markdown")

async def tg_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "bitcoin"
    price = fetch_pair_price(symbol, "crypto")
    if price:
        await update.message.reply_text(f"💵 **{symbol.upper()}:** ${price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Ticker not found.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("cloud_bot:api_app", host="0.0.0.0", port=port)
