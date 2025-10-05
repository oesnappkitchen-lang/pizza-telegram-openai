**server.py (نسخه‌ی به‌روز فقط با تغییر پرامپت)**
```python
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

# -------- Prompt Builder (فقط همین بخش جدید شده) --------
from typing import Optional, Dict

def build_prompt_fa(vendor: str = "", item: str = "", params: Optional[Dict[str, str]] = None) -> str:
    """
    پرامپت تخصصی و فشرده برای ارزیابی پخت پیتزا از روی تصویر.
    - می‌تواند بعداً با vendor/item/params پر شود (از شیت یا کپشن)، ولی فعلاً اختیاری‌اند.
    """
    params = params or {}
    # کلیدهای فارسی/انگلیسی هر دو پشتیبانی می‌شوند
    oven_temp   = params.get("OvenTemp")   or params.get("دمای فر")        or ""
    bake_time   = params.get("BakeTime")   or params.get("زمان پخت")       or ""
    style       = params.get("Style")      or params.get("استایل")         or ""
    hydration   = params.get("Hydration")  or params.get("هیدریشن")        or ""
    cheese_type = params.get("Cheese")     or params.get("نوع پنیر")       or ""
    sauce       = params.get("Sauce")      or params.get("سس")             or ""

    meta = []
    if vendor:      meta.append(f"- وندور: {vendor}")
    if item:        meta.append(f"- آیتم: {item}")
    if style:       meta.append(f"- استایل: {style}")
    if oven_temp:   meta.append(f"- دمای مرجع: {oven_temp}")
    if bake_time:   meta.append(f"- زمان مرجع: {bake_time}")
    if hydration:   meta.append(f"- هیدریشن: {hydration}")
    if cheese_type: meta.append(f"- پنیر: {cheese_type}")
    if sauce:       meta.append(f"- سس: {sauce}")
    meta_block = ("\n" + "\n".join(meta) + "\n") if meta else ""

    return (
        "تو یک سرآشپز و متخصص پخت پیتزا هستی و باید از روی تصویر، کیفیت پخت را دقیق ارزیابی کنی.\n"
        "مشخصات مورد توجه:" + meta_block + "\n"
        "راهنما (فقط برای تحلیل ذهنی تو — چیزی از این بخش چاپ نکن):\n"
        "• لبه/کرست: پف، رنگ (قهوه‌ای طلایی/سوختگی)، لئوپاردینگ، خشکی/نمناک\n"
        "• کف (Undercarriage): روشن/یکنواخت/سوختگی نقطه‌ای/خام\n"
        "• مرکز: پخت کامل یا خمیری/آب‌افتادگی\n"
        "• پنیر: ذوب، روغن‌اندازی، کشسانی، دانه‌دانه/لاستیکی\n"
        "• تاپینگ: توزیع، رطوبت، فشار روی پخت خمیر\n"
        "• همخوانی با استایل/برند (نئاپولیتن مرکز نرم قابل‌قبول؛ نیویورکی کف سفت‌تر)\n"
        "• اگر پارامترهای مرجع بالا موجودند، توصیه‌ها را با آنها همسو کن.\n\n"
        "اکنون فقط این خروجی کوتاه چاپ شود (بدون توضیح اضافه، بدون ایموجی، حداکثر ۲ خط):\n"
        "1) وضعیت پخت: «خوب» یا «کم‌پخت» یا «بیش‌پخت/سوخته» + یک اشارهٔ ۲-۳ کلمه‌ای به علت.\n"
        "2) ۲ تا ۳ توصیهٔ دقیق و اجراپذیر (دما/زمان/پیش‌گرمایش/جایگاه در فر/تاپینگ/ضخامت خمیر) بسیار کوتاه.\n"
    )

# -------- OpenAI Vision (gpt-4o-mini) --------
async def oai_analyze(image_bytes: bytes) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    # در آینده اگر از شیت/کپشن vendor/item داری، اینجا پاس بده:
    # prompt_fa = build_prompt_fa(vendor, item, params)
    prompt_fa = build_prompt_fa()  # فعلاً بدون vendor/item
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
```


