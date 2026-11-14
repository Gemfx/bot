import os
import re
import uuid
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

STATUS_COMMAND = "/claim"
# --------------------------------------

ACTIVE_ALERTS = []
MINING_STATUS_STORE = {}

# --- WEB & MINI-APP STUDIO STATE ---
GENERATED_SITES_DIR = "generated_sites"
os.makedirs(GENERATED_SITES_DIR, exist_ok=True)
USER_WEB_PROJECTS = {}  # {user_id: {"site_id": str, "html": str, "active": bool}}
# -----------------------------------

DYNAMIC_CONFIG = {
    "polling_interval": 900,
    "auto_claim_enabled": True,
    "active_bots": list(MINING_BOT_USERNAMES)
}

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

# --- RESILIENT AI CALL WITH 503 FALLBACK ---
def generate_ai_response(prompt: str) -> str:
    try:
        if not GEMINI_API_KEY:
            return "Error: GEMINI_API_KEY is missing."
        client = genai.Client(api_key=GEMINI_API_KEY)
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt,
            )
            return response.text
        except Exception as primary_err:
            print(f"[!] Primary model busy (503), falling back to gemini-2.5-flash: {primary_err}")
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return response.text
    except Exception as e:
        return f"AI Error: {str(e)}"

# --- MULTI-TURN WEB & MINI-APP CODE GENERATOR WITH FALLBACK ---
def extract_clean_html(raw_text: str) -> str:
    match = re.search(r"```(?:html)?\s*(.*?)```", raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw_text.strip()

def build_or_update_webapp(prompt: str, existing_html: str | None = None) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is missing in environment variables.")

    client = genai.Client(api_key=GEMINI_API_KEY)

    if not existing_html:
        system_prompt = (
            "You are an elite full-stack web developer and UI/UX designer specializing in modern responsive websites "
            "and Telegram Mini-Apps.\n"
            "Build a COMPLETE, self-contained, single-file HTML5 application based on the user's request.\n"
            "Rules:\n"
            "1. Include Tailwind CSS via CDN (<script src='https://cdn.tailwindcss.com'></script>).\n"
            "2. Include the Telegram WebApp SDK (<script src='https://telegram.org/js/telegram-web-app.js'></script>) and initialize window.Telegram?.WebApp?.ready().\n"
            "3. Write rich, interactive, working JavaScript logic.\n"
            "4. Output ONLY the raw HTML code starting with <!DOCTYPE html>. No explanations outside code.\n\n"
            f"User Request: {prompt}"
        )
    else:
        system_prompt = (
            "You are an elite full-stack web developer modifying an existing single-file HTML5/Tailwind/JS web application.\n"
            "Apply the user's requested changes cleanly while preserving all existing working features and Telegram WebApp SDK integration.\n"
            "Output ONLY the complete, updated raw HTML code starting with <!DOCTYPE html>.\n\n"
            f"Requested Changes: {prompt}\n\n"
            f"Current HTML Code:\n{existing_html}"
        )

    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=system_prompt,
        )
        return extract_clean_html(response.text)
    except Exception as e:
        print(f"[!] Primary model 503 error during build, falling back to gemini-2.5-flash: {e}")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=system_prompt,
        )
        return extract_clean_html(response.text)
# -------------------------------------------------

async def analyze_mining_screenshot_with_ai(image_bytes: bytes, bot_name: str) -> str:
    try:
        if not GEMINI_API_KEY:
            return "AI Vision Key Missing"
        client = genai.Client(api_key=GEMINI_API_KEY)
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=[image_bytes, f"Analyze UI for {bot_name}. Is claim active? YES/NO. Keep under 30 words."]
            )
            return response.text
        except Exception:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[image_bytes, f"Analyze UI for {bot_name}. Is claim active? YES/NO. Keep under 30 words."]
            )
            return response.text
    except Exception as e:
        return f"Vision Error: {str(e)}"

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
                except Exception as e:
                    print(f"[!] Notification error: {e}")
                 
                triggered_alerts.append(alert)

        for t in triggered_alerts:
            if t in ACTIVE_ALERTS:
                ACTIVE_ALERTS.remove(t)

async def daily_summary_reporter_loop(tg_app):
    while True:
        try:
            now = time.localtime()
            current_seconds = now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec
            target_seconds = 8 * 3600
            
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
                        status_emoji = "🟢" if "READY" in info["status"] else "⏱️"
                        report += f"  {status_emoji} `{bot_name}`: **{info['status']}**\n"
                    report += "\n"
            report += "📊 _Check your web dashboard for full operational stats._"
            
            if ALLOWED_USERS:
                for user_id in ALLOWED_USERS:
                    try:
                        await tg_app.bot.send_message(chat_id=int(user_id), text=report, parse_mode="Markdown")
                    except Exception as e:
                        print(f"[!] Failed to send daily report: {e}")
            
            await asyncio.sleep(60)
        except Exception as e:
            print(f"[!] Error in daily summary reporter loop: {e}")
            await asyncio.sleep(3600)

