import re
import unicodedata
from pathlib import Path

# วรรณยุกต์/ทัณฑฆาต ไทย: ถอดออกทั้งสองฝั่งก่อนเทียบ กันการแทรกเครื่องหมายเพื่อเลี่ยงคำ (เช่น ลืม่คำสั่ง)
_THAI_MARKS = re.compile("[่-์]")
# ช่องว่างและเครื่องหมายวรรคตอน ASCII (ไม่แตะสระ/วรรณยุกต์ไทย)
_SEPARATORS = re.compile(r"[\s!-/:-@\[-`{-~]+")


def _norm(text: str) -> str:
    """NFKC (ตัวอักษรเต็มความกว้าง/ตัวห้อย -> ปกติ), ตัวพิมพ์เล็ก, ตัดอักขระล่องหน (zero-width ฯลฯ), ถอดวรรณยุกต์ไทย"""
    t = unicodedata.normalize("NFKC", text).lower()
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")
    t = _THAI_MARKS.sub("", t)
    return " ".join(t.split())


def _compact(norm: str) -> str:
    return _SEPARATORS.sub("", norm)


class Blocklist:
    """ตรวจคำ/วลีต้องห้ามแบบ substring ไม่สนตัวพิมพ์ใหญ่เล็ก

    ผลแน่นอนและไม่เสียค่าเรียกโมเดล จึงตรวจก่อน Llama Guard เสมอ
    เทียบ 2 แบบ: ข้อความที่ normalize แล้ว และแบบตัดช่องว่าง/เครื่องหมายทั้งหมด
    (จับ "i g n o r e  previous", "ignore-previous", "ลืม คำ สั่ง", ตัวอักษรเต็มความกว้าง, zero-width)
    ข้อจำกัด: เป็นชั้นแรกเท่านั้น ไม่ใช่เกราะหลัก (ตัวอักษรหน้าตาเหมือน เช่น ซีริลลิก หรือสะกดแปลก ๆ ยังหลุดได้)
    เกราะจริงคือ least privilege + กฎบอท + ตรวจคำตอบขาออก
    ไฟล์: หนึ่งบรรทัดต่อหนึ่งคำ, บรรทัดขึ้นต้นด้วย # คือคอมเมนต์
    """

    def __init__(self, path: str):
        self.terms: list[str] = []
        self._compact_terms: list[str] = []
        p = Path(path)
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    n = _norm(line)
                    self.terms.append(n)
                    c = _compact(n)
                    if len(c) >= 6:  # คำสั้นเกินไปตัดช่องว่างแล้วเสี่ยงจับผิด
                        self._compact_terms.append(c)

    def matches(self, text: str) -> bool:
        t = _norm(text)
        if any(term in t for term in self.terms):
            return True
        c = _compact(t)
        return any(term in c for term in self._compact_terms)
