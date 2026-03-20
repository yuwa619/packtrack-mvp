from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    project_name: str = "PackTrack MVP"
    environment: str = "local"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = "postgresql+psycopg://packtrack:packtrack@postgres:5432/packtrack"

    @property
    def resolved_database_url(self) -> str:
        """Normalize Render/Heroku-style URLs to use the psycopg (v3) driver."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+psycopg://", 1)
        return url
    redis_url: str = "redis://redis:6379/0"
    minio_internal_endpoint: str | None = None
    minio_public_endpoint: str = "http://localhost:9000"
    minio_endpoint: str | None = None  # legacy fallback for older env files
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_region: str = "us-east-1"
    minio_bucket_raw: str = "raw-uploads"
    minio_bucket_preprocessed: str = "preprocessed"
    minio_bucket_reports: str = "reports"
    minio_force_local: bool = False
    minio_allow_local_fallback: bool = True
    minio_fallback_dir: str = "data/minio_stub"

    ocr_confidence_threshold: float = 0.70
    classification_confidence_threshold: float = 0.85
    max_upload_size_bytes: int = 50 * 1024 * 1024
    zip_max_file_count: int = 200
    zip_max_total_uncompressed_bytes: int = 250 * 1024 * 1024
    upload_url_expiry_seconds: int = 900
    processing_queue_name: str = "packtrack:queue:preprocess"
    pipeline_stage_max_attempts: int = 3
    enable_demo_endpoints: bool = False
    cors_origins: str = "http://localhost:5173"
    ner_enabled: bool = False
    ner_model_path: str = "data/models/spacy_ner/latest/model-best"
    ner_registry_path: str = "data/models/spacy_ner/latest.json"
    ner_min_overall_f1: float = 0.60
    ner_min_invoice_ref_f1: float = 0.90

    @property
    def resolved_minio_internal_endpoint(self) -> str:
        return self.minio_internal_endpoint or self.minio_endpoint or "http://minio:9000"


settings = Settings()
