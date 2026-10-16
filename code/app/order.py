"""สรุปคำสั่งซื้อ: โมเดลอ่านบทสนทนาแล้วบอกเพียง "รหัสสินค้า + จำนวน" ส่วนราคา ค่าส่ง โปรโมชัน ยอดรวม
และข้อความตอบ คำนวณ/สร้างในโค้ดทั้งหมดจากรายการสินค้าของร้าน (app/catalog.py)

หลักความปลอดภัย (ตรงกับเงื่อนไขร้าน "ห้ามแต่งข้อมูลเอง" และ "ห้ามให้ส่วนลดเกินจริง")
- ราคาไม่เคยมาจากโมเดลหรือจากข้อความของลูกค้า/ประวัติแชท (ประวัติปลอมได้) ใช้ราคาในรายการร้านเท่านั้น
- รหัสสินค้าที่โมเดลคืนมาต้องมีอยู่จริงในรายการ จำนวนต้องเป็นจำนวนเต็ม 1-MAX_QTY
- ข้อความตอบสร้างจากแม่แบบตายตัว ไม่ echo ข้อความลูกค้า ยกเว้นชื่อสินค้าที่ไม่พบ (ตัดความยาว ผ่าน blocklist)
- ไม่ยืนยันว่าชำระเงินสำเร็จ ให้พนักงานตรวจสลิปเสมอ
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .blocklist import Blocklist
from .catalog import PRODUCTS
from .config import Settings
from .vision import parse_json

log = logging.getLogger("gooddude.order")

# ---------------------------------------------------------------------------
# กติกาของร้าน (ตรงกับ knowledge/01_shop_profile.md และ 04_policies.md)
# ---------------------------------------------------------------------------
FREE_SHIPPING_MIN = 500  # ซื้อครบ 500 บาทขึ้นไปส่งฟรี
SHIPPING_FEE = 40  # ต่ำกว่า 500 บาท ค่าส่ง 40 บาท
DELIVERY_RADIUS_KM = 5
MAX_QTY = 99
MAX_LINES = 15

# บัญชีสมมติสำหรับสาธิตเท่านั้น (ตรงกับข้อมูลใน knowledge/01_shop_profile.md)
PAYMENT_METHOD = "โอนผ่านพร้อมเพย์"
PAYMENT_NAME = "ร้านกู๊ดดู๊ด"
PAYMENT_NUMBER = "000-000-0000"

# โปรโมชันประจำ (04_policies.md): (รหัส, จำนวนต่อชุด, ราคาปกติทั้งชุด, ราคาโปรทั้งชุด, ชื่อโปร)
PROMOS = [
    ("GD-H002", 3, 417, 390, "ทรายแมวเบนทอไนต์ ซื้อ 3 ถุง"),
    ("GD-C004", 12, 216, 200, "อาหารเปียกแมวซอง ยกโหล 12 ซอง"),
]


@dataclass(frozen=True)
class Product:
    code: str
    name: str
    size: str
    price: int

    @property
    def label(self) -> str:
        return f"{self.name} {self.size}".strip()


CATALOG: dict[str, Product] = {c: Product(c, n, s, p) for c, n, s, p in PRODUCTS}


def catalog_text() -> str:
    return "\n".join(f"{p.code} | {p.name} | {p.size} | {p.price} บาท" for p in CATALOG.values())


# ---------------------------------------------------------------------------
# สถานะคำสั่งซื้อที่โมเดลอ่านได้จากบทสนทนา (ผ่านการตรวจแล้ว)
# ---------------------------------------------------------------------------

@dataclass
class Line:
    code: str | None  # None = ยังระบุสินค้าไม่ได้
    qty: int
    name: str = ""  # ข้อความที่ลูกค้าเรียก (ใช้แสดงเมื่อไม่พบเท่านั้น)
    candidates: list[str] = field(default_factory=list)


@dataclass
class OrderState:
    kind: str = "cart"  # cart | shipping_info | cancel
    lines: list[Line] = field(default_factory=list)
    receive: str | None = None  # pickup | delivery | None
    name: str | None = None
    address: str | None = None
    phone: str | None = None
    has_slip: bool = False  # เคยส่งสลิปในแชทนี้แล้ว (ดูจากประวัติ)


_NAME_MAX = 40
_CODE_RE = re.compile(r"^GD-[A-Z]\d{3}$")
_PHONE_RE = re.compile(r"^0\d{8,9}$")
_POSTCODE_RE = re.compile(r"(?<!\d)\d{5}(?!\d)")


def _text(v: Any, limit: int) -> str | None:
    if not isinstance(v, str):
        return None
    t = re.sub(r"\s+", " ", v).strip()
    return t[:limit] or None


def parse_state(raw: str, blocklist: Blocklist | None = None) -> OrderState | None:
    """แปลงผลจากโมเดลเป็น OrderState แบบไม่เชื่อ: ผิดรูปแบบคืน None"""
    obj = parse_json(raw or "")
    if not obj:
        return None
    kind = str(obj.get("message_type", "cart")).strip().lower()
    if kind not in ("cart", "shipping_info", "cancel"):
        kind = "cart"
    state = OrderState(kind=kind)

    merged: dict[tuple[str | None, str], Line] = {}
    items = obj.get("items")
    for it in (items if isinstance(items, list) else [])[:MAX_LINES * 2]:
        if not isinstance(it, dict):
            continue
        try:
            qty = int(it.get("qty"))
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        code = it.get("code")
        code = code.strip().upper() if isinstance(code, str) else None
        name = _text(it.get("name"), _NAME_MAX) or ""
        if blocklist and name and blocklist.matches(name):
            name = ""
        cands = [c for c in (it.get("candidates") or []) if isinstance(c, str) and c.strip().upper() in CATALOG]
        cands = [c.strip().upper() for c in cands][:4]
        if code and (not _CODE_RE.match(code) or code not in CATALOG):
            code = None  # รหัสที่โมเดลแต่งขึ้น: ไม่เชื่อ ให้ถือว่าระบุสินค้าไม่ได้
            cands = []
        if code is None and len(set(cands)) == 1:
            code, cands = cands[0], []  # ตัวเลือกเดียว = ระบุสินค้าได้แล้ว ไม่ต้องถามขนาด
        key = (code, name if code is None else "")
        if key in merged:
            merged[key].qty += qty
        else:
            merged[key] = Line(code, qty, name, cands if code is None else [])
        if len(merged) >= MAX_LINES:
            break
    state.lines = list(merged.values())

    recv = str(obj.get("receive") or "").strip().lower()
    state.receive = recv if recv in ("pickup", "delivery") else None
    cust = obj.get("customer") if isinstance(obj.get("customer"), dict) else {}
    state.name = _text(cust.get("name"), 100)
    state.address = _text(cust.get("address"), 300)
    phone = re.sub(r"[\s\-.()]", "", str(cust.get("phone") or ""))
    state.phone = phone if _PHONE_RE.match(phone) else None
    return state


# ---------------------------------------------------------------------------
# คำนวณยอด (ทำในโค้ดทั้งหมด)
# ---------------------------------------------------------------------------

@dataclass
class Totals:
    lines: list[tuple[Product, int, int]]  # (สินค้า, จำนวน, ยอดรายการ)
    pieces: int
    subtotal: int
    discounts: list[tuple[str, int]]
    discount_total: int
    goods: int  # ยอดสินค้าหลังหักโปร
    shipping: int | None  # None = ยังไม่ทราบ
    shipping_note: str
    total: int | None  # None = ยังไม่ทราบค่าส่ง จึงยังไม่มียอดสุดท้าย


def compute(lines: list[Line], receive: str | None) -> Totals:
    merged: dict[str, int] = {}
    for ln in lines:
        if ln.code:
            merged[ln.code] = merged.get(ln.code, 0) + ln.qty
    rows = [(CATALOG[c], q, CATALOG[c].price * q) for c, q in merged.items()]
    subtotal = sum(r[2] for r in rows)
    discounts: list[tuple[str, int]] = []
    for code, per_set, normal, promo, title in PROMOS:
        sets = merged.get(code, 0) // per_set
        if sets:
            discounts.append((title, sets * (normal - promo)))
    discount_total = sum(d for _, d in discounts)
    goods = subtotal - discount_total

    if goods >= FREE_SHIPPING_MIN:
        shipping, note = 0, "ฟรี (ซื้อครบ 500 บาท)"
    elif receive == "pickup":
        shipping, note = 0, "รับที่ร้าน ไม่มีค่าจัดส่ง"
    elif receive == "delivery":
        shipping, note = SHIPPING_FEE, f"{SHIPPING_FEE} บาท (ต่ำกว่า 500 บาท)"
    else:
        shipping, note = None, "ยังไม่รวมค่าจัดส่ง"
    total = None if shipping is None else goods + shipping
    return Totals(rows, sum(r[1] for r in rows), subtotal, discounts, discount_total, goods, shipping, note, total)


def _b(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------------------
# ข้อความตอบ (แม่แบบตายตัว)
# ---------------------------------------------------------------------------

NEED_ITEMS_TEXT = (
    "ยังไม่มีสินค้าในคำสั่งซื้อค่ะ 🛒 บอกชื่อหรือรหัสสินค้าและจำนวนที่ต้องการได้เลยนะคะ "
    "เช่น \"GD-C003 2 ถุง และ GD-H002 3 ถุง\" แล้วน้องดู๊ดจะสรุปยอดให้ค่ะ"
)
CANCEL_TEXT = "ยกเลิกคำสั่งซื้อให้แล้วค่ะ หากต้องการสั่งใหม่ บอกสินค้าและจำนวนได้เลยนะคะ 🐾"
ERROR_TEXT = (
    "ตอนนี้สรุปคำสั่งซื้อไม่ได้ชั่วคราวค่ะ รบกวนลองใหม่อีกครั้ง "
    "หรือสั่งกับพนักงานทาง LINE @gooddude หรือโทร 02-000-0000 ได้เลยนะคะ"
)


def _lines_text(t: Totals) -> list[str]:
    out = []
    for i, (p, q, amt) in enumerate(t.lines, 1):
        out.append(f"{i}. {p.label} ({p.code}) จำนวน {q} ชิ้น × {_b(p.price)} บาท = {_b(amt)} บาท")
    return out


def _unresolved_text(lines: list[Line]) -> list[str]:
    out = []
    for ln in lines:
        if ln.code:
            continue
        if ln.qty > MAX_QTY:
            out.append(f"• จำนวนที่ระบุมากเกินไป ({ln.qty} ชิ้น) รบกวนติดต่อพนักงานเพื่อสั่งจำนวนมากค่ะ")
        elif ln.candidates:
            opts = " / ".join(f"{CATALOG[c].size} ({c}) {_b(CATALOG[c].price)} บาท" for c in ln.candidates)
            title = CATALOG[ln.candidates[0]].name
            out.append(f"• {title} มีหลายขนาดค่ะ: {opts} ต้องการขนาดไหนคะ")
        else:
            what = f"\"{ln.name}\"" if ln.name else "สินค้าที่ระบุ"
            out.append(f"• ไม่พบ {what} ในรายการสินค้าของร้านค่ะ รบกวนแจ้งชื่อหรือรหัสสินค้าที่ตรงกับรายการนะคะ")
    return out


def missing_info(state: OrderState) -> list[str]:
    """ข้อมูลที่ยังขาดสำหรับจัดส่ง (คืนเฉพาะที่ยังไม่ได้รับ) ถ้ารับที่ร้านเองไม่ต้องใช้ที่อยู่"""
    miss = []
    if not state.name:
        miss.append("ชื่อ–นามสกุลผู้รับ")
    if state.receive != "pickup" and not (state.address and _POSTCODE_RE.search(state.address)):
        miss.append("ที่อยู่จัดส่ง พร้อมรหัสไปรษณีย์")
    if not state.phone:
        miss.append("เบอร์ติดต่อ")
    return miss


def payment_block(total: int, state: OrderState) -> list[str]:
    lines = [
        "",
        "สามารถชำระผ่านบัญชีร้านค้าได้ตามรายละเอียดด้านล่างค่ะ",
        f"ช่องทาง: {PAYMENT_METHOD}",
        f"ชื่อบัญชี: {PAYMENT_NAME}",
        f"เลขพร้อมเพย์: {PAYMENT_NUMBER}",
        "(ข้อมูลบัญชีสมมติ ใช้สาธิตเท่านั้น)",
        "",
        "หลังชำระเงินแล้ว รบกวนส่งข้อมูลดังนี้ค่ะ",
        "• สลิปการชำระเงิน (กดปุ่มแนบรูปในแชทนี้ได้เลย)",
        "• ชื่อ–นามสกุลผู้รับ",
    ]
    if state.receive != "pickup":
        lines.append("• ที่อยู่จัดส่ง พร้อมรหัสไปรษณีย์ (จัดส่งเฉพาะในรัศมี 5 กม. จากร้าน)")
    lines += ["• เบอร์ติดต่อ", "", "ทางร้านจะตรวจสอบการชำระเงินและยืนยันคำสั่งซื้อให้ค่ะ"]
    return lines


def render(state: OrderState) -> tuple[str, list[str]]:
    """คืน (ข้อความตอบ, ปุ่มคำถามต่อเนื่อง)"""
    if state.kind == "cancel":
        return CANCEL_TEXT, []

    t = compute(state.lines, state.receive)
    unresolved = _unresolved_text(state.lines)

    if state.kind == "shipping_info" and t.lines:
        return _render_shipping_info(state, t, unresolved)

    if not t.lines and not unresolved:
        return NEED_ITEMS_TEXT, []

    out = ["สรุปคำสั่งซื้อของคุณค่ะ 🛒", ""]
    if t.lines:
        out += _lines_text(t)
        out += ["", f"รวม {len(t.lines)} รายการ จำนวน {t.pieces} ชิ้น", f"ยอดสินค้า: {_b(t.subtotal)} บาท"]
        for title, d in t.discounts:
            out.append(f"โปรโมชัน ({title}): -{_b(d)} บาท")
        if t.discounts:
            out.append(f"ยอดสินค้าหลังหักโปรโมชัน: {_b(t.goods)} บาท")
    if unresolved:
        out += ["", "รบกวนยืนยันรายการเหล่านี้ก่อนนะคะ จึงจะสรุปยอดชำระให้ได้ค่ะ"] + unresolved
        return "\n".join(out).strip(), []

    out.append(f"ค่าจัดส่ง: {t.shipping_note}")
    if t.total is None:
        need = FREE_SHIPPING_MIN - t.goods
        out += [
            "",
            f"ยอดชำระสุดท้ายยังสรุปไม่ได้ เพราะยอดสินค้าต่ำกว่า {FREE_SHIPPING_MIN} บาท "
            f"หากให้ทางร้านจัดส่ง (เฉพาะรัศมี {DELIVERY_RADIUS_KM} กม. จากร้าน) มีค่าส่ง {SHIPPING_FEE} บาท "
            "แต่รับที่ร้านเองไม่มีค่าส่งค่ะ",
            "ต้องการรับที่ร้านหรือให้ร้านจัดส่งคะ "
            f"(หรือเพิ่มสินค้าอีก {_b(need)} บาท เพื่อส่งฟรีก็ได้นะคะ)",
        ]
        return "\n".join(out), ["รับสินค้าที่ร้านเอง", "ให้ร้านจัดส่งที่บ้าน", "ยกเลิกคำสั่งซื้อ"]
    out.append(f"ยอดชำระทั้งหมด: {_b(t.total)} บาท")
    out += payment_block(t.total, state)
    return "\n".join(out), ["สรุปยอดอีกครั้ง", "ยกเลิกคำสั่งซื้อ"]


def _render_shipping_info(state: OrderState, t: Totals, unresolved: list[str]) -> tuple[str, list[str]]:
    got = []
    if state.name:
        got.append("ชื่อผู้รับ")
    if state.address:
        got.append("ที่อยู่")
    if state.phone:
        got.append("เบอร์ติดต่อ")
    miss = missing_info(state)
    out = [f"ได้รับข้อมูล{', '.join(got)}แล้วค่ะ" if got else "รับทราบค่ะ"]
    if miss:
        out += ["รบกวนส่งข้อมูลที่ยังขาดอีกนิดนะคะ"] + [f"• {m}" for m in miss]
    else:
        out.append("ข้อมูลจัดส่งครบแล้วค่ะ")
        if state.has_slip:
            out.append("ทางร้านจะตรวจสอบการชำระเงินและยืนยันคำสั่งซื้อให้ค่ะ")
        elif t.total is not None:
            out.append(f"หากยังไม่ได้ชำระเงิน โอนยอด {_b(t.total)} บาท แล้วส่งสลิปในแชทนี้ได้เลยค่ะ ทางร้านจะตรวจสอบและยืนยันให้")
        else:
            out.append("ขอทราบวิธีรับสินค้า (รับที่ร้านหรือให้ร้านจัดส่ง) เพื่อสรุปยอดชำระสุดท้ายด้วยนะคะ")
    return "\n".join(out), []


def slip_additions(state: OrderState | None, slip_amount: int | None) -> str:
    """ข้อความต่อท้ายคำตอบสลิป: เทียบยอด + ถามเฉพาะข้อมูลจัดส่งที่ยังขาด (ไม่ยืนยันว่าชำระสำเร็จ)"""
    if state is None or not any(ln.code for ln in state.lines):
        return "ยังไม่พบรายการสั่งซื้อในแชทนี้ค่ะ หากต้องการสั่งซื้อ บอกสินค้าและจำนวนได้เลยนะคะ"
    t = compute(state.lines, state.receive)
    out = []
    if t.total is not None and slip_amount is not None and slip_amount != t.total:
        out.append(
            f"หมายเหตุ: ยอดสั่งซื้อของคุณคือ {_b(t.total)} บาท แต่ยอดในสลิปอ่านได้ {_b(slip_amount)} บาท "
            "ทางร้านจะตรวจสอบให้อีกครั้งค่ะ"
        )
    miss = missing_info(state)
    if miss:
        out.append("รบกวนส่งข้อมูลจัดส่งที่ยังขาดค่ะ")
        out += [f"• {m}" for m in miss]
    else:
        out.append("ข้อมูลจัดส่งครบแล้วค่ะ ทางร้านจะตรวจสอบและยืนยันคำสั่งซื้อให้ค่ะ")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# เรียกโมเดลอ่านบทสนทนา
# ---------------------------------------------------------------------------

_HIST_MSGS = 12
_HIST_CHARS = 1500


def load_prompt(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").replace("{{CATALOG}}", catalog_text()).strip()


def _defang(text: str) -> str:
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def build_user_content(text: str, history: list[dict[str, str]]) -> str:
    turns = history[-_HIST_MSGS:]
    if turns:
        lines = [
            f"[{'ลูกค้า' if m['role'] == 'user' else 'น้องดู๊ด'}] {_defang(m['content'][:_HIST_CHARS])}" for m in turns
        ]
        hist = "\n".join(lines)
    else:
        hist = "(ว่าง)"
    return f"ประวัติแชท:\n{hist}\n\nข้อความล่าสุดของลูกค้า:\n<<<\n{_defang(text)}\n>>>"


def has_slip(history: list[dict[str, str]]) -> bool:
    return any(m["role"] == "user" and m["content"].startswith("[ส่งรูป: slip]") for m in history)


async def extract(
    client: httpx.AsyncClient, settings: Settings, text: str, history: list[dict[str, str]],
    blocklist: Blocklist | None = None,
) -> OrderState | None:
    """คืน None เมื่อเรียกโมเดลไม่ได้หรือผลผิดรูปแบบ (ผู้เรียกต้องบอกลูกค้าตรง ๆ ไม่เดาราคา)"""
    try:
        system = load_prompt(settings.order_prompt_path)
        payload = {
            "model": settings.dialogue_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_content(text, history)},
            ],
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
        }
        url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}
        resp = await client.post(url, headers=headers, json=payload, timeout=settings.dialogue_timeout_seconds)
        if resp.status_code in (400, 422):
            payload.pop("response_format")
            resp = await client.post(url, headers=headers, json=payload, timeout=settings.dialogue_timeout_seconds)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"] or ""
    except (httpx.HTTPError, KeyError, IndexError, ValueError, OSError) as exc:
        log.error("order extract failed: %r", exc)
        return None
    state = parse_state(raw, blocklist)
    if state is None:
        log.error("order extract: unparsable model output")
        return None
    state.has_slip = has_slip(history)
    return state
