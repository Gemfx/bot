"""
cloud_bot.py — Mining Radar Cloud Bot (fixed build)

Fixes in this version:
  - Ready alerts are sent by your real bot (python-telegram-bot), not the userbot,
    so the "Open & Claim" button actually works.
  - The radar waits for the mining bot's actual reply and ignores your own /claim message.
  - Every dashboard API endpoint requires auth (Telegram WebApp initData or an API key).
  - Stars payments are saved to a database with a real 30-day expiry.
  - Config, mining status and price alerts survive restarts (SQLite or Postgres).
  - Gemini model is configurable via GEMINI_MODEL (default: gemini-2.5-flash).
  - Messages use HTML formatting, so bold text and @bot_names display correctly.
  - Daily report runs on your timezone (default Africa/Lagos).
  - Dead/logged-out sessions are detected and reported instead of hanging.
  - Clean shutdown on redeploy (reduces Telegram 409 Conflict errors).
  - Unused imports removed; /ai and /alert commands now actually exist.
"""

import os
import re
import json
import time
import hmac
import random
import hashlib
import asyncio
import traceback
from html import escape as h
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import requests
import uvicorn
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from telegram import (
    Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    PreCheckoutQueryHandler, MessageHandler, filters,
)
from telegram.request import HTTPXRequest

from google import genai
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

# =====================================================================
# CONFIG (all from environment variables — no secrets in code)
# =====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
ALLOWED_USERS = [x.strip() for x in os.getenv("ALLOWED_USERS", "").split(",") if x.strip()]
DASHBOARD_API_KEY = os.getenv("DASHBOARD_API_KEY", "").strip()

_api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_ID = int(_api_id_raw) if _api_id_raw.isdigit() else None
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

ENV_BOTS = [
    b.strip()
    for b in os.getenv("MINING_BOT_USERNAME", "@UltrawalletTrade_Bot,@ATF_AIRDROP_bot").split(",")
    if b.strip()
]
STATUS_COMMAND = os.getenv("STATUS_COMMAND", "/claim").strip()
REPLY_TIMEOUT = int(os.getenv("REPLY_TIMEOUT", "20"))  # seconds to wait for a mining bot reply

REPORT_TZ = ZoneInfo(os.getenv("REPORT_TIMEZONE", "Africa/Lagos"))
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))

PASS_PRICE_STARS = int(os.getenv("PASS_PRICE_STARS", "150"))
PASS_DAYS = 30
PASS_PAYLOAD = "monthly_subscription_pass"

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///cloud_bot.db").strip()
# Tell SQLAlchemy to use the psycopg2 driver we install (SQLAlchemy 2.1+ otherwise looks for "psycopg" v3).
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_prefix):]
        break

DEFAULT_CONFIG = {
    "polling_interval": 900,          # seconds between full radar sweeps
    "auto_claim_enabled": True,       # master on/off switch for the radar
    "active_bots": list(ENV_BOTS),
    "ready_keywords": ["ready to claim", "ready", "claim available", "harvest", "complete", "limit reached"],
    "not_ready_keywords": ["remaining", "hrs", "mins", "come back", "next claim in"],
}
TIMER_PATTERN = re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")  # e.g. 17:35:17

TICKER_MAP = {
    "usdt": "tether", "btc": "bitcoin", "eth": "ethereum",
    "sol": "solana", "bnb": "binancecoin", "xrp": "ripple",
    "doge": "dogecoin", "ada": "cardano", "ton": "the-open-network",
}

# =====================================================================
# RUNTIME STATE
# =====================================================================
DYNAMIC_CONFIG = dict(DEFAULT_CONFIG)
MINING_STATUS_STORE = {}
ACTIVE_ALERTS = []
TG_APP = None
TELETHON_CLIENTS = {}     # {1: TelegramClient, 2: TelegramClient}
BACKGROUND_TASKS = []
_gemini_client = None


