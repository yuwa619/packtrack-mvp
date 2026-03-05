from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    project_name: str = "PackTrack MVP"
    environment: str = "local"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    database_url: str = "postgresql+psycopg://packtrack:packtrack@postgres:5432/packtrack"
    redis_url: str = "redis://redis:6379/0"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket_raw: str = "raw-uploads"
    minio_bucket_preprocessed: str = "preprocessed"
    minio_bucket_reports: str = "reports"
    minio_force_local: bool = False
    minio_allow_local_fallback: bool = True
    minio_fallback_dir: str = "data/minio_stub"

    ocr_confidence_threshold: float = 0.70
    classification_confidence_threshold: float = 0.85
    max_upload_size_bytes: int = 50 * 1024 * 1024
    upload_url_expiry_seconds: int = 900
    processing_queue_name: str = "packtrack:queue:preprocess"
    pipeline_stage_max_attempts: int = 3
    enable_demo_endpoints: bool = False


settings = Settings()
