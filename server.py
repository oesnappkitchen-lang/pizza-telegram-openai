# server.py
import os, io, base64
import httpx
import numpy as np
from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import Optional, Dict

# ===== Env vars =====
TELEGRAM_TOKEN = os.getenv("8075927731:AAEOpiI9so1Sx03UWmQkMTo5xFcSn8hUxl8", "").strip()
OPENAI = OpenAI()
TG_API  = f"https://api.telegram.org/bot{8075927731:AAEOpiI9so1Sx03UWmQkMTo5xFcSn8hUxl8}"
TG_FILE = f"https://api.telegram.org/file/bot{8075927731:AAEOpiI9so1Sx03UWmQkMTo5xFcSn8hUxl8}"

app = FastAPI(title="Pizza AI Telegram (OpenAI)")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/webhook")
async def webhook(req: Request):
    if not TELEGRAM_TOKEN:
        return JSONResponse({"ok": False, "error": "missing TELEGRAM_TOKEN"}, status_code=500)

    data = await req.json()
    msg = data.get("message") or data.get("edited_message") or {}
    chat = (msg.get("chat") or {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        await send_text(chat_id, "سلام! عکس پیتزات رو بفرست تا تحلیل خیلی کوتاه و عملی بدم 🍕🤖")
        return {"ok": True}

    photos = msg.get("photo") or []
    if photos:
        file_id = photos[-1]["file_id"]
        try:
            img_bytes = await download_telegram_file(file_id)
            # اگر خواستی وندور/آیتم را از کپشن بخوانی:
            # vendor, item = parse_vendor_item_from_caption(text)
            message_fa = await oai_analyze(img_bytes)  # می‌توانی vendor,item را هم پاس بدهی
            await send_text(chat_id, message_fa)
        except Exception:
            await send_text(chat_id, "⚠️ خطا در پردازش تصویر. دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "پیامت رسید ✅ یک عکس هم بفرست تا دقیق‌تر راهنمایی کنم.")
    return {"ok": True}

# -------- Prompt Builder (سفت و کوتاه) --------
def build_prompt_fa(vendor: str = "", item: str = "", params: Optional[Dict[str, str]] = None) -> str:
    """
    خروجی باید دقیقاً دو خط باشد؛ بدون تیتر، شماره، بولت، ایموجی یا خط خالی.
    خط۱: verdict کوتاه + علت ۲–۳ کلمه در پرانتز → خوب | کم‌پخت | بیش‌پخت/سوخته
    خط۲: سه توصیهٔ خیلی کوتاه و اجراپذیر؛ با جداکننده «؛».
    """
    params = params or {}
    meta = []
    if vendor: meta.append(f"وندور: {vendor}")
    if item:   meta.append(f"آیتم: {item}")
    meta_txt = (" | ".join(meta) + " | ") if meta else ""
    return (
        f"{meta_txt}فقط نتیجه را بده.\n"
        "خروجی دقیقاً دو خط؛ هیچ تیتر/شماره/بولت/ایموجی/خط خالی نگذار.\n"
        "خط۱: یکی از خوب/کم‌پخت/بیش‌پخت یا سوخته + علت کوتاه در پرانتز.\n"
        "خط۲: سه توصیهٔ خیلی کوتاه و اجراپذیر (دما/زمان/پیش‌گرمایش/جایگاه فر/تاپینگ/ضخامت) با «؛» جدا شود.\n"
        "نمونهٔ قالب:\n"
        "کم‌پخت (کف روشن)\n"
        "پیش‌گرمایش کامل؛ دما +۱۵°C؛ زمان +۳۰ث"
    )

# -------- OpenAI Vision (gpt-4o-mini) --------
async def oai_analyze(image_bytes: bytes, vendor: str = "", item: str = "", params: Optional[Dict[str, str]] = None) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt_fa = build_prompt_fa(vendor, item, params)

    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            # قانون پاسخ: دقیقاً دو خط
            {"role": "system",
             "content": ("You are a pizza-baking expert. Reply in Persian. "
                         "EXACTLY TWO LINES. No titles, numbering, bullets, emojis, or blank lines. "
                         "Line1: verdict (good | underbaked | overbaked/burnt) with a 2–3 word reason in parentheses. "
                         "Line2: three ultra-concise actionable tips separated by '؛'. "
                         "Keep each tip under 5 words.")},
            {"role": "user",
             "content": [
                 {"type": "text", "text": prompt_fa},
                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
             ]}
        ],
        temperature=0.2,
        max_tokens=80,   # کوتاه برای جلوگیری از اضافات
    )
    return (resp.choices[0].message.content or "نتیجه‌ای دریافت نشد.").strip()

# -------- Telegram helpers --------
async def download_telegram_file(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.get(f"{TG_API}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        fr = await cx.get(f"{TG_FILE}/{file_path}")
        fr.raise_for_status()
        return fr.content

async def send_text(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

# (اختیاری) اگر روزی خواستی vendor/item را از کپشن بخوانی:
def parse_vendor_item_from_caption(caption: str) -> tuple[str, str]:
    # ساده و اختیاری: "vendor: X | item: Y"
    v = i = ""
    if not caption: return v, i
    parts = [p.strip() for p in caption.split("|")]
    for p in parts:
        if p.lower().startswith("vendor:"): v = p.split(":",1)[1].strip()
        if p.lower().startswith("item:"):   i = p.split(":",1)[1].strip()
    return v, i