def clip(s: str, limit: int = 4000) -> str:
    """Telegram messages max out at 4096 characters."""
    return s if len(s) <= limit else s[: limit - 3] + "..."


# =====================================================================
# DATABASE (SQLite locally, Postgres in production via DATABASE_URL)
# =====================================================================
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def db_init():
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS kv_store (k VARCHAR(100) PRIMARY KEY, v TEXT NOT NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS payments ("
            " charge_id VARCHAR(255) PRIMARY KEY,"
            " user_id BIGINT NOT NULL,"
            " stars INTEGER NOT NULL,"
            " paid_at DOUBLE PRECISION NOT NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS subscriptions ("
            " user_id BIGINT PRIMARY KEY,"
            " expires_at DOUBLE PRECISION NOT NULL)"
        ))


def kv_get(key: str, default=None):
    with engine.connect() as conn:
        row = conn.execute(text("SELECT v FROM kv_store WHERE k = :k"), {"k": key}).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(key: str, value):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO kv_store (k, v) VALUES (:k, :v) "
                 "ON CONFLICT (k) DO UPDATE SET v = excluded.v"),
            {"k": key, "v": json.dumps(value)},
        )


async def save_state(key: str, value):
    try:
        await asyncio.to_thread(kv_set, key, value)
    except Exception as e:
        print(f"[!] DB save error ({key}): {e}")


def record_payment(user_id: int, charge_id: str, stars: int) -> float:
    """Saves a payment and extends the user's pass. Returns the new expiry timestamp.
    Duplicate charge IDs (Telegram retries) never extend the pass twice."""
    now = time.time()
    with engine.begin() as conn:
        inserted = conn.execute(
            text("INSERT INTO payments (charge_id, user_id, stars, paid_at) "
                 "VALUES (:c, :u, :s, :t) ON CONFLICT (charge_id) DO NOTHING"),
            {"c": charge_id, "u": user_id, "s": stars, "t": now},
        ).rowcount
        row = conn.execute(
            text("SELECT expires_at FROM subscriptions WHERE user_id = :u"), {"u": user_id}
        ).fetchone()
        current = row[0] if row else 0.0
        if not inserted:
            return current
        new_expiry = max(now, current) + PASS_DAYS * 86400
        conn.execute(
            text("INSERT INTO subscriptions (user_id, expires_at) VALUES (:u, :e) "
                 "ON CONFLICT (user_id) DO UPDATE SET expires_at = excluded.expires_at"),
            {"u": user_id, "e": new_expiry},
        )
    return new_expiry


def get_subscription_expiry(user_id: int) -> float | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT expires_at FROM subscriptions WHERE user_id = :u"), {"u": user_id}
        ).fetchone()
    return row[0] if row else None


def load_state_sync():
    global DYNAMIC_CONFIG, MINING_STATUS_STORE, ACTIVE_ALERTS
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(kv_get("config", {}) or {})

    # If you changed MINING_BOT_USERNAME on Render, the new env list wins.
    # Otherwise the list saved from the dashboard is kept.
    if kv_get("env_bots_snapshot", None) != ENV_BOTS:
        cfg["active_bots"] = list(ENV_BOTS)
        kv_set("env_bots_snapshot", ENV_BOTS)

    DYNAMIC_CONFIG = cfg
    kv_set("config", cfg)
    MINING_STATUS_STORE = kv_get("mining_status", {}) or {}
    ACTIVE_ALERTS = kv_get("price_alerts", []) or []


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, REPORT_TZ).strftime("%d %b %Y, %H:%M")


