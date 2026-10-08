import os
import html
import re
import requests
from contextlib import asynccontextmanager
from typing import List, Dict, Any
from fastapi import FastAPI, BackgroundTasks, Request
import libsql_client

# ==============================================================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TURSO_DB_URL = os.getenv("TURSO_DB_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# Auto-correct protocol scheme for Turso client
if TURSO_DB_URL.startswith("libsql://") or TURSO_DB_URL.startswith("wss://"):
    TURSO_DB_URL = TURSO_DB_URL.replace("libsql://", "https://").replace("wss://", "https://")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# USD to VND conversion baseline
VND_PER_USD = 26060

# Translation mapping for common Vietnamese real estate terms
TRANSLATION_DICT = {
    r"\bcho thuê\b": "For Rent:",
    r"\bcăn hộ\b": "Apartment",
    r"\bchung cư\b": "Condo",
    r"\bphòng trọ\b": "Studio/Room",
    r"\bnhà nguyên căn\b": "Whole House",
    r"\bbiệt thự\b": "Villa",
    r"\bđầy đủ nội thất\b": "Fully Furnished",
    r"\bfull nội thất\b": "Fully Furnished",
    r"\bphòng ngủ\b": "Bedroom(s)",
    r"\bpn\b": "BR",
    r"\bgiá rẻ\b": "Affordable",
    r"\btrung tâm\b": "Central",
    r"\bchính chủ\b": "Direct Owner",
    r"\bban công\b": "with Balcony",
    r"\bthang máy\b": "with Elevator",
}

# ==============================================================================
# 2. HELPER FUNCTIONS: TRANSLATION & LAYOUT FORMATTING
# ==============================================================================
def translate_title(title: str) -> str:
    """Translates key Vietnamese terms in housing titles into English."""
    translated = title
    for pattern, replacement in TRANSLATION_DICT.items():
        translated = re.sub(pattern, replacement, translated, flags=re.IGNORECASE)
    return translated.strip()

def extract_bedrooms(text: str, default_val: Any = None) -> str:
    """Extracts bedroom count from title or raw metadata."""
    if default_val and str(default_val) not in ["0", "None", ""]:
        return str(default_val)
    match = re.search(r'(\d+)\s*(?:pn|phòng ngủ|bedroom|br)', text, re.IGNORECASE)
    return match.group(1) if match else "Studio / N/A"

def format_price(raw_price: Any, price_string: str = "") -> str:
    """Formats numeric VND price and appends USD conversion (~$XXX USD)."""
    try:
        if raw_price and int(raw_price) > 0:
            vnd_val = int(raw_price)
            usd_val = round(vnd_val / VND_PER_USD)
            formatted_vnd = f"{vnd_val:,}".replace(",", ".")
            return f"{formatted_vnd} VND / month (~${usd_val:,} USD)"
    except (ValueError, TypeError):
        pass

    if price_string:
        return price_string

    return "Contact for Price"

def build_listing_message(item: Dict[str, Any]) -> str:
    """Formats message layout strictly matching the design screenshot."""
    district = html.escape(item['location'].upper())
    title_en = html.escape(item['title_en'])
    price_fmt = html.escape(item['price_formatted'])
    location = html.escape(item['location'])
    bedrooms = html.escape(str(item['bedrooms']))

    message = (
        f"🚨 <b>NEW LISTING ALERT | {district}</b>\n\n"
        f"🏠 <b>{title_en}</b>\n"
        f"💰 <b>Price:</b> {price_fmt}\n"
        f"📍 <b>Area:</b> {location}, Hanoi\n"
        f"🛏 <b>Bedrooms:</b> {bedrooms}\n"
        f"📲 <b>Contact:</b> Contact on Site\n"
        f"🌐 <b>Source:</b> Cho Tot\n\n"
        f"🤖 <i>Powered by Hanoi Housing Radar</i>"
    )
    return message

