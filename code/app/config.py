from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# code/app/config.py -> parents[2] = โฟลเดอร์โปรเจกต์ (มี prompts/ และ knowledge/)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CODE_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenRouter (ใช้ทั้ง Llama Guard และโมเดลอ่านรูป)
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    guard_model: str = "meta-llama/llama-guard-4-12b"
    vision_model: str = "openai/gpt-4o-mini"

    # ชั้นบทสนทนา (แยกเจตนา/เขียนคำถามใหม่/คุยเล่น) ใช้โมเดลเล็กผ่าน OpenRouter
    dialogue_enabled: bool = True
    dialogue_model: str = "openai/gpt-4o-mini"
    dialogue_timeout_seconds: float = 20.0

    # LightRAG
    lightrag_url: str = "http://localhost:9621"
    lightrag_api_key: str = ""
    rag_mode: str = "mix"
    rag_top_k: int = 20

    # พฤติกรรมของบอท
    guard_enabled: bool = True
    request_timeout_seconds: float = 60.0
    guard_timeout_seconds: float = 20.0
    max_message_chars: int = 1000
    max_history_turns: int = 10
    max_history_turn_chars: int = 1500  # ต่อข้อความในประวัติ (ประวัติมาจากเบราว์เซอร์ จึงไม่น่าไว้ใจ)
    max_history_total_chars: int = 8000
    max_image_mb: float = 5.0

    # ป้องกันการใช้งานเกินเหตุ/เปลืองเงินค่าโมเดล (ทุกคำขอเรียกโมเดลได้ถึง 4 ครั้ง)
    rate_limit_per_minute: int = 30  # ต่อ IP, 0 = ปิด
    rate_limit_trust_proxy: bool = False  # true เฉพาะเมื่ออยู่หลัง reverse proxy ที่เชื่อถือได้ (อ่าน X-Forwarded-For)
    max_concurrent_requests: int = 8
    queue_wait_seconds: float = 20.0
    max_json_body_bytes: int = 64 * 1024
    enable_check_endpoint: bool = True  # /api/check ไว้ทดสอบด้วย Postman ตอน deploy จริงควรปิด

    # path ของไฟล์ที่แก้ได้โดยไม่ต้องแก้โค้ด
    rules_path: str = str(_PROJECT_ROOT / "prompts" / "bot_rules.md")
    vision_prompt_path: str = str(_PROJECT_ROOT / "prompts" / "vision_system.md")
    dialogue_prompt_path: str = str(_PROJECT_ROOT / "prompts" / "dialogue_system.md")
    order_prompt_path: str = str(_PROJECT_ROOT / "prompts" / "order_system.md")
    blocklist_path: str = str(_CODE_ROOT / "app" / "blocklist.txt")
    static_dir: str = str(_CODE_ROOT / "static")

    # ค่าเริ่มต้นอนุญาตเฉพาะหน้าเว็บของแอปเอง (เพิ่มโดเมนอื่นคั่นด้วย , ถ้า deploy)
    cors_allowed_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
