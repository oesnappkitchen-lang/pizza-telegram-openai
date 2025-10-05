
# server.py
@app.get("/set_webhook")
async def set_webhook():
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "missing TELEGRAM_TOKEN"}
    url = "https://pizza-telegram-openai.onrender.com/webhook"  # دقیقا دامنه سرویس خودت
    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.get(f"{TG_API}/setWebhook", params={"url": url})
        return r.json()

@app.get("/get_webhook_info")
async def get_webhook_info():
    async with httpx.AsyncClient(timeout=20) as cx:
        r = await cx.get(f"{TG_API}/getWebhookInfo")
        return r.json()

import os, base64, csv, io, json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import Optional, Dict, Tuple

# ========= ENV =========
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI           = OpenAI()  # uses OPENAI_API_KEY
SHEET_CSV_URL    = os.getenv("SHEET_CSV_URL", "https://docs.google.com/spreadsheets/d/e/2PACX-1vSaY9JOJ_VfO6sf1Y-KNr2YU202184PFpydDTpwTMV9zwxiBnZHKij46yx4qkbadHTJfJagLg4Lq01P/pub?gid=1314383190&single=true&output=csv").strip()  # لینک publish شده با output=csv

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Pizza AI Telegram (OpenAI)")

# ========= Health =========
@app.get("/health")
async def health():
    return {"ok": True}

# ========= Webhook =========
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
            "سلام! یک عکس پیتزا بفرست و در کپشن فارسی بنویس:\n"
            "برند: <نام برند> | شعبه: <نام شعبه>\n"
            "مثال: برند: پلنت | شعبه: سعادت‌آباد"
        )
        return {"ok": True}

    if photos:
        file_id = photos[-1]["file_id"]
        caption = (msg.get("caption") or "").strip()

        try:
            brand, branch = parse_brand_branch_caption(caption)
            if not brand:
                await send_text(chat_id, "⚠️ لطفاً برند را در کپشن بنویس: «برند: ... | شعبه: ...»")
                return {"ok": True}

            # 1) دانلود عکس
            img_bytes = await download_telegram_file(file_id)

            # 2) خواندن مرجع فقط با برند از CSV
            ref = await sheet_lookup_by_brand(brand)

            # 3) پارام‌های مرجع برای پرامپت
            params: Dict[str, str] = {}
            if ref:
                if ref.get("OvenTemp"): params["OvenTemp"] = ref["OvenTemp"]
                if ref.get("BakeTime"): params["BakeTime"] = ref["BakeTime"]

            # 4) تحلیل دوخطی
            result = await oai_analyze(img_bytes, brand=brand, params=params)

            # 5) خط مرجع
            ref_line = ""
            if ref:
                bits = []
                if branch:               bits.append(f"شعبه: {branch}")
                if ref.get("OvenTemp"):  bits.append(f"دمای مرجع: {ref['OvenTemp']}")
                if ref.get("BakeTime"):  bits.append(f"زمان مرجع: {ref['BakeTime']}")
                if bits:
                    ref_line = "———\n" + " | ".join(bits)

            await send_text(chat_id, result + ("\n" + ref_line if ref_line else ""))

        except Exception as e:
            print("ERROR processing:", repr(e))
            await send_text(chat_id, "⚠️ خطا در پردازش. لطفاً دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "برای بهترین نتیجه: عکس + کپشن فارسی شامل «برند» و (اختیاری) «شعبه» بفرست.")
    return {"ok": True}

# ========= Caption parsing (FA): "برند: X | شعبه: Y" =========
def parse_brand_branch_caption(text: str) -> Tuple[str, str]:
    brand = branch = ""
    if not text:
        return brand, branch
    # نرمال‌سازی
    t = text.replace("ي", "ی").replace("ك", "ک").strip().replace("：", ":")
    parts = [p.strip() for p in t.split("|")]
    for p in parts:
        if p.startswith("برند:"):
            brand = p.split(":", 1)[1].strip()
        elif p.startswith("شعبه:"):
            branch = p.split(":", 1)[1].strip()
    return brand, branch

# ========= Prompt (exactly two lines) =========
def build_prompt_fa(brand: str = "", params: Optional[Dict[str, str]] = None) -> str:
    params = params or {}
    meta = []
    if brand: meta.append(f"برند: {brand}")
    if params.get("OvenTemp"): meta.append(f"دمای مرجع: {params['OvenTemp']}")
    if params.get("BakeTime"): meta.append(f"زمان مرجع: {params['BakeTime']}")
    meta_txt = (" | ".join(meta) + " | ") if meta else ""
    return (
        f"{meta_txt}فقط نتیجه را بده.\n"
        "خروجی دقیقاً دو خط؛ هیچ تیتر/شماره/بولت/ایموجی/خط خالی نگذار.\n"
        "خط۱: یکی از خوب/کم‌پخت/بیش‌پخت یا سوخته + علت کوتاه در پرانتز.\n"
        "خط۲: سه توصیهٔ خیلی کوتاه و اجراپذیر (دما/زمان/پیش‌گرمایش/جایگاه فر/تاپینگ/ضخامت) با «؛» جدا شود."
    )

# ========= OpenAI (vision) =========
async def oai_analyze(image_bytes: bytes, brand: str = "", params: Optional[Dict[str, str]] = None) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt_fa = build_prompt_fa(brand, params)

    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system",
             "content": ("You are a pizza-baking expert. Reply in Persian. "
                         "EXACTLY TWO LINES. No titles, numbering, bullets, emojis, or blank lines. "
                         "Line1: verdict (good | underbaked | overbaked/burnt) with a brief reason in parentheses. "
                         "Line2: three concise actionable tips separated by '؛'.")},
            {"role": "user",
             "content": [
                 {"type": "text", "text": prompt_fa},
                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
             ]}
        ],
        temperature=0.2,
        max_tokens=80,
    )
    return (resp.choices[0].message.content or "نتیجه‌ای دریافت نشد.").strip()

# ========= CSV lookup by Brand only =========
async def sheet_lookup_by_brand(brand_fa: str) -> Optional[Dict[str, str]]:
    """
    فقط با برند از CSV منتشرشده می‌خواند.
    هدرهای قابل‌قبول:
      فارسی:  وندور/برند | دما | زمان
      انگلیسی: Vendor/Brand | OvenTemp/Temp | BakeTime/Time
    """
    csv_url = SHEET_CSV_URL
    brand_fa = (brand_fa or "").strip()
    if not (csv_url and brand_fa):
        print("CSV/brand missing:", bool(csv_url), brand_fa)
        return None

    def norm(s: str) -> str:
        return (s or "").strip().replace("ي","ی").replace("ك","ک")

    wanted = {
        "brand": ["وندور","برند","Vendor","Brand"],
        "temp":  ["دما","OvenTemp","Temp"],
        "time":  ["زمان","BakeTime","Time"],
    }

    try:
        async with httpx.AsyncClient(timeout=20) as cx:
            r = await cx.get(csv_url)
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8"))))
    except Exception as e:
        print("CSV fetch error:", e)
        return None

    def get_val(row: dict, keys: list[str]) -> str:
        for k in keys:
            for kk in row.keys():
                if norm(kk) == norm(k):
                    v = str(row[kk]).strip()
                    if v:
                        return v
        return ""

    b_low = norm(brand_fa).lower()
    for row in rows:
        rv = norm(get_val(row, wanted["brand"])).lower()
        if rv == b_low:
            return {
                "OvenTemp": get_val(row, wanted["temp"]),
                "BakeTime": get_val(row, wanted["time"]),
            }
    return None

# ========= Telegram helpers =========
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
