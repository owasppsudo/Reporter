from telethon import TelegramClient, events, types
import asyncio
import aiohttp
import logging
import os
from datetime import datetime, timedelta
import json
from typing import Dict, Optional, List
import aiofiles
from urllib.parse import urlparse
import sqlite3
from aiogram.utils.markdown import hbold, hlink
import hashlib
import jwt
import pytz
from googletrans import Translator
import re
from ratelimit import limits, sleep_and_retry
import aiohttp_socks
from bs4 import BeautifulSoup
import base64

SESSION_NAME = "world_reporter_session"
REPORT_DIR = "reports"
LOG_FILE = "reporter.log"
DB_FILE = "reporter.db"
SECRET_KEY = "your-secret-key"  # For JWT tokens (could be user-provided too)
RATE_LIMIT_CALLS = 5  # Max reports per minute
RATE_LIMIT_PERIOD = 60  # Seconds

SUPPORTED_PLATFORMS = {
    "telegram": ["channel", "group", "account", "bot"],
    "instagram": ["account", "post", "story", "comment"],
    "whatsapp": ["account", "group", "message"],
    "twitter": ["account", "tweet", "dm"],
    "facebook": ["account", "post", "group", "comment"],
    "tiktok": ["account", "video", "comment"],
    "youtube": ["channel", "video", "comment"],
    "reddit": ["subreddit", "post", "comment", "user"],
    "discord": ["server", "user", "message"],
    "snapchat": ["account", "snap", "story"],
    "linkedin": ["profile", "post", "comment"],
    "other": None
}

