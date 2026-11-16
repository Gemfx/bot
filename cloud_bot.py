import os
import re
import uuid
import time
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest

from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ALLOWED_USERS = [x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "1234567"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "f72565d820fde421d56172f2261dadd4")
MINING_BOT_USERNAMES = [b.strip() for b in os.getenv("MINING_BOT_USERNAME", "@UltrawalletTrade_Bot,@ATF_AIRDROP_bot").split(",") if b.strip()]

STATUS_COMMAND = "/claim"
MINING_STATUS_STORE = {}
GENERATED_SITES_DIR = "generated_sites"
os.makedirs(GENERATED_SITES_DIR, exist_ok=True)
USER_WEB_PROJECTS = {}

STEP_TYPE, STEP_TITLE, STEP_DESC, STEP_ITEMS, STEP_THEME = range(5)

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

THEME_PALETTES = {
    "emerald": {"accent": "emerald", "hex": "16, 185, 129"},
    "blue":    {"accent": "sky",     "hex": "14, 165, 233"},
    "purple":  {"accent": "purple",  "hex": "168, 85, 247"},
    "gold":    {"accent": "amber",   "hex": "245, 158, 11"},
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_app_global, telethon_client_one

    if TELEGRAM_TOKEN:
        tg_app_global = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()

        build_conv = ConversationHandler(
            entry_points=[CommandHandler(["build", "BUILD"], build_start)],
            states={
                STEP_TYPE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, build_step_type)],
                STEP_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, build_step_title)],
                STEP_DESC:  [MessageHandler(filters.TEXT & ~filters.COMMAND, build_step_desc)],
                STEP_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, build_step_items)],
                STEP_THEME: [MessageHandler(filters.TEXT & ~filters.COMMAND, build_step_theme)],
            },
            fallbacks=[CommandHandler(["cancel", "CANCEL"], build_cancel)],
        )

        tg_app_global.add_handler(build_conv)
        tg_app_global.add_handler(CommandHandler(["help", "HELP"], tg_help))
        tg_app_global.add_handler(CommandHandler(["crypto", "CRYPTO"], tg_crypto))

        await tg_app_global.initialize()
        await tg_app_global.start()

        tg_menu = [
            BotCommand("build", "Interactive Mini-App Builder"),
            BotCommand("cancel", "Cancel Current Build"),
            BotCommand("crypto", "Crypto Price Lookup"),
            BotCommand("help", "Show Commands"),
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

    print("[+] Cloud Bot & Interactive Builder started.")
    yield

    if tg_app_global:
        await tg_app_global.updater.stop()
        await tg_app_global.stop()
        await tg_app_global.shutdown()

api_app = FastAPI(title="Cloud Bot & Interactive Web Studio API", lifespan=lifespan)

@api_app.get("/", response_class=JSONResponse)
def root():
    return {"status": "online", "service": "Cloud Bot & Interactive Builder Active"}

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

def render_interactive_webapp(data: dict) -> str:
    app_type = data.get("type", "store")
    title = data.get("title", "My Mini-App")
    desc = data.get("desc", "Welcome to our Telegram Mini-App")
    raw_items = data.get("items", "")
    theme_key = data.get("theme", "emerald")

    palette = THEME_PALETTES.get(theme_key, THEME_PALETTES["emerald"])
    c = palette["accent"]
    rgb = palette["hex"]
    parsed_items = [x.strip() for x in raw_items.split(",") if x.strip()]

    if app_type == "store":
        cards = []
        if not parsed_items:
            parsed_items = ["Featured Item 1 - $50", "Featured Item 2 - $75"]
        for item in parsed_items:
            if "-" in item:
                name, price = [p.strip() for p in item.split("-", 1)]
            elif ":" in item:
                name, price = [p.strip() for p in item.split(":", 1)]
            else:
                name, price = item, "$25.00"
            cards.append(f"""
            <div class="bg-gray-900 border border-{c}-500/30 rounded-xl p-3 text-left flex flex-col justify-between">
                <div>
                    <div class="text-xs text-white font-bold">{name}</div>
                    <div class="text-{c}-400 text-xs font-semibold mt-1">{price}</div>
                </div>
                <button onclick="orderItem('{name}', '{price}')" class="mt-3 w-full py-1.5 bg-{c}-500 hover:bg-{c}-400 text-gray-950 font-bold rounded-lg text-xs transition">
                    Order Now
                </button>
            </div>""")
        body_section = f'<div class="grid grid-cols-2 gap-3">{"".join(cards)}</div>'

    elif app_type == "crypto":
        if not parsed_items:
            parsed_items = ["BTC", "ETH", "SOL"]
        rows = []
        for coin in parsed_items:
            sym = coin.upper()
            rows.append(f"""
            <div class="bg-gray-900 border border-{c}-500/30 rounded-xl p-3.5 flex justify-between items-center">
                <div class="text-left">
                    <div class="text-sm font-bold text-white">{sym} / USDT</div>
                    <div class="text-[11px] text-gray-400">24h Staking Pool</div>
                </div>
                <div class="text-right">
                    <div class="text-sm font-bold text-{c}-400">APR 18.5%</div>
                    <button onclick="alert('Staked {sym}!')" class="mt-1 px-3 py-1 bg-{c}-500 text-gray-950 font-bold rounded text-[11px]">Stake</button>
                </div>
            </div>""")
        body_section = f'<div class="space-y-2.5">{"".join(rows)}</div>'

    elif app_type == "booking":
        if not parsed_items:
            parsed_items = ["Standard Session - $30", "VIP Consultation - $80"]
        rows = []
        for srv in parsed_items:
            if "-" in srv:
                name, price = [p.strip() for p in srv.split("-", 1)]
            else:
                name, price = srv, "Book Slot"
            rows.append(f"""
            <div class="bg-gray-900 border border-{c}-500/30 rounded-xl p-3.5 flex justify-between items-center">
                <div class="text-left">
                    <div class="text-xs font-bold text-white">{name}</div>
                    <div class="text-[11px] text-{c}-400">{price}</div>
                </div>
                <button onclick="orderItem('{name}', '{price}')" class="px-3 py-1.5 bg-{c}-500 text-gray-950 font-bold rounded-lg text-xs">
                    Book
                </button>
            </div>""")
        body_section = f'<div class="space-y-2.5">{"".join(rows)}</div>'

    else:
        if not parsed_items:
            parsed_items = ["Fast Delivery", "24/7 Support", "Verified Quality"]
        badges = "".join([
            f'<div class="bg-gray-900 border border-{c}-500/30 rounded-xl p-3 text-left text-xs text-white">✦ {feat}</div>'
            for feat in parsed_items
        ])
        body_section = f"""
        <div class="space-y-2">{badges}</div>
        <button onclick="alert('Contact request sent!')" class="w-full py-3 bg-{c}-500/20 border border-{c}-500 text-{c}-300 font-bold rounded-xl text-xs uppercase tracking-wider">
            Get In Touch
        </button>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>.app-glow {{ box-shadow: 0 0 22px rgba({rgb}, 0.18); }}</style>
</head>
<body class="bg-gray-950 text-{c}-400 min-h-screen font-sans p-4 flex flex-col items-center justify-between">
    <div class="max-w-md w-full space-y-4">
        <div class="bg-gray-900 border border-{c}-500/30 rounded-2xl p-4 app-glow flex justify-between items-center text-left">
            <div>
                <h1 class="text-lg font-bold text-white tracking-tight">{title}</h1>
                <p class="text-xs text-gray-400 mt-0.5">{desc}</p>
            </div>
            <span class="bg-{c}-500/10 border border-{c}-500/40 px-2.5 py-1 rounded-full text-[11px] text-{c}-400 font-bold">LIVE</span>
        </div>
        {body_section}
    </div>
    <div class="max-w-md w-full mt-6">
        <button onclick="Telegram.WebApp.close()" class="w-full py-3 bg-{c}-500 hover:bg-{c}-400 text-gray-950 font-bold rounded-xl transition uppercase tracking-wider text-xs">
            Close Mini-App
        </button>
    </div>
    <script>
        const tg = window.Telegram?.WebApp;
        if (tg) {{ tg.ready(); tg.expand(); }}
        function orderItem(name, price) {{ alert("✅ Selected: " + name + " (" + price + ")"); }}
    </script>
</body>
</html>"""

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
                    if any(w in message_text for w in ["remaining", "hrs", "mins", "sec"]):
                        is_ready = False

                    if account_label not in MINING_STATUS_STORE:
                        MINING_STATUS_STORE[account_label] = {}
                    MINING_STATUS_STORE[account_label][bot_username] = {
                        "status": "READY TO CLAIM" if is_ready else "MINING IN PROGRESS",
                        "last_response": latest_msg.message,
                        "timestamp": time.time()
                    }

                    if is_ready and last_known_state.get(bot_username) != "ready":
                        alert_msg = (
                            f"🚨 **MINING REWARD READY! [{account_label}]** 🚨\n\n"
                            f"🤖 **Mini-App Bot:** `{bot_username}`\n"
                            f"⚡ Cycle complete! Tap below to open the Mini-App and claim."
                        )
                        keyboard = [[InlineKeyboardButton("🎯 OPEN MINI-APP", url=f"https://t.me/{bot_username.lstrip('@')}")]]
                        me = await telethon_client.get_me()
                        await telethon_client.send_message(me, alert_msg, reply_markup=InlineKeyboardMarkup(keyboard))
                        last_known_state[bot_username] = "ready"
                    elif not is_ready:
                        last_known_state[bot_username] = "active"
                    break
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[-] [{account_label}] Error checking {bot_username}: {e}")

        await asyncio.sleep(DYNAMIC_CONFIG.get("polling_interval", 900))

async def build_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS:
        return ConversationHandler.END

    context.user_data["build_data"] = {}
    keyboard = [
        ["🛍 E-Commerce Store", "📈 Crypto / Staking"],
        ["📅 Service Booking", "🌐 Portfolio / Brand Page"],
    ]
    await update.message.reply_text(
        "🛠 **Interactive Mini-App Builder (Step 1 of 5)**\n\n"
        "What type of Mini-App do you want to build?\n"
        "_(Send /cancel at any time to stop)_",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return STEP_TYPE

async def build_step_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "store" in text or "commerce" in text:
        app_type = "store"
    elif "crypto" in text or "staking" in text:
        app_type = "crypto"
    elif "booking" in text or "service" in text:
        app_type = "booking"
    else:
        app_type = "portfolio"

    context.user_data["build_data"]["type"] = app_type
    await update.message.reply_text(
        "✏️ **Step 2 of 5: App Name / Title**\n\n"
        "What is the name of your brand or Mini-App?\n"
        "_(Example: `Gemi Wears Store` or `Alpha Vault`)_",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return STEP_TITLE

async def build_step_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["build_data"]["title"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 **Step 3 of 5: Short Description / Tagline**\n\n"
        "Enter a short tagline that appears under your title:\n"
        "_(Example: `Luxury streetwear delivered nationwide`)_",
        parse_mode="Markdown"
    )
    return STEP_DESC

async def build_step_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["build_data"]["desc"] = update.message.text.strip()
    app_type = context.user_data["build_data"].get("type", "store")

    if app_type == "store":
        prompt_hint = "List your products separated by commas:\n_(Example: `Cargo Pants - $45, Graphic Tee - $25, Cap - $15`)_"
    elif app_type == "crypto":
        prompt_hint = "List the tokens/pools separated by commas:\n_(Example: `TON, BTC, ETH, USDT`)_"
    elif app_type == "booking":
        prompt_hint = "List your services & prices separated by commas:\n_(Example: `Haircut - $20, Beard Trim - $10, VIP Package - $40`)_"
    else:
        prompt_hint = "List 3 highlights or services separated by commas:\n_(Example: `Graphic Design, Video Editing, Data Analytics`)_"

    await update.message.reply_text(
        f"📦 **Step 4 of 5: Content & Items**\n\n{prompt_hint}",
        parse_mode="Markdown"
    )
    return STEP_ITEMS

async def build_step_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["build_data"]["items"] = update.message.text.strip()
    keyboard = [
        ["🟢 Emerald Green", "🔵 Cyber Blue"],
        ["🟣 Neon Purple", "🟡 Gold Luxury"],
    ]
    await update.message.reply_text(
        "🎨 **Step 5 of 5: Pick a Color Theme**\n\n"
        "Choose the accent style for your Mini-App:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return STEP_THEME

async def build_step_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "blue" in text:
        theme = "blue"
    elif "purple" in text:
        theme = "purple"
    elif "gold" in text or "yellow" in text:
        theme = "gold"
    else:
        theme = "emerald"

    build_data = context.user_data.get("build_data", {})
    build_data["theme"] = theme

    status_msg = await update.message.reply_text(
        "⚡ Building and deploying your custom Mini-App...",
        reply_markup=ReplyKeyboardRemove()
    )

    try:
        html_code = render_interactive_webapp(build_data)
        site_id = f"app_{uuid.uuid4().hex[:8]}"
        file_path = os.path.join(GENERATED_SITES_DIR, f"{site_id}.html")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(html_code)

        user_id = update.effective_user.id
        USER_WEB_PROJECTS[user_id] = {"site_id": site_id, "html": html_code}

        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com").rstrip("/")
        live_url = f"{base_url}/apps/{site_id}"

        keyboard = [
            [InlineKeyboardButton("🚀 Open Live Mini-App", web_app={"url": live_url})],
            [InlineKeyboardButton("🌐 Open in External Browser", url=live_url)]
        ]
        await status_msg.edit_text(
            f"✅ **{build_data.get('title')} is Live!**\n\n"
            f"🔗 **URL:** `{live_url}`\n"
            f"Want to build another? Send `/build` anytime.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Build failed: {e}")

    return ConversationHandler.END

async def build_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("build_data", None)
    await update.message.reply_text("❌ Mini-App builder cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "🤖 **Cloud Bot & Interactive Web Studio**\n\n"
        "🛠 `/build` — Launch step-by-step Mini-App Builder\n"
        "❌ `/cancel` — Cancel active builder wizard\n"
        "💵 `/crypto <ticker>` — Crypto Spot Price\n"
        "ℹ️ `/help` — Show commands",
        parse_mode="Markdown"
    )

async def tg_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS:
        return
    symbol = context.args[0] if context.args else "bitcoin"
    price = fetch_pair_price(symbol, "crypto")
    if price:
        await update.message.reply_text(f"💵 **{symbol.upper()}:** ${price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Ticker not found.")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("cloud_bot:api_app", host="0.0.0.0", port=port)
