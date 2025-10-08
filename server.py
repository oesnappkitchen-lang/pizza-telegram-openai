# server.py
import os, base64, re
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from typing import List, Dict, Tuple, Optional

# ===== ENV =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI = OpenAI()  # uses OPENAI_API_KEY from env

TG_API  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_FILE = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}"

app = FastAPI(title="Pizza Bake QA — Multi-Ref per Brand (No Sheet)")

# ===== YOUR DATA (چند ردیف برای هر برند) =====
# همینجا هر خط یک «برند + زمان + دما» است؛ فرم‌های زیر را قبول می‌کند:
# - پلنت 8:20 دقیقه 240 درجه
# - پلنت  9:20 240 درجه
# - هپی پیتزا 8:20 و 240 درجه
# - (Time اول/Temp دوم مهم نیست؛ پارسر هر دو را پیدا می‌کند)
DEFAULT_DATA_TEXT = """
پلنت 8:20 دقیقه 240 درجه
پلنت  9:20 240 درجه
هپی پیتزا 8:20 240 درجه
هپی پیتزا 9:20 240 درجه
ایتزا 9:20 240 درجه
ایتزا 8:20 و 240 درجه
"""

# ===== Internal store =====
ACTIVE_DATA_TEXT: str = DEFAULT_DATA_TEXT
DATA_PARSED: bool = False
BRAND_MAP: Dict[str, List[Dict[str, str]]] = {}  # "پلنت" -> [{"time":"8:20 دقیقه","temp":"240 درجه"}, {...}]

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
    """
    از یک تکه متن فارسی، زمان (مثل 8:20 یا 9:20 دقیقه) و دما (مثل 240 درجه / 310 درجه) را بیرون می‌کشد.
    """
    frag = norm(fragment)
    # زمان: الگوهای 8:20 ، 9:20 ، 8:20 دقیقه ، 9:20دقیقه
    m_time = re.search(r"(\d{1,2}\s*[:：]\s*\d{1,2})(?:\s*دقیقه)?", frag)
    # دما: 240 درجه / 310 درجه / 240° (درجه اختیاری)
    m_temp = re.search(r"(\d{2,3})\s*(?:درجه|°)\b", frag)
    time_txt = None
    temp_txt = None
    if m_time:
        # اگر «دقیقه» در متن بود اضافه‌اش می‌کنیم تا طبیعی‌تر شود
        tail = "دقیقه" if "دقیقه" in frag[m_time.end(): m_time.end()+7] or "دقیقه" in frag[:m_time.start()] else ""
        time_txt = (m_time.group(1).replace(" ", "") + (f" {tail}" if tail else "")).strip()
    if m_temp:
        temp_txt = f"{m_temp.group(1)} درجه"
    return time_txt, temp_txt

def parse_lines_to_brand_map(text: str) -> Dict[str, List[Dict[str, str]]]:
    """
    هر خط: «برند ... زمان ... دما ...» یا برعکس
    خروجی: نقشهٔ برند → لیستی از {time, temp}
    """
    mapping: Dict[str, List[Dict[str, str]]] = {}
    for raw in (text or "").splitlines():
        line = norm(raw)
        if not line or line.startswith("#"):  # comment/empty
            continue
        # برند را کلمهٔ اول (یا دو کلمهٔ اول) در نظر می‌گیریم تا قبل از اولین الگوی عددی/دو نقطه‌ای
        # راه ساده: تا قبل از اولین عدد/رقم را برند بگیریم
        m_num = re.search(r"\d", line)
        if not m_num:
            # خطی بدون عدد: رد
            continue
        brand_part = line[:m_num.start()].strip()
        rest_part  = line[m_num.start():].strip()
        brand = brand_part
        # تمرکز روی استخراج زمان/دما از rest_part
        time_txt, temp_txt = _extract_time_and_temp(rest_part)
        if not (time_txt or temp_txt):
            # یک تلاش دیگر: کل خط
            time_txt, temp_txt = _extract_time_and_temp(line)
        if not brand:
            continue
        bkey = norm(brand)
        if bkey not in mapping:
            mapping[bkey] = []
        mapping[bkey].append({
            "time": time_txt or "",
            "temp": temp_txt or ""
        })
    return mapping

def ensure_data():
    global DATA_PARSED, BRAND_MAP, ACTIVE_DATA_TEXT
    if not DATA_PARSED:
        BRAND_MAP = parse_lines_to_brand_map(ACTIVE_DATA_TEXT)
        # پاکسازی: حذف ورودی‌های خالی
        for k in list(BRAND_MAP.keys()):
            BRAND_MAP[k] = [x for x in BRAND_MAP[k] if (x.get("time") or x.get("temp"))]
            if not BRAND_MAP[k]:
                BRAND_MAP.pop(k, None)
        DATA_PARSED = True