# =====================================================================
# AI + MARKET DATA
# =====================================================================
def get_gemini():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def generate_ai_response(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return "Error: GEMINI_API_KEY is missing."
    try:
        response = get_gemini().models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return response.text or "(empty response)"
    except Exception as e:
        print(f"[!] Gemini error with model '{GEMINI_MODEL}': {e}")
        return f"AI Error ({GEMINI_MODEL}): {e}"


def fetch_pair_price(symbol: str, asset_type: str = "crypto") -> float | None:
    symbol_clean = symbol.lower().strip()
    try:
        if asset_type == "crypto":
            symbol_clean = TICKER_MAP.get(symbol_clean, symbol_clean)
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={symbol_clean}&vs_currencies=usd"
            res = requests.get(url, timeout=8).json()
            if symbol_clean in res:
                return float(res[symbol_clean]["usd"])
        elif asset_type == "forex":
            base = symbol_clean[:3].upper()
            target = symbol_clean[3:].upper() if len(symbol_clean) >= 6 else "USD"
            res = requests.get(f"https://open.er-api.com/v6/latest/{base}", timeout=8).json()
            if "rates" in res and target in res["rates"]:
                return float(res["rates"][target])
    except Exception as e:
        print(f"[!] Price fetch error: {e}")
    return None


def analyze_market_trend(symbol: str) -> str:
    symbol_clean = TICKER_MAP.get(symbol.lower().strip(), symbol.lower().strip())
    url = (f"https://api.coingecko.com/api/v3/coins/{symbol_clean}/market_chart"
           f"?vs_currency=usd&days=14&interval=daily")
    try:
        res = requests.get(url, timeout=8).json()
        if "prices" not in res or len(res["prices"]) < 2:
            return f"❌ Could not retrieve market data for '{symbol}'."
        prices = [p[1] for p in res["prices"]]
        current_price = prices[-1]
        avg_14d = sum(prices) / len(prices)
        change_14d = ((current_price - prices[0]) / prices[0]) * 100

        gains, losses = [], []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

        prompt = (
            "Act as a quantitative market analyst. Concise technical analysis:\n"
            f"• Asset: {symbol.upper()}\n"
            f"• Current Price: ${current_price:,.2f}\n"
            f"• 14-Day Average: ${avg_14d:,.2f}\n"
            f"• 14-Day Change: {change_14d:.2f}%\n"
            f"• 14-Day RSI: {rsi:.1f}\n\n"
            "Provide Sentiment, Indicator Signal, Target Levels & Risk Caution. "
            "Under 160 words. Plain text, no markdown."
        )
        return generate_ai_response(prompt)
    except Exception as e:
        return f"Analysis error: {e}"


# =====================================================================
# NOTIFICATIONS (always sent by the real bot, never the userbot)
# =====================================================================
async def notify_owners(message_html: str, button_text: str | None = None, button_url: str | None = None):
    if not TG_APP or not ALLOWED_USERS:
        print("[!] Cannot notify: bot not started or ALLOWED_USERS is empty.")
        return
    markup = None
    if button_text and button_url:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])
    for uid in ALLOWED_USERS:
        try:
            await TG_APP.bot.send_message(
                chat_id=int(uid), text=clip(message_html),
                parse_mode=ParseMode.HTML, reply_markup=markup,
            )
        except Exception as e:
            print(f"[!] Notify failed for {uid} (have they pressed /start on the bot?): {e}")


# =====================================================================
# MINI-APP RADAR
# =====================================================================
def classify_reply(raw_text: str) -> str:
    t = raw_text.lower()
    if not t.strip():
        return "UNKNOWN"
    if TIMER_PATTERN.search(t) or any(k in t for k in DYNAMIC_CONFIG.get("not_ready_keywords", [])):
        return "MINING IN PROGRESS"
    if any(k in t for k in DYNAMIC_CONFIG.get("ready_keywords", [])):
        return "READY TO CLAIM"
    return "MINING IN PROGRESS"


