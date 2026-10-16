import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import dialogue, guard, rag, security, vision
from .blocklist import Blocklist
from .categories import BLOCKLIST_CODE, BLOCKLIST_MESSAGE
from .config import get_settings
from .errors import AppError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gooddude")

settings = get_settings()
blocklist = Blocklist(settings.blocklist_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    app.state.limiter = security.RateLimiter(settings.rate_limit_per_minute)
    app.state.sem = asyncio.Semaphore(settings.max_concurrent_requests)
    key = settings.lightrag_api_key
    if not key or key.lower().startswith("change-me"):
        log.warning("LIGHTRAG_API_KEY ว่างหรือยังเป็นค่าตัวอย่าง: ใครเข้าถึงพอร์ต LightRAG ได้ก็อ่าน/แก้ฐานความรู้ได้ ตั้งค่าใหม่ใน .env")
    yield
    await app.state.http.aclose()


app = FastAPI(title="Good Dude Pet Shop Chatbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}}, headers=headers)


@app.middleware("http")
async def protect(request: Request, call_next):
    """ด่านหน้า: จำกัดขนาด body และจำนวนคำขอก่อนถึงโค้ดแชท + ใส่ส่วนหัวความปลอดภัยทุกคำตอบ"""
    path = request.url.path
    if request.method == "POST" and path.startswith("/api/"):
        if path == "/api/chat/image":
            limit = int((settings.max_image_mb + 1) * 1024 * 1024)  # เผื่อ multipart overhead
        else:
            limit = settings.max_json_body_bytes
        err = security.body_error(request.method, request.headers, limit)
        if err:
            return _with_security_headers(_error(*err))
        ok, retry_after = request.app.state.limiter.allow(security.client_ip(request, settings.rate_limit_trust_proxy))
        if not ok:
            return _with_security_headers(_error(
                429, "rate_limited", "ส่งข้อความถี่เกินไป กรุณารอสักครู่แล้วลองใหม่ค่ะ",
                headers={"Retry-After": str(retry_after)},
            ))
    return _with_security_headers(await call_next(request))


def _with_security_headers(response):
    for k, v in security.SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.exception_handler(AppError)
async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return _error(exc.status, exc.code, exc.message)


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error: %r", exc)  # ไม่ส่งรายละเอียดภายในให้ผู้ใช้
    return _error(500, "internal_error", "ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งค่ะ")


@asynccontextmanager
async def _slot():
    """จำกัดจำนวนคำขอที่เรียกโมเดลพร้อมกัน ถ้ารอนานเกินให้บอกลูกค้าตรง ๆ แทนที่จะค้าง"""
    sem: asyncio.Semaphore = app.state.sem
    try:
        await asyncio.wait_for(sem.acquire(), settings.queue_wait_seconds)
    except asyncio.TimeoutError as exc:
        raise AppError(503, "busy", "ตอนนี้มีผู้ใช้งานจำนวนมาก กรุณารอสักครู่แล้วลองใหม่ค่ะ") from exc
    try:
        yield
    finally:
        sem.release()


# ---------------------------------------------------------------------------
# โครงสร้างข้อมูล
# ---------------------------------------------------------------------------

class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    history: list[Turn] = Field(default_factory=list, max_length=50)


class ChatResponse(BaseModel):
    reply: str
    blocked: bool = False  # ถูกชั้นความปลอดภัยปฏิเสธ
    stage: Literal["input", "output", "image", "none"] = "none"  # ปฏิเสธที่ขั้นไหน
    categories: list[str] = []
    fallback: bool = False  # ตอบด้วยข้อความ fallback (นอกคลังความรู้)
    image_type: str | None = None
    elapsed_ms: int = 0
    intent: str = "shop"  # shop | smalltalk | clarify | off_topic
    rewritten: str | None = None  # คำถามที่ครบในตัวหลังเข้าใจบริบท (ถ้าต่างจากที่ลูกค้าพิมพ์)
    suggestions: list[str] = []  # ปุ่มคำถามต่อเนื่อง


class CheckRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    direction: Literal["input", "output"]


def _clean_history(history: list[Turn]) -> list[dict[str, str]]:
    """ประวัติมาจากเบราว์เซอร์ จึงปลอมได้ (รวมถึงข้อความของ 'assistant') ต้องไม่เชื่อ:
    ทิ้งข้อความที่มีคำสั่งแทรก, ตัดความยาวต่อข้อความและรวมทั้งหมด, เก็บเฉพาะรอบล่าสุด"""
    kept: list[dict[str, str]] = []
    for t in history[-(settings.max_history_turns * 2):]:
        content = t.content.strip()
        if not content or blocklist.matches(content):
            continue
        kept.append({"role": t.role, "content": content[: settings.max_history_turn_chars]})
    out: list[dict[str, str]] = []
    total = 0
    for m in reversed(kept):  # เก็บข้อความใหม่สุดก่อน จนเต็มโควตาตัวอักษร
        total += len(m["content"])
        if total > settings.max_history_total_chars:
            break
        out.append(m)
    out.reverse()
    return out


def _validate_message(text: str) -> str:
    text = text.strip()
    if not text:
        raise AppError(400, "empty_message", "กรุณาพิมพ์ข้อความก่อนส่งค่ะ")
    if len(text) > settings.max_message_chars:
        raise AppError(
            413, "message_too_long",
            f"ข้อความยาวเกิน {settings.max_message_chars} ตัวอักษร กรุณาย่อข้อความแล้วลองใหม่ค่ะ",
        )
    return text


async def _answer(question: str, history: list[dict[str, str]]) -> tuple[str, bool, list[str]]:
    """RAG + ตรวจขาออก คืน (ข้อความ, ถูกบล็อกขาออกไหม, หมวด)"""
    client = app.state.http
    answer = await rag.query(client, settings, question, history)
    if answer == rag.FALLBACK_TEXT:
        return answer, False, []  # ข้อความตายตัวของร้าน ไม่ต้องตรวจซ้ำ
    verdict = await guard.check(client, settings, blocklist, answer, "output")
    if not verdict.safe:
        return verdict.message or "", True, verdict.categories
    return answer, False, []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """ตรวจว่าแอปและ LightRAG พร้อมหรือไม่ (UI ใช้แสดงสถานะ)
    dialogue: ok = ชั้นเข้าใจบริบท/คุยเล่นทำงาน, degraded = เรียกโมเดลไม่ได้ (ระบบยังตอบได้แต่ไม่เข้าใจประโยคตาม)"""
    rag_ok = False
    try:
        r = await app.state.http.get(f"{settings.lightrag_url.rstrip('/')}/health", timeout=3)
        rag_ok = r.status_code == 200
    except httpx.HTTPError:
        pass
    return {
        "status": "ok",
        "rag": "ok" if rag_ok else "down",
        "guard": "on" if settings.guard_enabled else "off",
        "dialogue": dialogue.status() if settings.dialogue_enabled else "off",
    }


@app.post("/api/check")
async def check_endpoint(req: CheckRequest):
    """เรียกชั้นความปลอดภัยโดยตรง (ใช้ทดสอบด้วย Postman) ปิดได้ด้วย ENABLE_CHECK_ENDPOINT=false"""
    if not settings.enable_check_endpoint:
        raise AppError(404, "not_found", "ไม่พบบริการนี้ค่ะ")
    v = await guard.check(app.state.http, settings, blocklist, req.message, req.direction)
    return {
        "result": "safe" if v.safe else "unsafe",
        "direction": req.direction,
        "categories": v.categories,
        "msg": req.message if v.safe else v.message,
    }


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    t0 = time.perf_counter()
    client = app.state.http
    text = _validate_message(req.message)
    history = _clean_history(req.history)

    def ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    # 0) blocklist ก่อนทุกอย่าง: ข้อความโจมตีไม่ถูกส่งต่อให้โมเดลใด ๆ
    if blocklist.matches(text):
        verdict = await guard.check(client, settings, blocklist, text, "input")
        return ChatResponse(
            reply=verdict.message or BLOCKLIST_MESSAGE, blocked=True, stage="input",
            categories=verdict.categories, elapsed_ms=ms(),
        )

    # 1) ทักทาย/ขอบคุณ/ลา ล้วน ๆ: ตอบทันที ไม่เรียกโมเดล
    canned = dialogue.fast_smalltalk(text, history)
    if canned:
        return ChatResponse(
            reply=canned, intent="smalltalk", suggestions=dialogue.DEFAULT_SUGGESTIONS, elapsed_ms=ms(),
        )

    async with _slot():
        # 2) ตรวจความปลอดภัยขาเข้า พร้อมกับแยกเจตนา/เขียนคำถามใหม่ (ขนานกัน ลดเวลารอ)
        verdict, analysis = await asyncio.gather(
            guard.check(client, settings, blocklist, text, "input"),
            dialogue.analyze(client, settings, text, history),
        )
        if not verdict.safe:
            return ChatResponse(
                reply=verdict.message or BLOCKLIST_MESSAGE, blocked=True, stage="input",
                categories=verdict.categories, elapsed_ms=ms(),
            )

        # 3) คุยเล่น / ถามกลับ / นอกเรื่อง: ตอบด้วยบุคลิกของน้องดู๊ด (ตรวจขาออกก่อนส่ง)
        if analysis.intent != "shop":
            out = await guard.check(client, settings, blocklist, analysis.reply, "output")
            if not out.safe:
                return ChatResponse(
                    reply=out.message or "", blocked=True, stage="output", categories=out.categories,
                    intent=analysis.intent, elapsed_ms=ms(),
                )
            return ChatResponse(
                reply=analysis.reply, intent=analysis.intent,
                suggestions=dialogue.DEFAULT_SUGGESTIONS if analysis.intent != "clarify" else [], elapsed_ms=ms(),
            )

        # 4) เรื่องของร้าน: ค้นด้วยคำถามที่ครบในตัว (เข้าใจประโยคตามจากประวัติแชท)
        question = analysis.question or text
        rewritten = question if question.strip() != text.strip() else None
        if rewritten:
            # คำถามที่เขียนใหม่ผสมข้อมูลจากประวัติ (ซึ่งปลอมได้) จึงต้องผ่าน blocklist + Llama Guard เหมือนข้อความลูกค้า
            v2 = await guard.check(client, settings, blocklist, rewritten, "input")
            if not v2.safe:
                return ChatResponse(
                    reply=v2.message or BLOCKLIST_MESSAGE, blocked=True, stage="input",
                    categories=v2.categories, elapsed_ms=ms(),
                )

        reply, out_blocked, cats = await _answer(question, history)
    is_fallback = rag.is_fallback(reply)
    return ChatResponse(
        reply=reply, blocked=out_blocked, stage="output" if out_blocked else "none",
        categories=cats, fallback=is_fallback, rewritten=rewritten,
        suggestions=[] if out_blocked else (dialogue.DEFAULT_SUGGESTIONS if is_fallback else dialogue.suggest(question)),
        elapsed_ms=ms(),
    )


@app.post("/api/chat/image", response_model=ChatResponse)
async def chat_image(
    file: UploadFile = File(...),
    message: str = Form(""),
) -> ChatResponse:
    t0 = time.perf_counter()
    client = app.state.http

    def ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    caption = message.strip()
    if len(caption) > settings.max_message_chars:
        raise AppError(413, "message_too_long", "ข้อความยาวเกินกำหนด กรุณาย่อข้อความแล้วลองใหม่ค่ะ")

    data = await file.read(int(settings.max_image_mb * 1024 * 1024) + 1)
    mime = vision.validate_image(data, settings)

    async with _slot():
        if caption:
            v = await guard.check(client, settings, blocklist, caption, "input")
            if not v.safe:
                return ChatResponse(
                    reply=v.message or BLOCKLIST_MESSAGE, blocked=True, stage="input",
                    categories=v.categories, elapsed_ms=ms(),
                )

        # ผลอ่านรูปคือข้อมูลที่ไม่น่าไว้ใจ: บังคับชนิด/ความยาวของทุกช่องก่อนใช้
        info = vision.sanitize(await vision.extract(client, settings, data, mime))
        image_type = info["image_type"]

        # ข้อความที่ฝังอยู่ในรูปอาจเป็นคำสั่งโจมตี ตรวจ blocklist ทุกช่องที่มาจากรูปก่อนใช้ต่อ
        embedded = vision.embedded_text(info)
        if embedded and blocklist.matches(embedded):
            return ChatResponse(
                reply=BLOCKLIST_MESSAGE, blocked=True, stage="image", categories=[BLOCKLIST_CODE],
                image_type=image_type, elapsed_ms=ms(),
            )

        kind, content = vision.plan(info, caption)
        if kind == "reply":
            return ChatResponse(reply=content, image_type=image_type, elapsed_ms=ms())

        reply, out_blocked, cats = await _answer(content, [])
        # สินค้า/ป้ายราคาที่ไม่อยู่ในคลัง: ตอบว่า "ไม่พบในรายการร้าน" พร้อมสิ่งที่อ่านได้ แทนข้อความ fallback ทั่วไป
        if not out_blocked and rag.is_fallback(reply) and image_type in ("product_label", "price_tag"):
            return ChatResponse(
                reply=vision.not_found_text(info), fallback=True, image_type=image_type, elapsed_ms=ms(),
            )
    return ChatResponse(
        reply=reply, blocked=out_blocked, stage="output" if out_blocked else "none",
        categories=cats, fallback=rag.is_fallback(reply), image_type=image_type, elapsed_ms=ms(),
    )


# ---------------------------------------------------------------------------
# หน้าเว็บ
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(f"{settings.static_dir}/index.html")
