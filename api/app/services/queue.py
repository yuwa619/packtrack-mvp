from __future__ import annotations

import json
from typing import Any

from redis import Redis

from ..config import settings


class JobQueue:
    def __init__(self, redis_url: str | None = None) -> None:
        self._redis_url = redis_url or settings.redis_url
        self._redis = Redis.from_url(self._redis_url)

    def enqueue(self, queue_name: str, payload: dict[str, Any]) -> None:
        self._redis.rpush(queue_name, json.dumps(payload))
