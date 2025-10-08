# server.py
import os, base64, re
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import List, Dict, Tuple, Optional

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI = OpenAI()  # uses OPENAI_API_KEY

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Pizza QA — Brand → Item → Branch")

# ===== مرجع دما/زمان هر برند (همان دیتای قبلی) =====
DEFAULT_DATA_TEXT = """
پلنت 8:20 دقیقه 240 درجه
پلنت  9:20 240 درجه
هپی پیتزا 8:20 240 درجه
هپی پیتزا 9:20 240 درجه
ایتزا 9:20 240 درجه
ایتزا 8:20 و 240 درجه
"""

# ===== آیتم‌های هر برند (Vendor) —ــــ اینجا لیست خودت را بگذار =====
VENDOR_ITEMS: Dict[str, List[str]] = {
    # TODO: نمونه‌ها؛ بر اساس برند خودت تکمیل/تغییر بده
    "پلنت":    ["پیتزا قارچ گوشت", "پیتزا پپرونی", "پیتزا سوسیس قارچ", "سوپریم","دونر مخصوص","پیتزا قارچ و مرغ","پیتزا بیکن","استرامبولی کباب ترکی"
    ,"استرامبولی مرغ و قارچ","استرامبولی مرغ و بادمجون","نان سیر","پیتزا مخصوص","پیتزا ژامبون استیک","پیتزا هالوپینو ژامبون استیک","پیتزا سالامی فلفل"
,"استرامبولی چیزی بلونیا","استرامبولی قارچ و گوشت هالوپینو","پیتزا چیکن پستو","استرامبولی چیکن پستو","سیب زمینی تنوری"],

    "هپی پیتزا": [" پنینی پپرونی", "پنینی رست بیف ", "پنینی سبزیجات", "پنینی مرغ و پستو","پیتزا اسپشیال","پیتزا پپرونی","پیتزا پرومکس","پیتزا پرومکس"
    ,"پیتزا رست بیف","پیتزا سبزیجات","پیتزا سیر و استیک","پیتزا قارچ و اسفناج","پیتزا هپی چیکن","پیتزا هپی میت","سیب زمینی تنوری","نان سیر","پیتزا هالو چیکن"],
   
    "ایتزا":    ["پیتزا بیکن", "پیتزا اسپشیال", "پیتزا استیک", "پیتزا مرغ ایتالیایی","پیتزا پپرونی ","پیتزا رستبیف"],
}

# ===== لیست شعبات —ــــ اینجا شعب خودت را بگذار =====
BRANCH_CHOICES: List[str] = [
    # TODO: نمونه‌ها؛ بر اساس شعب خودت تکمیل/تغییر بده
    "سعادت‌آباد", "سهروردی", "میرداماد", "پونک", "تجریش","فلسطین","نواب","گیشا","ساعی","صادقیه","مجیدیه","وکیل آباد"
]

# ===== In-memory session (ساده) =====
ACTIVE_DATA_TEXT: str = DEFAULT_DATA_TEXT
DATA_PARSED: bool = False
BRAND_MAP: Dict[str, List[Dict[str, str]]] = {}    # "پلنت" -> [{"time":"8:20 دقیقه","temp":"240 درجه"}, ...]
SESSION: Dict[int, Dict[str, object]] = {}          # chat_id -> {"image": bytes, "brand": str, "item": str, "branch": str}

# ---------- Utils ----------
def norm(s: str) -> str:
    return (s or "").strip().replace("ي","ی").replace("ك","ک")

def _extract_time_and_temp(fragment: str) -> Tuple[Optional[str], Optional[str]]:
    frag = norm(fragment)
    m_time = re.search(r"(\d{1,2}\s*[:：]\s*\d{1,2})(?:\s*دقیقه)?", frag)
    m_temp = re.search(r"(\d{2,3})\s*(?:درجه|°)\b", frag)
    time_txt = None
    temp_txt = None
    if m_time:
        tail = "دقیقه" if "دقیقه" in frag else ""
        time_txt = (m_time.group(1).replace(" ", "") + (f" {tail}" if tail else "")).strip()
    if m_temp:
        temp_txt = f"{m_temp.group(1)} درجه"
    return time_txt, temp_txt

def parse_lines_to_brand_map(text: str) -> Dict[str, List[Dict[str, str]]]:
    mapping: Dict[str, List[Dict[str, str]]] = {}
    for raw in (text or "").splitlines():
        line = norm(raw)
        if not line or line.startswith("#"):
            continue
        m_num = re.search(r"\d", line)
        if not m_num:
            continue
        brand_part = line[:m_num.start()].strip()
        rest_part  = line[m_num.start():].strip()
        brand = brand_part
        time_txt, temp_txt = _extract_time_and_temp(rest_part)
        if not (time_txt or temp_txt):
            time_txt, temp_txt = _extract_time_and_temp(line)
        if not brand:
            continue
        bkey = norm(brand)
        mapping.setdefault(bkey, []).append({"time": time_txt or "", "temp": temp_txt or ""})
    for k in list(mapping.keys()):
        mapping[k] = [x for x in mapping[k] if (x.get("time") or x.get("temp"))]
        if not mapping[k]:
            mapping.pop(k, None)
    return mapping

