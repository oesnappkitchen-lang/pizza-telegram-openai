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

app = FastAPI(title="Pizza Bake QA — Photo → Pick Brand → Result")

# ===== DATA: چند ردیف برای هر برند =====
DEFAULT_DATA_TEXT = """
پلنت 8:20 دقیقه 240 درجه
پلنت  9:20 240 درجه
هپی پیتزا 8:20 240 درجه
هپی پیتزا 9:20 240 درجه
ایتزا 9:20 240 درجه
ایتزا 8:20 و 240 درجه
"""

# ===== In-memory storage (ساده برای MVP) =====
ACTIVE_DATA_TEXT: str = DEFAULT_DATA_TEXT
DATA_PARSED: bool = False
BRAND_MAP: Dict[str, List[Dict[str, str]]] = {}   # "پلنت" -> [{"time":"8:20 دقیقه","temp":"240 درجه"}, ...]
SESSION_LAST_IMAGE: Dict[int, bytes] = {}          # chat_id -> image bytes

# ---------- Utils ----------
def norm(s: str) -> str:
    return (s or "").strip().replace("ي","ی").replace("ك","ک")

def parse_brand_branch_caption(text: str) -> Tuple[str, str]:
    brand = branch = ""
    if not text: return brand, branch
    t = norm(text).replace("：", ":")
    for part in [p.strip() for p in t.split("|")]:
        if part.startswith("برند:"):
            brand = part.split(":", 1)[1].strip()
        elif part.startswith("شعبه:"):
            branch = part.split(":", 1)[1].strip()
    return brand, branch

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
        if not line or line.startswith("#"):  # comment/empty
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
    # پاکسازی ردیف‌های خالی
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
    return list(BRAND_MAP.keys())

def lookup_brand_all(brand_fa: str) -> List[Dict[str, str]]:
    ensure_data()
    b = norm(brand_fa)
    if b in BRAND_MAP:
        return BRAND_MAP[b]
    for k, lst in BRAND_MAP.items():
        if b == norm(k) or b in norm(k) or norm(k) in b:
            return lst
    return []

# ---------- Telegram helpers ----------
async def send_text(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text})

async def send_brand_keyboard(chat_id: int, prompt_text: str = "برند را انتخاب کن:"):
    brands = all_brands()
    if not brands:
        await send_text(chat_id, "هیچ برندی در دیتا ثبت نیست. از /setdata استفاده کن.")
        return
    # حداکثر 8 تا برای سادگی (می‌تونی بیشتر هم بگذاری)
    brands = brands[:8]
    keyboard = [[{"text": b, "callback_data": f"pick_brand::{b}"}] for b in brands]
    payload = {
        "chat_id": chat_id,
        "text": prompt_text,
        "reply_markup": {"inline_keyboard": keyboard}
    }
    async with httpx.AsyncClient(timeout=20) as cx:
        await cx.post(f"{TG_API}/sendMessage", json=payload)

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

