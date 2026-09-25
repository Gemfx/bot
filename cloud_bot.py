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

from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

import discord
from discord.ext import commands as discord_commands

from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS_RAW.split(",") if x.strip()]

# --- TELETHON MINING USERBOT CONFIG ---
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "1234567"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "f72565d820fde421d56172f2261dadd4")

MINING_BOTS_RAW = os.getenv("MINING_BOT_USERNAME", "@YourTargetMiningBot")
MINING_BOT_USERNAMES = [b.strip() for b in MINING_BOTS_RAW.split(",") if b.strip()]

STATUS_COMMAND = "/balance"
CLAIM_KEYWORDS = ["full", "ready", "storage full", "available", "harvest", "collect", "cycle is complete"]
# --------------------------------------

ACTIVE_ALERTS = []

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
            model="gemini-3.6-flash",
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

# --- ADVANCED MINI APP WEBVIEW AUTO-CLAIMER ---
async def auto_claimer_loop(telethon_client, account_label="Account-1"):
    print(f"[+] Telethon Mini-App Auto-Claimer started for [{account_label}] targeting: {MINING_BOT_USERNAMES}")
    await asyncio.sleep(10)
    while True:
        for bot_username in MINING_BOT_USERNAMES:
            try:
                print(f"[*] [{account_label}] Checking status for mining bot: {bot_username}")
                bot_entity = await telethon_client.get_entity(bot_username)
                await telethon_client.send_message(bot_entity, STATUS_COMMAND)
                
                await asyncio.sleep(6)
                messages = await telethon_client.get_messages(bot_entity, limit=3)
                
                for latest_msg in messages:
                    if not latest_msg.message:
                        continue
                    message_text = latest_msg.message.lower()
                    print(f"[*] [{account_label}] [{bot_username}] Got Message: {latest_msg.message}")
                    
                    should_claim = any(keyword in message_text for keyword in CLAIM_KEYWORDS)
                    if should_claim:
                        print(f"[+] [{account_label}] [{bot_username}] Storage full detected! Launching WebApp session...")
                        claimed = False
                        
                        if latest_msg.reply_markup and hasattr(latest_msg.reply_markup, 'rows'):
                            try:
                                for row in latest_msg.reply_markup.rows:
                                    for button in row.buttons:
                                        # Check if button is a WebApp button
                                        if hasattr(button, 'url') and button.url:
                                            btn_text = button.text.lower()
                                            if any(k in btn_text for k in ["claim", "harvest", "collect", "reward"]):
                                                print(f"[+] [{account_label}] Opening WebApp URL for button: {button.text}")
                                                # Fetch and trigger the WebApp container session via Telethon MTProto
                                                webview = await telethon_client(RequestWebViewRequest(
                                                    peer=bot_entity,
                                                    bot=bot_entity,
                                                    platform='android',
                                                    url=button.url
                                                ))
                                                # Hit the webview url via requests to simulate a ping/claim execution
                                                if webview and hasattr(webview, 'url'):
                                                    requests.get(webview.url, timeout=10)
                                                    print(f"[+] [{account_label}] Successfully triggered WebApp claim URL!")
                                                    claimed = True
                                                    break
                                    if claimed:
                                        break
                            except Exception as web_err:
                                print(f"[-] [{account_label}] WebApp invocation error: {web_err}")
                        
                        # Fallback text commands if WebApp trigger fails
                        if not claimed:
                            await telethon_client.send_message(bot_entity, "/claim")
                            print(f"[+] [{account_label}] Sent text fallback command: /claim")
                        break
                
                await asyncio.sleep(10)
            except Exception as e:
                print(f"[-] [{account_label}] Error checking bot {bot_username}: {e}")
        
        await asyncio.sleep(1800)

# Telegram Handlers
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **24/7 Cloud Bot Commands**\n\n"
        "📈 `/predict <symbol>` — AI Technical Analysis\n"
        "💵 `/crypto <ticker>` — Live Crypto Price\n"
        "💱 `/forex <pair>` — Live Forex Rate\n"
        "🚨 `/alert <crypto|forex> <symbol> <price> <above|below>`\n"
        "📊 `/alerts` — List Active Alerts\n"
        "🧹 `/clearalerts` — Clear Active Alerts\n"
        "🤖 `/ai <prompt>` — Gemini AI Assistant"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

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

async def tg_forex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "eurusd"
    price = await asyncio.to_thread(fetch_pair_price, symbol, "forex")
    if price:
        await update.message.reply_text(f"💱 **{symbol.upper()}:** {price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Forex pair not found.")

async def tg_set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    if len(context.args) < 4:
        return await update.message.reply_text("Usage: `/alert crypto bitcoin 95000 above`", parse_mode="Markdown")
    asset_type, symbol, target_price, condition = context.args[0].lower(), context.args[1].lower(), float(context.args[2]), context.args[3].lower()
    
    ACTIVE_ALERTS.append({
        "platform": "telegram",
        "channel_id": update.effective_chat.id,
        "symbol": symbol,
        "target_price": target_price,
        "condition": condition,
        "type": asset_type
    })
    await update.message.reply_text(f"✅ Alert set for {symbol.upper()} {condition} ${target_price:,.4f}")

