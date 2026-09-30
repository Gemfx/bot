import os
import time
import asyncio
from datetime import timedelta
from urllib.parse import urlparse
import ssl
import socket

from dotenv import load_dotenv
import psutil
import requests
from bs4 import BeautifulSoup
import yt_dlp

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, PreCheckoutQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

import discord
from discord.ext import commands as discord_commands

from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession

# --- FASTAPI BACKEND IMPORTS ---
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
# -------------------------------

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS_RAW.split(",") if x.strip()]

# --- TELETHON MINING USERBOT CONFIG ---
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "1234567"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "f72565d820fde421d56172f2261dadd4")

MINING_BOTS_RAW = os.getenv("MINING_BOT_USERNAME", "@UltrawalletTrade_Bot,@ATF_AIRDROP_bot")
MINING_BOT_USERNAMES = [b.strip() for b in MINING_BOTS_RAW.split(",") if b.strip()]

STATUS_COMMAND = "/balance"
# --------------------------------------

ACTIVE_ALERTS = []
MINING_STATUS_STORE = {}  # Shared state for the backend API

# --- DYNAMIC SETTINGS STORE ---
DYNAMIC_CONFIG = {
    "polling_interval": 900,  # Default 15 minutes
    "auto_claim_enabled": True,
    "active_bots": list(MINING_BOT_USERNAMES)
}
# ------------------------------

# Global references for active Telethon clients (used by API actions)
telethon_client_one = None
telethon_client_two = None

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
        elif asset_type == "forex":
            base = symbol_clean[:3].upper()
            target = symbol_clean[3:].upper() if len(symbol_clean) >= 6 else "USD"
            url = f"https://open.er-api.com/v6/latest/{base}"
            res = requests.get(url, timeout=5).json()
            if "rates" in res and target in res["rates"]:
                return float(res["rates"][target])
    except Exception as e:
        print(f"[!] Price fetch error: {e}")
    return None

def generate_ai_response(prompt: str) -> str:
    try:
        if not GEMINI_API_KEY:
            return "Error: GEMINI_API_KEY is missing."
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"AI Error: {str(e)}"

def analyze_market_trend(symbol: str) -> str:
    symbol_clean = TICKER_MAP.get(symbol.lower().strip(), symbol.lower().strip())
    url = f"https://api.coingecko.com/api/v3/coins/{symbol_clean}/market_chart?vs_currency=usd&days=14&interval=daily"
    try:
        res = requests.get(url, timeout=5).json()
        if "prices" not in res:
            return f"❌ Could not retrieve market data for '{symbol}'."
        prices = [p[1] for p in res["prices"]]
        current_price = prices[-1]
        avg_14d = sum(prices) / len(prices)
        price_change_14d = ((current_price - prices[0]) / prices[0]) * 100

        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(diff if diff > 0 else 0)
            losses.append(abs(diff) if diff < 0 else 0)
         
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        rs = (avg_gain / avg_loss) if avg_loss != 0 else 100
        rsi = 100 - (100 / (1 + rs))

        prompt = (
            f"Act as a quantitative market analyst. Concise technical analysis:\n"
            f"• Asset: {symbol.upper()}\n"
            f"• Current Price: ${current_price:,.2f}\n"
            f"• 14-Day Average: ${avg_14d:,.2f}\n"
            f"• 14-Day Change: {price_change_14d:.2f}%\n"
            f"• 14-Day RSI: {rsi:.1f}\n\n"
            f"Provide Sentiment, Indicator Signal, Target Levels & Risk Caution. Under 160 words."
        )
        return generate_ai_response(prompt)
    except Exception as e:
        return f"Analysis error: {str(e)}"

