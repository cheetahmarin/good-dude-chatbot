"""ชั้นความปลอดภัย: blocklist -> Llama Guard 4 (ผ่าน OpenRouter)

ใช้ทั้งขาเข้า (ข้อความลูกค้า) และขาออก (คำตอบของบอท)
fail-closed: ถ้าเรียก Llama Guard ไม่ได้ ถือว่าตรวจไม่ผ่าน ไม่ปล่อยผ่านเงียบ ๆ
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import httpx

from .blocklist import Blocklist
from .categories import (
    BLOCKLIST_CODE,
    BLOCKLIST_MESSAGE,
    CATEGORIES,
    OUTPUT_BLOCKED_MESSAGE,
)
from .config import Settings
from .errors import AppError

log = logging.getLogger("gooddude.guard")
_CODE_RE = re.compile(r"S\d{1,2}", re.IGNORECASE)

Direction = Literal["input", "output"]


class GuardError(Exception):
    """เรียก OpenRouter ไม่สำเร็จ หรืออ่านผลของ Llama Guard ไม่ได้"""


@dataclass
class Verdict:
    safe: bool
    message: str | None = None  # ข้อความปฏิเสธภาษาไทย (เมื่อ unsafe)
    categories: list[str] = field(default_factory=list)


def parse_verdict(raw: str) -> tuple[bool, list[str]]:
    """คืน (is_unsafe, [รหัสหมวด]) จากผล 'safe' หรือ 'unsafe\\nS1,S2'"""
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        raise GuardError("ผลจาก Llama Guard ว่างเปล่า")
    first = lines[0].lower()
    if first == "safe":
        return False, []
    if first == "unsafe":
        return True, [c.upper() for c in _CODE_RE.findall(" ".join(lines[1:]))]
    raise GuardError(f"อ่านผลจาก Llama Guard ไม่ได้: {raw[:100]!r}")


# S6 (Specialized Advice) ของ Llama Guard ไวเกินกับร้านสัตว์เลี้ยง: คำถามปกติอย่าง
# "หมาหนัก 12 กก. ใช้ปลอกคอไซซ์ไหน" หรือ "ให้อาหารวันละกี่กรัม" ถูกตีว่าเป็นคำแนะนำเฉพาะทาง
# จึงให้ S6 บล็อกเฉพาะเมื่อข้อความพูดถึงสุขภาพ/อาการ/ยา/การรักษาจริง ๆ (ที่เหลือมีกฎ N5/M6 ใน prompt คุมอยู่)
_HEALTH_RE = re.compile(
    r"ป่วย|อาเจียน|ท้องเสีย|ท้องร่วง|ถ่าย(?:เป็น)?เลือด|เลือดออก|อาการ|วินิจฉัย|โรค|ติดเชื้อ|พยาธิ|หมัด|เห็บ|"
    r"ยา(?![วงกมน])|รักษา|ฉีด|วัคซีน|ผ่าตัด|มิลลิกรัม|\bmg\b|ขนาดยา|ชัก|ซึม|ไม่กินอาหาร|แพ้อย่างรุนแรง|"
    r"vomit|diarrh|symptom|diagnos|disease|infect|medic|dose|dosage|vaccin|surgery|treat",
    re.IGNORECASE,
)


# S1 บนคำตอบขาออกก็ไวเกินเช่นกัน (เคยตีคำตอบเรื่องขนาดปลอกคอ/สายจูงว่าเป็นความรุนแรง)
_VIOLENCE_RE = re.compile(
    r"ทำร้าย|ฆ่า|ฆาตกรรม|อาวุธ|ปืน|มีด|ระเบิด|ทรมาน|ทุบตี|เตะ|วางยา|kill|murder|weapon|\bgun\b|knife|bomb|tortur|assault|attack|beat",
    re.IGNORECASE,
)
# S2 ก็เคยตีคำตอบเรื่องส่วนลดซื้อ 3 ถุงว่าเป็นอาชญากรรม
_CRIME_RE = re.compile(
    r"ผิดกฎหมาย|โกง|ฉ้อโกง|ขโมย|ลักขโมย|ลักลอบ|ยาเสพติด|ปลอมแปลง|แฮก|หลอกลวง|ฟอกเงิน|ปลอมบัตร|"
    r"steal|theft|fraud|scam|narcotic|\bdrugs?\b|hack|counterfeit|launder|illegal|crime|forg(?:e|ery)",
    re.IGNORECASE,
)
_LEXICAL_GATES = {"S6": _HEALTH_RE, "S1": _VIOLENCE_RE, "S2": _CRIME_RE}


def pick_blocked(codes: list[str], text: str = "") -> list[str]:
    """ตัดหมวดที่ไวเกิน (S6, S1, S2) ออก ถ้าข้อความไม่มีคำที่เกี่ยวข้องจริง"""
    codes = [c for c in codes if not (c in _LEXICAL_GATES and text and not _LEXICAL_GATES[c].search(text))]
    return _pick_blocked(codes)


def _pick_blocked(codes: list[str]) -> list[str]:
    return [c for c in codes if c in CATEGORIES and CATEGORIES[c].blocked]


# ขอข้อมูลส่วนตัวของลูกค้าคนอื่น: ตัดสินในโค้ด (แน่นอน ไม่เสียค่าโมเดล) เพราะ Llama Guard ไม่ได้จับเคสนี้
# และ LightRAG จะตอบ fallback ทั่วไปแทนการอธิบายเรื่องความเป็นส่วนตัว
_PII_DATA_RE = re.compile(r"เบอร์|โทรศัพท์|ที่อยู่|ไลน์|line id|อีเมล|ชื่อ-?นามสกุล|ชื่อจริง|ประวัติ(?:การ)?สั่ง|ยอดสั่งซื้อ", re.IGNORECASE)
_OTHER_CUSTOMER_RE = re.compile(r"ลูกค้า.{0,20}(?:ที่สั่ง|ที่ซื้อ|คนอื่น|ท่านอื่น|รายอื่น|คนก่อน|คนอื่น|เมื่อวาน|คนที่)|(?:คนอื่น|ท่านอื่น|รายอื่น).{0,15}ลูกค้า")


def asks_other_customer_data(text: str) -> bool:
    return bool(_PII_DATA_RE.search(text) and _OTHER_CUSTOMER_RE.search(text))


async def classify(
    client: httpx.AsyncClient, settings: Settings, text: str, direction: Direction
) -> tuple[bool, list[str]]:
    role = "user" if direction == "input" else "assistant"
    try:
        resp = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json={
                "model": settings.guard_model,
                "messages": [{"role": role, "content": text}],
                "temperature": 0,
            },
            timeout=settings.guard_timeout_seconds,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise GuardError(f"เรียก OpenRouter ไม่สำเร็จ: {exc}") from exc

    unsafe, codes = parse_verdict(raw or "")
    if not unsafe:
        return False, []
    blocked = pick_blocked(codes, text)
    return bool(blocked), blocked


async def check(
    client: httpx.AsyncClient,
    settings: Settings,
    blocklist: Blocklist,
    text: str,
    direction: Direction,
) -> Verdict:
    # 1) blocklist: ผลแน่นอน ไม่เสียค่าโมเดล (ขาออกไม่ต้องตรวจคำ injection)
    if direction == "input" and blocklist.matches(text):
        return Verdict(False, BLOCKLIST_MESSAGE, [BLOCKLIST_CODE])

    if direction == "input" and asks_other_customer_data(text):
        return Verdict(False, CATEGORIES["S7"].message, ["S7"])

    # 2) Llama Guard 4
    if not settings.guard_enabled:
        return Verdict(True)
    try:
        unsafe, codes = await classify(client, settings, text, direction)
    except GuardError as exc:
        log.error("guard failed (fail-closed): %s", exc)
        raise AppError(
            502, "guard_unavailable",
            "ระบบตรวจสอบความปลอดภัยยังไม่พร้อมใช้งาน กรุณาลองใหม่อีกครั้งค่ะ",
        ) from exc

    if not unsafe:
        return Verdict(True)
    log.warning("guard blocked %s categories=%s", direction, codes)
    msg = CATEGORIES[codes[0]].message if direction == "input" else OUTPUT_BLOCKED_MESSAGE
    return Verdict(False, msg, codes)
