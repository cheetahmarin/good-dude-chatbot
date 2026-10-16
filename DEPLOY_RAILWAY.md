# Deploy บน Railway (2 services ในโปรเจกต์เดียว)

เป้าหมาย: เปิดถึง 27 ต.ค. ภายใต้เครดิตทดลอง $5 ให้ประหยัดที่สุด

## 0) ก่อน push ขึ้น GitHub
- ห้ามมี `.env` ใน repo (`code/.gitignore` กันไว้แล้ว ตรวจด้วย `git status`)
- repo ต้องมี `Dockerfile.railway`, `code/`, `prompts/` (โฟลเดอร์ `old/` ไม่จำเป็น ไม่ต้อง push ก็ได้)

## 1) Service: lightrag (Deploy from Docker image)
New → Docker Image → `ghcr.io/hkuds/lightrag:latest`
Variables (Raw Editor วางได้ทั้งก้อน แล้วแก้ค่า `<...>`):
```
HOST=0.0.0.0
PORT=9621
WORKING_DIR=/app/data/rag_storage
INPUT_DIR=/app/data/inputs
LLM_BINDING=openai
LLM_BINDING_HOST=https://openrouter.ai/api/v1
LLM_BINDING_API_KEY=<OPENROUTER_API_KEY>
LLM_MODEL=openai/gpt-4o-mini
EMBEDDING_BINDING=openai
EMBEDDING_BINDING_HOST=https://openrouter.ai/api/v1
EMBEDDING_BINDING_API_KEY=<OPENROUTER_API_KEY>
EMBEDDING_MODEL=openai/text-embedding-3-large
EMBEDDING_DIM=3072
EMBEDDING_USE_BASE64=false
SUMMARY_LANGUAGE=Thai
LIGHTRAG_API_KEY=<คีย์ยาวสุ่มเอง>
WHITELIST_PATHS=/health
TIMEOUT=150
```
- Settings → Volumes → Add Volume mount path `/app/data/rag_storage` (เก็บ index ไม่ให้หายตอน redeploy)
- ตั้งชื่อ service ว่า `lightrag` (ใช้ในที่อยู่ภายใน)
- Networking: ยังไม่ต้องสร้างโดเมนสาธารณะ ยกเว้นขั้นตอน 3

## 2) Service: app (Deploy from GitHub repo)
New → GitHub Repo → เลือก repo นี้ แล้วตั้งค่า
- Variables: `RAILWAY_DOCKERFILE_PATH=Dockerfile.railway`
- Root Directory: ปล่อยว่าง (ต้องเป็นรากของ repo เพราะต้องใช้ทั้ง code/ และ prompts/)
```
RAILWAY_DOCKERFILE_PATH=Dockerfile.railway
OPENROUTER_API_KEY=<OPENROUTER_API_KEY>
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
VISION_MODEL=openai/gpt-4o-mini
GUARD_MODEL=meta-llama/llama-guard-4-12b
DIALOGUE_MODEL=openai/gpt-4o-mini
DIALOGUE_ENABLED=true
LIGHTRAG_URL=http://lightrag.railway.internal:9621
LIGHTRAG_API_KEY=<ค่าเดียวกับ lightrag>
RAG_MODE=mix
GUARD_ENABLED=true
ENABLE_CHECK_ENDPOINT=false
RATE_LIMIT_PER_MINUTE=30
RATE_LIMIT_TRUST_PROXY=true
CORS_ALLOWED_ORIGINS=https://<โดเมนของ app>.up.railway.app
```
- Networking → Generate Domain (พอร์ต 8000) แล้วเอาโดเมนที่ได้ไปใส่ `CORS_ALLOWED_ORIGINS` แล้ว redeploy
- `RATE_LIMIT_TRUST_PROXY=true` ถูกต้องที่นี่ เพราะอยู่หลัง proxy ของ Railway

## 3) Ingest ความรู้ (ครั้งเดียว)
1. ที่ service lightrag: Networking → Generate Domain ชั่วคราว (พอร์ต 9621)
2. ที่เครื่อง Mac:
   ```
   cd code
   LIGHTRAG_API_KEY=<คีย์ lightrag> python scripts/ingest.py --url https://<โดเมน lightrag>
   ```
   (ใช้เวลาหลายนาที รอจนขึ้นว่าเสร็จ)
3. ลบโดเมนสาธารณะของ lightrag ทิ้งทันที

## 4) ตรวจหลัง deploy
- เปิด `https://<โดเมน app>/api/health` ต้องเห็น lightrag ok
- ลองถามในหน้าเว็บ 2-3 ข้อ + ส่งรูป 1 รูป
- รัน test suite ชี้ไปที่โดเมนจริง (ใช้ `--base https://<โดเมน app>`) ก่อนวันนำเสนอ

## 5) คุมค่าใช้จ่าย
- ดู Usage ใน Railway ทุกวัน ถ้าเครดิตเหลือน้อยกว่าที่คาดให้ตั้ง Resource limit ของ lightrag ลง
- หลังวันที่ 27 ลบโปรเจกต์ทิ้ง (volume ถูกลบเมื่อลบโปรเจกต์)
- ตั้ง spend limit/ลิมิตใน OpenRouter ด้วย เพราะใครมีลิงก์ก็ยิงได้ (rate limit 30/นาที/IP กันได้บางส่วน)
