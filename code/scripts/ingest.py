"""นำไฟล์ใน knowledge/ เข้า LightRAG (POST /documents/text) แล้วรอจนประมวลผลเสร็จ

ใช้: python scripts/ingest.py [--url http://localhost:9621] [--dir ../knowledge]
คีย์อ่านจาก .env (LIGHTRAG_API_KEY) หรือ environment variable
เอกสารเดิมที่เนื้อหาเหมือนกันจะถูก LightRAG ข้ามเอง รันซ้ำได้ปลอดภัย
ห้ามนำ docs/price_reference.md เข้า เพราะมีชื่อแบรนด์จริง
"""

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("LIGHTRAG_URL", "http://localhost:9621"))
    ap.add_argument("--dir", default=str(ROOT.parent / "knowledge"))
    ap.add_argument("--timeout", type=int, default=900, help="วินาทีที่รอประมวลผลสูงสุด")
    args = ap.parse_args()

    headers = {}
    if os.getenv("LIGHTRAG_API_KEY"):
        headers["X-API-Key"] = os.environ["LIGHTRAG_API_KEY"]

    files = sorted(Path(args.dir).glob("*.md"))
    if not files:
        print(f"ไม่พบไฟล์ .md ใน {args.dir}")
        return 1

    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as c:
        try:
            c.get("/health").raise_for_status()
        except httpx.HTTPError as e:
            print(f"เชื่อมต่อ LightRAG ที่ {args.url} ไม่ได้: {e}\nเปิด docker compose up -d ก่อนหรือยัง?")
            return 1

        for f in files:
            r = c.post("/documents/text", json={"text": f.read_text(encoding="utf-8"), "file_source": f.name})
            if r.status_code >= 400:
                print(f"✗ {f.name}: HTTP {r.status_code} {r.text[:200]}")
                return 1
            print(f"✓ ส่ง {f.name}: {r.json().get('status', 'ok')}")

        print("รอ LightRAG ประมวลผล (สร้าง knowledge graph) …")
        t0 = time.time()
        time.sleep(3)
        while time.time() - t0 < args.timeout:
            st = c.get("/documents/pipeline_status").json()
            msg = (st.get("latest_message") or "")[:90]
            print(f"  busy={st.get('busy')} batch={st.get('cur_batch')}/{st.get('batchs')} {msg}")
            if not st.get("busy"):
                print(f"เสร็จใน {time.time() - t0:.0f} วินาที")
                return 0
            time.sleep(10)
    print("หมดเวลารอ ตรวจสถานะที่ http://localhost:9621/webui")
    return 2


if __name__ == "__main__":
    sys.exit(main())
