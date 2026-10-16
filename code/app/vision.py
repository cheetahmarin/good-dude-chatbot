"""อ่านรูปด้วยโมเดล vision ผ่าน OpenRouter แล้วสร้างคำตอบตามชนิดของรูป

แนวทางจาก chacharin/chatbot-with-image-bill: ให้โมเดลดึงข้อมูลเป็น JSON
ส่วนการตัดสินใจ (นับวัน, ตอบเรื่องสลิป) ทำในโค้ด ไม่ให้โมเดลคำนวณหรือรับรองเอง
"""

import base64
import json
import logging
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .errors import AppError, map_http_error

log = logging.getLogger("gooddude.vision")

TZ = ZoneInfo("Asia/Bangkok")
RETURN_WINDOW_DAYS = 7

# magic bytes -> mime (ตรวจจากเนื้อไฟล์ ไม่เชื่อนามสกุลหรือ content-type ที่ลูกค้าส่งมา)
_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
]

RETAKE_TEXT = (
    "น้องดู๊ดอ่านรูปนี้ไม่ชัดเจนค่ะ รบกวนถ่ายรูปใหม่ให้เห็นข้อความชัด ๆ "
    "(แสงพอ ไม่เบลอ เห็นครบทั้งใบ) แล้วส่งมาอีกครั้งนะคะ"
)
OFF_TOPIC_TEXT = (
    "รูปนี้ดูเหมือนไม่เกี่ยวกับสินค้าหรือการสั่งซื้อของร้านค่ะ "
    "น้องดู๊ดรับเฉพาะสลิปโอนเงิน ฉลากหรือป้ายราคาสินค้า ใบเสร็จ และรูปสินค้าที่มีตำหนิ "
    "ถ้าสอบถามเรื่องอื่น พิมพ์ถามได้เลยนะคะ"
)


def detect_mime(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(data: bytes, settings: Settings) -> str:
    if not data:
        raise AppError(400, "image_empty", "ไม่พบไฟล์รูปภาพ กรุณาเลือกรูปแล้วลองใหม่ค่ะ")
    if len(data) > settings.max_image_mb * 1024 * 1024:
        raise AppError(413, "image_too_large", f"ไฟล์รูปใหญ่เกิน {settings.max_image_mb:g} MB กรุณาย่อรูปแล้วลองใหม่ค่ะ")
    mime = detect_mime(data)
    if mime is None:
        raise AppError(415, "image_type", "รองรับเฉพาะไฟล์รูป JPG, PNG หรือ WEBP ค่ะ")
    return mime


def parse_json(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text).strip()
    braces = re.search(r"\{.*\}", text, re.DOTALL)
    for candidate in (text, braces.group(0) if braces else None):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


async def extract(
    client: httpx.AsyncClient, settings: Settings, data: bytes, mime: str
) -> dict[str, Any]:
    prompt_path = Path(settings.vision_prompt_path)
    if not prompt_path.exists():
        raise AppError(500, "vision_prompt_missing", "ไม่พบไฟล์ prompt อ่านรูป กรุณาแจ้งผู้ดูแลระบบค่ะ")
    data_uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    payload = {
        "model": settings.vision_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt_path.read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "อ่านรูปนี้ แล้วตอบเป็น JSON ตามรูปแบบที่กำหนด"},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ],
    }
    async def call(body: dict) -> str:
        resp = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json=body,
            timeout=settings.request_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    try:
        try:
            raw = await call({**payload, "response_format": {"type": "json_object"}})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 422):
                raise
            raw = await call(payload)  # ผู้ให้บริการบางรายไม่รองรับ response_format
        parsed = parse_json(raw)
        if parsed is None:  # โมเดลบางครั้งตอบไม่เป็น JSON ลองใหม่ 1 ครั้งก่อนยอมแพ้
            log.warning("vision returned non-JSON (retry once): %r", raw[:200])
            raw = await call(payload)
            parsed = parse_json(raw)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        log.error("vision failed: %r", exc)
        raise map_http_error(exc, "vision") from exc

    if parsed is None:
        log.warning("vision returned non-JSON: %r", raw[:200])
        return {"image_type": "other", "readable": False, "fields": {}, "text_in_image": ""}
    return parsed


# ---------------------------------------------------------------------------
# ทำความสะอาดผลจากโมเดล (ข้อมูลจากรูปคือ input ที่ไม่น่าไว้ใจ: ชนิด/รูปแบบอาจผิด หรือมีคำสั่งแอบแฝง)
# ---------------------------------------------------------------------------

IMAGE_TYPES = {"slip", "product_label", "price_tag", "receipt", "defect", "other"}
_STR_FIELDS = ("date", "time", "product_name", "size", "note")
_MAX_FIELD = 120
_MAX_ITEMS = 20


def _clean_str(v: Any, limit: int = _MAX_FIELD) -> str | None:
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    t = " ".join(str(v).split())
    return t[:limit] or None


