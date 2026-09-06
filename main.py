import os
import json
import requests
import libsql_client
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel, Field
from openai import OpenAI

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1004325097866")
TURSO_DB_URL = os.environ.get("TURSO_DB_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

VND_PER_USD = 25400

app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


# ---------------- 1. STRUCTURED OUTPUT SCHEMA ----------------
class RentalListing(BaseModel):
    title_en: str = Field(description="Expat Housing - Worry Free / High quality concise summary title")
    price_vnd: int = Field(description="Price in VND as an integer (e.g., 12000000). Set to 0 if unknown.")
    district: str = Field(description="District name in Hanoi e.g., Tay Ho, Cau Giay, Ba Dinh")
    bedrooms: int = Field(description="Number of bedrooms, default to 1 if unknown")
    contact_phone: str = Field(description="Contact phone number or 'Contact on Site' if not available")


# ---------------- 2. DATABASE HELPERS ----------------
def get_db():
    return libsql_client.create_client_sync(
        url=TURSO_DB_URL,
        auth_token=TURSO_AUTH_TOKEN
    )

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            district TEXT,
            price_vnd INTEGER,
            bedrooms INTEGER,
            contact_phone TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            listing_url TEXT NOT NULL,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, listing_url)
        )
    """)
    conn.close()

# Initialize DB on start
try:
    init_db()
except Exception as e:
    print(f"Database initialization warning: {e}")


# ---------------- 3. SCRAPER & TELEGRAM LOGIC ----------------
def is_url_seen(url: str) -> bool:
    conn = get_db()
    res = conn.execute("SELECT id FROM seen_listings WHERE url = ?", (url,))
    seen = len(res.rows) > 0
    conn.close()
    return seen

def save_seen_listing(url, title, district, price_vnd, bedrooms, contact_phone, source="ChoTot") -> int:
    conn = get_db()
    res = conn.execute("""
        INSERT INTO seen_listings (url, title, district, price_vnd, bedrooms, contact_phone, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (url, title, district, price_vnd, bedrooms, contact_phone, source))
    listing_id = res.last_insert_rowid
    conn.close()
    return listing_id

def send_telegram_alert(listing_id, title, price_vnd, district, bedrooms, phone, original_url):
    usd_approx = round(price_vnd / VND_PER_USD) if price_vnd > 0 else 0
    price_display = f"<b>{price_vnd:,.0f} VND</b> / month (~${usd_approx:,} USD)" if price_vnd > 0 else "<b>Contact Landlord</b>"
    
    maps_url = f"https://www.google.com/maps/search/?api=1&query=Apartment+for+rent+{district.replace(' ', '+')}+Hanoi"

    msg_html = (
        f"🏠 <b>{title}</b>\n\n"
        f"💰 <b>Price:</b> {price_display}\n"
        f"📍 <b>District:</b> {district}, Hanoi\n"
        f"🛏️ <b>Bedrooms:</b> {bedrooms}\n"
        f"📲 <b>Contact Phone:</b> {phone}\n\n"
        f"🔍 <i>Scraped from ChoTot</i>"
    )

    inline_keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔗 View Listing", "url": original_url},
                {"text": "📍 Map", "url": maps_url}
            ],
            [
                {"text": "⭐ Bookmark", "callback_data": f"bm:{listing_id}"}
            ]
        ]
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg_html,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(inline_keyboard)
    }
    requests.post(url, data=payload)

def scrape_chotot():
    """Scrapes ChoTot API for fresh Hanoi rental listings."""
    chotot_url = "https://gateway.chotot.com/v1/public/ad-listing?cg=1010&region=12&limit=10"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(chotot_url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"ChoTot API returned status {response.status_code}")
            return 0

        data = response.json()
        ads = data.get("ads", [])
        new_count = 0

        for ad in ads:
            list_id = ad.get("list_id")
            if not list_id:
                continue

            item_url = f"https://nhatot.com/vi/{list_id}.htm"
            if is_url_seen(item_url):
                continue

            raw_subject = ad.get("subject", "Hanoi Rental Property")
            price = ad.get("price", 0)
            area_name = ad.get("area_name", "Hanoi")
            bedrooms = ad.get("num_bedrooms", 1)
            phone = ad.get("phone", "Contact on Site")

            # Save listing to Turso
            listing_id = save_seen_listing(item_url, raw_subject, area_name, price, bedrooms, phone)
            
            # Post alert to Telegram Channel
            send_telegram_alert(listing_id, raw_subject, price, area_name, bedrooms, phone, item_url)
            new_count += 1

        return new_count
    except Exception as e:
        print(f"Error during ChoTot scraping: {e}")
        return 0


# ---------------- 4. API ENDPOINTS ----------------

