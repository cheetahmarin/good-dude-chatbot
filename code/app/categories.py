"""หมวดของ Llama Guard 4 (S1-S14) และข้อความปฏิเสธภาษาไทยรายหมวด

ปรับจาก chacharin/llama-guard-layer ให้เหมาะกับร้านค้า
- blocked=False -> ถ้าโมเดลตอบ unsafe เฉพาะหมวดนี้ จะถือว่า safe
- กติกาเฉพาะร้าน (ส่วนลด นโยบาย ข้อมูลลูกค้า) ไม่ใช่หน้าที่ของ Llama Guard
  ต้องบังคับผ่านกฎใน prompts/bot_rules.md และคลังความรู้
"""

from dataclasses import dataclass

_TAIL = " หากต้องการสอบถามเรื่องสินค้าของร้าน ยินดีช่วยเหลือค่ะ"


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    blocked: bool
    message: str | None = None


_CATEGORIES = [
    Category("S1", "Violent Crimes", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาเกี่ยวกับความรุนแรงได้" + _TAIL),
    Category("S2", "Non-Violent Crimes", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาเกี่ยวกับการกระทำผิดกฎหมายได้" + _TAIL),
    Category("S3", "Sex-Related Crimes", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาเกี่ยวกับอาชญากรรมทางเพศได้" + _TAIL),
    Category("S4", "Child Sexual Exploitation", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาเรื่องนี้ได้"),
    Category("S5", "Defamation", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาที่อาจเป็นการใส่ร้ายบุคคลหรือองค์กรได้"),
    Category(
        "S6", "Specialized Advice", True,
        "ขออภัยค่ะ น้องดู๊ดเป็นผู้ช่วยของร้านขายสินค้า ไม่สามารถวินิจฉัยอาการหรือแนะนำยาได้ "
        "หากสัตว์เลี้ยงมีอาการผิดปกติ กรุณาพาไปพบสัตวแพทย์โดยเร็วนะคะ",
    ),
    Category(
        "S7", "Privacy", True,
        "ขออภัยค่ะ ร้านไม่สามารถเปิดเผยข้อมูลส่วนบุคคลของลูกค้าท่านอื่นได้ เพื่อความเป็นส่วนตัวของทุกคนค่ะ",
    ),
    Category("S8", "Intellectual Property", False),
    Category("S9", "Indiscriminate Weapons", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาเรื่องอาวุธได้"),
    Category("S10", "Hate", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาที่แสดงความเกลียดชังได้" + _TAIL),
    Category(
        "S11", "Suicide & Self-Harm", True,
        "ขออภัยค่ะ น้องดู๊ดตอบเรื่องนี้ไม่ได้ หากรู้สึกไม่ไหว โปรดพูดคุยกับคนใกล้ชิดหรือผู้เชี่ยวชาญด้านสุขภาพจิตนะคะ",
    ),
    Category("S12", "Sexual Content", True, "ขออภัยค่ะ ไม่สามารถตอบเนื้อหาทางเพศได้" + _TAIL),
    Category("S13", "Elections", False),
    Category("S14", "Code Interpreter Abuse", False),
]

CATEGORIES: dict[str, Category] = {c.code: c for c in _CATEGORIES}

BLOCKLIST_CODE = "BLOCKLIST"
# ไม่บอกว่าติดคำไหน เพื่อไม่ให้ลองเลี่ยงได้ง่าย
BLOCKLIST_MESSAGE = (
    "ขออภัยค่ะ น้องดู๊ดไม่สามารถทำตามข้อความนี้ได้ "
    "หากต้องการสอบถามเรื่องสินค้า ราคา หรือนโยบายของร้าน ยินดีช่วยเหลือค่ะ"
)
# ใช้เมื่อคำตอบของบอทเองไม่ผ่านการตรวจขาออก
OUTPUT_BLOCKED_MESSAGE = (
    "ขออภัยค่ะ น้องดู๊ดตอบเรื่องนี้ไม่ได้ รบกวนติดต่อพนักงานทาง LINE @gooddude หรือโทร 02-000-0000 นะคะ"
)
