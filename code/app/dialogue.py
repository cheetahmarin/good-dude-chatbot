"""ชั้นบทสนทนา: เข้าใจบริบทหลายรอบ + คุยเล่น (chit-chat) ก่อนเข้า RAG

เทคนิคที่ใช้ (สรุปในเอกสารโครงงาน):
1) Fast path: ทักทาย/ขอบคุณ/ลา/รับทราบล้วน ๆ ตอบทันทีด้วยข้อความหลายแบบ ไม่เรียกโมเดล (เร็ว ถูก ไม่ซ้ำเดิม)
2) Intent routing: โมเดลเล็กแยกเจตนา shop / smalltalk / clarify / off_topic
3) Query rewriting: เขียนประโยคตามให้เป็นคำถามที่ครบในตัวก่อนค้น ("แล้วไซซ์ L ล่ะ" -> "ปลอกคอ ไซซ์ L ราคาเท่าไหร่")
4) Clarifying question: คำถามกำกวมให้ถามกลับ ไม่เดา
5) Suggested replies: ปุ่มคำถามต่อเนื่องตามหัวข้อ (ไม่เรียกโมเดล)

หลักความปลอดภัย: ตัวนี้ "จัดประเภทและเขียนใหม่" เท่านั้น ข้อเท็จจริงของร้านมาจาก RAG เสมอ
ถ้าโมเดลเผลอใส่ตัวเลข/ราคาใน reply ที่ไม่ผ่าน RAG จะถูกโยนกลับไปเข้า RAG แทน
ถ้าเรียกโมเดลไม่ได้ ระบบถอยกลับเป็นพฤติกรรมเดิม (ส่งข้อความตรง ๆ เข้า RAG) ไม่ล่ม
"""

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .vision import parse_json

log = logging.getLogger("gooddude.dialogue")

_CLARIFY_MAX_CHARS = 14
INTENTS = {"shop", "smalltalk", "clarify", "off_topic", "order"}
_MAX_QUESTION = 500
_MAX_REPLY = 500
_HISTORY_MSGS = 6  # ส่งให้ตัววิเคราะห์เฉพาะ 6 ข้อความล่าสุด (3 รอบ)
_HISTORY_CHARS = 500


# นับผลเรียกโมเดลแยกเจตนา ให้ /api/health บอกได้ว่าฟีเจอร์ทำงานจริงหรือถอยกลับเงียบ ๆ
STATS = {"ok": 0, "failed": 0, "last_ok": True}


def status() -> str:
    """ok | degraded (ครั้งล่าสุดเรียกโมเดลไม่ได้ ระบบถอยกลับเป็นส่งข้อความตรง ๆ เข้า RAG) | unused (ยังไม่เคยเรียก)"""
    if STATS["ok"] + STATS["failed"] == 0:
        return "unused"
    return "ok" if STATS["last_ok"] else "degraded"


@dataclass
class Analysis:
    intent: str = "shop"
    question: str = ""  # คำถามที่ครบในตัว (เฉพาะ shop)
    reply: str = ""  # ข้อความตอบ (เฉพาะ smalltalk/clarify/off_topic)
    used_model: bool = False  # False = ถอยกลับเพราะเรียกโมเดลไม่ได้


# ---------------------------------------------------------------------------
# 1) Fast path
# ---------------------------------------------------------------------------

_NON_TEXT = re.compile(r"[^฀-๿a-z0-9]")
_TAIL_PARTICLES = re.compile(r"(ครับผม|คร้าบ|ครับ|ค่ะ|ค่า|คะ|นะคะ|นะครับ|นะจ๊ะ|นะ|จ้า|จ้ะ|จ๊ะ|น้า|เลย|ๆ)+$")

_GREET = {
    "สวัสดี", "สวัสดีตอนเช้า", "สวัสดีตอนบ่าย", "สวัสดีตอนเย็น", "หวัดดี", "ดีจ้า", "ดี", "อรุณสวัสดิ์",
    "hello", "hi", "hey", "ฮัลโหล", "เฮลโหล", "สวัสดีน้องดู๊ด", "หวัดดีน้องดู๊ด", "hiน้องดู๊ด", "สวัสดีร้านกู๊ดดู๊ด",
}
_THANKS = {
    "ขอบคุณ", "ขอบคุณมาก", "ขอบคุณมากๆ", "ขอบคุณน้องดู๊ด", "ขอบใจ", "แต๊งกิ้ว", "thanks", "thankyou", "thx",
    "โอเคขอบคุณ", "โอเคขอบคุณมาก", "ได้แล้วขอบคุณ", "เข้าใจแล้วขอบคุณ", "ขอบคุณสำหรับข้อมูล", "ขอบคุณที่ช่วย",
}
_BYE = {
    "บาย", "บ๊ายบาย", "ไปก่อน", "ไปก่อนนะ", "แล้วเจอกัน", "ลาก่อน", "bye", "goodbye", "ไว้คุยกันใหม่", "ไปแล้ว",
}
_ACK = {"โอเค", "ok", "okay", "เข้าใจแล้ว", "ได้", "ได้เลย", "รับทราบ", "เข้าใจ", "โอเคเข้าใจแล้ว"}