@app.get("/scrape")
@app.post("/scrape")
async def trigger_scrape(background_tasks: BackgroundTasks):
    """Triggered by Cron-Job.org every 15 minutes."""
    background_tasks.add_task(scrape_chotot)
    return {"status": "ok", "message": "Scraper task queued successfully"}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Handles Telegram inline button callbacks and commands."""
    update = await request.json()

    # 1. Handle Bookmark Button Click
    if "callback_query" in update:
        cb = update["callback_query"]
        cb_id = cb["id"]
        user_id = str(cb["from"]["id"])
        data_str = cb.get("data", "")

        if data_str.startswith("bm:"):
            try:
                listing_id = int(data_str.split("bm:")[1])
                conn = get_db()
                res = conn.execute("SELECT url, title FROM seen_listings WHERE id = ?", (listing_id,))
                row = res.rows[0] if res.rows else None
                
                if row:
                    b_url = row[0]
                    # Insert bookmark
                    try:
                        conn.execute("INSERT INTO bookmarks (user_id, listing_url) VALUES (?, ?)", (user_id, b_url))
                        toast_text = "⭐ Saved to your bot bookmarks!"
                    except Exception:
                        toast_text = "ℹ️ Already in your bookmarks!"
                    conn.close()

                    # Answer popup toast
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={
                        "callback_query_id": cb_id,
                        "text": toast_text,
                        "show_alert": False
                    })

                    # Update button text to "✅ Bookmarked"
                    msg_obj = cb.get("message")
                    if msg_obj:
                        msg_chat_id = msg_obj["chat"]["id"]
                        msg_id = msg_obj["message_id"]
                        existing_markup = msg_obj.get("reply_markup", {})

                        if "inline_keyboard" in existing_markup:
                            updated_keyboard = []
                            for row_item in existing_markup["inline_keyboard"]:
                                new_row = []
                                for button in row_item:
                                    if button.get("callback_data") == data_str:
                                        new_row.append({"text": "✅ Bookmarked", "callback_data": data_str})
                                    else:
                                        new_row.append(button)
                                updated_keyboard.append(new_row)

                            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup", json={
                                "chat_id": msg_chat_id,
                                "message_id": msg_id,
                                "reply_markup": json.dumps({"inline_keyboard": updated_keyboard})
                            })
            except Exception as e:
                print(f"Callback error: {e}")

    # 2. Handle /bookmarks Command
    elif "message" in update and "text" in update["message"]:
        msg = update["message"]
        text = msg["text"].strip()
        chat_id = msg["chat"]["id"]
        user_id = str(msg["from"]["id"])

        if text in ["/bookmarks", f"/bookmarks@{TELEGRAM_BOT_TOKEN}"]:
            conn = get_db()
            res = conn.execute("""
                SELECT s.title, s.district, s.price_vnd, s.bedrooms, s.contact_phone, s.source, b.listing_url, b.created_at
                FROM bookmarks b
                LEFT JOIN seen_listings s ON b.listing_url = s.url
                WHERE b.user_id = ?
                ORDER BY b.created_at DESC
            """, (user_id,))
            bookmarks = res.rows
            conn.close()

            if not bookmarks:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "<b>You don't have any saved listings yet!</b>\nTap ⭐ Bookmark on any post in the channel to save it.",
                    "parse_mode": "HTML"
                })
            else:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": f"⭐ <b>YOUR SAVED LISTINGS ({len(bookmarks)})</b>",
                    "parse_mode": "HTML"
                })

                for b in bookmarks:
                    title, district, price_vnd, bedrooms, phone, source, b_url, b_time = b
                    if title:
                        usd_approx = round(price_vnd / VND_PER_USD) if price_vnd and price_vnd > 0 else 0
                        price_display = f"<b>{price_vnd:,.0f} VND</b> / month (~${usd_approx:,} USD)" if price_vnd and price_vnd > 0 else "<b>Contact Landlord</b>"
                        card_msg = (
                            f"🏠 <b>{title}</b>\n"
                            f"💰 <b>Price:</b> {price_display}\n"
                            f"📍 <b>Area:</b> {district}, Hanoi\n"
                            f"🛏️ <b>Bedrooms:</b> {bedrooms}\n"
                            f"📲 <b>Contact:</b> {phone}\n"
                            f"📅 <i>Saved on: {str(b_time)[:10]}</i>"
                        )
                    else:
                        card_msg = f"🔗 <a href='{b_url}'>View Original Listing</a>"

                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                        "chat_id": chat_id,
                        "text": card_msg,
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "🔗 Open Listing", "url": b_url}]]})
                    })

    return {"status": "ok"}


@app.get("/")
def health_check():
    return {"status": "bot online"}