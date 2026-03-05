from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


def log_json(
    logger: logging.Logger,
    *,
    level: str,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "event": event,
        "message": message,
        **fields,
    }
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(json.dumps(payload, default=str, sort_keys=True))