async def check_price_alerts_loop(tg_app, discord_bot):
    global ACTIVE_ALERTS
    while True:
        await asyncio.sleep(30)
        if not ACTIVE_ALERTS:
            continue
         
        triggered_alerts = []
        for alert in list(ACTIVE_ALERTS):
            current_price = await asyncio.to_thread(fetch_pair_price, alert["symbol"], alert["type"])
            if current_price is None:
                continue

            hit = False
            if alert["condition"] == "above" and current_price >= alert["target_price"]:
                hit = True
            elif alert["condition"] == "below" and current_price <= alert["target_price"]:
                hit = True

            if hit:
                msg = (
                    f"🚨 **PRICE ALERT TRIGGERED!** 🚨\n\n"
                    f"📈 **Asset:** {alert['symbol'].upper()}\n"
                    f"🎯 **Target:** ${alert['target_price']:,.4f}\n"
                    f"💵 **Current:** ${current_price:,.4f}"
                )
                try:
                    if alert["platform"] == "telegram":
                        await tg_app.bot.send_message(chat_id=alert["channel_id"], text=msg, parse_mode="Markdown")
                    elif alert["platform"] == "discord":
                        channel = discord_bot.get_channel(alert["channel_id"])
                        if channel:
                            await channel.send(msg)
                except Exception as e:
                    print(f"[!] Notification error: {e}")
                 
                triggered_alerts.append(alert)

        for t in triggered_alerts:
            if t in ACTIVE_ALERTS:
                ACTIVE_ALERTS.remove(t)

# --- DAILY SUMMARY REPORTER LOOP ---
async def daily_summary_reporter_loop(tg_app):
    while True:
        try:
            now = time.localtime()
            current_seconds = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
            target_seconds = 8 * 3600  # 8:00 AM
            
            if current_seconds >= target_seconds:
                sleep_seconds = (24 * 3600) - current_seconds + target_seconds
            else:
                sleep_seconds = target_seconds - current_seconds
                
            await asyncio.sleep(sleep_seconds)
            
            report = "☀ **Good Morning! Daily Mining Radar Digest** ☀\n\n"
            if not MINING_STATUS_STORE:
                report += "⚠️ No mining status data recorded yet."
            else:
                for account_label, bots in MINING_STATUS_STORE.items():
                    report += f"👤 **{account_label}**\n"
                    for bot_name, info in bots.items():
                        status_emoji = "🟢" if "ACTIVE" in info["status"] else "🚨"
                        report += f"  {status_emoji} `{bot_name}`: **{info['status']}**\n"
                    report += "\n"
            report += "📊 _Check your web dashboard for full operational stats._"
            
            if ALLOWED_USERS:
                for user_id in ALLOWED_USERS:
                    try:
                        await tg_app.bot.send_message(chat_id=int(user_id), text=report, parse_mode="Markdown")
                    except Exception as e:
                        print(f"[!] Failed to send daily report to user {user_id}: {e}")
            
            await asyncio.sleep(60)
        except Exception as e:
            print(f"[!] Error in daily summary reporter loop: {e}")
            await asyncio.sleep(3600)

