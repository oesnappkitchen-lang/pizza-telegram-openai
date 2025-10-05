# server.py
import os, base64, json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import Optional, Dict, Tuple

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI = OpenAI()  # uses OPENAI_API_KEY

# Google Sheets (read-only)
SHEET_ID   = os.getenv("SHEET_ID", "1oGqOX01oweZ3nnfogK1VPmTBeqQcMjL0lhpvFVeM_ag").strip()          # فقط ID شیت (نه لینک کامل)
SHEET_TAB  = os.getenv("SHEET_TAB", "pizza").strip()   # اسم تب
GCP_SA_JSON_B64 = os.getenv("GCP_SA_JSON_BASE64", "").strip()  # JSON سرویس‌اکانت Base64

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Pizza AI Telegram (OpenAI)")

# ===== Health =====
@app.get("/health")
async def health():
    return {"ok": True}

# ===== Webhook =====
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
        brand, branch = parse_brand_branch_caption(caption)  # ← فقط برند و شعبه

        try:
            # 1) دانلود عکس
            img_bytes = await download_telegram_file(file_id)

            # 2) خواندن مرجع از شیت (اولویت: برند+شعبه؛ اگر ستون شعبه در شیت نبود/پیدا نشد → فقط برند)
            ref = await sheet_lookup_brand_branch(brand, branch)

            # 3) پارام‌های مرجع برای پرامپت
            params: Dict[str, str] = {}
            if ref:
                if ref.get("OvenTemp"): params["OvenTemp"] = ref["OvenTemp"]
                if ref.get("BakeTime"): params["BakeTime"] = ref["BakeTime"]

            # 4) تحلیل هوش مصنوعی (خروجی دقیقاً دو خط)
            result = await oai_analyze(img_bytes, brand=brand, params=params)

            # 5) ساخت خط مرجع (فقط دما و زمان؛ اگر در شیت موجود باشد)
            ref_line = ""
            if ref:
                bits = []
                if ref.get("OvenTemp"): bits.append(f"دمای مرجع: {ref['OvenTemp']}")
                if ref.get("BakeTime"): bits.append(f"زمان مرجع: {ref['BakeTime']}")
                if bits:
                    ref_line = "———\n" + " | ".join(bits)

            # 6) ارسال پاسخ ترکیبی
            await send_text(chat_id, (result + ("\n" + ref_line if ref_line else "")))

        except Exception as e:
            print("ERROR:", e)
            await send_text(chat_id, "⚠️ خطا در پردازش. لطفاً دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "برای بهترین نتیجه: عکس + کپشن فارسی شامل «برند» و «شعبه» بفرست. مثال /start را ببین.")
    return {"ok": True}

# ===== Parse Persian caption: "برند: X | شعبه: Y" =====
def parse_brand_branch_caption(text: str) -> Tuple[str, str]:
    brand = branch = ""
    if not text:
        return brand, branch
    parts = [p.strip() for p in text.split("|")]
    for p in parts:
        t = p.replace("ي","ی").replace("ك","ک").strip()  # نرمال‌سازی
        if t.startswith("برند:"):
            brand = t.split(":", 1)[1].strip()
        if t.startswith("شعبه:"):
            branch = t.split(":", 1)[1].strip()
    return brand, branch

# ===== Prompt (خروجی دقیقاً دو خط) =====
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

# ===== OpenAI (تحلیل تصویر) =====
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

# ===== Google Sheets lookup (Brand [+ Branch if available]) =====
async def sheet_lookup_brand_branch(brand_fa: str, branch_fa: str) -> Optional[Dict[str, str]]:
    """
    از شیت می‌خواند:
      - اگر ستون «شعبه/Branch» وجود داشت: دقیقاً برند+شعبه را پیدا می‌کند.
      - اگر نبود یا پیدا نشد: اولین ردیف مطابق با برند.
    ستون‌های پذیرفته‌شده:
      فارسی:  وندور/برند | شعبه | دما | زمان
      انگلیسی: Vendor/Brand | Branch | OvenTemp/Temp | BakeTime/Time
    """
    if not (SHEET_ID and GCP_SA_JSON_B64 and brand_fa):
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    sa_info = json.loads(base64.b64decode(GCP_SA_JSON_B64).decode("utf-8"))
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    rows = ws.get_all_records()  # list[dict]

    def get_val(r: dict, *keys):
        for k in keys:
            if k in r and str(r[k]).strip():
                return str(r[k]).strip()
        return ""

    b_low = brand_fa.strip().lower()
    br_low = (branch_fa or "").strip().lower()

    # تلاش 1: برند + شعبه (اگر ستون شعبه وجود داشته باشد)
    found_branch_column = any("شعبه" in r or "Branch" in r for r in rows[:1] or [{}])
    if found_branch_column and br_low:
        for r in rows:
            rv = (get_val(r, "وندور", "برند", "Vendor", "Brand")).lower()
            rb = (get_val(r, "شعبه", "Branch")).lower()
            if rv == b_low and rb == br_low:
                return {
                    "OvenTemp": get_val(r, "دما", "OvenTemp", "Temp"),
                    "BakeTime": get_val(r, "زمان", "BakeTime", "Time"),
                }

    # تلاش 2: فقط برند
    for r in rows:
        rv = (get_val(r, "وندور", "برند", "Vendor", "Brand")).lower()
        if rv == b_low:
            return {
                "OvenTemp": get_val(r, "دما", "OvenTemp", "Temp"),
                "BakeTime": get_val(r, "زمان", "BakeTime", "Time"),
            }
    return None

# ===== Telegram helpers =====
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
