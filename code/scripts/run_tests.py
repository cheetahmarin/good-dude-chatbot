"""รันชุดทดสอบกับแชทบอทจริง แล้วบันทึกผลเป็นตาราง Markdown + CSV

ใช้:
  python scripts/run_tests.py --suite text      # 10 คำถามข้อความ
  python scripts/run_tests.py --suite safety    # กรณีความปลอดภัย
  python scripts/run_tests.py --suite context   # ประโยคตามที่ต้องเข้าใจบริบท (ส่ง history ไปด้วย)
  python scripts/run_tests.py --suite chitchat  # ทักทาย/คุยเล่น/ถามกลับ/นอกเรื่อง
  python scripts/run_tests.py --suite image     # รูปภาพ (ต้องมี tests/images/manifest.json)
  python scripts/run_tests.py --suite order     # สรุปคำสั่งซื้อหลายรอบ (O7 ต้องมี tests/images/slip_01.jpg)
  python scripts/run_tests.py --suite all

ผล "ผ่าน" ที่สคริปต์ตัดสินเป็นเพียงข้อเสนอจากการจับคำสำคัญ (AUTO)
ต้องอ่านคำตอบแล้วยืนยันเองก่อนใส่ในรายงาน คอลัมน์ "ผ่าน" ในไฟล์ผลจึงให้แก้ได้
"""

import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def contains(text: str, option: str) -> bool:
    """option แบบ 'a|b' = มีคำใดคำหนึ่ง"""
    return any(alt in text for alt in option.split("|"))


def judge(case: dict, data: dict) -> tuple[bool, list[str]]:
    reply = data.get("reply", "")
    problems = []
    if case.get("expect_image_type") and data.get("image_type") != case["expect_image_type"]:
        problems.append(f"ชนิดรูปที่อ่านได้ = {data.get('image_type')!r} (คาดว่า {case['expect_image_type']!r})")
    if case.get("expect_blocked") and not data.get("blocked"):
        problems.append("ควรถูกบล็อกแต่ไม่ถูกบล็อก")
    for kw in case.get("expect_all", []):
        if not contains(reply, kw):
            problems.append(f"ไม่พบ '{kw}'")
    any_kw = case.get("expect_any")
    if any_kw and not any(contains(reply, k) for k in any_kw):
        problems.append("ไม่พบคำใดใน expect_any")
    for kw in case.get("forbid", []):
        if kw in reply:
            problems.append(f"พบคำต้องห้าม '{kw}'")
    return (not problems), problems


def run_case(client: httpx.Client, base: str, case: dict, images_dir: Path) -> dict:
    t0 = time.perf_counter()
    try:
        if case.get("image"):
            path = images_dir / case["image"]
            with path.open("rb") as fh:
                r = client.post(
                    f"{base}/api/chat/image",
                    files={"file": (path.name, fh, "image/png" if path.suffix == ".png" else "image/jpeg")},
                    data={"message": case.get("message", ""), "history": json.dumps(case.get("history", []), ensure_ascii=False)},
                )
        else:
            r = client.post(f"{base}/api/chat", json={"message": case["message"], "history": case.get("history", [])})
        elapsed = time.perf_counter() - t0
        data = r.json()
        if r.status_code >= 400:
            data = {"reply": f"[HTTP {r.status_code}] {data.get('error', {}).get('message', '')}", "blocked": False}
    except Exception as e:  # noqa: BLE001
        elapsed = time.perf_counter() - t0
        data = {"reply": f"[ERROR] {e}", "blocked": False}
    ok, problems = judge(case, data)
    return {
        "id": case["id"], "kind": case.get("kind", ""), "question": (("[ก่อนหน้า: " + " / ".join(t["content"] for t in case["history"] if t["role"] == "user") + "] ") if case.get("history") else "") + (case.get("message") or f"(รูป {case.get('image')})"),
        "reply": data.get("reply", ""), "blocked": data.get("blocked", False), "stage": data.get("stage", ""), "categories": ",".join(data.get("categories", []) or []),
        "fallback": data.get("fallback", False), "image_type": data.get("image_type") or "",
        "seconds": round(elapsed, 2), "auto_pass": "ผ่าน" if ok else "ไม่ผ่าน", "notes": "; ".join(problems),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["text", "safety", "context", "chitchat", "image", "order", "all"], default="text")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--timeout", type=float, default=120)
    args = ap.parse_args()

    cases = json.loads((TESTS / "cases.json").read_text(encoding="utf-8"))
    images_dir = TESTS / "images"
    manifest = images_dir / "manifest.json"
    if manifest.exists():
        cases["image"] = json.loads(manifest.read_text(encoding="utf-8"))

    suites = ["text", "safety", "context", "chitchat", "image", "order"] if args.suite == "all" else [args.suite]
    out_dir = TESTS / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    with httpx.Client(timeout=args.timeout) as client:
        for suite in suites:
            if suite not in cases:
                print(f"[{suite}] ไม่มีเคส (ภาพ: สร้าง tests/images/manifest.json ก่อน)")
                continue
            rows = []
            for case in cases[suite]:
                if case.get("image") and not (images_dir / case["image"]).exists():
                    print(f"[{suite}] {case['id']:>4} ข้าม: ไม่พบไฟล์ tests/images/{case['image']}")
                    continue
                row = run_case(client, args.base, case, images_dir)
                rows.append(row)
                print(f"[{suite}] {row['id']:>4} {row['auto_pass']:<7} {row['seconds']:>6.2f}s  {row['reply'][:70]!r}")

            if not rows:
                continue
            csv_path = out_dir / f"{suite}_{stamp}.csv"
            with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

            md = [f"# ผลทดสอบชุด {suite} ({stamp})", "",
                  "| ข้อ | คำถาม | ผลตอบของบอท | ผ่าน (AUTO) | เวลา (วิ) | หมายเหตุ |", "|---|---|---|---|---|---|"]
            for r in rows:
                cell = lambda s: str(s).replace("|", "\\|").replace("\n", "<br>")  # noqa: E731
                tag = " (ถูกบล็อก:" + r["stage"] + " " + r.get("categories", "") + ")" if r["blocked"] else ""
                md.append(f"| {r['id']} | {cell(r['question'])} | {cell(r['reply'])}{tag} | {r['auto_pass']} | {r['seconds']} | {cell(r['notes'])} |")
            times = [r["seconds"] for r in rows]
            md += ["", f"ผ่าน (AUTO) {sum(r['auto_pass']=='ผ่าน' for r in rows)}/{len(rows)} · เวลาเฉลี่ย {sum(times)/len(times):.2f} วิ · สูงสุด {max(times):.2f} วิ"]
            (out_dir / f"{suite}_{stamp}.md").write_text("\n".join(md), encoding="utf-8")
            print(f"บันทึกผล: {csv_path.name} / {csv_path.stem}.md\n")


if __name__ == "__main__":
    main()