def lookup_brand_all(brand_fa: str) -> List[Dict[str, str]]:
    ensure_data()
    b = norm(brand_fa)
    # exact
    if b in BRAND_MAP:
        return BRAND_MAP[b]
    # fuzzy: اگر فاصله/نقطه‌گذاری متفاوت باشد
    for k, lst in BRAND_MAP.items():
        if b == norm(k) or b in norm(k) or norm(k) in b:
            return lst
    return []

# ===== Routes =====
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
            "سلام! عکس پیتزا را بفرست. من فقط کیفیت پخت را از روی تصویر، دقیق و حرفه‌ای ارزیابی می‌کنم (بدون دما/زمان پیشنهادی).\n"
            "برای اضافه‌شدن مرجع برند زیر تحلیل، در کپشن بنویس: «برند: ... | شعبه: ...»."
        )
        return {"ok": True}

    # امکانِ تعویض/افزودن داده‌ها از داخل تلگرام (اختیاری)
    if text and text.lower().startswith("/setdata"):
        new_text = text.split("\n", 1)[1].strip() if "\n" in text else ""
        if not new_text:
            await send_text(chat_id, "فرمت:\n/setdata\nپلنت 8:20 دقیقه 240 درجه\nهپی پیتزا 9:20 240 درجه")
            return {"ok": True}
        global ACTIVE_DATA_TEXT, DATA_PARSED
        ACTIVE_DATA_TEXT = new_text
        DATA_PARSED = False
        ensure_data()
        await send_text(chat_id, f"✅ داده ثبت شد. برندها: {', '.join(BRAND_MAP.keys()) or '—'}")
        return {"ok": True}

    # مرجع یک برند را بدون عکس بگیر
    if text and text.startswith("/ref"):
        brand = text.split(" ", 1)[1].strip() if " " in text else ""
        rows = lookup_brand_all(brand)
        if rows:
            bullets = "\n".join([f"• {(r.get('temp') or '—')}" + (f" | {r.get('time')}" if r.get('time') else "") for r in rows])
            await send_text(chat_id, f"مرجع «{brand}»:\n{bullets}")
        else:
            await send_text(chat_id, "چیزی برای این برند پیدا نشد.")
        return {"ok": True}

    if photos:
        file_id = photos[-1]["file_id"]
        caption = (msg.get("caption") or "").strip()
        brand, branch = parse_brand_branch_caption(caption)

        try:
            # 1) دانلود تصویر
            img_bytes = await download_telegram_file(file_id)
            # 2) تحلیل حرفه‌ای (دو خط)
            analysis = await analyze_bake_only(image_bytes=img_bytes)

            # 3) خط مرجع برند (همهٔ ردیف‌ها)
            ref_block = ""
            if brand:
                rows = lookup_brand_all(brand)
                if rows:
                    header = f"مرجع «{brand}»" + (f" | شعبه: {branch}" if branch else "")
                    lines  = [f"• {(r.get('temp') or '—')}" + (f" | {r.get('time')}" if r.get('time') else "") for r in rows]
                    ref_block = f"\n———\n{header}:\n" + "\n".join(lines)

            await send_text(chat_id, analysis + ref_block)

        except Exception as e:
            print("ERROR processing:", repr(e))
            await send_text(chat_id, "⚠️ خطا در پردازش تصویر. دوباره تلاش کن.")
        return {"ok": True}

    if text:
        await send_text(chat_id, "برای ارزیابی، یک عکس پیتزا بفرست. برای مرجع: /ref <نام برند>")
    return {"ok": True}

# ---------- Analysis (PRO prompt, exactly two lines) ----------
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
        "Return EXACTLY TWO LINES. No bullets, no emojis, no extra whitespace. "
        "Do NOT suggest temperatures, times, or numeric settings. No recipes."
        "Line1 must start with: «وضعیت پخت: » followed by one of {خوب, خام, سوخته, نیاز به بهبود}. "
        "Line2 must start with: «توضیح سرآشپز: » and describe the visual reasons concisely (cheese melt, crust color/char, center sogginess, evenness)."
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
    # fallback
    return "وضعیت پخت: نیاز به بهبود\nتوضیح سرآشپز: تصویر جزئیات کافی نشان نمی‌دهد؛ لطفاً عکس واضح‌تری بفرست."

# ---------- Telegram helpers ----------
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