# --- SMART THRESHOLD CLAIMER RADAR ---
async def auto_claimer_loop(telethon_client, account_label="Account-1"):
    print(f"[+] Smart Radar started for [{account_label}]")
    await asyncio.sleep(10)
     
    last_known_state = {}

    while True:
        current_bots = DYNAMIC_CONFIG.get("active_bots", MINING_BOT_USERNAMES)
        for bot_username in current_bots:
            try:
                print(f"[*] [{account_label}] Polling status for: {bot_username}")
                bot_entity = await telethon_client.get_entity(bot_username)
                 
                await telethon_client.send_message(bot_entity, STATUS_COMMAND)
                await asyncio.sleep(6)
                 
                messages = await telethon_client.get_messages(bot_entity, limit=2)
                for latest_msg in messages:
                    if not latest_msg.message:
                        continue
                 
                    message_text = latest_msg.message.lower()
                    is_ready_to_claim = any(k in message_text for k in ["frozen", "full", "ready", "complete", "harvest", "claim available", "limit reached"])
                 
                    if account_label not in MINING_STATUS_STORE:
                        MINING_STATUS_STORE[account_label] = {}
                    MINING_STATUS_STORE[account_label][bot_username] = {
                        "status": "READY / FROZEN" if is_ready_to_claim else "ACTIVE / MINING",
                        "last_response": latest_msg.message,
                        "timestamp": time.time()
                    }

                    if is_ready_to_claim:
                        if last_known_state.get(bot_username) != "ready":
                            clicked_successfully = False
                            if DYNAMIC_CONFIG.get("auto_claim_enabled", True):
                                try:
                                    if latest_msg.buttons:
                                        for row in latest_msg.buttons:
                                            for button in row:
                                                btn_text = button.text.lower()
                                                if any(k in btn_text for k in ["claim", "harvest", "start", "proceed", "collect", "balance"]):
                                                    await button.click()
                                                    clicked_successfully = True
                                                    print(f"[+] [{account_label}] Auto-clicked button: '{button.text}' on {bot_username}")
                                                    break
                                            if clicked_successfully:
                                                break
                                except Exception as click_err:
                                    print(f"[-] [{account_label}] Failed to auto-click button for {bot_username}: {click_err}")
                            else:
                                print(f"[!] [{account_label}] Auto-claim is disabled via settings panel.")

                            alert_msg = (
                                f"🚨 **MINING REWARD READY! [{account_label}]** 🚨\n\n"
                                f"🤖 **Bot:** `{bot_username}`\n"
                                f"⚙️ **Auto-Click Action:** {'✅ Executed Successfully!' if clicked_successfully else '⚠️ Manual action needed (Disabled or no button found).'}"
                            )
                            keyboard = [[InlineKeyboardButton("🎯 OPEN BOT", url=f"https://t.me/{bot_username.lstrip('@')}")]]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                             
                            me = await telethon_client.get_me()
                            await telethon_client.send_message(me, alert_msg, buttons=reply_markup)
                            last_known_state[bot_username] = "ready"
                    else:
                        if "claim" not in message_text and "frozen" not in message_text:
                            last_known_state[bot_username] = "active"
                    break
                 
                await asyncio.sleep(5)
            except Exception as e:
                print(f"[-] [{account_label}] Error checking {bot_username}: {e}")
         
        poll_interval = DYNAMIC_CONFIG.get("polling_interval", 900)
        await asyncio.sleep(poll_interval)

# --- FASTAPI BACKEND API SETUP ---
api_app = FastAPI(title="Cloud Bot & Mining Dashboard API")

@api_app.on_event("startup")
async def startup_event():
    asyncio.create_task(main())

@api_app.get("/", response_class=HTMLResponse)
def root():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return {"status": "online", "service": "Master Cloud Bot active"}

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
        return {"status": "success", "message": "Settings updated successfully!", "settings": DYNAMIC_CONFIG}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@api_app.post("/api/create-invoice")