def ensure_data():
    global DATA_PARSED, BRAND_MAP, ACTIVE_DATA_TEXT
    if not DATA_PARSED:
        BRAND_MAP = parse_lines_to_brand_map(ACTIVE_DATA_TEXT)
        DATA_PARSED = True

def all_brands() -> List[str]:
    ensure_data()
    # فقط برندهایی که مرجع دارند یا در VENDOR_ITEMS تعریف شده‌اند
    keys = set(BRAND_MAP.keys()) | set(norm(k) for k in VENDOR_ITEMS.keys())
    # برگرداندن نام اصلی (بدون norm) اگر لازم داشتی می‌تونی مرتب‌سازی کنی
    return list({k for k in VENDOR_ITEMS.keys()} | {k for k in BRAND_MAP.keys()})

def lookup_brand_all(brand_fa: str) -> List[Dict[str, str]]:
    ensure_data()
    b = norm(brand_fa)
    if b in BRAND_MAP:
        return BRAND_MAP[b]
    for k, lst in BRAND_MAP.items():
        if b == norm(k) or b in norm(k) or norm(k) in b:
            return lst
    return []

def get_session(chat_id: int) -> Dict[str, object]:
    if chat_id not in SESSION:
        SESSION[chat_id] = {"image": None, "brand": "", "item": "", "branch": ""}
    return SESSION[chat_id]

def _chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

# ---------- Telegram helpers ----------
async def send_text(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

async def answer_callback(cb_id: str, text: str = ""):
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text})

async def download_telegram_file(file_id: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as cx:
        r = await cx.get(f"{TG_API}/getFile", params={"file_id": file_id})
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        fr = await cx.get(f"{TG_FILE}/{file_path}")
        fr.raise_for_status()
        return fr.content

# ---------- Keyboards ----------
async def send_brand_keyboard(chat_id: int):
    brands = all_brands()
    if not brands:
        await send_text(chat_id, "هیچ برندی در دیتا ثبت نیست. از /setdata استفاده کن.")
        return
    rows = []
    for row in _chunk(brands[:12], 3):
        rows.append([{"text": b, "callback_data": f"brand::{b}"} for b in row])
    payload = {
        "chat_id": chat_id,
        "text": "برند را انتخاب کن:",
        "reply_markup": {"inline_keyboard": rows}
    }
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json=payload)

async def send_item_keyboard(chat_id: int, brand: str):
    items = VENDOR_ITEMS.get(brand, [])
    if not items:
        # اگر برای برند آیتم تعریف نشده بود، حداقل یک گزینه عبوری بده
        items = ["—"]
    rows = []
    for row in _chunk(items[:18], 3):
        rows.append([{"text": it, "callback_data": f"item::{it}"} for it in row])
    rows.append([{"text": "⏭️ بدون انتخاب", "callback_data": "item::<skip>"}])
    payload = {"chat_id": chat_id, "text": f"آیتم «{brand}» را انتخاب کن:", "reply_markup": {"inline_keyboard": rows}}
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json=payload)

async def send_branch_keyboard(chat_id: int):
    rows = []
    for row in _chunk(BRANCH_CHOICES, 3):
        rows.append([{"text": b, "callback_data": f"branch::{b}"} for b in row])
    rows.append([{"text": "⏭️ بدون انتخاب", "callback_data": "branch::<skip>"}])
    payload = {"chat_id": chat_id, "text": "شعبه را انتخاب کن:", "reply_markup": {"inline_keyboard": rows}}
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json=payload)

# ---------- OpenAI (دوخطیِ کیفیت پخت) ----------
async def analyze_bake_only(image_bytes: bytes) -> str:
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")
    system_prompt = (
        "You are a Michelin-level pizza chef and quality auditor who ONLY evaluates bake quality from an image. "
        "Language: Persian (fa-IR). Return EXACTLY TWO LINES. No emojis or numbers or temperature/time suggestions. "
        "Line1: «وضعیت پخت: » + {خوب, خام, سوخته, نیاز به بهبود}. "
        "Line2: «توضیح سرآشپز: » + concise visual reasons (cheese melt, crust color/char, center sogginess, evenness)."
    )
    user_prompt = (
        "از روی تصویر فقط کیفیت پخت را ارزیابی کن. به آب‌شدن پنیر، یکنواختی برشتگی و رنگ لبه‌ها، خام/سوخته بودن خمیر، "
        "خیس بودن مرکز و لکه‌های تیره توجه کن. هیچ اشاره‌ای به دما/زمان نکن. خروجی دقیقاً دو خط باشد."
    )
    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":[
                {"type":"text","text":user_prompt},
                {"type":"image_url","image_url":{"url": f"data:image/jpeg;base64,{img_b64}"}}
            ]}
        ],
        temperature=0.12,
        max_tokens=110,
    )
    content = (resp.choices[0].message.content or "").strip()
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if len(lines) >= 2:
        return f"{lines[0]}\n{lines[1]}"
    return "وضعیت پخت: نیاز به بهبود\nتوضیح سرآشپز: تصویر جزئیات کافی نشان نمی‌دهد؛ لطفاً عکس واضح‌تری بفرست."

