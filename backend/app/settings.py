from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration loaded exclusively from the local environment."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Factualize"
    api_prefix: str = "/api/v1"
    data_dir: Path = Field(default=PROJECT_ROOT / "data", validation_alias="FACTUALIZE_DATA_DIR")
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_extraction_model: str = Field(default="", validation_alias="GEMINI_EXTRACTION_MODEL")
    gemini_reasoning_model: str = Field(default="", validation_alias="GEMINI_REASONING_MODEL")
    max_upload_mb: int = Field(default=100, validation_alias="FACTUALIZE_MAX_UPLOAD_MB")
    max_pages: int = Field(default=1000, validation_alias="FACTUALIZE_MAX_PAGES")
    gemini_min_interval_seconds: float = Field(
        default=4.0, validation_alias="FACTUALIZE_GEMINI_MIN_INTERVAL_SECONDS"
    )
    gemini_max_automatic_attempts: int = Field(
        default=2, validation_alias="FACTUALIZE_GEMINI_MAX_AUTOMATIC_ATTEMPTS"
    )
    gemini_max_manual_attempts: int = Field(
        default=1, validation_alias="FACTUALIZE_GEMINI_MAX_MANUAL_ATTEMPTS"
    )
    entity_merge_candidate_budget: int = Field(
        default=100, validation_alias="FACTUALIZE_ENTITY_MERGE_CANDIDATE_BUDGET"
    )
    relationship_gemini_call_budget: int = Field(
        default=8, validation_alias="FACTUALIZE_RELATIONSHIP_GEMINI_CALL_BUDGET"
    )
    gemini_daily_request_budget: int = Field(default=15, ge=1, validation_alias="FACTUALIZE_GEMINI_DAILY_REQUEST_BUDGET")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "factualize.sqlite3"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def docling_models_dir(self) -> Path:
        """Local, copy-based Docling artifacts for Windows-safe first use."""
        return self.cache_dir / "docling-models"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.documents_dir, self.cache_dir, self.renders_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def gemini_is_configured(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_extraction_model)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