def extract_button_url(messages) -> str | None:
    """Grabs the mining bot's own link button (e.g. '🚀 Open Ultra Wallet') if it has one."""
    for m in messages:
        try:
            rows = m.buttons or []
        except Exception:
            continue
        for row in rows:
            for b in row:
                raw = getattr(b, "button", b)
                url = getattr(raw, "url", None)
                # Only plain link buttons; Mini-App (WebView) buttons need Telegram's login data to open.
                if type(raw).__name__ == "KeyboardButtonUrl" and isinstance(url, str) \
                        and url.startswith(("https://", "http://", "tg://")):
                    return url
    return None


async def fetch_bot_replies(client: TelegramClient, bot_username: str):
    """Sends the status command and returns ONLY the bot's replies to it (oldest first)."""
    entity = await client.get_entity(bot_username)
    sent = await client.send_message(entity, STATUS_COMMAND)
    deadline = time.monotonic() + REPLY_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        msgs = await client.get_messages(entity, min_id=sent.id, limit=10)
        if any(not m.out for m in msgs):
            await asyncio.sleep(2)  # some bots reply in several messages
            msgs = await client.get_messages(entity, min_id=sent.id, limit=10)
            return [m for m in reversed(msgs) if not m.out]
    return []


async def check_one_bot(client: TelegramClient, account_label: str, bot_username: str) -> dict:
    replies = await fetch_bot_replies(client, bot_username)
    combined = "\n".join(m.message for m in replies if m.message)
    status = "NO REPLY" if not replies else classify_reply(combined)
    record = {
        "status": status,
        "last_response": combined[:1000],
        "button_url": extract_button_url(replies),
        "timestamp": time.time(),
    }
    MINING_STATUS_STORE.setdefault(account_label, {})[bot_username] = record
    return record


async def radar_loop(client: TelegramClient, account_label: str):
    print(f"[+] Mini-App Radar started for [{account_label}]")
    await asyncio.sleep(10)
    last_state = {}
    session_dead_notified = False

    while True:
        try:
            if not DYNAMIC_CONFIG.get("auto_claim_enabled", True):
                await asyncio.sleep(60)
                continue

            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                if not session_dead_notified:
                    await notify_owners(
                        f"⚠️ <b>{h(account_label)}</b> session is logged out or revoked.\n"
                        "Generate a new session string and update it on Render."
                    )
                    session_dead_notified = True
                await asyncio.sleep(1800)
                continue
            session_dead_notified = False

            for bot_username in list(DYNAMIC_CONFIG.get("active_bots", [])):
                try:
                    record = await check_one_bot(client, account_label, bot_username)
                    print(f"[*] [{account_label}] {bot_username}: {record['status']}")

                    if record["status"] == "READY TO CLAIM":
                        if last_state.get(bot_username) != "READY TO CLAIM":
                            url = record.get("button_url") or f"https://t.me/{bot_username.lstrip('@')}"
                            await notify_owners(
                                f"🚨 <b>MINING REWARD READY</b> [{h(account_label)}]\n\n"
                                f"🤖 Bot: <code>{h(bot_username)}</code>\n"
                                "⚡ Cycle complete. Tap below to open and claim.",
                                "🎯 Open & Claim", url,
                            )
                    last_state[bot_username] = record["status"]

                except FloodWaitError as e:
                    print(f"[!] [{account_label}] Flood wait {e.seconds}s from Telegram — pausing.")
                    await asyncio.sleep(e.seconds + 5)
                except Exception as e:
                    print(f"[-] [{account_label}] Error checking {bot_username}: {e}")

                await asyncio.sleep(random.uniform(4, 10))  # human-like gap between bots

            await save_state("mining_status", MINING_STATUS_STORE)

        except Exception as e:
            print(f"[!] [{account_label}] Radar loop error: {e}")

        interval = max(60, int(DYNAMIC_CONFIG.get("polling_interval", 900)))
        await asyncio.sleep(interval + random.uniform(0, 60))


