import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, redis_url: str) -> None:
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def publish(self, channel: str, event: dict[str, Any]) -> None:
        await self._redis.publish(channel, json.dumps(event))
        logger.info(
            "event_published channel=%s event_type=%s event_id=%s correlation_id=%s",
            channel,
            event.get("event_type"),
            event.get("event_id"),
            event.get("correlation_id"),
        )

    async def subscribe(self, *channels: str) -> aioredis.client.PubSub:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        return pubsub

    async def close(self) -> None:
        await self._redis.aclose()


# Lazy module-level singleton — one connection shared across FastAPI and the
# pipeline worker. Call get_event_bus() to access it.
_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus(settings.REDIS_URL)
    return _bus
