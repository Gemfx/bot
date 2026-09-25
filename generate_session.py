import asyncio
from telethon import TelegramClient

# Put your real API ID and API Hash here
API_ID =33394338 
API_HASH ="f72565d820fde421d56172f2261dadd4"

async def main():
    print("[*] Starting Telethon to generate session file...")
    client = TelegramClient('mining_session', API_ID, API_HASH)
    await client.start()
    print("\n[+] SUCCESS! Your 'mining_session.session' file has been created!")

if __name__ == "__main__":
    asyncio.run(main())