# =====================================================================
# PRICE ALERTS + DAILY REPORT
# =====================================================================
async def price_alerts_loop():
    while True:
        await asyncio.sleep(30)
        if not ACTIVE_ALERTS:
            continue
        triggered = []
        for alert in list(ACTIVE_ALERTS):
            price = await asyncio.to_thread(fetch_pair_price, alert["symbol"], alert.get("type", "crypto"))
            if price is None:
                continue
            hit = (alert["condition"] == "above" and price >= alert["target_price"]) or \
                  (alert["condition"] == "below" and price <= alert["target_price"])
            if hit:
                try:
                    await TG_APP.bot.send_message(
                        chat_id=alert["chat_id"], parse_mode=ParseMode.HTML,
                        text=(f"🚨 <b>PRICE ALERT</b>\n\n"
                              f"📈 Asset: {h(alert['symbol'].upper())}\n"
                              f"🎯 Target: {alert['condition']} ${alert['target_price']:,.4f}\n"
                              f"💵 Current: ${price:,.4f}"),
                    )
                except Exception as e:
                    print(f"[!] Price alert send error: {e}")
                triggered.append(alert)
        if triggered:
            for t in triggered:
                if t in ACTIVE_ALERTS:
                    ACTIVE_ALERTS.remove(t)
            await save_state("price_alerts", ACTIVE_ALERTS)


def seconds_until_next_report() -> float:
    now = datetime.now(REPORT_TZ)
    target = now.replace(hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_report_loop():
    while True:
        try:
            await asyncio.sleep(seconds_until_next_report())
            report = "☀️ <b>Good Morning! Daily Mining Radar Digest</b>\n\n"
            if not MINING_STATUS_STORE:
                report += "⚠️ No mining status recorded yet."
            else:
                for account_label, bots in MINING_STATUS_STORE.items():
                    report += f"👤 <b>{h(account_label)}</b>\n"
                    for bot_name, info in bots.items():
                        emoji = "🟢" if info["status"] == "READY TO CLAIM" else "⏱️"
                        report += f"  {emoji} <code>{h(bot_name)}</code>: {h(info['status'])}\n"
                    report += "\n"
            await notify_owners(report)
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[!] Daily report error: {e}")
            await asyncio.sleep(3600)


# =====================================================================
# TELEGRAM COMMAND HANDLERS
# =====================================================================
def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and (not ALLOWED_USERS or str(user.id) in ALLOWED_USERS)


async def tg_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "🤖 <b>Mining Radar Cloud Bot</b>\n\n"
        "📊 /dashboard — Open web dashboard\n"
        "📡 /status — Radar status &amp; your pass\n"
        "📈 /predict &lt;symbol&gt; — AI technical analysis\n"
        "💵 /crypto &lt;ticker&gt; — Live crypto price\n"
        "🔔 /alert &lt;ticker&gt; &lt;above|below&gt; &lt;price&gt; — Price alert\n"
        "🧠 /ai &lt;prompt&gt; — Ask the AI assistant\n"
        "⭐ /upgrade — Buy the Pro Pass with Stars",
        parse_mode=ParseMode.HTML,
    )


async def tg_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    app_url = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Open Mining Dashboard", web_app=WebAppInfo(url=app_url))]])
    await update.message.reply_text("🚀 <b>Mining Radar Dashboard</b>", reply_markup=markup, parse_mode=ParseMode.HTML)


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    expiry = await asyncio.to_thread(get_subscription_expiry, update.effective_user.id)
    if expiry and expiry > time.time():
        pass_line = f"⭐ Pro Pass active until <b>{fmt_time(expiry)}</b>"
    else:
        pass_line = "⭐ No active Pro Pass — /upgrade"

    lines = [f"📡 <b>Radar:</b> {'ON' if DYNAMIC_CONFIG.get('auto_claim_enabled') else 'OFF'}", pass_line, ""]
    if not MINING_STATUS_STORE:
        lines.append("No checks recorded yet.")
    for account_label, bots in MINING_STATUS_STORE.items():
        lines.append(f"👤 <b>{h(account_label)}</b>")
        for bot_name, info in bots.items():
            emoji = "🟢" if info["status"] == "READY TO CLAIM" else "⏱️"
            lines.append(f"  {emoji} <code>{h(bot_name)}</code>: {h(info['status'])} ({fmt_time(info['timestamp'])})")
    await update.message.reply_text(clip("\n".join(lines)), parse_mode=ParseMode.HTML)


