import os
import json
import requests
import libsql_experimental as libsql
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from openai import OpenAI

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.environ.get("8845223451:AAEdPyWR8XXFH3mJyYyujqgfi15AMrY8dh4")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1004325097866")
TURSO_DB_URL = os.environ.get("libsql://hanoi-housing-henghan0608.aws-ap-northeast-1.turso.io")
TURSO_AUTH_TOKEN = os.environ.get("eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODg3MjI0OTQsImlkIjoiMDFhMDc4MmEtYzUwMS03NGYzLWJjNDQtMWE5MDg0ZTFmOTE5Iiwia2lkIjoiTlhSUmQ5QjNnVEU2T1ZQdV9DMEt1S000VW9xNU5DUEc2OHY5RjhFMGNiZyIsInJpZCI6IjBiYTg5ZmYyLWFjZjItNDVlMy04ZDYwLTU5OTcyYmUzZTU1YiJ9.Nv3UApmgGtiFIBZNE3KFkUMNQZk9mVVrHqeHAHm3Z8SeBzhNnu-s-MPOjaE6SVpdUeOJ6HnEe4rbzZ-5ZePkBQ")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

VND_PER_USD = 25400

app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------------- DATABASE CONNECTION ----------------
def get_db():
    return libsql.connect(f"{TURSO_DB_URL}?auth_token={TURSO_AUTH_TOKEN}")

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
    conn.commit()
    conn.close()

init_db()

# ---------------- HELPER FUNCTIONS ----------------
def get_listing_details_by_id(listing_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT url, title, district, price_vnd, bedrooms, contact_phone, source FROM seen_listings WHERE id = ?", (listing_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"url": row[0], "title": row[1], "district": row[2], "price_vnd": row[3], "bedrooms": row[4], "contact_phone": row[5], "source": row[6]}
    return None

def save_bookmark(user_id: str, listing_url: str) -> bool:
    conn = get_db()
    try:
        conn.execute("INSERT INTO bookmarks (user_id, listing_url) VALUES (?, ?)", (user_id, listing_url))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()

def get_user_bookmarks(user_id: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT listing_url, created_at FROM bookmarks WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# ---------------- WEBHOOK ENDPOINT ----------------
@app.post("/webhook")
async def telegram_webhook(request: Request):
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
                listing = get_listing_details_by_id(listing_id)

                if listing:
                    is_new = save_bookmark(user_id, listing["url"])
                    toast_text = "⭐ Saved to your bot bookmarks!" if is_new else "ℹ️ Already in your bookmarks!"

                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={
                        "callback_query_id": cb_id,
                        "text": toast_text,
                        "show_alert": False
                    })

                    msg_obj = cb.get("message")
                    if msg_obj:
                        msg_chat_id = msg_obj["chat"]["id"]
                        msg_id = msg_obj["message_id"]
                        existing_markup = msg_obj.get("reply_markup", {})

                        if "inline_keyboard" in existing_markup:
                            updated_keyboard = []
                            for row in existing_markup["inline_keyboard"]:
                                new_row = []
                                for button in row:
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

    # 2. Handle Commands (/start, /bookmarks)
    elif "message" in update and "text" in update["message"]:
        msg = update["message"]
        text = msg["text"].strip()
        chat_id = msg["chat"]["id"]
        user_id = str(msg["from"]["id"])

        if text == "/start":
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": "👋 Welcome to <b>Hanoi Housing Radar</b>!\n\nUse /bookmarks to view saved properties.",
                "parse_mode": "HTML"
            })

        elif text == "/bookmarks" or text.startswith("/bookmarks@"):
            bookmarks = get_user_bookmarks(user_id)
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

                for b_url, b_time in bookmarks:
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute("SELECT title, district, price_vnd, bedrooms, contact_phone, source FROM seen_listings WHERE url = ?", (b_url,))
                    row = cursor.fetchone()
                    conn.close()

                    if row:
                        title, district, price_vnd, bedrooms, contact_phone, source = row
                        usd_approx = round(price_vnd / VND_PER_USD) if price_vnd > 0 else 0
                        price_display = f"<b>{price_vnd:,.0f} VND</b> / month (~${usd_approx:,} USD)" if price_vnd > 0 else "<b>Contact Landlord</b>"
                        card_msg = (
                            f"🏠 <b>{title}</b>\n"
                            f"💰 <b>Price:</b> {price_display}\n"
                            f"📍 <b>Area:</b> {district}, Hanoi\n"
                            f"🛏️ <b>Bedrooms:</b> {bedrooms}\n"
                            f"📲 <b>Contact:</b> {contact_phone}\n"
                            f"🌐 <b>Source:</b> {source}\n"
                            f"📅 <i>Saved on: {b_time[:10]}</i>"
                        )
                    else:
                        card_msg = f"🔗 <a href='{b_url}'>View Original Listing</a>\n📅 <i>Saved on: {b_time[:10]}</i>"

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