# ---------- Routes ----------
@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/set_webhook")
async def set_webhook():
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "missing TELEGRAM_TOKEN"}
    url = "https://pizza-telegram-openai.onrender.com/webhook"  # دامنه سرویس خودت
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

    payload = await req.json()

    # ---- Callback Buttons ----
    if "callback_query" in payload:
        cb = payload["callback_query"]
        cb_id = cb.get("id")
        data = (cb.get("data") or "")
        msg  = cb.get("message") or {}
        chat = (msg.get("chat") or {})
        chat_id = chat.get("id")
        ses = get_session(chat_id)

        # برند انتخاب شد → برو به انتخاب آیتم (بر اساس همان برند)
        if data.startswith("brand::"):
            brand = data.split("::", 1)[1]
            ses["brand"] = brand
            ses["item"] = ""
            ses["branch"] = ""
            await answer_callback(cb_id, f"برند: {brand}")
            await send_item_keyboard(chat_id, brand)
            return {"ok": True}

        # آیتم انتخاب شد → برو به انتخاب شعبه
        if data.startswith("item::"):
            val = data.split("::", 1)[1]
            ses["item"] = "" if val == "<skip>" else val
            await answer_callback(cb_id, "آیتم ثبت شد.")
            await send_branch_keyboard(chat_id)
            return {"ok": True}

        # شعبه انتخاب شد → تحلیل + مرجع
        if data.startswith("branch::"):
            val = data.split("::", 1)[1]
            ses["branch"] = "" if val == "<skip>" else val

            img_bytes = ses.get("image")
            if not img_bytes:
                await answer_callback(cb_id, "ابتدا یک عکس ارسال کن.")
                await send_text(chat_id, "ابتدا یک عکس بفرست.")
                return {"ok": True}

            analysis = await analyze_bake_only(img_bytes)
            brand  = ses.get("brand") or ""
            item   = ses.get("item") or ""
            branch = ses.get("branch") or ""

            rows = lookup_brand_all(brand)
            ref_block = ""
            if brand and rows:
                bits = [f"مرجع «{brand}»"]
                if item:   bits.append(f"آیتم: {item}")
                if branch: bits.append(f"شعبه: {branch}")
                header = " | ".join(bits)
                lines  = [f"• {(r.get('temp') or '—')}" + (f" | {r.get('time')}" if r.get('time') else "") for r in rows]
                ref_block = f"\n———\n{header}:\n" + "\n".join(lines)

            await answer_callback(cb_id, "ثبت شد ✅")
            await send_text(chat_id, analysis + ref_block)

            # در صورت تمایل جلسه را پاک کن تا برای عکس بعدی از ابتدا شروع شود:
            # SESSION.pop(chat_id, None)

            return {"ok": True}

        return {"ok": True}

    # ---- پیام معمولی (عکس/متن) ----
    msg  = payload.get("message") or payload.get("edited_message") or {}
    chat = (msg.get("chat") or {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    text   = (msg.get("text") or "").strip()
    photos = msg.get("photo") or []

    if text and text.startswith("/start"):
        await send_text(
            chat_id,
            "سلام! عکس پیتزا را بفرست. سپس با دکمه‌ها: برند → آیتم → شعبه را انتخاب می‌کنی و من تحلیل دوخطی + مرجع دما/زمان برند را می‌فرستم.\n"
            "برای تغییر مرجع برندها: /setdata"
        )
        return {"ok": True}

    if text and text.lower().startswith("/setdata"):
        new_text = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if not new_text:
            await send_text(chat_id, "فرمت:\n/setdata\nپلنت 8:20 دقیقه 240 درجه\nهپی پیتزا 9:20 240 درجه")
            return {"ok": True}
        global ACTIVE_DATA_TEXT, DATA_PARSED
        ACTIVE_DATA_TEXT = new_text
        DATA_PARSED = False
        ensure_data()
        await send_text(chat_id, f"✅ داده ثبت شد. برندها: {', '.join(all_brands()) or '—'}")
        return {"ok": True}

    if text and text.lower().startswith("/brands"):
        ensure_data()
        await send_text(chat_id, "برندهای موجود: " + (", ".join(all_brands()) or "—"))
        return {"ok": True}

    if photos:
        try:
            file_id = photos[-1]["file_id"]
            img_bytes = await download_telegram_file(file_id)
            ses = get_session(chat_id)
            ses["image"] = img_bytes
            ses["brand"] = ""
            ses["item"] = ""
            ses["branch"] = ""
            await send_brand_keyboard(chat_id)
        except Exception as e:
            print("ERROR processing:", repr(e))
            await send_text(chat_id, "⚠️ خطا در دریافت تصویر. دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "برای شروع، یک عکس بفرست تا مراحل دکمه‌ای اجرا شود.")
    return {"ok": True}