# ---------- OpenAI analysis (دو خط حرفه‌ای) ----------
async def analyze_bake_only(image_bytes: bytes) -> str:
    """
    EXACTLY two lines:
      1) «وضعیت پخت: خوب/خام/سوخته/نیاز به بهبود»
      2) «توضیح سرآشپز: …» (توصیف بصری دقیق؛ بدون دما/زمان/اعداد/ایموجی)
    """
    img_b64 = base64.b64encode(image_bytes).decode("utf-8")

    system_prompt = (
        "You are a Michelin-level pizza chef and quality auditor who ONLY evaluates bake quality from an image. "
        "Language: Persian (fa-IR). "
        "Return EXACTLY TWO LINES. No bullets, emojis, or extra whitespace. "
        "NEVER suggest temperatures, times, or numeric settings. No recipes. "
        "Line1 must start with: «وضعیت پخت: » followed by one of {خوب, خام, سوخته, نیاز به بهبود}. "
        "Line2 must start with: «תوضیح سرآشپز: » and concisely describe visual reasons (cheese melt, crust color/char, center sogginess, evenness)."
    )
    user_prompt = (
        "از روی تصویر فقط کیفیت پخت را ارزیابی کن. "
        "به آب‌شدن پنیر، یکنواختی برشتگی و رنگ لبه‌ها، خام/سوخته بودن خمیر، خیس بودن مرکز و لکه‌های تیره توجه کن. "
        "هیچ اشاره‌ای به دما/زمان یا اعداد نکن. "
        "خروجی دقیقاً دو خط باشد:\n"
        "وضعیت پخت: ...\n"
        "توضیح سرآشپز: ..."
    )

    resp = OPENAI.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":system_prompt},
            {"role":"user","content":[
                {"type":"text","text":user_prompt},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{img_b64}"}}
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
    url = "https://pizza-telegram-openai.onrender.com/webhook"  # دامنهٔ سرویس خودت
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

    # ---- 1) Callback query (کاربر روی دکمه‌ی برند کلیک کرده) ----
    if "callback_query" in payload:
        cb = payload["callback_query"]
        cb_id = cb.get("id")
        data = (cb.get("data") or "")
        msg  = cb.get("message") or {}
        chat = (msg.get("chat") or {})
        chat_id = chat.get("id")

        if data.startswith("pick_brand::"):
            brand = data.split("::", 1)[1]
            img_bytes = SESSION_LAST_IMAGE.get(chat_id)
            if not img_bytes:
                await answer_callback(cb_id, "ابتدا یک عکس ارسال کن.")
                await send_text(chat_id, "ابتدا یک عکس بفرست تا تحلیل انجام شود.")
                return {"ok": True}

            # تحلیل و ساخت خروجی
            analysis = await analyze_bake_only(img_bytes)
            rows = lookup_brand_all(brand)
            ref_block = ""
            if rows:
                header = f"مرجع «{brand}»"
                lines  = [f"• {(r.get('temp') or '—')}" + (f" | {r.get('time')}" if r.get('time') else "") for r in rows]
                ref_block = f"\n———\n{header}:\n" + "\n".join(lines)

            await answer_callback(cb_id, f"برند انتخاب شد: {brand}")
            await send_text(chat_id, analysis + ref_block)
            # (می‌تونی اگر خواستی بعد ارسال پاک کنی تا عکس بعدی لازم باشه)
            # SESSION_LAST_IMAGE.pop(chat_id, None)
        return {"ok": True}

    # ---- 2) معمولی: message/edited_message ----
    msg  = payload.get("message") or payload.get("edited_message") or {}
    chat = (msg.get("chat") or {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    text   = (msg.get("text") or "").strip()
    photos = msg.get("photo") or []

    # /start
    if text and text.startswith("/start"):
        await send_text(
            chat_id,
            "سلام! عکس پیتزا را بفرست. بعد من لیست برندها را می‌دهم تا انتخاب کنی؛ "
            "سپس تحلیل دوخطی + مرجع همان برند (چند حالت) را می‌فرستم."
        )
        return {"ok": True}

    # /setdata  (اختیاری: تعویض دیتا از چت)
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

    # /brands (اختیاری: نمایش لیست برندها)
    if text and text.lower().startswith("/brands"):
        ensure_data()
        await send_text(chat_id, "برندهای موجود: " + (", ".join(all_brands()) or "—"))
        return {"ok": True}

    # کاربر عکس می‌فرستد → عکس را ذخیره کن و کیبورد برند بده
    if photos:
        try:
            file_id = photos[-1]["file_id"]
            img_bytes = await download_telegram_file(file_id)
            SESSION_LAST_IMAGE[chat_id] = img_bytes
            await send_brand_keyboard(chat_id, "برند را انتخاب کن:")
        except Exception as e:
            print("ERROR processing:", repr(e))
            await send_text(chat_id, "⚠️ خطا در دریافت تصویر. دوباره تلاش کن.")
        return {"ok": True}

    # فقط متن عادی
    if text:
        await send_text(chat_id, "برای شروع، یک عکس بفرست تا کیبورد انتخاب برند نمایش داده شود.")
    return {"ok": True}
