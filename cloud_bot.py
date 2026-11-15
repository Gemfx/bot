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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
# -------------------------------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
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
TEMPLATES_DIR = "app_templates"
GENERATED_SITES_DIR = "generated_sites"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(GENERATED_SITES_DIR, exist_ok=True)
USER_WEB_PROJECTS = {}  # {user_id: {"site_id": str, "html": str, "active": bool}}
# -----------------------------------

DYNAMIC_CONFIG = {
    "polling_interval": 900,
    "auto_claim_enabled": True,
    "active_bots": list(MINING_BOT_USERNAMES)
}

telethon_client_one = None
tg_app_global = None

TICKER_MAP = {
    "usdt": "tether", "btc": "bitcoin", "eth": "ethereum",
    "sol": "solana", "bnb": "binancecoin", "xrp": "ripple",
    "doge": "dogecoin", "ada": "cardano"
}

# --- DEFAULT MASTER TEMPLATE ---
DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body class="bg-gray-950 text-white min-h-screen flex flex-col items-center justify-center p-6">
    <div class="max-w-md w-full bg-gray-900 border border-gray-800 rounded-2xl p-6 shadow-2xl text-center">
        <h1 class="text-2xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-cyan-400 mb-3">{{TITLE}}</h1>
        <p class="text-gray-400 text-sm mb-6">{{DESCRIPTION}}</p>
        <div class="bg-gray-800/50 rounded-xl p-4 mb-6 border border-gray-700/50">
            <span class="text-xs uppercase tracking-wider text-gray-500 font-semibold">Status</span>
            <div class="text-lg font-mono text-emerald-400 mt-1">🟢 Active & Operational</div>
        </div>
        <button onclick="Telegram.WebApp.close()" class="w-full py-3 px-4 bg-emerald-500 hover:bg-emerald-600 text-gray-950 font-bold rounded-xl transition shadow-lg shadow-emerald-500/20">
            Close Mini-App
        </button>
    </div>
    <script>
        window.Telegram?.WebApp?.ready();
    </script>
