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

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Exchange rate baseline (1 USD = ~25,400 VND)
VND_PER_USD = 25400

# ==============================================================================
# 2. HELPER FUNCTIONS: CURRENCY & FORMATTING
# ==============================================================================
def format_price(raw_price: Any, price_string: str = "") -> str:
    """Formats numeric VND price and converts to USD (~$XXX/mo)."""
    try:
        if raw_price and int(raw_price) > 0:
            vnd_val = int(raw_price)
            usd_val = round(vnd_val / VND_PER_USD)
            formatted_vnd = f"{vnd_val:,}".replace(",", ".")
            return f"{formatted_vnd} VND (~${usd_val:,}/mo)"
    except (ValueError, TypeError):
        pass

    # Fallback to string if price is a string like "15 triệu/tháng"
    if price_string:
        return price_string

    return "Contact for Price"

# ==============================================================================
# 3. DATABASE INITIALIZATION & LIFESPAN
# ==============================================================================
async def init_db():
    """Ensures required tables exist in Turso SQLite database."""
    if not TURSO_DB_URL or "your-turso-db-name" in TURSO_DB_URL:
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
# 4. TELEGRAM API HELPERS (HTML Safe)
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
    payload = {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": False
    }
    try:
        requests.post(f"{TELEGRAM_API_URL}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to answer callback query: {e}")

def edit_message_reply_markup(chat_id: int, message_id: int, inline_keyboard: list):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": inline_keyboard}
    }
    try:
        requests.post(f"{TELEGRAM_API_URL}/editMessageReplyMarkup", json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to edit message reply markup: {e}")

# ==============================================================================
# 5. CHO TOT SCRAPER LOGIC
# ==============================================================================
def fetch_chotot_listings() -> List[Dict[str, Any]]:
    """Fetches recent apartment rental listings in Hanoi from Cho Tot API."""
    url = "https://gateway.chotot.com/v1/public/ad-listing?region_v2=12000&cg=1010&limit=20"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            ads = data.get("ads", [])
            listings = []
            for ad in ads:
                list_id = str(ad.get("list_id"))
                subject = ad.get("subject", "No Title")
                
                # Raw numeric price vs string representation
                raw_price = ad.get("price")
                price_str = ad.get("price_string", "")
                formatted_price = format_price(raw_price, price_str)

                area_name = ad.get("area_name", "Hanoi")
                link = f"https://www.chotot.com/{list_id}.htm"
                
                listings.append({
                    "id": list_id,
                    "title": subject,
                    "price": formatted_price,
                    "location": area_name,
                    "url": link
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
                        (item["id"], item["title"], item["price"], item["location"], item["url"])
                    )
                    
                    # Post notification to Telegram
                    if TELEGRAM_CHAT_ID:
                        safe_title = html.escape(item['title'])
                        safe_price = html.escape(item['price'])
                        safe_loc = html.escape(item['location'])

                        text = (
                            f"🏠 <b>{safe_title}</b>\n"
                            f"💰 <b>Price:</b> {safe_price}\n"
                            f"📍 <b>Location:</b> {safe_loc}"
                        )
                        markup = {
                            "inline_keyboard": [
                                [
                                    {"text": "⭐ Bookmark", "callback_data": f"bookmark:{item['id']}"},
                                    {"text": "🔗 View Listing", "url": item["url"]}
                                ]
                            ]
                        }
                        send_telegram_message(TELEGRAM_CHAT_ID, text, reply_markup=markup)
    except Exception as e:
        print(f"Error processing scraped listings: {e}")

# ==============================================================================
# 6. FASTAPI ROUTES & WEBHOOKS
# ==============================================================================
@app.get("/")
def read_root():
    return {"status": "online", "service": "Hanoi Housing Scraper"}

@app.get("/scrape")
def trigger_scrape(background_tasks: BackgroundTasks):
    """Endpoint triggered by Cron-Job.org every 15 minutes."""
    background_tasks.add_task(run_scraper_task)
    return {"status": "ok", "message": "Scraper task queued successfully"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handles incoming callback queries and commands from Telegram."""
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

                    new_keyboard = [[{"text": "❌ Remove Bookmark", "callback_data": f"unbookmark:{listing_id}"}]]
                    if "reply_markup" in message and "inline_keyboard" in message["reply_markup"]:
                        orig_buttons = message["reply_markup"]["inline_keyboard"][0]
                        for btn in orig_buttons:
                            if "url" in btn:
                                new_keyboard[0].append(btn)

                    edit_message_reply_markup(chat_id, message_id, new_keyboard)

                elif action == "unbookmark":
                    await db.execute(
                        "DELETE FROM user_bookmarks WHERE user_id = ? AND listing_id = ?",
                        (user_id, listing_id)
                    )
                    answer_callback_query(callback_id, "Removed from bookmarks.")

                    new_keyboard = [[{"text": "⭐ Bookmark", "callback_data": f"bookmark:{listing_id}"}]]
                    if "reply_markup" in message and "inline_keyboard" in message["reply_markup"]:
                        orig_buttons = message["reply_markup"]["inline_keyboard"][0]
                        for btn in orig_buttons:
                            if "url" in btn:
                                new_keyboard[0].append(btn)

                    edit_message_reply_markup(chat_id, message_id, new_keyboard)

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