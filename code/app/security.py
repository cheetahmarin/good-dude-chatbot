"""ชั้นป้องกันระดับ HTTP: จำกัดจำนวนคำขอ, จำกัดขนาด body, ส่วนหัวความปลอดภัย

เหตุผล: ทุกคำขอแชทเรียกโมเดลที่คิดเงินได้ถึง 4 ครั้ง (guard ขาเข้า, แยกเจตนา, LightRAG, guard ขาออก)
ถ้าไม่จำกัด ใครเรียกซ้ำ ๆ ก็ทำให้ค่า OpenRouter พุ่งหรือเครื่องค้างได้
"""

import time
from collections import deque
from typing import Callable

from starlette.requests import Request

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # หน้าเว็บใช้ inline script/style และ Google Fonts เท่านั้น ห้ามติดต่อเซิร์ฟเวอร์อื่น (connect-src 'self')
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class RateLimiter:
    """sliding window ต่อ key (เช่น IP) เก็บในหน่วยความจำ limit <= 0 = ปิด"""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        """คืน (อนุญาตไหม, วินาทีที่ควรรอก่อนลองใหม่)"""
        if self.limit <= 0:
            return True, 0
        now = self.clock()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self.max_keys:
                self._prune(now)
            q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False, max(1, int(self.window - (now - q[0])) + 1)
        q.append(now)
        return True, 0

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.window]:
            del self._hits[k]
        if len(self._hits) >= self.max_keys:  # ยังเต็ม (ถูกถล่มจากหลาย IP) ทิ้งครึ่งที่เก่าสุด
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: self.max_keys // 2]:
                del self._hits[k]


def client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def body_error(method: str, headers, limit: int) -> tuple[int, str, str] | None:
    """ตรวจ Content-Length ก่อนอ่าน body คืน (status, code, ข้อความ) ถ้าไม่ผ่าน"""
    if method not in ("POST", "PUT", "PATCH"):
        return None
    raw = headers.get("content-length")
    if raw is None:
        return 411, "length_required", "คำขอไม่ระบุขนาดข้อมูล (Content-Length) กรุณาลองใหม่ค่ะ"
    try:
        size = int(raw)
    except ValueError:
        return 400, "bad_length", "คำขอไม่ถูกต้อง กรุณาลองใหม่ค่ะ"
    if size < 0:
        return 400, "bad_length", "คำขอไม่ถูกต้อง กรุณาลองใหม่ค่ะ"
    if size > limit:
        return 413, "body_too_large", "ข้อมูลที่ส่งมีขนาดใหญ่เกินกำหนด กรุณาลดขนาดแล้วลองใหม่ค่ะ"
    return None