async def tg_list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    chat_alerts = [a for a in ACTIVE_ALERTS if a["channel_id"] == update.effective_chat.id]
    if not chat_alerts: return await update.message.reply_text("No active alerts.")
    out = "📊 **Active Alerts:**\n"
    for i, a in enumerate(chat_alerts, 1):
        out += f"{i}. {a['symbol'].upper()} - Target: ${a['target_price']:,.4f} ({a['condition']})\n"
    await update.message.reply_text(out, parse_mode="Markdown")

async def tg_clear_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    global ACTIVE_ALERTS
    ACTIVE_ALERTS = [a for a in ACTIVE_ALERTS if a["channel_id"] != update.effective_chat.id]
    await update.message.reply_text("🧹 Alerts cleared.")

async def tg_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    prompt = " ".join(context.args)
    if not prompt: return await update.message.reply_text("Usage: /ai <prompt>")
    msg = await update.message.reply_text("Thinking...")
    reply = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit_text(reply[:3800])

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
discord_bot = discord_commands.Bot(command_prefix="!", intents=intents, help_command=None)

@discord_bot.command(name="help")
async def discord_help(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    embed = discord.Embed(title="🤖 24/7 Cloud Bot Commands", color=discord.Color.blue())
    embed.add_field(name="Commands", value="`!predict <symbol>`\n`!crypto <ticker>`\n`!forex <pair>`\n`!alert <type> <symbol> <price> <above|below>`\n`!alerts`\n`!clearalerts`\n`!ai <prompt>`", inline=False)
    await ctx.send(embed=embed)

@discord_bot.command(name="predict")
async def discord_predict(ctx, symbol: str = "bitcoin"):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    msg = await ctx.send(f"📊 Analyzing {symbol.upper()}...")
    analysis = await asyncio.to_thread(analyze_market_trend, symbol)
    await msg.edit(content=analysis[:1900])

@discord_bot.command(name="crypto")
async def discord_crypto(ctx, ticker: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    price = await asyncio.to_thread(fetch_pair_price, ticker, "crypto")
    if price: await ctx.send(f"💵 **{ticker.upper()}:** ${price:,.4f}")
    else: await ctx.send("❌ Ticker not found.")

@discord_bot.command(name="forex")
async def discord_forex(ctx, symbol: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    price = await asyncio.to_thread(fetch_pair_price, symbol, "forex")
    if price: await ctx.send(f"💱 **{symbol.upper()}:** {price:,.4f}")
    else: await ctx.send("❌ Forex pair not found.")

@discord_bot.command(name="ai")
async def discord_ai(ctx, *, prompt: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    msg = await ctx.send("Thinking...")
    reply = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit(content=reply[:1900])

async def main():
    tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()
    
    handlers = {
        "help": tg_help, "predict": tg_predict, "crypto": tg_crypto, 
        "forex": tg_forex, "alert": tg_set_alert, "alerts": tg_list_alerts, 
        "clearalerts": tg_clear_alerts, "ai": tg_ai
    }
    for name, handler in handlers.items():
        tg_app.add_handler(CommandHandler([name, name.upper()], handler))

    await tg_app.initialize()
    await tg_app.start()
    
    tg_menu = [
        BotCommand("predict", "AI Technical Analysis"),
        BotCommand("crypto", "Crypto Price Lookup"),
        BotCommand("forex", "Forex Rate Lookup"),
        BotCommand("alert", "Set Price Alert"),
        BotCommand("alerts", "List Active Alerts"),
        BotCommand("help", "Show Bot Commands")
    ]
    await tg_app.bot.set_my_commands(tg_menu)
    await tg_app.updater.start_polling(drop_pending_updates=True)
    
    asyncio.create_task(check_price_alerts_loop(tg_app, discord_bot))

    # --- INITIALIZE BOTH TELETHON CLIENTS ---
    telethon_client_one = TelegramClient('mining_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    
    second_session_string = os.getenv("SECOND_SESSION_STRING", "")
    telethon_client_two = TelegramClient(StringSession(second_session_string), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    
    await telethon_client_one.start()
    await telethon_client_two.start()
    
    asyncio.create_task(auto_claimer_loop(telethon_client_one, "Account-1"))
    asyncio.create_task(auto_claimer_loop(telethon_client_two, "Account-2"))
    # ----------------------------------------

    print("[+] Master Cloud Bot + Dual Telegram WebApp Auto-Claimers active 24/7.")
    try:
        await discord_bot.start(DISCORD_TOKEN)
    finally:
        await telethon_client_one.disconnect()
        await telethon_client_two.disconnect()
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
