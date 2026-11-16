import os
import re
import uuid
import time
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from dotenv import load_dotenv
import requests

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from telethon import TelegramClient
from telethon.sessions import StringSession

# --- FASTAPI BACKEND IMPORTS ---
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
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
tg_app_global = None

TICKER_MAP = {
    "usdt": "tether", "btc": "bitcoin", "eth": "ethereum",
    "sol": "solana", "bnb": "binancecoin", "xrp": "ripple",
    "doge": "dogecoin", "ada": "cardano"
}

# --- DYNAMIC TEMPLATE BUILDER ENGINE ---
def build_webapp_from_template(prompt: str) -> str:
    prompt_lower = prompt.lower()
    
    # Check if user wants an e-commerce or shop layout
    if "shop" in prompt_lower or "store" in prompt_lower or "ecommerce" in prompt_lower or "e-commerce" in prompt_lower:
        title = "E-Commerce Store"
        description = "Instant Telegram Shop Catalog"
        extra_html = """
        <div class="grid grid-cols-2 gap-3 mb-4">
            <div class="bg-gray-900 border border-emerald-500/30 rounded-xl p-3 text-left">
                <div class="text-xs text-white font-bold">Cyber Jacket</div>
                <div class="text-emerald-400 text-xs mt-1">$120.00</div>
                <button onclick="alert('Added to Cart!')" class="mt-2 w-full py-1 bg-emerald-500 text-gray-950 font-bold rounded text-xs">Buy</button>
            </div>
            <div class="bg-gray-900 border border-emerald-500/30 rounded-xl p-3 text-left">
                <div class="text-xs text-white font-bold">Neural Visor</div>
                <div class="text-emerald-400 text-xs mt-1">$85.00</div>
                <button onclick="alert('Added to Cart!')" class="mt-2 w-full py-1 bg-emerald-500 text-gray-950 font-bold rounded text-xs">Buy</button>
            </div>
        </div>
        """
    else:
        # Default Crypto / Dashboard Layout
        parts = [p.strip() for p in prompt.split("|")]
        title = parts[0] if len(parts) > 0 and parts[0] else "Telegram Mini-App"
        description = parts[1] if len(parts) > 1 and parts[1] else "Custom built via template engine."
        extra_html = """
        <div class="bg-gray-900 border border-emerald-500/30 rounded-xl p-4 cyber-glow mb-4">
            <div class="flex justify-between items-center mb-2">
                <span class="text-sm text-gray-400">BTC / USDT</span>
                <span class="text-xs text-emerald-400 bg-emerald-950 px-2 py-0.5 rounded">+4.25%</span>
            </div>
            <div class="text-2xl font-bold text-white">$91,420.50</div>
        </div>
        """

    # Build final HTML template dynamically
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>.cyber-glow {{ box-shadow: 0 0 20px rgba(16, 185, 129, 0.2); }}</style>
</head>
<body class="bg-gray-950 text-emerald-400 min-h-screen font-mono p-4 flex flex-col items-center justify-center">
    <div class="max-w-md w-full space-y-4 text-center">
        <div class="bg-gray-900 border border-emerald-500/30 rounded-xl p-4 cyber-glow flex justify-between items-center text-left">
            <div>
                <h1 class="text-lg font-bold text-white tracking-wider">{title}</h1>
                <p class="text-xs text-gray-400">{description}</p>
            </div>
            <div class="bg-emerald-500/10 border border-emerald-500/50 px-3 py-1 rounded-full text-xs text-emerald-400 font-bold">🟢 LIVE</div>
        </div>
        {extra_html}
        <button onclick="Telegram.WebApp.close()" class="w-full py-3 bg-emerald-500 hover:bg-emerald-400 text-gray-950 font-bold rounded-lg transition shadow-lg shadow-emerald-500/20 uppercase tracking-wider text-xs">
            Close Mini-App
        </button>
    </div>
    <script>window.Telegram?.WebApp?.ready();</script>
</body>
</html>
"""
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
     
    last_known
