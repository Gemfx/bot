import os
import time
import asyncio
from datetime import timedelta
from threading import Thread
from urllib.parse import urlparse
import ssl
import socket

from dotenv import load_dotenv
from PIL import ImageGrab
import psutil
import requests
from bs4 import BeautifulSoup
import cv2
import yt_dlp

# Flask Webhook
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# Telegram Bot SDK
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# Discord Bot SDK
import discord
from discord.ext import commands as discord_commands

# Google GenAI SDK
from google import genai

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS_RAW.split(",") if x.strip()]

# ==========================================
# TRADING & PRICE ALERT UTILITIES
# ==========================================
ACTIVE_ALERTS = []

TICKER_MAP = {
    "usdt": "tether",
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "doge": "dogecoin",
    "ada": "cardano"
}

def fetch_pair_price(symbol: str, asset_type: str = "crypto") -> float | None:
    """Fetches real-time price for crypto (CoinGecko) or forex (ExchangeRate API)."""
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
        print(f"[!] Error fetching price for {symbol}: {e}")
    return None

def analyze_market_trend(symbol: str) -> str:
    """Fetches historical daily prices and uses TA + Gemini AI to generate a technical summary."""
    symbol_clean = TICKER_MAP.get(symbol.lower().strip(), symbol.lower().strip())
    url = f"https://api.coingecko.com/api/v3/coins/{symbol_clean}/market_chart?vs_currency=usd&days=14&interval=daily"
    
    try:
        res = requests.get(url, timeout=5).json()
        if "prices" not in res:
            return f"❌ Could not retrieve market data for '{symbol}'. Check symbol spelling."
        
        prices = [p[1] for p in res["prices"]]
        current_price = prices[-1]
        avg_14d = sum(prices) / len(prices)
        price_change_14d = ((current_price - prices[0]) / prices[0]) * 100

        # Calculate 14-period RSI
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
            f"Act as a quantitative market analyst. Provide a concise technical analysis for this asset:\n"
            f"• Asset: {symbol.upper()}\n"
            f"• Current Price: ${current_price:,.2f}\n"
            f"• 14-Day Average: ${avg_14d:,.2f}\n"
            f"• 14-Day Change: {price_change_14d:.2f}%\n"
            f"• 14-Day RSI: {rsi:.1f}\n\n"
            f"Provide:\n"
            f"1. Market Sentiment (Bullish/Bearish/Consolidating)\n"
            f"2. Indicator Signal (Evaluate RSI overbought/oversold status)\n"
            f"3. Potential Target Levels & Key Risk Caution\n\n"
            f"Keep the summary structured, direct, and under 160 words. Include a brief disclaimer that this is technical analysis, not financial advice."
        )
        
        return generate_ai_response(prompt)
    except Exception as e:
        return f"Market analysis error: {str(e)}"

async def check_price_alerts_loop(tg_app, discord_bot):
    """Background task running every 30 seconds to check active alerts for Telegram and Discord."""
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
                    f"🎯 **Target Price:** ${alert['target_price']:,.4f}\n"
                    f"💵 **Current Price:** ${current_price:,.4f}\n"
                    f"⚙️ **Condition:** Cross {alert['condition']}"
                )
                try:
                    if alert["platform"] == "telegram":
                        await tg_app.bot.send_message(chat_id=alert["channel_id"], text=msg, parse_mode="Markdown")
                    elif alert["platform"] == "discord":
                        channel = discord_bot.get_channel(alert["channel_id"])
                        if channel:
                            await channel.send(msg)
                except Exception as e:
                    print(f"[!] Alert notification error: {e}")
                
                triggered_alerts.append(alert)

        for t in triggered_alerts:
            if t in ACTIVE_ALERTS:
                ACTIVE_ALERTS.remove(t)

