# Good Dude Pet Shop Chatbot (น้องดู๊ด)

แชทบอทร้านขายอาหารและของใช้สุนัข-แมว (ข้อมูลร้านจำลอง) สำหรับโปรเจกต์วิชา 06048308 Intelligent Chatbot Development

```
เบราว์เซอร์ (static/index.html)
   │  POST /api/chat | /api/chat/image
   ▼
FastAPI (app/main.py)
   ├─ ตรวจขาเข้า: blocklist.txt → Llama Guard 4 (OpenRouter)   [fail-closed]
   ├─ ข้อความ → LightRAG /query (mode=mix, user_prompt = prompts/bot_rules.md)
   ├─ รูป → vision model อ่านเป็น JSON → โค้ดตัดสิน (สลิป/นับวันคืนสินค้า) → LightRAG
   └─ ตรวจขาออก: Llama Guard 4 → ตอบลูกค้า
```

## โครงสร้าง
| path | หน้าที่ |
|---|---|
| `app/` | backend (config, guard, rag, vision, errors, main) |
| `app/blocklist.txt` | คำต้องห้าม/คำสั่งแทรก แก้ได้ทันที ต้อง restart |
| `static/index.html` | หน้าแชท (สถานะกำลังโหลด, error + ปุ่มลองใหม่, แนบรูป) |
| `scripts/ingest.py` | นำ `../knowledge/*.md` เข้า LightRAG |
| `scripts/run_tests.py` | รันชุดทดสอบ บันทึกตารางผล + เวลาตอบ |
| `tests/cases.json` | 10 คำถาม + กรณีความปลอดภัย พร้อมเกณฑ์จับคำสำคัญ |
| `tests/test_unit.py` | unit test แบบ mock (ไม่ใช้คีย์จริง) |
| `../prompts/bot_rules.md`, `vision_system.md` | กฎบอท / prompt อ่านรูป (อ่านใหม่ทุกคำขอ แก้แล้วเห็นผลทันที) |

## วิธีรัน
1. `cp .env.example .env` แล้วใส่ `OPENROUTER_API_KEY` และตั้ง `LIGHTRAG_API_KEY`
2. `docker compose up -d --build` (เปิด LightRAG พอร์ต 9621 และแอปพอร์ต 8000)
3. `pip install -r requirements-dev.txt` (มี pytest; ตัวแอปใน Docker ใช้ `requirements.txt` เท่านั้น) แล้ว `python scripts/ingest.py` รอจน busy=False
4. เปิด http://localhost:8000
5. ทดสอบ: `python scripts/run_tests.py --suite text` (และ `safety`, `image`) ผลอยู่ใน `tests/results/`

รันแอปนอก Docker: `uvicorn app.main:app --port 8000` (ต้องมี LightRAG รันอยู่แล้ว และ `LIGHTRAG_URL=http://localhost:9621`)

## API (ทดสอบด้วย Postman ได้)
- `GET /api/health` สถานะแอป + LightRAG + `dialogue` (ok/degraded/unused/off: ชั้นเข้าใจบริบทและคุยเล่นทำงานอยู่หรือไม่ ถ้า degraded แปลว่าเรียกโมเดลไม่ได้ ระบบยังตอบได้แต่ไม่เข้าใจประโยคตาม)
- `POST /api/chat` `{"message": "...", "history": [{"role":"user","content":"..."}]}`
- `POST /api/chat/image` multipart: `file` (JPG/PNG/WEBP ≤ 5 MB), `message` (ไม่บังคับ)
- `POST /api/check` `{"message": "...", "direction": "input|output"}` เรียกชั้นความปลอดภัยโดยตรง

ข้อผิดพลาดทุกกรณีตอบ `{"error": {"code", "message"}}` ข้อความภาษาไทย (400/413/415 ข้อมูลผิด, 429 ถูกจำกัด, 502 guard ใช้ไม่ได้, 503/504 LightRAG หรือโมเดลไม่พร้อม/ช้า)

## หมายเหตุ
- ไม่ใช้ Vercel และไม่เชื่อม LINE ตามที่ตกลง ประวัติสนทนาเก็บฝั่งเบราว์เซอร์ เซิร์ฟเวอร์ไม่เก็บ state
- ข้อความที่ฝังในรูปจะถูกตรวจ blocklist ก่อนใช้ต่อ กัน prompt injection ผ่านรูป
- สลิป: บอทอ่านยอด/วันที่/เวลาแล้วบอกว่าร้านต้องตรวจยอดเอง ไม่ยืนยันการชำระเงินเด็ดขาด
- ถ้า embedding ผ่าน OpenRouter ไม่ได้ ให้ลองเปลี่ยน `EMBEDDING_USE_BASE64` หรือชื่อโมเดลใน `docker-compose.yml`

## ชั้นเข้าใจบริบท / คุยเล่น (dialogue)
ข้อความถูกแยกเจตนา (shop / smalltalk / clarify / off_topic) และเขียนคำถามใหม่ให้ครบในตัวโดยใช้ประวัติแชท ก่อนส่งค้นข้อมูลร้าน คำทักทาย/ขอบคุณ/ลาตอบทันทีโดยไม่เรียกโมเดล ปิดทั้งชั้นได้ด้วย `DIALOGUE_ENABLED=false` (prompt อยู่ที่ `prompts/dialogue_system.md`)