</body>
</html>
"""

if not os.path.exists(os.path.join(TEMPLATES_DIR, "default.html")):
    with open(os.path.join(TEMPLATES_DIR, "default.html"), "w", encoding="utf-8") as f:
        f.write(DEFAULT_TEMPLATE)

def build_webapp_from_template(prompt: str, existing_html: str | None = None) -> str:
    parts = [p.strip() for p in prompt.split("|")]
    title = parts[0] if len(parts) > 0 and parts[0] else "My Telegram Mini-App"
    description = parts[1] if len(parts) > 1 and parts[1] else "Custom built via template engine."

    template_path = os.path.join(TEMPLATES_DIR, "default.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    html_content = html_content.replace("{{TITLE}}", title)
    html_content = html_content.replace("{{DESCRIPTION}}", description)
    return html_content

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

async def auto_claimer_loop(telethon_client, account_label="Account-1"):
    print(f"[+] Autonomous Mini-App Radar started for [{account_label}]")
    await asyncio.sleep(10)
     
    last_known_state = {}

    while True:
        current_bots = DYNAMIC_CONFIG.get("active_bots", MINING_BOT_USERNAMES)
        for bot_username in current_bots:
            try:
                bot_entity = await telethon_client.get_entity(bot_username)
                await telethon_client.send_message(bot_entity, STATUS_COMMAND)
                await asyncio.sleep(6)
                 
                messages = await telethon_client.get_messages(bot_entity, limit=2)
                for latest_msg in messages:
                    if not latest_msg.message:
                        continue
                 
                    message_text = latest_msg.message.lower()
                    is_ready = any(k in message_text for k in ["ready", "complete", "harvest", "claim available", "limit reached", "balance: 0"])
                    
                    if "remaining" in message_text or "hrs" in message_text or "mins" in message_text or "sec" in message_text:
                        is_ready = False

                    if account_label not in MINING_STATUS_STORE:
                        MINING_STATUS_STORE[account_label] = {}
                    MINING_STATUS_STORE[account_label][bot_username] = {
                        "status": "READY TO CLAIM" if is_ready else "MINING IN PROGRESS",
                        "last_response": latest_msg.message,
                        "timestamp": time.time()
                    }

                    if is_ready:
                        if last_known_state.get(bot_username) != "ready":
                            alert_msg = (
                                f"🚨 **MINING REWARD READY! [{account_label}]** 🚨\n\n"
                                f"🤖 **Mini-App Bot:** `{bot_username}`\n"
                                f"⚡ Cycle complete! Tap below to open the Mini-App and claim."
                            )
                            keyboard = [[InlineKeyboardButton("🎯 OPEN MINI-APP", url=f"https://t.me/{bot_username.lstrip('@')}")]]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                             
                            me = await telethon_client.get_me()
                            await telethon_client.send_message(me, alert_msg, reply_markup=reply_markup)
                            last_known_state[bot_username] = "ready"
                    else:
                        last_known_state[bot_username] = "active"
                    break
                 
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[-] [{account_label}] Error checking {bot_username}: {e}")
         
        poll_interval = DYNAMIC_CONFIG.get("polling_interval", 900)
        await asyncio.sleep(poll_interval)

# --- FASTAPP LIFESPAN & BACKEND SETUP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app_global, telethon_client_one
    
    # 1. Initialize Telegram Bot Application
    tg_app_global = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()
     
    handlers = {
        "help": tg_help,
        "build": tg_build,
        "done": tg_done,
        "dashboard": tg_dashboard,
        "crypto": tg_crypto,
    }
    for name, handler in handlers.items():
        tg_app_global.add_handler(CommandHandler([name, name.upper()], handler))

    tg_app_global.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_studio_message_handler))

    await tg_app_global.initialize()
    await tg_app_global.start()
     
    tg_menu = [
        BotCommand("build", "Create & Host Mini-App"),
        BotCommand("done", "Exit Edit Mode"),
        BotCommand("dashboard", "Open Mining Dashboard"),
        BotCommand("crypto", "Crypto Price Lookup"),
        BotCommand("help", "Show Commands")
    ]
    await tg_app_global.bot.set_my_commands(tg_menu)
    await tg_app_global.updater.start_polling(drop_pending_updates=True)

    # 2. Initialize Telethon Userbot if session string provided
    session_one = os.getenv("SESSION_STRING_ONE", "").strip()
    if session_one:
        try:
            telethon_client_one = TelegramClient(StringSession(session_one), TELEGRAM_API_ID, TELEGRAM_API_HASH)
            await telethon_client_one.start()
            asyncio.create_task(auto_claimer_loop(telethon_client_one, "Account-1"))
        except Exception as e:
            print(f"[!] Failed Account-1: {e}")

    print("[+] Cloud Bot, FastAPI Server & Template Engine successfully started.")
    yield
    
    # Shutdown cleanup
    if tg_app_global:
        await tg_app_global.stop()
        await tg_app_global.shutdown()

api_app = FastAPI(title="Cloud Bot & Mining Dashboard API", lifespan=lifespan)

@api_app.get("/", response_class=HTMLResponse)
def root():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return {"status": "online", "service": "Master Cloud Bot & Template Web Studio active"}

@api_app.get("/apps/{site_id}", response_class=HTMLResponse)
async def serve_generated_app(site_id: str):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", site_id)
    file_path = os.path.join(GENERATED_SITES_DIR, f"{safe_id}.html")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Generated Web App not found or expired.")

@api_app.get("/api/mining-status")
async def get_mining_status():
    return {"status": "online", "accounts": MINING_STATUS_STORE}

@api_app.get("/api/settings")
async def get_settings():
    return {"status": "success", "settings": DYNAMIC_CONFIG}

@api_app.post("/api/settings")
async def update_settings(payload: dict):
    global DYNAMIC_CONFIG, MINING_BOT_USERNAMES
    try:
        if "polling_interval" in payload:
            DYNAMIC_CONFIG["polling_interval"] = int(payload["polling_interval"])
        if "auto_claim_enabled" in payload:
            DYNAMIC_CONFIG["auto_claim_enabled"] = bool(payload["auto_claim_enabled"])
        if "active_bots" in payload and isinstance(payload["active_bots"], list):
            clean_bots = [b.strip() for b in payload["active_bots"] if b.strip()]
            if clean_bots:
                DYNAMIC_CONFIG["active_bots"] = clean_bots
                MINING_BOT_USERNAMES = clean_bots
        return {"status": "success", "message": "Settings updated!", "settings": DYNAMIC_CONFIG}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- TELEGRAM COMMAND HANDLERS ---
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **Cloud Bot & Template Web Studio**\n\n"
        "🌐 `/build Title | Description` — Instant template deployment\n"
        "✅ `/done` — Exit interactive Studio mode\n"
        "📊 `/dashboard` — Open Mining Control Center\n"
        "💵 `/crypto <ticker>` — Crypto Spot Price\n"
        "ℹ️ `/help` — Show commands"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def tg_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "💡 **Usage:** `/build Title | Description`\n"
            "Example: `/build Crypto Tracker | Live portfolio dashboard`",
            parse_mode="Markdown"
        )
        return

    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("⚡ Generating and hosting your Mini-App...", parse_mode="Markdown")

    try:
        html_code = build_webapp_from_template(prompt, None)
        site_id = f"app_{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(GENERATED_SITES_DIR, f"{site_id}.html")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html_code)

        USER_WEB_PROJECTS[user_id] = {
            "site_id": site_id,
            "html": html_code,
            "active": True
        }

        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com").rstrip("/")
        live_url = f"{base_url}/apps/{site_id}"

        keyboard = [
            [InlineKeyboardButton("🚀 Open Live Mini-App", web_app={"url": live_url})],
            [InlineKeyboardButton("🌐 Open in External Browser", url=live_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await status_msg.edit_text(
            f"✅ **Mini-App Deployed Instantly!**\n\n"
            f"🔗 **Live URL:** `{live_url}`\n\n"
            f"🎨 **Studio Mode Active:** Send text edits to update the title/description, or send `/done` to finish.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Build failed: {str(e)}")

async def tg_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    user_id = update.effective_user.id
    if user_id in USER_WEB_PROJECTS and USER_WEB_PROJECTS[user_id].get("active"):
        USER_WEB_PROJECTS[user_id]["active"] = False
        await update.message.reply_text("✅ **Web Studio Session Closed.** Your app remains live at its link.", parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ You don't have an active Web Studio session running.")

async def tg_studio_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS:
        return

    user_id = update.effective_user.id
    project = USER_WEB_PROJECTS.get(user_id)

    if not project or not project.get("active"):
        return

    edit_prompt = update.message.text.strip()
    try:
        updated_html = build_webapp_from_template(edit_prompt, project["html"])
        site_id = project["site_id"]
        file_path = os.path.join(GENERATED_SITES_DIR, f"{site_id}.html")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated_html)

        project["html"] = updated_html
        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com").rstrip("/")
        live_url = f"{base_url}/apps/{site_id}"

        await update.message.reply_text(f"✨ **App Updated!** Live at: `{live_url}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Update failed: {str(e)}")

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
