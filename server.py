# server.py
import os, base64
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import Optional, Dict, Tuple

# ========= ENV =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI = OpenAI()  # uses OPENAI_API_KEY from env

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Pizza Bake QA (No Sheet, Vendor Data Inline)")

# ========= YOUR DATA HERE =========
# اینجا دیتای خودت رو وارد کن.
# ساختار:
# - اگر فقط برای برند دهی: "برند": {"note": "متن دلخواه"}
# - اگر برای شعبه‌ها هم تفکیک داری:
#   "برند": {"branches": {"نام شعبه": "متن دلخواه", ...}, "note": "متن عمومی برای همه شعب"}
# اولویت نمایش: شعبه → برند → هیچ‌کدام
VENDOR_DATA: Dict[str, Dict] = {
    # نمونه‌ها — حذف/ویرایش کن:
    # "پلنت": {
    #     "branches": {
    #         "سعادت‌آباد": "مرجع پلنت/سعادت‌آباد: پنیر باید یکدست ذوب باشد، لبه‌ها طلایی و مرکز خشک.",
    #         "سهروردی":   "مرجع پلنت/سهروردی: برشتگی لبه کمی بیشتر، مرکز بدون آب‌افتادگی."
    #     },
    #     "note": "راهنمای عمومی پلنت: پنیر کاملاً ذوب، لبه‌های طلایی یکنواخت، بدون لکه‌های سوختگی عمیق."
    # },
    # "هپی پیتزا": {
    #     "note": "راهنمای عمومی هپی پیتزا: مرکز خشک، زیرِ خمیر ترد، پنیر بدون حوضچه روغن."
    # }
}

# ========= UTILS =========
def norm(s: str) -> str:
    return (s or "").strip().replace("ي", "ی").replace("ك", "ک")

def parse_brand_branch_caption(text: str) -> Tuple[str, str]:
    brand = branch = ""
    if not text:
        return brand, branch
    t = norm(text).replace("：", ":")
    for p in [x.strip() for x in t.split("|")]:
        if p.startswith("برند:"):
            brand = p.split(":", 1)[1].strip()
        elif p.startswith("شعبه:"):
            branch = p.split(":", 1)[1].strip()
    return brand, branch

def get_vendor_message(brand: str, branch: str) -> Optional[str]:
    """اگر متنی برای برند/شعبه در VENDOR_DATA باشد همان را برگردان؛ وگرنه None."""
    b = norm(brand)
    br = norm(branch)
    if not b:
        return None
    data = VENDOR_DATA.get(b)
    if not data:
        # تلاش با حروف مختلف (کیس‌این‌سنستیوِ ساده)
        for k in VENDOR_DATA.keys():
            if norm(k).lower() == b.lower():
                data = VENDOR_DATA[k]
                break
    if not data:
        return None
    # اول شعبه
    branches = data.get("branches") or {}
    for k, v in branches.items():
        if norm(k).lower() == br.lower() and str(v).strip():
            return str(v).strip()
    # بعد متن عمومی برند
    note = (data.get("note") or "").strip()
    return note or None

# ========= ROUTES =========
@app.get("/health")
async def health():
    return {"ok": True}

# کمک برای ست وبهوک (اختیاری)
@app.get("/set_webhook")
async def set_webhook():
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "missing TELEGRAM_TOKEN"}
    url = "https://pizza-telegram-openai.onrender.com/webhook"  # ← دامنه سرویس خودت
    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.get(f"{TG_API}/setWebhook", params={"url": url})
        return r.json()

@app.get("/get_webhook_info")
async def get_webhook_info():
    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.get(f"{TG_API}/getWebhookInfo")
        return r.json()

@app.post("/webhook")
async def webhook(req: Request):
    if not TELEGRAM_TOKEN:
        return JSONResponse({"ok": False, "error": "missing TELEGRAM_TOKEN"}, status_code=500)

    data = await req.json()
    msg  = data.get("message") or data.get("edited_message") or {}
    chat = (msg.get("chat") or {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    text   = (msg.get("text") or "").strip()
    photos = msg.get("photo") or []

    if text and text.startswith("/start"):
        await send_text(
            chat_id,
            "سلام! عکس پیتزا را بفرست. من فقط کیفیت پخت را از روی تصویر ارزیابی می‌کنم.\n"
            "اگر در کپشن بنویسی «برند: ... | شعبه: ...»، در صورت وجود داده، متن مرجع همان برند/شعبه را هم اضافه می‌کنم."
        )
        return {"ok": True}

    if photos:
        file_id = photos[-1]["file_id"]
        caption = (msg.get("caption") or "").strip()
        brand, branch = parse_brand_branch_caption(caption)

        try:
            # 1) متن مرجع شما (در صورت وجود)
            vendor_msg = get_vendor_message(brand, branch)

            # 2) دانلود تصویر
            img_bytes = await download_telegram_file(file_id)

            # 3) تحلیل «فقط کیفیت پخت»
            result = await analyze_bake_only(img_bytes)

            # 4) چسباندن متن مرجع شما (اگر بود)
            if vendor_msg:
                reply = f"{result}\n———\n{vendor_msg}"
            else:
                reply = result

            await send_text(chat_id, reply)

        except Exception as e:
            print("ERROR processing:", repr(e))
            await send_text(chat_id, "⚠️ خطا در پردازش تصویر. دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "برای ارزیابی، یک عکس پیتزا بفرست. کپشن «برند/شعبه» اختیاری است.")
    return {"ok": True}

# ========= ANALYSIS (ONLY bake quality) =========
async def analyze_bake_only(image_bytes: bytes) -> str:
    """
    خروجی دقیقاً دو خط:
    1) «وضعیت پخت: خوب/خام/سوخته/نیاز به بهبود»
    2) «توضیح سرآشپز: …» (بدون عدد/دما/زمان/ایموجی)
    """
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")

    system_prompt = (
        "You are a professional pizza chef who ONLY evaluates bake quality from an image. "
        "Reply in Persian (fa-IR). "
        "Return EXACTLY TWO LINES, no titles/bullets/emoji/extra lines. "
        "Do NOT suggest temperatures, times, or numbers. No recipes. "
        "Line1 format: «وضعیت پخت: خوب» OR «وضعیت پخت: خام» OR «وضعیت پخت: سوخته» OR «وضعیت پخت: نیاز به بهبود». "
        "Line2 format: «توضیح سرآشپز: …» with a concise visual reason (e.g., cheese not fully melted, crust pale/black, uneven bake, soggy center)."
    )

    user_prompt = (
        "از روی تصویر فقط کیفیت پخت را ارزیابی کن.\n"
        "به مواردی مثل آب‌شدن پنیر، یکنواختی برشتگی، خام/سوخته بودن خمیر، خیس بودن مرکز، رنگ لبه‌ها، لکه‌های تیره توجه کن.\n"
        "خروجی دقیقاً دو خط باشد و هیچ عدد/دما/زمانی نگو."
    )

    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": [
                 {"type": "text", "text": user_prompt},
                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
             ]}
        ],
        temperature=0.15,
        max_tokens=90,
    )
    content = (resp.choices[0].message.content or "").strip()

    # تضمین دو خط
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if len(lines) >= 2:
        return f"{lines[0]}\n{lines[1]}"
    return "وضعیت پخت: نیاز به بهبود\nتوضیح سرآشپز: تصویر واضح نیست؛ لطفاً دوباره عکس بفرست."

# ========= TELEGRAM HELPERS =========
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
