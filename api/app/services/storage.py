from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from minio import Minio

from ..config import settings


class ObjectStorage:
    def __init__(self) -> None:
        self._local_root = Path(settings.minio_fallback_dir)
        internal_endpoint, internal_secure = self._parse_endpoint(
            settings.resolved_minio_internal_endpoint
        )
        public_endpoint, public_secure = self._parse_endpoint(settings.minio_public_endpoint)
        self._client = Minio(
            internal_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=internal_secure,
            region=settings.minio_region,
        )
        self._presign_client = Minio(
            public_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=public_secure,
            region=settings.minio_region,
        )

    @staticmethod
    def build_uri(bucket: str, key: str) -> str:
        return f"minio://{bucket}/{key}"

    @staticmethod
    def parse_uri(uri: str) -> tuple[str, str]:
        prefix = "minio://"
        if not uri.startswith(prefix):
            raise ValueError(f"Unsupported storage URI: {uri}")
        bucket_and_key = uri[len(prefix) :]
        bucket, key = bucket_and_key.split("/", 1)
        return bucket, key

    def create_presigned_put_url(self, *, bucket: str, key: str, expires_seconds: int) -> str:
        if settings.minio_force_local:
            return self._build_local_upload_url(bucket=bucket, key=key)

        try:
            self._ensure_bucket_remote(bucket)
            return self._presign_client.presigned_put_object(
                bucket_name=bucket,
                object_name=key,
                expires=timedelta(seconds=expires_seconds),
            )
        except Exception:
            if not settings.minio_allow_local_fallback:
                raise
            return self._build_local_upload_url(bucket=bucket, key=key)

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> str:
        if settings.minio_force_local:
            return self._put_bytes_local(bucket, key, data)

        try:
            self._ensure_bucket_remote(bucket)
            stream = BytesIO(data)
            self._client.put_object(
                bucket_name=bucket,
                object_name=key,
                data=stream,
                length=len(data),
                content_type=content_type,
            )
            return self.build_uri(bucket, key)
        except Exception:
            if not settings.minio_allow_local_fallback:
                raise
            return self._put_bytes_local(bucket, key, data)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        if settings.minio_force_local:
            return self._get_bytes_local(bucket, key)

        try:
            response = self._client.get_object(bucket_name=bucket, object_name=key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()
        except Exception:
            if not settings.minio_allow_local_fallback:
                raise
            return self._get_bytes_local(bucket, key)

    def _ensure_bucket_remote(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    def _build_local_upload_url(self, *, bucket: str, key: str) -> str:
        path = (self._local_root / bucket / key).as_posix()
        return f"local://{path}"

    def _put_bytes_local(self, bucket: str, key: str, data: bytes) -> str:
        path = self._local_root / bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.build_uri(bucket, key)

    def _get_bytes_local(self, bucket: str, key: str) -> bytes:
        path = self._local_root / bucket / key
        return path.read_bytes()

    @staticmethod
    def _parse_endpoint(value: str) -> tuple[str, bool]:
        if "://" in value:
            parsed = urlparse(value)
            if parsed.hostname is None:
                raise ValueError(f"Invalid MinIO endpoint: {value}")
            endpoint = parsed.hostname
            if parsed.port is not None:
                endpoint = f"{endpoint}:{parsed.port}"
            return endpoint, parsed.scheme.lower() == "https"

        return value, settings.minio_secure