async def auto_claimer_loop(telethon_client, account_label="Account-1"):
    print(f"[+] Autonomous Mini-App Radar started for [{account_label}]")
    await asyncio.sleep(10)
     
    last_known_state = {}

    while True:
        current_bots = DYNAMIC_CONFIG.get("active_bots", MINING_BOT_USERNAMES)
        for bot_username in current_bots:
            try:
                print(f"[*] [{account_label}] Scanning Mini-App state for: {bot_username}")
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

# --- FASTAPI BACKEND API SETUP ---
api_app = FastAPI(title="Cloud Bot & Mining Dashboard API")

@api_app.on_event("startup")
async def startup_event():
    asyncio.create_task(main())

@api_app.get("/", response_class=HTMLResponse)
def root():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return {"status": "online", "service": "Master Cloud Bot & Web Studio active"}

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

@api_app.post("/api/create-invoice")
async def create_invoice():
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

# --- TELEGRAM COMMAND HANDLERS ---
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **Cloud Bot & AI Web Studio**\n\n"
        "🌐 `/build <idea>` — Create & host a live Website/Mini-App\n"
        "✅ `/done` — Exit interactive Web Studio mode\n"
        "📊 `/dashboard` — Open Mining Control Center\n"
        "📈 `/predict <symbol>` — AI Market Analysis\n"
        "💵 `/crypto <ticker>` — Crypto Spot Price\n"
        "⭐ `/upgrade` — Buy Pro Pass with Stars\n"
        "🧠 `/ai <prompt>` — Gemini AI Assistant"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def tg_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text(
            "💡 **Usage:** `/build <describe your website or mini-app>`\n"
            "Example: `/build A modern dark-mode crypto portfolio dashboard`",
            parse_mode="Markdown"
        )
        return

    user_id = update.effective_user.id
    status_msg = await update.message.reply_text("⚡ **Web Studio:** Architecting and deploying your app...", parse_mode="Markdown")

    try:
        html_code = await asyncio.to_thread(build_or_update_webapp, prompt, None)
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
            f"✅ **Website & Mini-App Deployed!**\n\n"
            f"🔗 **Live URL:** `{live_url}`\n\n"
            f"🎨 **Studio Mode Active:** Reply with any edit instructions to update the live site instantly, or send `/done` to finish.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        with open(file_path, "rb") as doc:
            await update.message.reply_document(
                document=doc,
                filename=f"{site_id}.html",
                caption="📦 Source code (`index.html` ready for hosting)"
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ Build failed: {str(e)}")

async def tg_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    user_id = update.effective_user.id
    if user_id in USER_WEB_PROJECTS and USER_WEB_PROJECTS[user_id].get("active"):
        USER_WEB_PROJECTS[user_id]["active"] = False
        await update.message.reply_text("✅ **Web Studio Session Closed.** Your site remains live at its link. Use `/build` anytime to start a new project.", parse_mode="Markdown")
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
    status_msg = await update.message.reply_text("🔄 **Applying live edits to your web app...**", parse_mode="Markdown")

    try:
        updated_html = await asyncio.to_thread(build_or_update_webapp, edit_prompt, project["html"])
        site_id = project["site_id"]
        file_path = os.path.join(GENERATED_SITES_DIR, f"{site_id}.html")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated_html)

        project["html"] = updated_html

        base_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com").rstrip("/")
        live_url = f"{base_url}/apps/{site_id}"

        keyboard = [
            [InlineKeyboardButton("🚀 Preview Updated Mini-App", web_app={"url": live_url})],
            [InlineKeyboardButton("🌐 Open in External Browser", url=live_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await status_msg.edit_text(
            f"✨ **Live App Updated!**\n\n"
            f"🔗 **URL:** `{live_url}`\n"
            f"💬 Keep sending messages for more changes, or type `/done` to close Studio Mode.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

        with open(file_path, "rb") as doc:
            await update.message.reply_document(
                document=doc,
                filename=f"{site_id}_updated.html",
                caption="📦 Updated HTML source file"
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ Update failed: {str(e)}")

async def tg_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Usage: `/ai <your question>`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🧠 Thinking...")
    reply = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit_text(reply)

async def tg_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    app_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
    keyboard = [[InlineKeyboardButton("📊 Open Mining Dashboard", web_app={"url": app_url})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("🚀 **Autonomous Mining Control Center**", reply_markup=reply_markup, parse_mode="Markdown")

async def tg_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
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
    await update.message.reply_text("🎉 **Subscription Activated!** Cloud automation pass active.")

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
        "help": tg_help,
        "build": tg_build,
        "done": tg_done,
        "ai": tg_ai,
        "dashboard": tg_dashboard,
        "predict": tg_predict,
        "crypto": tg_crypto,
        "upgrade": tg_upgrade
    }
    for name, handler in handlers.items():
        tg_app.add_handler(CommandHandler([name, name.upper()], handler))

    tg_app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_studio_message_handler))

    await tg_app.initialize()
    await tg_app.start()
     
    tg_menu = [
        BotCommand("build", "Create & Host a Website/Mini-App"),
        BotCommand("done", "Exit Web Studio Edit Mode"),
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

    print("[+] Cloud Bot, Mining Radar & Interactive Web Studio running.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