REPORT_API_ENDPOINTS = {
    "telegram": "https://api.telegram.org/bot{token}/sendMessage",
    "instagram": "https://graph.instagram.com/v12.0/{{id}}/report?access_token={token}",
    "twitter": "https://api.twitter.com/2/tweets/{{id}}/report_spam?api_key={token}",
    "facebook": "https://graph.facebook.com/v12.0/{{id}}/report?access_token={token}",
    "tiktok": "https://open-api.tiktok.com/report/content?access_token={token}",
    "youtube": "https://www.googleapis.com/youtube/v3/videos/reportAbuse?key={token}",
    "reddit": "https://oauth.reddit.com/api/report?access_token={token}",
    "discord": "https://discord.com/api/v9/report",
    "snapchat": "https://accounts.snapchat.com/report",
    "linkedin": "https://api.linkedin.com/v2/ugcPosts/{{id}}/report?oauth2_access_token={token}"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

user_states: Dict[int, Dict] = {}
translator = Translator()
os.makedirs(REPORT_DIR, exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS reports
                 (id INTEGER PRIMARY KEY, chat_id INTEGER, platform TEXT, category TEXT, details TEXT, 
                  timestamp TEXT, media TEXT, urls TEXT, status TEXT, verification_token TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (chat_id INTEGER PRIMARY KEY, language TEXT, report_count INTEGER, last_report TIMESTAMP, 
                  phone_number TEXT, api_id TEXT, api_hash TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS credentials
                 (chat_id INTEGER, platform TEXT, token TEXT, PRIMARY KEY (chat_id, platform))''')
    conn.commit()
    conn.close()

async def get_client(chat_id: int) -> TelegramClient:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT phone_number, api_id, api_hash FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    if result:
        phone, api_id, api_hash = result
        return TelegramClient(f"{SESSION_NAME}_{chat_id}", api_id, api_hash)
    return None

@sleep_and_retry
@limits(calls=RATE_LIMIT_CALLS, period=RATE_LIMIT_PERIOD)
async def save_report(chat_id: int, report_data: Dict) -> str:
    timestamp = datetime.now(pytz.utc).isoformat()
    filename = f"{REPORT_DIR}/report_{chat_id}_{timestamp.replace(':', '_')}.json"
    verification_token = jwt.encode({"chat_id": chat_id, "timestamp": timestamp}, SECRET_KEY, algorithm="HS256")

 
    async with aiofiles.open(filename, "w") as f:
        await f.write(json.dumps(report_data, indent=2))
    logger.info(f"Saved report from {chat_id} to {filename}")

 
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO reports (chat_id, platform, category, details, timestamp, media, urls, status, verification_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (chat_id, report_data["platform"], report_data["category"], report_data["details"], timestamp,
               report_data.get("media"), json.dumps(report_data["urls"]), "pending", verification_token))
    c.execute("INSERT OR REPLACE INTO users (chat_id, language, report_count, last_report) VALUES (?, ?, COALESCE((SELECT report_count FROM users WHERE chat_id = ?) + 1, 1), ?)",
              (chat_id, report_data.get("language", "en"), chat_id, timestamp))
    conn.commit()
    conn.close()

    
    platform = report_data["platform"]
    if platform in REPORT_API_ENDPOINTS and platform != "other":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT token FROM credentials WHERE chat_id = ? AND platform = ?", (chat_id, platform))
        token = c.fetchone()
        conn.close()
        if token:
            token = token[0]
            endpoint = REPORT_API_ENDPOINTS[platform].format(token=token, id=report_data.get("id", "unknown"))
            async with aiohttp.ClientSession() as session:
                try:
                    async with session.post(endpoint, json=report_data) as resp:
                        if resp.status in [200, 201]:
                            logger.info(f"Report sent to {platform} API successfully")
                        else:
                            logger.error(f"Failed to send report to {platform} API: {resp.status}")
                except Exception as e:
                    logger.error(f"Error sending report to {platform} API: {e}")

    return verification_token

async def download_media(client: TelegramClient, message: types.Message) -> Optional[str]:
    if message.media:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        media_path = f"{REPORT_DIR}/media_{message.chat_id}_{timestamp}"
        try:
            path = await client.download_media(message.media, media_path)
            logger.info(f"Downloaded media to {path}")
            return path
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
    return None

async def analyze_url(url: str) -> Dict:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    return {
                        "title": soup.title.string if soup.title else "No title",
                        "description": soup.find("meta", {"name": "description"})["content"] if soup.find("meta", {"name": "description"}) else "No description"
                    }
        except Exception as e:
            logger.error(f"Error analyzing URL {url}: {e}")
            return {"title": "Error", "description": str(e)}

@events.register(events.NewMessage(pattern="/start"))
async def start(event):
    chat_id = event.chat_id
    user_states[chat_id] = {"step": "phone_number"}
    await event.reply("Welcome to World Reporter! Please provide your phone number (e.g., +1234567890) to authenticate with Telegram:")

@events.register(events.NewMessage(pattern="/stats"))
async def stats(event):
    chat_id = event.chat_id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT report_count, last_report FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    if result:
        count, last_report = result
        await event.reply(f"You have submitted {count} reports. Last report: {last_report}")
    else:
        await event.reply("You haven't submitted any reports yet.")

@events.register(events.NewMessage(pattern="/verify (.+)"))
async def verify_report(event):
    chat_id = event.chat_id
    token = event.pattern_match.group(1)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if payload["chat_id"] == chat_id:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("UPDATE reports SET status = 'verified' WHERE verification_token = ? AND chat_id = ?", (token, chat_id))
            conn.commit()
            conn.close()
            await event.reply("Report verified successfully!")
        else:
            await event.reply("Invalid token or unauthorized access.")
    except jwt.InvalidTokenError:
        await event.reply("Invalid verification token.")

@events.register(events.NewMessage(pattern="/analytics"))
async def analytics(event):
    chat_id = event.chat_id
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT platform, COUNT(*) as count FROM reports GROUP BY platform")
    platform_counts = c.fetchall()
    c.execute("SELECT COUNT(*) FROM reports WHERE status = 'verified'")
    verified_count = c.fetchone()[0]
    conn.close()
    response = "Global Analytics:\n"
    for platform, count in platform_counts:
        response += f"{platform.capitalize()}: {count} reports\n"
    response += f"Total Verified Reports: {verified_count}"
    await event.reply(response)

@events.register(events.NewMessage(pattern="/set_token (.+)"))
async def set_token(event):
    chat_id = event.chat_id
    text = event.pattern_match.group(1).strip()
    platform, token = text.split(" ", 1)
    if platform.lower() in SUPPORTED_PLATFORMS:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO credentials (chat_id, platform, token) VALUES (?, ?, ?)", (chat_id, platform.lower(), token))
        conn.commit()
        conn.close()
        await event.reply(f"Token for {platform} set successfully!")
    else:
        await event.reply(f"Invalid platform. Use one of: {', '.join(SUPPORTED_PLATFORMS.keys())}")

async def handle_message(event):
    chat_id = event.chat_id
    text = event.message.text.strip()

    if not event.is_private:
        return

    if chat_id not in user_states or "step" not in user_states[chat_id]:
        await event.reply("Please use /start to begin.")
        return

    state = user_states[chat_id]
    language = state.get("language", "en")
    client = await get_client(chat_id)

    if state["step"] == "phone_number":
        state["phone_number"] = text
        await event.reply("Please provide your API ID from my.telegram.org:")
        state["step"] = "api_id"
        return

    elif state["step"] == "api_id":
        state["api_id"] = text
        await event.reply("Please provide your API Hash from my.telegram.org:")
        state["step"] = "api_hash"
        return

    elif state["step"] == "api_hash":
        state["api_hash"] = text
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO users (chat_id, phone_number, api_id, api_hash) VALUES (?, ?, ?, ?)",
                  (chat_id, state["phone_number"], state["api_id"], state["api_hash"]))
        conn.commit()
        conn.close()
        
        client = TelegramClient(f"{SESSION_NAME}_{chat_id}", state["api_id"], state["api_hash"])
        await client.start(phone=state["phone_number"])
        if not await client.is_user_authorized():
            await client.send_code_request(state["phone_number"])
            await event.reply("Please enter the code you received on Telegram:")
            state["step"] = "auth_code"
        else:
            await event.reply("Already authorized! Please choose your language (e.g., 'en' for English, 'es' for Spanish):")
            state["step"] = "choose_language"
        return

    elif state["step"] == "auth_code":
        try:
            await client.sign_in(state["phone_number"], text)
            await event.reply("Authentication successful! Please choose your language (e.g., 'en' for English, 'es' for Spanish):")
            state["step"] = "choose_language"
        except Exception as e:
            await event.reply(f"Authentication failed: {e}. Please try again with the correct code:")
        return

    elif state["step"] == "choose_language":
        state["language"] = text.lower()[:2]
        platform_list = ", ".join(SUPPORTED_PLATFORMS.keys())
        msg = translator.translate(f"Which platform do you want to report?\nOptions: {platform_list}", dest=language).text
        await event.reply(msg)
        state["step"] = "choose_platform"
        return

    elif state["step"] == "choose_platform":
        if text.lower() in SUPPORTED_PLATFORMS:
            state["platform"] = text.lower()
            if state["platform"] == "other":
                msg = translator.translate("What platform or app do you want to report? (e.g., Snapchat, Discord)", dest=language).text
                await event.reply(msg)
                state["step"] = "custom_platform"
            else:
                
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute("SELECT token FROM credentials WHERE chat_id = ? AND platform = ?", (chat_id, state["platform"]))
                token = c.fetchone()
                conn.close()
                if not token and state["platform"] not in ["whatsapp", "snapchat", "discord"]:  # Platforms without API token support
                    msg = translator.translate(f"Please provide your API token for {state['platform']} (use /set_token {state['platform']} <token> to set it later if you don't have it now):", dest=language).text
                    await event.reply(msg)
                    state["step"] = "set_platform_token"
                else:
                    categories = ", ".join(SUPPORTED_PLATFORMS[state["platform"]])
                    msg = translator.translate(f"What do you want to report on {state['platform'].capitalize()}?\nOptions: {categories}", dest=language).text
                    await event.reply(msg)
                    state["step"] = "choose_category"
        else:
            msg = translator.translate(f"Invalid platform. Please choose from: {', '.join(SUPPORTED_PLATFORMS.keys())}", dest=language).text
            await event.reply(msg)
        return

    elif state["step"] == "set_platform_token":
        
        if text.lower() == "skip":
            categories = ", ".join(SUPPORTED_PLATFORMS[state["platform"]])
            msg = translator.translate(f"What do you want to report on {state['platform'].capitalize()}?\nOptions: {categories}", dest=language).text
            await event.reply(msg)
            state["step"] = "choose_category"
        else:
            await event.reply(translator.translate("Please use /set_token <platform> <token> to set the token, or type 'skip' to proceed without it.", dest=language).text)
        return

    elif state["step"] == "custom_platform":
        state["custom_platform"] = text
        msg = translator.translate(f"What issue do you want to report for {text}? (e.g., account, content, bug)", dest=language).text
        await event.reply(msg)
        state["step"] = "choose_category"
        return

    elif state["step"] == "choose_category":
        platform = state["platform"]
        if platform == "other":
            state["category"] = text.lower()
            msg = translator.translate(f"Please provide details for your {state['custom_platform']} {text} report (include URLs or IDs if applicable):", dest=language).text
            await event.reply(msg)
            state["step"] = "report_details"
        elif text.lower() in SUPPORTED_PLATFORMS[platform]:
            state["category"] = text.lower()
            msg = translator.translate(f"Please provide details for your {platform.capitalize()} {text.lower()} report (include URLs or IDs if applicable):", dest=language).text
            await event.reply(msg)
            state["step"] = "report_details"
        else:
            msg = translator.translate(f"Invalid category. Please choose from: {', '.join(SUPPORTED_PLATFORMS[platform])}", dest=language).text
            await event.reply(msg)
        return

    elif state["step"] == "report_details":
        platform = state["platform"]
        category = state["category"]
        media_path = await download_media(client, event.message)
        urls = [url for url in text.split() if urlparse(url).scheme in ["http", "https"]]
        url_analyses = {url: await analyze_url(url) for url in urls}

       
        id_match = re.search(r"(?:id=|\/)(\d+)", text)
        report_id = id_match.group(1) if id_match else None

        report_data = {
            "chat_id": chat_id,
            "platform": platform,
            "category": category,
            "details": text,
            "timestamp": datetime.now(pytz.utc).isoformat(),
            "media": media_path,
            "urls": urls,
            "url_analyses": url_analyses,
            "language": language,
            "id": report_id
        }
        if platform == "other":
            report_data["custom_platform"] = state["custom_platform"]

        verification_token = await save_report(chat_id, report_data)
        msg = translator.translate(f"Thank you! Your report has been recorded. Verify it with /verify {verification_token}\nUse /start to report again or /stats to see your stats.", dest=language).text
        await event.reply(msg)
        state["step"] = "start"

async def main():
    
    init_db()
    logger.info("Starting World Reporter...")
    
    client = TelegramClient(SESSION_NAME, 1, "dummy")  # Dummy client to register handlers
    client.add_event_handler(start)
    client.add_event_handler(stats)
    client.add_event_handler(verify_report)
    client.add_event_handler(analytics)
    client.add_event_handler(set_token)
    client.add_event_handler(handle_message)

    await client.start()
    logger.info("run...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())