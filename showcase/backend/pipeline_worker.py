"""
Pipeline Worker — Phase 2 Event-Driven Pipeline
================================================
Long-running async process that subscribes to all pipeline channels and
routes events to the appropriate agent handler.

Run as:   python -m worker.pipeline_worker
Docker:   command: python -m worker.pipeline_worker
"""

import asyncio
import json
import logging
import signal
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agents.change_detection.detector import ChangeDetectionAgent
from agents.discovery.scraper import DiscoveryScraper, ScrapeTarget
from agents.validation.validator import ValidationAgent
from agents.vibe.tagger import VibeTagger
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.event_bus import EventBus
from app.core.events import AppChannel, PipelineChannel, make_event
from app.models.happy_hour import HappyHourVersion
from app.models.pipeline_event import PipelineEvent, PipelineStatus
from app.models.restaurant import Restaurant

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
)
logger = logging.getLogger("pipeline_worker")

MAX_RETRIES = 3

ALL_CHANNELS = [
    PipelineChannel.SCRAPE,
    PipelineChannel.VALIDATE,
    PipelineChannel.DB_WRITE,
    PipelineChannel.CHANGE_DETECT,
]

CHANNEL_STAGE_MAP = {
    PipelineChannel.SCRAPE: "scrape",
    PipelineChannel.VALIDATE: "validate",
    PipelineChannel.DB_WRITE: "db_write",
    PipelineChannel.CHANGE_DETECT: "change_detect",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _is_duplicate(session, event_id: str) -> bool:
    """True if this event_id was already successfully processed (idempotency guard)."""
    result = await session.execute(
        select(PipelineEvent).where(
            PipelineEvent.event_id == event_id,
            PipelineEvent.status == PipelineStatus.completed,
        )
    )
    return result.scalar_one_or_none() is not None


async def _upsert_event(
    session,
    event: dict,
    stage: str,
    status: PipelineStatus,
    error: str | None = None,
    retry_count: int = 0,
) -> None:
    """Insert or update a PipelineEvent row. Does NOT commit — caller is responsible."""
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(PipelineEvent)
        .values(
            id=uuid.uuid4(),
            event_id=event["event_id"],
            correlation_id=event["correlation_id"],
            event_type=event["event_type"],
            restaurant_id=event["payload"].get("restaurant_id"),
            stage=stage,
            status=status,
            error=error,
            retry_count=retry_count,
            payload=event["payload"],
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["event_id"],
            set_={
                "status": status,
                "error": error,
                "retry_count": retry_count,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)


async def _fail_or_retry(
    event_bus: EventBus,
    channel: str,
    event: dict,
    stage: str,
    error: str,
) -> None:
    """Record a failure. Re-publish with a new event_id if under retry limit,
    otherwise mark as dead_letter in the DB."""
    retry_count = event.get("retry_count", 0)
    next_retry = retry_count + 1
    is_dead = next_retry >= MAX_RETRIES

    async with async_session_factory() as session:
        await _upsert_event(
            session,
            event,
            stage,
            PipelineStatus.dead_letter if is_dead else PipelineStatus.failed,
            error=error,
            retry_count=retry_count,
        )
        await session.commit()

    if is_dead:
        logger.error(
            "dead_letter stage=%s event_id=%s restaurant_id=%s error=%s",
            stage,
            event.get("event_id"),
            event["payload"].get("restaurant_id"),
            error,
        )
        return

    retry_event = make_event(
        event_type=event["event_type"],
        payload=event["payload"],
        correlation_id=event["correlation_id"],
    )
    retry_event["retry_count"] = next_retry
    await event_bus.publish(channel, retry_event)
    logger.warning(
        "retry_scheduled stage=%s retry=%d/%d event_id=%s",
        stage,
        next_retry,
        MAX_RETRIES,
        event.get("event_id"),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_scrape(event: dict, event_bus: EventBus) -> None:
    stage = "scrape"
    restaurant_id = event["payload"].get("restaurant_id")
    correlation_id = event["correlation_id"]

    async with async_session_factory() as session:
        if await _is_duplicate(session, event["event_id"]):
            logger.info("duplicate_skipped event_id=%s", event["event_id"])
            return

        restaurant = (
            await session.execute(
                select(Restaurant).where(Restaurant.id == uuid.UUID(restaurant_id))
            )
        ).scalar_one_or_none()

        if restaurant is None:
            await _upsert_event(
                session, event, stage, PipelineStatus.dead_letter,
                error=f"Restaurant {restaurant_id} not found",
            )
            await session.commit()
            return

        target = ScrapeTarget(
            restaurant_id=restaurant_id,
            website_url=restaurant.website,
            yelp_url=restaurant.yelp_url,
            instagram_handle=restaurant.instagram_handle,
        )

    # Scraping is slow I/O — run outside the DB session
    try:
        result = await DiscoveryScraper().run(target=target)
    except Exception as exc:
        logger.exception("scrape_error restaurant_id=%s", restaurant_id)
        await _fail_or_retry(event_bus, PipelineChannel.SCRAPE, event, stage, str(exc))
        return

    if not result.success or not result.data.get("extractions"):
        error = "; ".join(result.errors) or "No extractions returned"
        await _fail_or_retry(event_bus, PipelineChannel.SCRAPE, event, stage, error)
        return

    async with async_session_factory() as session:
        await _upsert_event(session, event, stage, PipelineStatus.completed)
        await session.commit()

    next_event = make_event(
        event_type="scrape_completed",
        payload={
            "restaurant_id": restaurant_id,
            "extractions": result.data["extractions"],
        },
        correlation_id=correlation_id,
    )
    await event_bus.publish(PipelineChannel.VALIDATE, next_event)
    logger.info("scrape_completed restaurant_id=%s correlation_id=%s", restaurant_id, correlation_id)


async def handle_validate(event: dict, event_bus: EventBus) -> None:
    stage = "validate"
    payload = event["payload"]
    restaurant_id = payload.get("restaurant_id")
    correlation_id = event["correlation_id"]
    extractions = payload.get("extractions", [])

    async with async_session_factory() as session:
        if await _is_duplicate(session, event["event_id"]):
            return

    try:
        val_result = await ValidationAgent().run(extractions=extractions)
        if not val_result.success:
            error = "; ".join(val_result.errors)
            await _fail_or_retry(event_bus, PipelineChannel.VALIDATE, event, stage, error)
            return

        canonical = val_result.data.get("canonical", extractions[0])
        confidence = val_result.data.get("confidence_score", 0.0)

        vibe_result = await VibeTagger().run(text=canonical.get("deals_text", ""))
        vibe_tags = vibe_result.data.get("vibe_tags", []) if vibe_result.success else []

    except Exception as exc:
        logger.exception("validate_error restaurant_id=%s", restaurant_id)
        await _fail_or_retry(event_bus, PipelineChannel.VALIDATE, event, stage, str(exc))
        return

    async with async_session_factory() as session:
        await _upsert_event(session, event, stage, PipelineStatus.completed)
        await session.commit()

    next_event = make_event(
        event_type="validation_completed",
        payload={
            "restaurant_id": restaurant_id,
            "canonical": canonical,
            "confidence_score": confidence,
            "vibe_tags": vibe_tags,
        },
        correlation_id=correlation_id,
    )
    await event_bus.publish(PipelineChannel.DB_WRITE, next_event)
    logger.info(
        "validation_completed restaurant_id=%s confidence=%.2f", restaurant_id, confidence
    )


async def handle_db_write(event: dict, event_bus: EventBus) -> None:
    stage = "db_write"
    payload = event["payload"]
    restaurant_id = payload["restaurant_id"]
    correlation_id = event["correlation_id"]
    canonical = payload["canonical"]
    confidence = payload["confidence_score"]
    vibe_tags = payload.get("vibe_tags", [])

    async with async_session_factory() as session:
        if await _is_duplicate(session, event["event_id"]):
            return

        try:
            old_row = (
                await session.execute(
                    select(HappyHourVersion).where(
                        HappyHourVersion.restaurant_id == uuid.UUID(restaurant_id),
                        HappyHourVersion.is_current == True,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()

            old_version_dict = (
                {
                    "days": old_row.days,
                    "start_time": old_row.start_time,
                    "end_time": old_row.end_time,
                    "deals": old_row.deals,
                }
                if old_row
                else None
            )

            await session.execute(
                update(HappyHourVersion)
                .where(
                    HappyHourVersion.restaurant_id == uuid.UUID(restaurant_id),
                    HappyHourVersion.is_current == True,  # noqa: E712
                )
                .values(is_current=False)
            )

            if vibe_tags:
                restaurant = (
                    await session.execute(
                        select(Restaurant).where(Restaurant.id == uuid.UUID(restaurant_id))
                    )
                ).scalar_one_or_none()
                if restaurant:
                    restaurant.vibe_tags = vibe_tags

            new_version = HappyHourVersion(
                restaurant_id=uuid.UUID(restaurant_id),
                source=canonical.get("source", "website"),
                days=canonical.get("days", []),
                start_time=canonical.get("start_time"),
                end_time=canonical.get("end_time"),
                deals=canonical.get("deals_text"),
                structured_deals=canonical.get("structured_deals"),
                confidence_score=confidence,
                is_current=True,
            )
            session.add(new_version)

            # Record completion atomically with the business writes so that on
            # a crash-and-retry the dedup check prevents a duplicate DB write.
            await _upsert_event(session, event, stage, PipelineStatus.completed)
            await session.commit()

        except Exception as exc:
            await session.rollback()
            logger.exception("db_write_error restaurant_id=%s", restaurant_id)
            await _fail_or_retry(event_bus, PipelineChannel.DB_WRITE, event, stage, str(exc))
            return

    next_event = make_event(
        event_type="db_updated",
        payload={
            "restaurant_id": restaurant_id,
            "new_version_id": str(new_version.id),
            "old_version": old_version_dict,
            "new_version": {
                "days": new_version.days,
                "start_time": new_version.start_time,
                "end_time": new_version.end_time,
                "deals": new_version.deals,
            },
        },
        correlation_id=correlation_id,
    )
    await event_bus.publish(PipelineChannel.CHANGE_DETECT, next_event)
    logger.info("db_updated restaurant_id=%s version_id=%s", restaurant_id, new_version.id)


async def handle_change_detect(event: dict, event_bus: EventBus) -> None:
    stage = "change_detect"
    payload = event["payload"]
    restaurant_id = payload["restaurant_id"]
    correlation_id = event["correlation_id"]
    old_version = payload.get("old_version")
    new_version = payload["new_version"]

    async with async_session_factory() as session:
        if await _is_duplicate(session, event["event_id"]):
            return

    if old_version is None:
        # First scrape for this restaurant — nothing to diff
        async with async_session_factory() as session:
            await _upsert_event(session, event, stage, PipelineStatus.completed)
            await session.commit()
        logger.info("change_detect_skipped (first scrape) restaurant_id=%s", restaurant_id)
        return

    try:
        result = await ChangeDetectionAgent().run(
            old_version=old_version, new_version=new_version
        )
    except Exception as exc:
        logger.exception("change_detect_error restaurant_id=%s", restaurant_id)
        await _fail_or_retry(event_bus, PipelineChannel.CHANGE_DETECT, event, stage, str(exc))
        return

    async with async_session_factory() as session:
        await _upsert_event(session, event, stage, PipelineStatus.completed)
        await session.commit()

    if result.success and result.data.get("has_changes"):
        summary = result.data.get("summary", [])
        changed_event = make_event(
            event_type="happy_hour_changed",
            payload={"restaurant_id": restaurant_id, "diff_summary": summary},
            correlation_id=correlation_id,
        )
        await event_bus.publish(AppChannel.CHANGES, changed_event)
        logger.info("happy_hour_changed published restaurant_id=%s", restaurant_id)

    logger.info("change_detect_completed restaurant_id=%s", restaurant_id)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

HANDLERS = {
    PipelineChannel.SCRAPE: handle_scrape,
    PipelineChannel.VALIDATE: handle_validate,
    PipelineChannel.DB_WRITE: handle_db_write,
    PipelineChannel.CHANGE_DETECT: handle_change_detect,
}


async def main() -> None:
    logger.info("pipeline_worker_starting channels=%s", ALL_CHANNELS)
    event_bus = EventBus(settings.REDIS_URL)
    pubsub = await event_bus.subscribe(*ALL_CHANNELS)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        async for message in pubsub.listen():
            if stop.is_set():
                break
            if message["type"] != "message":
                continue

            channel = message["channel"]
            handler = HANDLERS.get(channel)
            if handler is None:
                logger.warning("no_handler_for_channel channel=%s", channel)
                continue

            try:
                event = json.loads(message["data"])
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error("malformed_message channel=%s error=%s", channel, exc)
                continue

            logger.info(
                "event_received event_type=%s event_id=%s channel=%s correlation_id=%s",
                event.get("event_type"),
                event.get("event_id"),
                channel,
                event.get("correlation_id"),
            )

            # Run handler as a Task so the listen loop stays unblocked
            asyncio.create_task(handler(event, event_bus))

    finally:
        await pubsub.unsubscribe(*ALL_CHANNELS)
        await event_bus.close()
        logger.info("pipeline_worker_stopped")


if __name__ == "__main__":
    asyncio.run(main())
