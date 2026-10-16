FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --no-create-home --uid 10001 appuser
COPY app ./app
COPY static ./static
# ไฟล์ที่ copy จาก macOS อาจมีสิทธิ์อ่านเฉพาะเจ้าของ: เปิดให้ผู้ใช้ appuser อ่านได้
RUN chmod -R a+rX /app
# prompts/ ถูก mount จากโฟลเดอร์โปรเจกต์ผ่าน docker-compose (แก้ไฟล์แล้วมีผลทันที)
ENV RULES_PATH=/app/prompts/bot_rules.md \
    VISION_PROMPT_PATH=/app/prompts/vision_system.md \
    DIALOGUE_PROMPT_PATH=/app/prompts/dialogue_system.md
# ไม่รันด้วย root: ถ้าแอปถูกเจาะ จะเขียนอะไรในคอนเทนเนอร์ไม่ได้
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
