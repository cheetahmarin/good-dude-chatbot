"""ข้อผิดพลาดของแอป พร้อมข้อความภาษาไทยที่แสดงให้ลูกค้าเห็นได้ทันที"""

import httpx


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def map_http_error(exc: Exception, service: str) -> AppError:
    """แปลงข้อผิดพลาดของ httpx เป็น AppError ภาษาไทย (service = 'rag' | 'vision')"""
    name = "ระบบค้นข้อมูลร้าน" if service == "rag" else "ระบบอ่านรูปภาพ"

    if isinstance(exc, httpx.TimeoutException):
        return AppError(504, f"{service}_timeout", f"{name}ตอบช้าเกินไป กรุณาลองใหม่อีกครั้งค่ะ")
    if isinstance(exc, httpx.ConnectError):
        return AppError(503, f"{service}_unavailable", f"{name}ยังไม่พร้อมใช้งาน กรุณาลองใหม่ในอีกสักครู่ค่ะ")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return AppError(500, f"{service}_auth", "ระบบตั้งค่าคีย์ไม่ถูกต้อง กรุณาแจ้งผู้ดูแลระบบค่ะ")
        if code == 404:
            return AppError(500, f"{service}_not_found", "ระบบตั้งค่าโมเดลหรือที่อยู่บริการไม่ถูกต้อง กรุณาแจ้งผู้ดูแลระบบค่ะ")
        if code == 429:
            return AppError(429, f"{service}_rate_limited", "มีผู้ใช้งานจำนวนมาก กรุณารอสักครู่แล้วลองใหม่ค่ะ")
        if code >= 500:
            return AppError(503, f"{service}_upstream", f"{name}ขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ")
    return AppError(502, f"{service}_error", f"{name}เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้งค่ะ")