# ==============================================================================
# 3. DATABASE INITIALIZATION & LIFESPAN
# ==============================================================================
async def init_db():
    """Ensures required tables exist in Turso database."""
    if not TURSO_DB_URL:
        print("Warning: TURSO_DB_URL is missing or unconfigured.")
        return

    try:
        async with libsql_client.create_client(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    price TEXT,
                    location TEXT,
                    url TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_bookmarks (
                    user_id BIGINT NOT NULL,
                    listing_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, listing_id)
                );
            """)
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database initialization error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="Hanoi Housing Scraper & Bot", lifespan=lifespan)

# ==============================================================================
# 4. TELEGRAM API HELPERS
# ==============================================================================
def send_telegram_message(chat_id: int | str, text: str, reply_markup: dict = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

def answer_callback_query(callback_query_id: str, text: str):
    try:
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery", json={
            "callback_query_id": callback_query_id,
            "text": text
        }, timeout=10)
    except Exception as e:
        print(f"Failed to answer callback query: {e}")

def edit_message_reply_markup(chat_id: int, message_id: int, inline_keyboard: list):
    try:
        requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json={
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": inline_keyboard}
        }, timeout=10)
    except Exception as e:
        print(f"Failed to edit message reply markup: {e}")

# ==============================================================================
# 5. CHO TOT SCRAPER & SCHEDULER TASK LOGIC
# ==============================================================================
def fetch_chotot_listings() -> List[Dict[str, Any]]:
    """Fetches recent apartment rental listings in Hanoi from Cho Tot API."""
    url = "https://gateway.chotot.com/v1/public/ad-listing?region_v2=12000&cg=1010&limit=20"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            ads = resp.json().get("ads", [])
            listings = []
            for ad in ads:
                list_id = str(ad.get("list_id"))
                raw_title = ad.get("subject", "No Title")
                
                translated_title = translate_title(raw_title)
                price_fmt = format_price(ad.get("price"), ad.get("price_string", ""))
                area_name = ad.get("area_name", "Hanoi")
                bedrooms = extract_bedrooms(raw_title, ad.get("rooms"))
                
                maps_url = f"https://www.google.com/maps/search/{requests.utils.quote(area_name + ' Hanoi')}"

                listings.append({
                    "id": list_id,
                    "title_en": translated_title,
                    "price_formatted": price_fmt,
                    "location": area_name,
                    "bedrooms": bedrooms,
                    "url": f"https://www.chotot.com/{list_id}.htm",
                    "maps_url": maps_url
                })
            return listings
    except Exception as e:
        print(f"Scraper error: {e}")
    return []

async def run_scraper_task():
    """Scrapes Cho Tot, identifies new listings, saves to Turso, and posts to Telegram."""
    listings = fetch_chotot_listings()
    if not listings or not TURSO_DB_URL:
        return

    try:
        async with libsql_client.create_client(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN) as db:
            for item in listings:
                res = await db.execute("SELECT id FROM listings WHERE id = ?", (item["id"],))
                if not res.rows:
                    # Save new listing to database
                    await db.execute(
                        "INSERT INTO listings (id, title, price, location, url) VALUES (?, ?, ?, ?, ?)",
                        (item["id"], item["title_en"], item["price_formatted"], item["location"], item["url"])
                    )
                    
                    # Post formatted alert to Telegram
                    if TELEGRAM_CHAT_ID:
                        text = build_listing_message(item)
                        
                        markup = {
                            "inline_keyboard": [
                                [
                                    {"text": "🔗 View Original Listing", "url": item["url"]}
                                ],
                                [
                                    {"text": "📍 Area Map", "url": item["maps_url"]},
                                    {"text": "⭐ Bookmark", "callback_data": f"bookmark:{item['id']}"}
                                ]
                            ]
                        }
                        send_telegram_message(TELEGRAM_CHAT_ID, text, reply_markup=markup)
    except Exception as e:
        print(f"Error processing scraped listings: {e}")

# ==============================================================================
# 6. FASTAPI ROUTES & CRON-JOB / WEBHOOK ENDPOINTS
# ==============================================================================
@app.get("/")
def read_root():
    return {"status": "online", "service": "Hanoi Housing Scraper"}

@app.get("/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
    """Safely triggers the scraper without ever returning large response payloads."""
    async def safe_scrape_wrapper():
        try:
            await run_scraper_task()
        except Exception as e:
            print(f"Scraper task encountered an error: {e}")

    background_tasks.add_task(safe_scrape_wrapper)
    return {"status": "ok"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handles incoming callback queries and user commands from Telegram."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "bad request"}

    # --- 1. Inline Button Callbacks (Bookmark / Unbookmark) ---
    if "callback_query" in data:
        callback = data["callback_query"]
        callback_id = callback["id"]
        user_id = callback["from"]["id"]
        action_data = callback.get("data", "")
        
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if ":" in action_data and TURSO_DB_URL:
            action, listing_id = action_data.split(":", 1)

            async with libsql_client.create_client(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN) as db:
                if action == "bookmark":
                    await db.execute(
                        "INSERT OR IGNORE INTO user_bookmarks (user_id, listing_id) VALUES (?, ?)",
                        (user_id, listing_id)
                    )
                    answer_callback_query(callback_id, "Saved to bookmarks!")

                    if "reply_markup" in message and "inline_keyboard" in message["reply_markup"]:
                        orig_rows = message["reply_markup"]["inline_keyboard"]
                        for row in orig_rows:
                            for btn in row:
                                if btn.get("callback_data") == f"bookmark:{listing_id}":
                                    btn["text"] = "❌ Remove Bookmark"
                                    btn["callback_data"] = f"unbookmark:{listing_id}"
                        edit_message_reply_markup(chat_id, message_id, orig_rows)

                elif action == "unbookmark":
                    await db.execute(
                        "DELETE FROM user_bookmarks WHERE user_id = ? AND listing_id = ?",
                        (user_id, listing_id)
                    )
                    answer_callback_query(callback_id, "Removed from bookmarks.")

                    if "reply_markup" in message and "inline_keyboard" in message["reply_markup"]:
                        orig_rows = message["reply_markup"]["inline_keyboard"]
                        for row in orig_rows:
                            for btn in row:
                                if btn.get("callback_data") == f"unbookmark:{listing_id}":
                                    btn["text"] = "⭐ Bookmark"
                                    btn["callback_data"] = f"bookmark:{listing_id}"
                        edit_message_reply_markup(chat_id, message_id, orig_rows)

        return {"status": "ok"}

    # --- 2. User Commands (/bookmarks) ---
    if "message" in data and "text" in data["message"]:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()

        if text == "/bookmarks" and TURSO_DB_URL:
            async with libsql_client.create_client(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN) as db:
                res = await db.execute("""
                    SELECT l.id, l.title, l.price, l.url 
                    FROM user_bookmarks b 
                    JOIN listings l ON b.listing_id = l.id 
                    WHERE b.user_id = ?
                    ORDER BY b.created_at DESC
                """, (chat_id,))
                rows = res.rows

            if not rows:
                send_telegram_message(chat_id, "You haven't saved any bookmarks yet.")
            else:
                send_telegram_message(chat_id, f"🔖 <b>Your Saved Bookmarks ({len(rows)}):</b>")
                for row in rows:
                    l_id, title, price, url = row[0], row[1], row[2], row[3]
                    safe_title = html.escape(str(title))
                    safe_price = html.escape(str(price))
                    
                    item_text = f"🏠 <b>{safe_title}</b>\n💰 {safe_price}"
                    markup = {
                        "inline_keyboard": [
                            [
                                {"text": "❌ Remove", "callback_data": f"unbookmark:{l_id}"},
                                {"text": "🔗 View Listing", "url": url}
                            ]
                        ]
                    }
                    send_telegram_message(chat_id, item_text, reply_markup=markup)

    return {"status": "ok"}

# ==============================================================================
# 7. HEARTBEAT / KEEP-ALIVE ENDPOINT
# ==============================================================================
@app.get("/ping")
def ping_healthcheck():
    """Lightweight endpoint called every 10 mins to keep Render awake."""
    return {"status": "awake"}