## ความปลอดภัย (สรุปจากการตรวจสอบโค้ด)
- ประวัติแชทมาจากเบราว์เซอร์จึงปลอมได้: เซิร์ฟเวอร์ทิ้งข้อความที่ติด blocklist, จำกัดความยาว/จำนวน และตรวจคำถามที่เขียนใหม่ด้วย Llama Guard ซ้ำ
- blocklist normalize ข้อความ (NFKC, ตัวพิมพ์, zero-width, วรรณยุกต์ไทย, เว้นวรรค/เครื่องหมายคั่น) กันการหลบด้วยช่องว่างหรืออักขระล่องหน
- จำกัดขนาด body, rate limit ต่อ IP (`RATE_LIMIT_PER_MINUTE`), จำกัดคำขอที่เรียกโมเดลพร้อมกัน (`MAX_CONCURRENT_REQUESTS`), ปิด `/docs`
- CORS อนุญาตเฉพาะโดเมนใน `CORS_ALLOWED_ORIGINS` (ห้ามใช้ `*` ตอน deploy) ส่วนหัว CSP/X-Frame-Options/nosniff ทุกคำตอบ
- ผลอ่านรูปถูกบังคับชนิด/ความยาวก่อนใช้ และไม่สะท้อนข้อความดิบจากโมเดลกลับไปหาลูกค้า
- `/api/check` ใช้ทดสอบเท่านั้น: ตอน deploy ตั้ง `ENABLE_CHECK_ENDPOINT=false`
- คอนเทนเนอร์แอปรันด้วยผู้ใช้ไม่ใช่ root มี HEALTHCHECK; compose ไม่ยอมเริ่มถ้าไม่ตั้งคีย์
- **ห้ามส่งไฟล์ `.env` พร้อมงาน** (มี `.gitignore`/`.dockerignore` กันไว้แล้ว)

### ความเสี่ยงที่ยังเหลือ
- ประวัติฝั่ง assistant ปลอมได้ทั้งหมดไม่ได้ถ้าไม่ลงลายเซ็นประวัติฝั่งเซิร์ฟเวอร์ (ลดผลกระทบด้วยการกรอง+Guard แล้ว)
- ตัวอักษรหน้าตาเหมือนกันข้ามภาษา (homoglyph) อาจหลบ blocklist ได้ จึงมี Llama Guard เป็นชั้นสอง
- CSP ยังต้องใช้ `'unsafe-inline'` เพราะหน้าเว็บเป็นไฟล์เดียว
- image ของ LightRAG ยังเป็น `latest` ก่อนส่งงานควรล็อกเวอร์ชันที่ทดสอบแล้ว

## ผลทดสอบล่าสุดและข้อจำกัดที่ทราบ (16 ต.ค. 2569)
| ชุดทดสอบ | ผล | หมายเหตุ |
|---|---|---|
| text | 10/10 | ผ่านครบ (เริ่มต้นผ่าน 3/10 ก่อนแก้ Llama Guard ตีผิด / ถามกลับเกินจำเป็น / ขาดรหัสสินค้า) |
| chitchat | 7/7 | ผ่านครบ |
| image | 5/5 | ผ่านครบ (รอบก่อนหน้า 3/5) ผลของโมเดลอ่านรูปไม่นิ่งระหว่างรอบ |
| context | 5/6 | C6 ไม่ผ่านเพราะเงื่อนไขตรวจคำว่า "ไม่ได้" เข้มเกิน (บอทตอบ "ไม่สามารถทิ้ง…" ซึ่งถูกต้อง) แก้เงื่อนไขแล้วแต่ไม่ได้รันซ้ำ |
| safety | 5/6 | S3 (อ้างรับประกันตลอดชีพ) บางรอบบอทตอบข้อความ fallback แทนที่จะแก้ข้อมูลตามนโยบาย ผลของโมเดลไม่นิ่ง ไม่ได้รันซ้ำ |

ข้อจำกัด
- ผลทดสอบมาจากการรันกับโมเดลจริงและไม่นิ่งระหว่างรอบ (ข้อที่ผ่านอาจไม่ผ่านในรอบถัดไปและกลับกัน) ตัวเลขข้างบนเป็นผลของรอบที่รันครั้งล่าสุดของแต่ละชุด
- S3: การปฏิเสธนโยบายที่ร้านไม่มีขึ้นกับโมเดล ไม่มีการตรวจในโค้ดเหมือน S2 (ข้อมูลลูกค้าอื่น) และ S4 (prompt injection) ที่ตัดสินในโค้ดจึงแน่นอน
- Llama Guard หมวด S1, S2, S6 ถูกลดความไวให้บล็อกเฉพาะข้อความที่มีคำที่เกี่ยวข้องจริง เพราะตีผิดกับคำถามสินค้าปกติ หมวดอื่น (เช่น S7 ความเป็นส่วนตัว S11 การทำร้ายตัวเอง) บล็อกเต็มรูปแบบ
- เส้นทางรูปทดสอบด้วยรูป 5 รูปเท่านั้น (สลิปเป็นภาพจำลอง ใบเสร็จเก่ากว่ากำหนดคืนสินค้า) โมเดลอ่านรูปเป็น gpt-4o-mini จึงอ่านป้ายราคาที่เล็กหรือรูปที่มีหลายป้ายได้ไม่แม่นเสมอไป
