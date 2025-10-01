# server.py
import os, io, base64
import httpx
import numpy as np
from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI

# ===== Env vars (بعداً در Render ست می‌کنی) =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN","").strip()
OPENAI = OpenAI()  # از OPENAI_API_KEY محیط می‌خواند

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

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
        await send_text(chat_id, "سلام! عکس پیتزات رو بفرست تا تحلیل هوش مصنوعی و نکات عملی بدم 🍕🤖")
        return {"ok": True}

    photos = msg.get("photo") or []
    if photos:
        file_id = photos[-1]["file_id"]
        try:
            img_bytes = await download_telegram_file(file_id)
            message_fa = await oai_analyze(img_bytes)
            await send_text(chat_id, message_fa)
        except Exception:
            await send_text(chat_id, "⚠️ خطا در پردازش تصویر. دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "پیامت رسید ✅ یک عکس هم بفرست تا دقیق‌تر راهنمایی کنم.")
    return {"ok": True}

# -------- OpenAI Vision (gpt-4o-mini) --------
async def oai_analyze(image_bytes: bytes) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt_fa = (
        "تو یک متخصص پخت پیتزا هستی. این عکس را ببین و خیلی کوتاه و کاربردی به فارسی بگو:\n"
        "1) وضعیت پخت (خوب/کم‌پخت/سوخته)\n"
        "2) 2 تا 3 توصیهٔ دقیق (مثلاً دما/زمان/پیش‌گرمایش/چیدمان تاپینگ)\n"
        "خروجی کوتاه باشد و مستقیم قابل اجرا."
    )
    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role":"user",
            "content":[
                {"type":"text","text": prompt_fa},
                {"type":"image_url","image_url":{"url": f"data:image/jpeg;base64,{img_b64}"}}
            ],
        }],
        temperature=0.2,
        max_tokens=250,
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