_REPLIES = {
    "greet": [
        "สวัสดีค่ะ น้องดู๊ดเองนะคะ 🐾 วันนี้อยากให้ช่วยเรื่องอะไรดีคะ อาหาร ทรายแมว ของเล่น หรือเรื่องการส่ง ถามได้เลยค่ะ",
        "สวัสดีค่ะ ยินดีต้อนรับสู่ Good Dude Pet Shop นะคะ น้องหมาน้องแมวที่บ้านสบายดีไหมคะ มีอะไรให้น้องดู๊ดช่วย บอกได้เลยค่ะ",
        "หวัดดีค่ะ 🐶 น้องดู๊ดพร้อมช่วยแล้วค่ะ อยากรู้เรื่องสินค้า ราคา หรือโปรโมชันตรงไหน พิมพ์มาได้เลยนะคะ",
    ],
    "thanks": [
        "ยินดีค่ะ 🐾 ถ้ามีอะไรอยากถามเพิ่ม น้องดู๊ดอยู่ตรงนี้นะคะ",
        "ด้วยความยินดีค่ะ หวังว่าน้อง ๆ ที่บ้านจะชอบนะคะ",
        "ไม่เป็นไรค่ะ ถามเพิ่มได้ตลอดเลยนะคะ",
    ],
    "bye": [
        "ขอบคุณที่แวะมานะคะ 🐾 ไว้มาคุยกันใหม่ ฝากความคิดถึงถึงน้อง ๆ ที่บ้านด้วยค่ะ",
        "แล้วเจอกันนะคะ ดูแลน้องหมาน้องแมวดี ๆ นะคะ 🐶",
    ],
    "ack": [
        "ได้เลยค่ะ ถ้ามีอะไรสงสัยเพิ่ม ถามน้องดู๊ดได้ตลอดนะคะ",
        "รับทราบค่ะ 🐾 อยากให้ช่วยเรื่องอื่นต่อไหมคะ",
    ],
}


def _normalize(text: str) -> str:
    t = _NON_TEXT.sub("", text.lower())
    return _TAIL_PARTICLES.sub("", t)


def _pick(kind: str, last_bot: str | None) -> str:
    options = [r for r in _REPLIES[kind] if r != last_bot] or _REPLIES[kind]
    return random.choice(options)


def fast_smalltalk(text: str, history: list[dict[str, str]]) -> str | None:
    """ถ้าข้อความเป็นแค่ทักทาย/ขอบคุณ/ลา/รับทราบ คืนคำตอบทันที (ไม่ซ้ำกับคำตอบก่อนหน้า) ไม่งั้นคืน None"""
    if len(text) > 40:
        return None
    n = _normalize(text)
    if not n:
        return None
    last_bot = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), None)
    for kind, words in (("greet", _GREET), ("thanks", _THANKS), ("bye", _BYE), ("ack", _ACK)):
        if n in words:
            # "โอเค/ได้" หลังบอทเพิ่งถามคำถามคือคำตอบ ไม่ใช่การรับทราบ ให้ตัววิเคราะห์ดูบริบทแทน
            if kind == "ack" and last_bot and _asks_question(last_bot):
                return None
            return _pick(kind, last_bot)
    return None


def _asks_question(bot_text: str) -> bool:
    t = bot_text.strip().rstrip("🐾🐶🐱 !.")
    return "?" in t or t.endswith(("คะ", "ไหม", "มั้ย", "หรือเปล่า"))


# ---------------------------------------------------------------------------
# 2-4) Intent routing + query rewriting + clarify (โมเดลเล็กตัวเดียว)
# ---------------------------------------------------------------------------

_FACT_RE = re.compile(r"[0-9๐-๙]|บาท|฿")


_SHOP_CONTACT_RE = re.compile(r"02-000-0000|@gooddude", re.IGNORECASE)