async def tg_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="Mining Radar Pro Pass",
            description=f"{PASS_DAYS}-Day Automated Cloud Radar & Dashboard Access",
            payload=PASS_PAYLOAD,
            currency="XTR",
            prices=[LabeledPrice("1 Month Pass", PASS_PRICE_STARS)],
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error generating invoice: {e}")


async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    if q.invoice_payload == PASS_PAYLOAD and q.currency == "XTR" and q.total_amount == PASS_PRICE_STARS:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="This invoice is no longer valid. Please run /upgrade again.")


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = update.message.successful_payment
    if p.invoice_payload != PASS_PAYLOAD:
        return
    try:
        expiry = await asyncio.to_thread(
            record_payment, update.effective_user.id, p.telegram_payment_charge_id, p.total_amount
        )
        await update.message.reply_text(
            f"🎉 <b>Pro Pass activated!</b>\nActive until <b>{fmt_time(expiry)}</b>.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"[!] PAYMENT NOT SAVED for user {update.effective_user.id}, charge {p.telegram_payment_charge_id}: {e}")
        await update.message.reply_text(
            "⚠️ Payment received but we couldn't save it. It has been logged; the admin will activate it manually."
        )


async def tg_predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    symbol = context.args[0] if context.args else "bitcoin"
    msg = await update.message.reply_text(f"📊 Analyzing {symbol.upper()}...")
    analysis = await asyncio.to_thread(analyze_market_trend, symbol)
    await msg.edit_text(clip(analysis))


async def tg_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    symbol = context.args[0] if context.args else "bitcoin"
    price = await asyncio.to_thread(fetch_pair_price, symbol, "crypto")
    if price is not None:
        await update.message.reply_text(f"💵 <b>{h(symbol.upper())}:</b> ${price:,.4f}", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Ticker not found.")


async def tg_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    args = context.args or []
    usage = "Usage: /alert <ticker> <above|below> <price>\nExample: /alert btc above 70000"
    if len(args) != 3 or args[1].lower() not in ("above", "below"):
        await update.message.reply_text(usage)
        return
    try:
        target = float(args[2].replace(",", ""))
    except ValueError:
        await update.message.reply_text(usage)
        return
    ACTIVE_ALERTS.append({
        "symbol": args[0].lower(), "type": "crypto",
        "condition": args[1].lower(), "target_price": target,
        "chat_id": update.effective_chat.id,
    })
    await save_state("price_alerts", ACTIVE_ALERTS)
    await update.message.reply_text(f"🔔 Alert set: {args[0].upper()} {args[1].lower()} ${target:,.4f}")


async def tg_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    prompt = " ".join(context.args or []).strip()
    if not prompt:
        await update.message.reply_text("Usage: /ai <your question>")
        return
    msg = await update.message.reply_text("🧠 Thinking...")
    answer = await asyncio.to_thread(generate_ai_response, prompt)
    await msg.edit_text(clip(answer))


# =====================================================================
# DASHBOARD API AUTH
# =====================================================================
def verify_init_data(init_data: str) -> dict | None:
    """Validates Telegram WebApp initData (proves the request came from your Mini-App)."""
    if not init_data or not TELEGRAM_TOKEN:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    if time.time() - int(pairs.get("auth_date", "0")) > 86400:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None


async def require_auth(
    x_api_key: str | None = Header(default=None),
    x_telegram_init_data: str | None = Header(default=None),
):
    if DASHBOARD_API_KEY and x_api_key and hmac.compare_digest(x_api_key, DASHBOARD_API_KEY):
        return {"via": "api_key"}
    user = verify_init_data(x_telegram_init_data or "")
    if user and str(user.get("id")) in ALLOWED_USERS:
        return {"via": "telegram", "user_id": user.get("id")}
    raise HTTPException(status_code=401, detail="Unauthorized")


# =====================================================================
# STARTUP / SHUTDOWN
# =====================================================================
async def start_services():
    global TG_APP
    if not TELEGRAM_TOKEN:
        print("[!] TELEGRAM_TOKEN is missing — bot not started.")
        return
    if not ALLOWED_USERS:
        print("[!] WARNING: ALLOWED_USERS is empty. Commands are open to everyone and alerts go nowhere.")

    await asyncio.to_thread(db_init)
    await asyncio.to_thread(load_state_sync)
    print(f"[+] Database ready ({DATABASE_URL.split(':')[0]}). Bots: {DYNAMIC_CONFIG['active_bots']}")

    TG_APP = (ApplicationBuilder().token(TELEGRAM_TOKEN)
              .request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build())

    handlers = {
        "help": tg_help, "start": tg_help, "dashboard": tg_dashboard, "status": tg_status,
        "predict": tg_predict, "crypto": tg_crypto, "alert": tg_alert, "ai": tg_ai,
        "upgrade": tg_upgrade,
    }
    for name, handler in handlers.items():
        TG_APP.add_handler(CommandHandler(name, handler))
    TG_APP.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    TG_APP.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    await TG_APP.initialize()
    await TG_APP.start()
    await TG_APP.bot.set_my_commands([
        BotCommand("dashboard", "Open Mining Dashboard"),
        BotCommand("status", "Radar status & your pass"),
        BotCommand("predict", "AI Technical Analysis"),
        BotCommand("crypto", "Crypto Price Lookup"),
        BotCommand("alert", "Set a price alert"),
        BotCommand("ai", "Ask the AI assistant"),
        BotCommand("upgrade", "Buy Pro Pass with Stars"),
        BotCommand("help", "Show Bot Commands"),
    ])
    await TG_APP.updater.start_polling(drop_pending_updates=True)

    BACKGROUND_TASKS.extend([
        asyncio.create_task(price_alerts_loop()),
        asyncio.create_task(daily_report_loop()),
    ])

    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH):
        print("[!] TELEGRAM_API_ID / TELEGRAM_API_HASH not set — mining radar disabled.")
    else:
        for idx, env_name in ((1, "SESSION_STRING_ONE"), (2, "SECOND_SESSION_STRING")):
            session_str = os.getenv(env_name, "").strip()
            if not session_str:
                continue
            label = f"Account-{idx}"
            try:
                client = TelegramClient(StringSession(session_str), TELEGRAM_API_ID, TELEGRAM_API_HASH)
                await client.connect()
                if not await client.is_user_authorized():
                    print(f"[!] {label}: session string is invalid or logged out.")
                    await notify_owners(f"⚠️ <b>{label}</b> session string is invalid. Update {env_name} on Render.")
                    await client.disconnect()
                    continue
                TELETHON_CLIENTS[idx] = client
                BACKGROUND_TASKS.append(asyncio.create_task(radar_loop(client, label)))
            except Exception as e:
                print(f"[!] Failed to start {label}: {e}")

    print(f"[+] Cloud Bot running. AI model: {GEMINI_MODEL}. Accounts: {list(TELETHON_CLIENTS)}")


async def _startup_wrapper():
    try:
        await start_services()
    except Exception:
        print("[!] STARTUP FAILED:")
        traceback.print_exc()


async def stop_services():
    for task in BACKGROUND_TASKS:
        task.cancel()
    for client in TELETHON_CLIENTS.values():
        try:
            await client.disconnect()
        except Exception:
            pass
    if TG_APP:
        try:
            if TG_APP.updater and TG_APP.updater.running:
                await TG_APP.updater.stop()
            if TG_APP.running:
                await TG_APP.stop()
            await TG_APP.shutdown()
        except Exception as e:
            print(f"[!] Telegram shutdown error: {e}")
    await save_state("mining_status", MINING_STATUS_STORE)
    print("[+] Shut down cleanly.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_task = asyncio.create_task(_startup_wrapper())
    yield
    startup_task.cancel()
    await stop_services()


# =====================================================================
# FASTAPI ROUTES
# =====================================================================
api_app = FastAPI(title="Mining Radar Dashboard API", lifespan=lifespan)


@api_app.get("/")
def root():
    if os.path.exists("templates/index.html"):
        return FileResponse("templates/index.html")
    return JSONResponse({"status": "online", "service": "Mining Radar Cloud Bot"})


@api_app.get("/api/health")
def health():
    """Public, for Render health checks. Reveals nothing sensitive."""
    return {"status": "online"}


@api_app.get("/api/mining-status")
async def get_mining_status(auth=Depends(require_auth)):
    return {"status": "online", "accounts": MINING_STATUS_STORE}


@api_app.get("/api/settings")
async def get_settings(auth=Depends(require_auth)):
    return {"status": "success", "settings": DYNAMIC_CONFIG}


@api_app.post("/api/settings")
async def update_settings(payload: dict, auth=Depends(require_auth)):
    try:
        if "polling_interval" in payload:
            DYNAMIC_CONFIG["polling_interval"] = max(60, int(payload["polling_interval"]))
        if "auto_claim_enabled" in payload:
            v = payload["auto_claim_enabled"]
            DYNAMIC_CONFIG["auto_claim_enabled"] = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes", "on")
        if isinstance(payload.get("active_bots"), list):
            bots = []
            for b in payload["active_bots"]:
                b = str(b).strip()
                if b:
                    bots.append(b if b.startswith("@") else f"@{b}")
            if bots:
                DYNAMIC_CONFIG["active_bots"] = bots
        for key in ("ready_keywords", "not_ready_keywords"):
            if isinstance(payload.get(key), list):
                DYNAMIC_CONFIG[key] = [str(k).strip().lower() for k in payload[key] if str(k).strip()]
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    await save_state("config", DYNAMIC_CONFIG)
    return {"status": "success", "message": "Settings updated!", "settings": DYNAMIC_CONFIG}


@api_app.post("/api/create-invoice")
async def create_invoice(auth=Depends(require_auth)):
    if not TG_APP:
        raise HTTPException(status_code=503, detail="Bot is still starting.")
    try:
        link = await TG_APP.bot.create_invoice_link(
            title="Mining Radar Pro Pass",
            description=f"{PASS_DAYS}-Day Automated Cloud Radar & Dashboard Access",
            payload=PASS_PAYLOAD,
            currency="XTR",
            prices=[LabeledPrice("1 Month Pass", PASS_PRICE_STARS)],
        )
        return {"status": "success", "invoice_link": link}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_app.post("/api/action/{account_id}/{action_type}")
async def trigger_dashboard_action(account_id: int, action_type: str, auth=Depends(require_auth)):
    client = TELETHON_CLIENTS.get(account_id)
    if not client or not client.is_connected():
        raise HTTPException(status_code=400, detail=f"Account {account_id} is not active.")
    if action_type != "force_check":
        raise HTTPException(status_code=400, detail="Unknown action type.")

    label = f"Account-{account_id}"
    results = {}
    for bot_username in list(DYNAMIC_CONFIG.get("active_bots", [])):
        try:
            results[bot_username] = (await check_one_bot(client, label, bot_username))["status"]
        except Exception as e:
            results[bot_username] = f"error: {e}"
        await asyncio.sleep(random.uniform(2, 5))
    await save_state("mining_status", MINING_STATUS_STORE)
    return {"status": "success", "account": label, "results": results}


if __name__ == "__main__":
    uvicorn.run(api_app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
