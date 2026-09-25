"""Runtime configuration, read from DOCPARSER_* environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCPARSER_", env_file=".env", extra="ignore")

    # LLM backend (Ollama HTTP API)
    ollama_url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b"
    auto_pull: bool = True  # pull missing models on startup (disable for air-gapped installs)
    request_timeout_s: float = 900.0
    num_ctx: int = 8192
    temperature: float = 0.0
    max_concurrent_jobs: int = 2

    # Prompt files
    prompts_dir: Path = Path(__file__).resolve().parent.parent / "prompts"

    # PDF handling
    max_upload_mb: int = 50
    max_pages: int = 200
    ocr_enabled: bool = True
    ocr_min_chars: int = 25  # pages with less extracted text than this are OCR'd
    ocr_dpi: int = 200
    ocr_lang: str = "eng"

    # Chunking for documents longer than the model context
    chunk_chars: int = 12000
    chunk_overlap: int = 400
    max_chunks: int = 8

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