def has_facts(reply: str) -> bool:
    """ข้อความคุยเล่นต้องไม่มีราคา/ตัวเลข เพราะยังไม่ได้ผ่านการค้นข้อมูลร้าน (ยกเว้นเบอร์/LINE ของร้านที่ตายตัว)"""
    return bool(_FACT_RE.search(_SHOP_CONTACT_RE.sub("", reply)))


def load_prompt(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _defang(text: str) -> str:
    """กันข้อความลูกค้า/ประวัติปิดตัวคั่น <<< >>> เพื่อหลุดออกจากส่วนข้อมูล"""
    return text.replace("<<<", "‹‹‹").replace(">>>", "›››")


def _format_history(history: list[dict[str, str]]) -> str:
    turns = history[-_HISTORY_MSGS:]
    if not turns:
        return "(ว่าง)"
    lines = []
    for m in turns:
        who = "ลูกค้า" if m["role"] == "user" else "น้องดู๊ด"
        lines.append(f"[{who}] {_defang(m['content'][:_HISTORY_CHARS])}")
    return "\n".join(lines)


def build_user_content(text: str, history: list[dict[str, str]]) -> str:
    return f"ประวัติแชท:\n{_format_history(history)}\n\nข้อความล่าสุดของลูกค้า:\n<<<\n{_defang(text)}\n>>>"


def parse_analysis(raw: str, text: str) -> Analysis:
    """แปลงผลจากโมเดลเป็น Analysis แบบปลอดภัย: ผิดรูปแบบหรือไม่น่าไว้ใจ ให้ถอยเป็น shop + ข้อความเดิม"""
    obj = parse_json(raw or "")
    if not obj:
        return Analysis("shop", text)
    intent = str(obj.get("intent", "shop")).strip().lower()
    question = str(obj.get("question") or "").strip()
    reply = str(obj.get("reply") or "").strip()
    if intent not in INTENTS:  # ผลแปลก ๆ จากโมเดล: ไม่เชื่อ ใช้ข้อความเดิมของลูกค้า
        return Analysis("shop", text, "", True)

    if intent == "order":  # สรุป/ยืนยัน/แก้ไข/ยกเลิกคำสั่งซื้อ: ตัวสรุปคำสั่งซื้อ (order.py) ทำงานต่อ ไม่ผ่าน RAG
        return Analysis("order", "", "", True)

    if intent == "shop":
        if not question or len(question) > _MAX_QUESTION:
            question = text
        return Analysis("shop", question, "", True)

    # โมเดลเล็กชอบถามกลับเกินจำเป็น: ข้อความยาวพอจะมีสินค้า/หัวข้อในตัว (เช่น "ร้านรับอาบน้ำตัดขนไหม")
    # ให้ถือเป็น shop เสมอ แล้วให้คลังความรู้ตอบหรือใช้ fallback เอง ส่วน clarify ใช้เฉพาะข้อความสั้นกำกวมจริง ๆ
    if intent == "clarify" and len(text.strip()) > _CLARIFY_MAX_CHARS:
        return Analysis("shop", text, "", True)

    # smalltalk/clarify/off_topic ต้องมีข้อความตอบที่ไม่ยาวเกินและไม่อ้างข้อเท็จจริงร้าน
    if not reply or len(reply) > _MAX_REPLY or has_facts(reply):
        return Analysis("shop", text, "", True)
    return Analysis(intent, "", reply, True)


async def _call_model(
    client: httpx.AsyncClient, settings: Settings, system: str, user: str, json_mode: bool
) -> str:
    payload: dict = {
        "model": settings.dialogue_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 400,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = await client.post(
        f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json=payload,
        timeout=settings.dialogue_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"] or ""


async def analyze(
    client: httpx.AsyncClient, settings: Settings, text: str, history: list[dict[str, str]]
) -> Analysis:
    """ไม่โยน exception: ถ้าเรียกโมเดลไม่ได้ ถอยกลับเป็น shop + ข้อความเดิม (พฤติกรรมเดิมของระบบ)"""
    if not settings.dialogue_enabled:
        return Analysis("shop", text)
    try:
        system = load_prompt(settings.dialogue_prompt_path)
        user = build_user_content(text, history)
        try:
            raw = await _call_model(client, settings, system, user, json_mode=True)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 422):
                raise
            # ผู้ให้บริการบางรายไม่รองรับ response_format ลองใหม่โดยไม่ใช้ (parse_json รับ JSON ที่ห่อด้วย ``` ได้)
            log.warning("dialogue model rejected response_format (%s) retry without", exc.response.status_code)
            raw = await _call_model(client, settings, system, user, json_mode=False)
    except (httpx.HTTPError, KeyError, IndexError, ValueError, OSError) as exc:
        STATS["failed"] += 1
        STATS["last_ok"] = False
        log.error("dialogue analyze failed (fallback to plain RAG): %r", exc)
        return Analysis("shop", text)
    STATS["ok"] += 1
    STATS["last_ok"] = True
    return parse_analysis(raw, text)


# ---------------------------------------------------------------------------
# 5) Suggested replies (ปุ่มคำถามต่อเนื่อง)
# ---------------------------------------------------------------------------

DEFAULT_SUGGESTIONS = ["อาหารสุนัขโต 10 กก. ราคาเท่าไหร่", "ร้านส่งสินค้าไหม ค่าส่งเท่าไหร่", "มีสมาชิกหรือโปรโมชันอะไรบ้าง"]

# (คำที่ตรวจพบ, ปุ่มแนะนำ) เรียงตามลำดับความสำคัญ ทุกข้อความต้องเป็นคำถามที่คลังความรู้ตอบได้
_TOPICS: list[tuple[tuple[str, ...], list[str]]] = [
    (("ตำหนิ", "คืนสินค้า", "คืนเงิน", "เปลี่ยนสินค้า", "ใบเสร็จ"),
     ["สินค้ามีตำหนิต้องทำยังไง", "สัตว์ไม่ยอมกินอาหาร คืนได้ไหม", "ชำระเงินได้อย่างไร"]),
    (("ส่งสินค้า", "ค่าส่ง", "จัดส่ง", "ส่งฟรี", "รัศมี"),
     ["ชำระเงินได้อย่างไร", "สั่งสินค้าได้ทางไหนบ้าง", "มีสมาชิกหรือโปรโมชันอะไรบ้าง"]),
    (("สมาชิก", "แต้ม", "โปร", "ส่วนลด", "club"),
     ["ลองอาหารก่อนซื้อได้ไหม", "ทรายแมวเบนทอไนต์ 3 ถุงราคาเท่าไหร่", "ร้านส่งสินค้าไหม ค่าส่งเท่าไหร่"]),
    (("ลองอาหาร", "ซองทดลอง", "ทดลอง"),
     ["มีสมาชิกหรือโปรโมชันอะไรบ้าง", "ลูกสุนัขหรือลูกแมวควรกินอาหารอะไร", "ร้านส่งสินค้าไหม ค่าส่งเท่าไหร่"]),
    (("ทราย", "กระบะ"),
     ["ทรายแมวมีกี่แบบ ต่างกันอย่างไร", "ทรายเบนทอไนต์ 3 ถุงราคาเท่าไหร่", "ร้านส่งสินค้าไหม ค่าส่งเท่าไหร่"]),
    (("ปลอกคอ", "สายจูง", "ไซซ์", "ที่นอน", "ของเล่น", "ชาม", "กรง"),
     ["ควรเลือกปลอกคอไซซ์ไหน", "ที่นอนไซซ์ M กับ L ราคาเท่าไหร่", "ของเล่นสำหรับแมวมีอะไรบ้าง"]),
    (("ลูกสุนัข", "ลูกหมา", "ลูกแมว", "puppy", "kitten", "อายุ"),
     ["ลูกสุนัขหรือลูกแมวควรกินอาหารอะไร", "อาหารลูกแมวราคาเท่าไหร่", "ลองอาหารก่อนซื้อได้ไหม"]),
    (("อาหาร", "ขนม", "กก.", "โปรตีน", "แพ้"),
     ["เก็บอาหารเม็ดอย่างไร", "สุนัขหนัก 10 กก. ให้อาหารวันละกี่กรัม", "ลองอาหารก่อนซื้อได้ไหม"]),
    (("เปิด", "ปิด", "เวลา", "ที่อยู่", "ติดต่อ", "โทร", "line"),
     ["สั่งสินค้าได้ทางไหนบ้าง", "ร้านส่งสินค้าไหม ค่าส่งเท่าไหร่", "มีสมาชิกหรือโปรโมชันอะไรบ้าง"]),
]


def suggest(text: str, limit: int = 3) -> list[str]:
    """เลือกปุ่มคำถามต่อเนื่องจากหัวข้อของคำถามล่าสุด (ไม่ซ้ำกับสิ่งที่ถามไปแล้ว)"""
    t = text.lower()
    for keywords, options in _TOPICS:
        if any(k in t for k in keywords):
            picked = [o for o in options if o != text.strip()]
            return picked[:limit]
    return DEFAULT_SUGGESTIONS[:limit]