# ==========================================
# SYSTEM & MEDIA UTILITIES
# ==========================================
async def execute_system_command(cmd: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-Command", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode('utf-8', errors='ignore').strip() or stderr.decode('utf-8', errors='ignore').strip()
        return output if output else "Command executed successfully with no output."
    except Exception as e:
        return f"Execution Error: {str(e)}"

def capture_desktop_screenshot(file_path: str):
    screenshot = ImageGrab.grab(all_screens=True)
    screenshot.save(file_path, "PNG")

def capture_webcam_snapshot(file_path: str) -> bool:
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        return False
    ret, frame = cap.read()
    if ret:
        cv2.imwrite(file_path, frame)
    cap.release()
    return ret

def inspect_website(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")
        
        server = response.headers.get("Server", "Undetected/Hidden")
        tech_signature = response.headers.get("X-Powered-By", "None exposed")
        sec_headers = []
        if "Strict-Transport-Security" in response.headers: sec_headers.append("HSTS")
        if "Content-Security-Policy" in response.headers: sec_headers.append("CSP")
        if "X-Frame-Options" in response.headers: sec_headers.append("X-Frame")

        scripts = [s.get("src") for s in soup.find_all("script") if s.get("src")]
        detected_js = set()
        api_endpoints = set()

        for s in scripts:
            s_lower = s.lower()
            if "react" in s_lower: detected_js.add("React")
            if "vue" in s_lower: detected_js.add("Vue.js")
            if "jquery" in s_lower: detected_js.add("jQuery")
            if "next" in s_lower: detected_js.add("Next.js")

            if "/api/" in s_lower or "v1" in s_lower or "graphql" in s_lower:
                api_endpoints.add(s)

        ssl_status = "N/A (HTTP)"
        if url.startswith("https://"):
            try:
                ctx = ssl.create_default_context()
                with socket.create_connection((domain, 443), timeout=3) as sock:
                    with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                        cert = ssock.getpeercert()
                        issuer = dict(x[0] for x in cert['issuer']).get('organizationName', 'Unknown Issuer')
                        ssl_status = f"Valid (Issuer: {issuer})"
            except Exception:
                ssl_status = "HTTPS Enabled"

        frameworks_str = ', '.join(detected_js) if detected_js else 'Standard HTML / Vanilla JS'
        sec_headers_str = ', '.join(sec_headers) if sec_headers else 'None detected'
        api_str = '\n'.join(list(api_endpoints)[:2]) if api_endpoints else 'None found in main HTML.'

        return (
            f"🌐 Deep Inspection Report: {domain}\n\n"
            f"🔒 Security & Infrastructure\n"
            f"• HTTP Status: {response.status_code}\n"
            f"• SSL/TLS: {ssl_status}\n"
            f"• Server: {server}\n"
            f"• Backend Header: {tech_signature}\n"
            f"• Security Headers: {sec_headers_str}\n\n"
            f"🎨 Frontend & Frameworks\n"
            f"• Frameworks: {frameworks_str}\n"
            f"• External Scripts: {len(scripts)}\n\n"
            f"📡 Exposed API Routes\n"
            f"• {api_str}"
        )
    except Exception as e:
        return f"Error performing inspection: {str(e)}"

def download_media_file(media_url: str) -> str:
    output_folder = os.path.join(os.getcwd(), "downloads")
    os.makedirs(output_folder, exist_ok=True)
    ydl_opts = {
        'outtmpl': os.path.join(output_folder, '%(title)s.%(ext)s'),
        'format': 'best[filesize<50M]',
        'quiet': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(media_url, download=True)
        return ydl.prepare_filename(info)

def get_system_specs() -> str:
    cpu_usage = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return (
        f"💻 System Hardware Specs\n"
        f"• CPU Usage: {cpu_usage}%\n"
        f"• RAM Usage: {ram.percent}% ({round(ram.used / (1024**3), 2)} GB / {round(ram.total / (1024**3), 2)} GB)\n"
        f"• Disk Usage: {disk.percent}% ({round(disk.used / (1024**3), 2)} GB / {round(disk.total / (1024**3), 2)} GB)"
    )

def get_system_uptime() -> str:
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)
    return f"⏱️ System Uptime: {str(timedelta(seconds=uptime_seconds))}"

def get_top_processes() -> str:
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_percent']):
        try:
            processes.append(proc.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    top_proc = sorted(processes, key=lambda p: p['memory_percent'] or 0, reverse=True)[:5]
    out = "📊 Top 5 Processes\n"
    for p in top_proc:
        mem_mb = round((p['memory_percent'] or 0) * psutil.virtual_memory().total / (100 * 1024 * 1024), 1)
        out += f"• {p['name']} (PID: {p['pid']}) — {mem_mb} MB RAM\n"
    return out

# ==========================================
# AI GENERATION UTILITY
# ==========================================
def generate_ai_response(prompt: str) -> str:
    """Queries Gemini API using gemini-3.6-flash."""
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return "Error: GEMINI_API_KEY is missing in your .env file."
        
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"AI Error: {str(e)}"

# ==========================================
# WHATSAPP WEBHOOK SERVER (FLASK)
# ==========================================
whatsapp_app = Flask(__name__)

@whatsapp_app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    incoming_msg = request.form.get('Body', '').strip()
    resp = MessagingResponse()
    reply = resp.message()

    if incoming_msg.startswith("!specs"):
        reply.body(get_system_specs())
    elif incoming_msg.startswith("!uptime"):
        reply.body(get_system_uptime())
    elif incoming_msg.startswith("!processes"):
        reply.body(get_top_processes())
    elif incoming_msg.startswith("!inspect "):
        url = incoming_msg.replace("!inspect ", "").strip()
        report = inspect_website(url)
        reply.body(report)
    else:
        reply.body("CentralBot WhatsApp Active.\nCommands: !specs, !uptime, !processes, !inspect <url>")

    return str(resp)

def run_flask():
    whatsapp_app.run(port=5000, debug=False, use_reloader=False)

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    help_text = (
        "🤖 **Bot Command Menu**\n\n"
        "📈 **Market & Analysis**\n"
        "• `/predict <symbol>` — AI technical market trend analysis\n"
        "• `/crypto <ticker>` — Check live crypto price\n"
        "• `/forex <pair>` — Check live Forex rate\n"
        "• `/alert <crypto|forex> <symbol> <price> <above|below>` — Set price alert\n"
        "• `/alerts` — View active alerts\n"
        "• `/clearalerts` — Clear all active alerts\n\n"
        "💻 **System Control & Utilities**\n"
        "• `/specs` — System CPU, RAM & Disk usage\n"
        "• `/uptime` — Server uptime duration\n"
        "• `/processes` — Top resource-consuming processes\n"
        "• `/shot` — Take desktop screenshot\n"
        "• `/cam` — Capture webcam snapshot\n"
        "• `/inspect <url>` — Perform deep web security inspection\n"
        "• `/download <url>` — Download media files\n"
        "• `/run <command>` — Execute PowerShell command\n"
        "• `/lock` — Lock computer screen\n"
        "• `/shutdown` — Gracefully shut down PC\n"
        "• `/ai <prompt>` — Query Gemini AI assistant"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def tg_run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    cmd = " ".join(context.args)
    if not cmd: return await update.message.reply_text("Usage: /run <command>")
    result = await execute_system_command(cmd)
    output_text = f"```\n{result[:3700]}\n```"
    await update.message.reply_text(output_text)

async def tg_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    img_path = os.path.join(os.getcwd(), "screen.png")
    await asyncio.to_thread(capture_desktop_screenshot, img_path)
    if os.path.exists(img_path):
        with open(img_path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption="Desktop Screenshot")
        os.remove(img_path)

async def tg_cam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    cam_path = os.path.join(os.getcwd(), "webcam.jpg")
    success = await asyncio.to_thread(capture_webcam_snapshot, cam_path)
    if success and os.path.exists(cam_path):
        with open(cam_path, "rb") as photo:
            await update.message.reply_photo(photo=photo, caption="Webcam Snapshot")
        os.remove(cam_path)
    else:
        await update.message.reply_text("Webcam not available.")

async def tg_inspect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    if not context.args: return await update.message.reply_text("Usage: /inspect <url>")
    report = await asyncio.to_thread(inspect_website, context.args[0])
    await update.message.reply_text(report)

async def tg_media_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    if not context.args: return await update.message.reply_text("Usage: /download <url>")
    msg = await update.message.reply_text("Downloading media...")
    try:
        file_path = await asyncio.to_thread(download_media_file, context.args[0])
        with open(file_path, "rb") as doc:
            await update.message.reply_document(document=doc)
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Download failed: {str(e)}")

async def tg_specs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    await update.message.reply_text(get_system_specs())

async def tg_uptime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    await update.message.reply_text(get_system_uptime())

async def tg_processes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    await update.message.reply_text(get_top_processes())

async def tg_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    await execute_system_command("rundll32.exe user32.dll,LockWorkStation")

async def tg_shutdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    await execute_system_command("shutdown /s /t 10")

async def tg_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    prompt = " ".join(context.args)
    if not prompt: return await update.message.reply_text("Usage: /ai <prompt>")
    
    msg = await update.message.reply_text("Thinking...")
    reply = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit_text(reply[:3800])

async def tg_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "bitcoin"
    price = await asyncio.to_thread(fetch_pair_price, symbol, "crypto")
    if price is not None:
        await update.message.reply_text(f"💵 **{symbol.upper()} Price:** ${price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Ticker '{symbol}' not found or unsupported.")

async def tg_forex(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "eurusd"
    price = await asyncio.to_thread(fetch_pair_price, symbol, "forex")
    if price is not None:
        await update.message.reply_text(f"💱 **{symbol.upper()} Rate:** {price:,.4f}", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Forex pair '{symbol}' not found.")

async def tg_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    symbol = context.args[0] if context.args else "bitcoin"
    msg = await update.message.reply_text(f"📊 Analyzing market data for **{symbol.upper()}**...", parse_mode="Markdown")
    analysis = await asyncio.to_thread(analyze_market_trend, symbol)
    await msg.edit_text(analysis)

async def tg_set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    if len(context.args) < 4:
        return await update.message.reply_text(
            "⚠️ Usage: `/alert <crypto|forex> <symbol> <target_price> <above|below>`\n"
            "Example: `/alert crypto bitcoin 95000 above`",
            parse_mode="Markdown"
        )
    
    asset_type = context.args[0].lower()
    symbol = context.args[1].lower()
    try:
        target_price = float(context.args[2])
    except ValueError:
        return await update.message.reply_text("❌ Target price must be a valid number.")
    
    condition = context.args[3].lower()
    if condition not in ["above", "below"]:
        return await update.message.reply_text("❌ Condition must be either `above` or `below`.")

    current_price = await asyncio.to_thread(fetch_pair_price, symbol, asset_type)
    if current_price is None:
        return await update.message.reply_text(f"❌ Could not verify asset `{symbol}`. Check spelling or symbol name.")

    ACTIVE_ALERTS.append({
        "platform": "telegram",
        "channel_id": update.effective_chat.id,
        "symbol": symbol,
        "target_price": target_price,
        "condition": condition,
        "type": asset_type
    })

    await update.message.reply_text(
        f"✅ **Alert Set!**\n"
        f"• **Asset:** {symbol.upper()} ({asset_type})\n"
        f"• **Current Price:** ${current_price:,.4f}\n"
        f"• **Trigger:** When price goes **{condition}** ${target_price:,.4f}",
        parse_mode="Markdown"
    )

async def tg_list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    chat_alerts = [a for a in ACTIVE_ALERTS if a["channel_id"] == update.effective_chat.id]
    if not chat_alerts:
        return await update.message.reply_text("No active price alerts set.")
    
    out = "📊 **Active Price Alerts:**\n\n"
    for i, a in enumerate(chat_alerts, 1):
        out += f"{i}. **{a['symbol'].upper()}** ({a['type']}) — Target: ${a['target_price']:,.4f} ({a['condition']})\n"
    await update.message.reply_text(out, parse_mode="Markdown")

async def tg_clear_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    global ACTIVE_ALERTS
    ACTIVE_ALERTS = [a for a in ACTIVE_ALERTS if a["channel_id"] != update.effective_chat.id]
    await update.message.reply_text("🧹 All price alerts cleared.")

# ==========================================
# DISCORD HANDLERS
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
discord_bot = discord_commands.Bot(command_prefix="!", intents=intents, help_command=None)

@discord_bot.event
async def on_ready():
    print(f"[+] Discord Bot connected as {discord_bot.user}")

@discord_bot.command(name="help")
async def discord_help(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    embed = discord.Embed(
        title="🤖 Bot Commands & Help Menu",
        description="Type any command prefixed with `!` to execute.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📈 Market & Analysis",
        value=(
            "`!predict <symbol>` — Market trend prediction via Gemini AI\n"
            "`!crypto <ticker>` — Real-time crypto price lookup\n"
            "`!forex <pair>` — Real-time Forex rate lookup\n"
            "`!alert <crypto|forex> <symbol> <price> <above|below>` — Set price alert\n"
            "`!alerts` — View active alerts\n"
            "`!clearalerts` — Clear active channel alerts"
        ),
        inline=False
    )
    embed.add_field(
        name="💻 System & Utilities",
        value=(
            "`!specs` — Hardware usage (CPU/RAM/Disk)\n"
            "`!uptime` — System uptime status\n"
            "`!processes` — Top RAM-consuming processes\n"
            "`!shot` — Capture desktop screenshot\n"
            "`!cam` — Take webcam snapshot\n"
            "`!inspect <url>` — Deep website technical inspection\n"
            "`!download <url>` — Download media\n"
            "`!run <command>` — Execute PowerShell script\n"
            "`!ai <prompt>` — Ask Gemini AI assistant"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@discord_bot.command(name="run")
async def discord_run(ctx, *, command_to_run: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    result = await execute_system_command(command_to_run)
    await ctx.send(f"```\n{result[:1800]}\n```")

@discord_bot.command(name="shot")
async def discord_shot(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    img_path = os.path.join(os.getcwd(), "screen.png")
    await asyncio.to_thread(capture_desktop_screenshot, img_path)
    if os.path.exists(img_path):
        await ctx.send(file=discord.File(img_path))
        os.remove(img_path)

@discord_bot.command(name="cam")
async def discord_cam(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    cam_path = os.path.join(os.getcwd(), "webcam.jpg")
    success = await asyncio.to_thread(capture_webcam_snapshot, cam_path)
    if success and os.path.exists(cam_path):
        await ctx.send(file=discord.File(cam_path))
        os.remove(cam_path)
    else:
        await ctx.send("Webcam not available.")

@discord_bot.command(name="inspect")
async def discord_inspect(ctx, url: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    report = await asyncio.to_thread(inspect_website, url)
    await ctx.send(report)

@discord_bot.command(name="download")
async def discord_download(ctx, url: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    msg = await ctx.send("Downloading media...")
    try:
        file_path = await asyncio.to_thread(download_media_file, url)
        await ctx.send(file=discord.File(file_path))
        os.remove(file_path)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Download failed: {str(e)}")

@discord_bot.command(name="specs")
async def discord_specs(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    await ctx.send(get_system_specs())

@discord_bot.command(name="uptime")
async def discord_uptime(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    await ctx.send(get_system_uptime())

@discord_bot.command(name="processes")
async def discord_processes(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    await ctx.send(get_top_processes())

@discord_bot.command(name="ai")
async def discord_ai(ctx, *, prompt: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    msg = await ctx.send("Thinking...")
    reply = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit(content=reply[:1900])

@discord_bot.command(name="crypto")
async def discord_crypto(ctx, ticker: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    price = await asyncio.to_thread(fetch_pair_price, ticker, "crypto")
    if price is not None:
        await ctx.send(f"💵 **{ticker.upper()} Price:** ${price:,.4f}")
    else:
        await ctx.send(f"❌ Ticker '{ticker}' not found or unsupported.")

@discord_bot.command(name="forex")
async def discord_forex(ctx, symbol: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    price = await asyncio.to_thread(fetch_pair_price, symbol, "forex")
    if price is not None:
        await ctx.send(f"💱 **{symbol.upper()} Rate:** {price:,.4f}")
    else:
        await ctx.send(f"❌ Forex pair '{symbol}' not found.")

@discord_bot.command(name="predict")
async def discord_predict(ctx, symbol: str = "bitcoin"):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    msg = await ctx.send(f"📊 Analyzing market trend for **{symbol.upper()}**...")
    analysis = await asyncio.to_thread(analyze_market_trend, symbol)
    await msg.edit(content=analysis[:1900])

@discord_bot.command(name="alert")
async def discord_set_alert(ctx, asset_type: str, symbol: str, target_price: float, condition: str):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    asset_type = asset_type.lower()
    symbol = symbol.lower()
    condition = condition.lower()

    if condition not in ["above", "below"]:
        return await ctx.send("❌ Condition must be either `above` or `below`.")

    current_price = await asyncio.to_thread(fetch_pair_price, symbol, asset_type)
    if current_price is None:
        return await ctx.send(f"❌ Could not verify asset `{symbol}`. Check spelling or symbol name.")

    ACTIVE_ALERTS.append({
        "platform": "discord",
        "channel_id": ctx.channel.id,
        "symbol": symbol,
        "target_price": target_price,
        "condition": condition,
        "type": asset_type
    })

    await ctx.send(
        f"✅ **Alert Set!**\n"
        f"• **Asset:** {symbol.upper()} ({asset_type})\n"
        f"• **Current Price:** ${current_price:,.4f}\n"
        f"• **Trigger:** When price goes **{condition}** ${target_price:,.4f}"
    )

@discord_bot.command(name="alerts")
async def discord_list_alerts(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    channel_alerts = [a for a in ACTIVE_ALERTS if a.get("channel_id") == ctx.channel.id]
    if not channel_alerts:
        return await ctx.send("No active price alerts set in this channel.")
    
    out = "📊 **Active Price Alerts:**\n\n"
    for i, a in enumerate(channel_alerts, 1):
        out += f"{i}. **{a['symbol'].upper()}** ({a['type']}) — Target: ${a['target_price']:,.4f} ({a['condition']})\n"
    await ctx.send(out)

@discord_bot.command(name="clearalerts")
async def discord_clear_alerts(ctx):
    if ALLOWED_USERS and str(ctx.author.id) not in ALLOWED_USERS: return
    global ACTIVE_ALERTS
    ACTIVE_ALERTS = [a for a in ACTIVE_ALERTS if a.get("channel_id") != ctx.channel.id]
    await ctx.send("🧹 All price alerts cleared for this channel.")

# ==========================================
# MAIN ENGINE
# ==========================================
async def main():
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("[+] WhatsApp Webhook Listener active on port 5000.")

    request_kwargs = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0
    )

    tg_app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(request_kwargs)
        .build()
    )

    # Register Telegram Handlers
    commands_dict = {
        "help": tg_help,
        "run": tg_run_command,
        "shot": tg_screenshot,
        "cam": tg_cam,
        "inspect": tg_inspect,
        "download": tg_media_download,
        "specs": tg_specs,
        "uptime": tg_uptime,
        "processes": tg_processes,
        "lock": tg_lock,
        "shutdown": tg_shutdown,
        "ai": tg_ai,
        "crypto": tg_crypto,
        "forex": tg_forex,
        "predict": tg_predict,
        "alert": tg_set_alert,
        "alerts": tg_list_alerts,
        "clearalerts": tg_clear_alerts
    }

    for name, handler in commands_dict.items():
        tg_app.add_handler(CommandHandler([name, name.upper(), name.capitalize()], handler))

    await tg_app.initialize()
    await tg_app.start()

    # Automatically registers command menu suggestions directly inside Telegram UI
    tg_menu_commands = [
        BotCommand("predict", "AI Technical Trend Analysis"),
        BotCommand("crypto", "Fetch Crypto Price"),
        BotCommand("forex", "Fetch Forex Exchange Rate"),
        BotCommand("alert", "Set Price Trigger Alert"),
        BotCommand("alerts", "List Active Price Alerts"),
        BotCommand("specs", "System CPU, RAM & Disk Usage"),
        BotCommand("shot", "Capture Desktop Screenshot"),
        BotCommand("cam", "Capture Webcam Snapshot"),
        BotCommand("inspect", "Perform Web Security Inspection"),
        BotCommand("help", "Display All Bot Commands")
    ]
    await tg_app.bot.set_my_commands(tg_menu_commands)

    await tg_app.updater.start_polling(drop_pending_updates=True)

    asyncio.create_task(check_price_alerts_loop(tg_app, discord_bot))

    print("[+] Bot Engine active across Telegram, Discord, and WhatsApp with Interactive Menus.")

    try:
        await discord_bot.start(DISCORD_TOKEN)
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[-] Shutting down bot engine.")