def _clean_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(str(v).replace(",", "")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and 0 < x < 10_000_000 else None


def sanitize(info: Any) -> dict[str, Any]:
    """บังคับรูปแบบผลอ่านรูป: ชนิดต้องอยู่ในรายการ ช่องทุกช่องมีชนิดและความยาวที่แน่นอน ไม่โยน exception"""
    if not isinstance(info, dict):
        info = {}
    raw_fields = info.get("fields") if isinstance(info.get("fields"), dict) else {}
    kind = str(info.get("image_type", "other")).strip().lower()
    fields: dict[str, Any] = {k: _clean_str(raw_fields.get(k)) for k in _STR_FIELDS}
    fields["amount"] = _clean_number(raw_fields.get("amount"))
    fields["price"] = _clean_number(raw_fields.get("price"))
    items = raw_fields.get("items")
    fields["items"] = [t for t in (_clean_str(i, 80) for i in items[:_MAX_ITEMS]) if t] if isinstance(items, list) else []
    return {
        "image_type": kind if kind in IMAGE_TYPES else "other",
        "readable": info.get("readable") in (True, "true", "True"),
        "fields": fields,
        "text_in_image": _clean_str(info.get("text_in_image"), 300) or "",
    }


def embedded_text(info: dict[str, Any]) -> str:
    """รวมข้อความทุกช่องที่มาจากรูป เพื่อตรวจ blocklist (กัน prompt injection ที่ซ่อนในรูป)"""
    f = info.get("fields") or {}
    parts = [info.get("text_in_image")] + [f.get(k) for k in _STR_FIELDS] + list(f.get("items") or [])
    return " ".join(str(p) for p in parts if p)


# ---------------------------------------------------------------------------
# ตรรกะหลังอ่านรูป (ทำในโค้ด ไม่ใช้โมเดลตัดสิน)
# ---------------------------------------------------------------------------

_TH_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_TH_MONTHS = {
    "มกราคม": 1, "มค": 1, "กุมภาพันธ์": 2, "กพ": 2, "มีนาคม": 3, "มีค": 3, "เมษายน": 4, "เมย": 4,
    "พฤษภาคม": 5, "พค": 5, "มิถุนายน": 6, "มิย": 6, "กรกฎาคม": 7, "กค": 7, "สิงหาคม": 8, "สค": 8,
    "กันยายน": 9, "กย": 9, "ตุลาคม": 10, "ตค": 10, "พฤศจิกายน": 11, "พย": 11, "ธันวาคม": 12, "ธค": 12,
}
_TH_MONTH_ABBR = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


def normalize_date(value: Any) -> date | None:
    """แปลงวันที่ที่อ่านได้เป็น date รองรับ พ.ศ./ค.ศ., เลขไทย, YYYY-MM-DD, DD/MM/YYYY, "5 ต.ค. 69", "5 ตุลาคม 2569" """
    if not value:
        return None
    s = str(value).strip().translate(_TH_DIGITS)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.search(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.search(r"(\d{1,2})\s*([\u0E00-\u0E7F.]{2,14})\s*(\d{2,4})", s)
            mo = _TH_MONTHS.get(m[2].replace(".", "").strip()) if m else None
            if not m or not mo:
                return None
            d, y = int(m[1]), int(m[3])
    if y < 100:
        y += 2500 if y > 50 else 2000
    if y > 2400:  # พ.ศ.
        y -= 543
    if not 1900 <= y <= 2100:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def th_date(d: date) -> str:
    """แสดงวันที่แบบไทยจากค่าที่แปลงสำเร็จแล้วเท่านั้น เช่น 5 ต.ค. 2569"""
    return f"{d.day} {_TH_MONTH_ABBR[d.month]} {d.year + 543}"


def days_since(d: date, today: date | None = None) -> int:
    return ((today or datetime.now(TZ).date()) - d).days


def _baht(x: Any) -> str | None:
    n = _clean_number(x)
    return None if n is None else f"{n:,.2f}".rstrip("0").rstrip(".")


_DEFECT_WORDS = re.compile(r"ขาด|ฉีก|บุบ|รั่ว|แตก|เสียหาย|ตำหนิ|หมดอายุ|ขึ้นรา|เปื้อน|damag|torn|broken|expired", re.IGNORECASE)


def not_found_text(info: dict[str, Any]) -> str:
    """คำตอบตายตัวเมื่อสินค้า/ป้ายราคาในรูปไม่อยู่ในคลังความรู้ของร้าน (อ่านจากช่องที่ผ่าน sanitize แล้วเท่านั้น)"""
    f = info.get("fields") or {}
    name = f.get("product_name")
    size = f.get("size")
    seen = " ".join(x for x in (name, size) if x)
    price = _baht(f.get("price"))
    head = f"น้องดู๊ดอ่านรูปได้ว่าเป็น {seen}" if seen else "น้องดู๊ดอ่านรูปนี้ได้"
    if price:
        head += f" ราคาบนป้าย {price} บาท"
    return (
        f"{head} แต่ไม่พบสินค้านี้ในรายการสินค้าของร้านค่ะ "
        "ถ้าสนใจสินค้าใกล้เคียง บอกชนิดสัตว์และขนาดที่ต้องการได้เลยนะคะ "
        "น้องดู๊ดจะช่วยดูจากรายการของร้านให้ หรือสอบถามพนักงานทาง LINE @gooddude ได้ค่ะ"
    )


def plan(info: dict[str, Any], caption: str = "", today: date | None = None) -> tuple[str, str]:
    """คืน ("reply", ข้อความตอบสำเร็จรูป) หรือ ("rag", คำถามที่จะส่งเข้า LightRAG)"""
    f = info.get("fields") or {}
    kind = str(info.get("image_type", "other"))
    if not info.get("readable", False):
        return "reply", RETAKE_TEXT
    note = f" ข้อความเพิ่มเติมจากลูกค้า: {caption.strip()}" if caption.strip() else ""
    # โมเดลเล็กมักจัดถุงที่ขาดแต่เห็นฉลากเป็น product_label: ถ้าลูกค้าบอกเองว่าเสียหาย ให้ถือเป็น defect
    if kind == "product_label" and _DEFECT_WORDS.search(caption or ""):
        kind = "defect"

    if kind == "slip":
        amount = _baht(f.get("amount"))
        if amount is None:
            return "reply", RETAKE_TEXT
        lines = ["น้องดู๊ดอ่านสลิปได้ดังนี้ค่ะ", f"- ยอดโอน: {amount} บาท"]
        slip_date = normalize_date(f.get("date"))
        if slip_date:
            lines.append(f"- วันที่: {th_date(slip_date)}")
        if re.fullmatch(r"\d{1,2}[:.]\d{2}", str(f.get("time") or "").strip()):
            lines.append(f"- เวลา: {str(f['time']).strip().replace('.', ':')}")
        lines.append(
            "ร้านจะตรวจสอบยอดเข้าบัญชีและยืนยันอีกครั้งนะคะ "
            "น้องดู๊ดยืนยันการชำระเงินหรือการสั่งซื้อเองไม่ได้ค่ะ"
        )
        return "reply", "\n".join(lines)

    if kind in ("product_label", "price_tag"):
        name = f.get("product_name") or "(อ่านชื่อไม่ได้)"
        size = f.get("size") or "(ไม่ระบุ)"
        price = _baht(f.get("price"))
        seen = f"ชื่อ: {name} / ขนาด: {size}" + (f" / ราคาบนป้าย: {price} บาท" if price else "")
        return "rag", (
            f"ลูกค้าส่งรูป{'ป้ายราคา' if kind == 'price_tag' else 'ฉลากสินค้า'} อ่านได้ว่า {seen}. "
            "ร้านมีสินค้านี้ในรายการหรือไม่ ราคาเท่าไหร่ "
            "ถ้าไม่มีในรายการให้บอกว่าไม่พบ และแนะนำสินค้าที่ใกล้เคียงจากรายการร้านเท่านั้น" + note
        )

    if kind == "receipt":
        d = normalize_date(f.get("date"))
        items = ", ".join(str(i) for i in (f.get("items") or [])[:_MAX_ITEMS]) or "(อ่านรายการไม่ได้)"
        if d is None or days_since(d, today) < 0:
            return "rag", (
                f"ลูกค้าส่งรูปใบเสร็จเพื่อขอคืนสินค้า แต่อ่านวันที่ซื้อไม่ได้ชัดเจน รายการ: {items}. "
                "ตอบตามนโยบายการคืนสินค้า บอกให้ลูกค้าถ่ายรูปใบเสร็จให้เห็นวันที่ชัดเจน "
                "และบอกว่าพนักงานเป็นผู้อนุมัติ" + note
            )
        n = days_since(d, today)
        verdict = "อยู่ภายในกำหนด" if n <= RETURN_WINDOW_DAYS else "เกินกำหนด"
        return "rag", (
            f"ลูกค้าส่งรูปใบเสร็จเพื่อขอคืนสินค้า วันที่ซื้อ {d.isoformat()} "
            f"ผ่านมาแล้ว {n} วัน ({verdict} {RETURN_WINDOW_DAYS} วัน) รายการ: {items}. "
            "ตอบตามนโยบายการคืนสินค้าของร้านตามประเภทสินค้า "
            "และบอกว่าการอนุมัติเป็นหน้าที่ของพนักงาน บอทอนุมัติเองไม่ได้" + note
        )

    if kind == "defect":
        what = f.get("note") or "สินค้าที่ลูกค้าแจ้งว่ามีตำหนิ"
        return "rag", (
            f"ลูกค้าส่งรูปสินค้ามีตำหนิ: {what}. ตอบตามนโยบายสินค้ามีตำหนิ "
            "(ต้องแจ้งภายในกี่วัน ต้องใช้อะไร) และแจ้งให้ติดต่อพนักงานเพื่อตรวจสอบ "
            "ห้ามอนุมัติคืนเงินเอง" + note
        )

    return "reply", OFF_TOPIC_TEXT
