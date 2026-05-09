import asyncio
import logging
import uuid

from sqlalchemy import select, update

from worker.celery_app import celery_app
from worker.db import get_sync_session

logger = logging.getLogger(__name__)


@celery_app.task(name="worker.tasks.scrape_restaurant", bind=True)
def scrape_restaurant(self, restaurant_id: str) -> dict:
    """Scrape a single restaurant's happy hour info and persist results."""
    from agents.change_detection.detector import ChangeDetectionAgent
    from agents.discovery.scraper import DiscoveryScraper, ScrapeTarget
    from agents.validation.validator import ValidationAgent
    from agents.vibe.tagger import VibeTagger
    from app.models.happy_hour import HappyHourVersion
    from app.models.restaurant import Restaurant

    session = get_sync_session()
    try:
        # Look up the restaurant
        result = session.execute(
            select(Restaurant).where(Restaurant.id == uuid.UUID(restaurant_id))
        )
        restaurant = result.scalar_one_or_none()
        if restaurant is None:
            return {"status": "error", "message": f"Restaurant {restaurant_id} not found"}

        # Build scrape target
        target = ScrapeTarget(
            restaurant_id=str(restaurant.id),
            website_url=restaurant.website,
            yelp_url=restaurant.yelp_url,
            instagram_handle=restaurant.instagram_handle,
        )

        # Run the async scraper from sync Celery context
        scraper = DiscoveryScraper()
        scrape_result = asyncio.run(scraper.run(target=target))

        if not scrape_result.success:
            logger.warning(
                "Scrape failed for restaurant %s: %s",
                restaurant_id,
                scrape_result.errors,
            )
            return {
                "status": "error",
                "restaurant_id": restaurant_id,
                "errors": scrape_result.errors,
            }

        extractions = scrape_result.data.get("extractions", [])
        if not extractions:
            return {
                "status": "no_data",
                "restaurant_id": restaurant_id,
                "message": "Scrape succeeded but no extractions found",
            }

        # Run validation
        validator = ValidationAgent()
        validation_result = asyncio.run(validator.run(extractions=extractions))

        if not validation_result.success:
            return {
                "status": "validation_failed",
                "restaurant_id": restaurant_id,
                "errors": validation_result.errors,
            }

        canonical = validation_result.data.get("canonical", extractions[0])
        confidence = validation_result.data.get("confidence_score", 0.0)

        # Run vibe tagger on the deals text and update the restaurant
        deals_text = canonical.get("deals_text", "")
        vibe_tagger = VibeTagger()
        vibe_result = asyncio.run(vibe_tagger.run(text=deals_text))
        if vibe_result.success:
            vibe_tags = vibe_result.data.get("vibe_tags", [])
            if vibe_tags:
                restaurant.vibe_tags = vibe_tags
                logger.info("Vibe tags for restaurant %s: %s", restaurant_id, vibe_tags)

        # Fetch the current version for change detection before replacing it
        old_version_result = session.execute(
            select(HappyHourVersion).where(
                HappyHourVersion.restaurant_id == uuid.UUID(restaurant_id),
                HappyHourVersion.is_current == True,  # noqa: E712
            )
        )
        old_version = old_version_result.scalar_one_or_none()
        old_version_dict = (
            {
                "days": old_version.days,
                "start_time": old_version.start_time,
                "end_time": old_version.end_time,
                "deals": old_version.deals,
            }
            if old_version
            else None
        )

        # Mark previous versions as not current
        session.execute(
            update(HappyHourVersion)
            .where(
                HappyHourVersion.restaurant_id == uuid.UUID(restaurant_id),
                HappyHourVersion.is_current == True,  # noqa: E712
            )
            .values(is_current=False)
        )

        # Create new version
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
        session.commit()

        # Run change detection between old and new version
        change_summary = []
        if old_version_dict is not None:
            new_version_dict = {
                "days": new_version.days,
                "start_time": new_version.start_time,
                "end_time": new_version.end_time,
                "deals": new_version.deals,
            }
            change_detector = ChangeDetectionAgent()
            change_result = asyncio.run(
                change_detector.run(old_version=old_version_dict, new_version=new_version_dict)
            )
            if change_result.success and change_result.data.get("has_changes"):
                change_summary = change_result.data.get("summary", [])
                logger.info(
                    "Changes detected for restaurant %s: %s", restaurant_id, change_summary
                )

        logger.info(
            "Scrape complete for restaurant %s — confidence=%.2f",
            restaurant_id,
            confidence,
        )
        return {
            "status": "success",
            "restaurant_id": restaurant_id,
            "version_id": str(new_version.id),
            "confidence_score": confidence,
            "vibe_tags": restaurant.vibe_tags,
            "changes": change_summary,
        }

    except Exception:
        session.rollback()
        logger.exception("Error scraping restaurant %s", restaurant_id)
        raise
    finally:
        session.close()


@celery_app.task(name="worker.tasks.monthly_rescrape_all")
def monthly_rescrape_all() -> dict:
    """Re-scrape all restaurants: publishes scrape_triggered events to the pipeline."""
    import json

    import redis as sync_redis

    from app.core.config import settings
    from app.core.events import PipelineChannel, make_event
    from app.models.restaurant import Restaurant

    session = get_sync_session()
    try:
        result = session.execute(select(Restaurant.id))
        restaurant_ids = [str(row[0]) for row in result.all()]
    finally:
        session.close()

    r = sync_redis.from_url(settings.REDIS_URL)
    try:
        for rid in restaurant_ids:
            event = make_event("scrape_triggered", {"restaurant_id": rid})
            r.publish(PipelineChannel.SCRAPE, json.dumps(event))
    finally:
        r.close()

    logger.info("Published scrape_triggered for %d restaurants", len(restaurant_ids))
    return {"status": "published", "count": len(restaurant_ids)}


@celery_app.task(name="worker.tasks.validate")
def validate(restaurant_id: str, extractions: list[dict]) -> dict:
    """Validate extracted data and compute confidence scores."""
    from agents.validation.validator import ValidationAgent
    from app.models.happy_hour import HappyHourVersion

    validator = ValidationAgent()
    validation_result = asyncio.run(validator.run(extractions=extractions))

    if not validation_result.success:
        return {
            "status": "validation_failed",
            "restaurant_id": restaurant_id,
            "errors": validation_result.errors,
        }

    confidence = validation_result.data.get("confidence_score", 0.0)

    # Update the current version's confidence score if one exists
    session = get_sync_session()
    try:
        result = session.execute(
            select(HappyHourVersion).where(
                HappyHourVersion.restaurant_id == uuid.UUID(restaurant_id),
                HappyHourVersion.is_current == True,  # noqa: E712
            )
        )
        current_version = result.scalar_one_or_none()
        if current_version:
            current_version.confidence_score = confidence
            session.commit()

        return {
            "status": "success",
            "restaurant_id": restaurant_id,
            "confidence_score": confidence,
            "validation_errors": validation_result.data.get("validation_errors", []),
        }
    finally:
        session.close()