async def create_invoice():
    """Generates a Telegram Stars invoice link for a 30-day subscription pass."""
    try:
        amount_stars = 150
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/createInvoiceLink"
        body = {
            "title": "Mining Radar Pro Pass",
            "description": "30-Day Automated Bot Cloud Hosting & Dashboard Access",
            "payload": "monthly_subscription_pass",
            "currency": "XTR",
            "prices": [{"label": "1 Month Pass", "amount": amount_stars}]
        }
        res = requests.post(url, json=body, timeout=5).json()
        if res.get("ok"):
            return {"status": "success", "invoice_link": res["result"]}
        else:
            raise HTTPException(status_code=400, detail=res.get("description", "Failed to create invoice"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@api_app.post("/api/action/{account_id}/{action_type}")
async def trigger_dashboard_action(account_id: int, action_type: str):
    global telethon_client_one, telethon_client_two
    try:
        client = telethon_client_one if account_id == 1 else telethon_client_two
        if not client or not client.is_connected():
            raise HTTPException(status_code=400, detail=f"Account {account_id} client is not active.")

        if action_type == "force_check":
            target_bot = MINING_BOT_USERNAMES[0] if MINING_BOT_USERNAMES else "@UltrawalletTrade_Bot"
            bot_entity = await client.get_entity(target_bot)
            await client.send_message(bot_entity, STATUS_COMMAND)
            return {"status": "success", "message": f"Account {account_id} dispatched status check."}
        elif action_type == "restart_mining":
            return {"status": "success", "message": f"Account {account_id} mining action triggered."}
        else:
            raise HTTPException(status_code=400, detail="Unknown action type.")
    except Exception as e:
        return {"status": "error", "message": str(e)}

# Telegram Handlers
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **Cloud Bot Commands**\n\n"
        "📊 `/dashboard` — Open Mini-App UI\n"
        "📈 `/predict <symbol>` — AI Technical Analysis\n"
        "💵 `/crypto <ticker>` — Live Crypto Price\n"
        "⭐ `/upgrade` — Buy Pro Automation Pass\n"
        "🤖 `/ai <prompt>` — Gemini AI Assistant"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def tg_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    app_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
    keyboard = [[InlineKeyboardButton("📊 Open Mining Dashboard", web_app={"url": app_url})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🚀 **Smart Mining Radar Dashboard**", reply_markup=reply_markup, parse_mode="Markdown")

async def tg_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    # Send native star invoice directly in chat
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="Mining Radar Pro Pass",
            description="30-Day Automated Bot Cloud Hosting & Dashboard Access",
            payload="monthly_subscription_pass",
            currency="XTR",
            prices=[LabeledPrice("1 Month Pass", 150)]
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating invoice: {e}")

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    await update.message.reply_text("🎉 **Subscription Activated!** Your cloud automation pass is now active for 30 days.")

async def tg_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "bitcoin"
    msg = await update.message.reply_text(f"📊 Analyzing {symbol.upper()}...")
    analysis = await asyncio.to_thread(analyze_market_trend, symbol)
    await msg.edit_text(analysis)

async def tg_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "bitcoin"
    price = await asyncio.to_thread(fetch_pair_price, symbol, "crypto")
    if price:
        await update.message.reply_text(f"💵 **{symbol.upper()}:** ${price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Ticker not found.")

async def main():
    global telethon_client_one, telethon_client_two

    tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()
     
    handlers = {
        "help": tg_help, "dashboard": tg_dashboard, "predict": tg_predict, 
        "crypto": tg_crypto, "upgrade": tg_upgrade
    }
    for name, handler in handlers.items():
        tg_app.add_handler(CommandHandler([name, name.upper()], handler))

    tg_app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    await tg_app.initialize()
    await tg_app.start()
     
    tg_menu = [
        BotCommand("dashboard", "Open Mining Mini-App"),
        BotCommand("predict", "AI Technical Analysis"),
        BotCommand("crypto", "Crypto Price Lookup"),
        BotCommand("upgrade", "Buy Pro Pass with Stars"),
        BotCommand("help", "Show Bot Commands")
    ]
    await tg_app.bot.set_my_commands(tg_menu)
    await tg_app.updater.start_polling(drop_pending_updates=True)
     
    asyncio.create_task(check_price_alerts_loop(tg_app, None))
    asyncio.create_task(daily_summary_reporter_loop(tg_app))

    session_one = os.getenv("SESSION_STRING_ONE", "").strip()
    if session_one:
        try:
            telethon_client_one = TelegramClient(StringSession(session_one), TELEGRAM_API_ID, TELEGRAM_API_HASH)
            await telethon_client_one.start()
            asyncio.create_task(auto_claimer_loop(telethon_client_one, "Account-1"))
        except Exception as e:
            print(f"[!] Failed Account-1: {e}")

    second_session_string = os.getenv("SECOND_SESSION_STRING", "").strip()
    if second_session_string:
        try:
            telethon_client_two = TelegramClient(StringSession(second_session_string), TELEGRAM_API_ID, TELEGRAM_API_HASH)
            await telethon_client_two.start()
            asyncio.create_task(auto_claimer_loop(telethon_client_two, "Account-2"))
        except Exception as e:
            print(f"[!] Failed Account-2: {e}")

    print("[+] Cloud Bot & Dashboard running successfully.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
