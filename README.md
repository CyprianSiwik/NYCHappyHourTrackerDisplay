# Happy Hour Tracker NYC

**Real-time iOS app that surfaces active NYC happy hours based on the user's current location.**

![Python 3.11](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![Swift](https://img.shields.io/badge/Swift-5.9-FA7343?logo=swift&logoColor=white)
![SwiftUI](https://img.shields.io/badge/SwiftUI-iOS%2017%2B-1a7bef?logo=apple&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20PostGIS-336791?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-5-37814A?logo=celery&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

## Overview

Happy Hour Tracker NYC scrapes happy hour data from restaurant websites, Yelp, and Instagram, validates and scores it against multiple sources, then pushes real-time change notifications to iOS clients via WebSocket. The app uses CoreLocation to query a PostGIS-backed radius search, showing only venues that are actively running a happy hour within walking distance of the user right now.

The backend is fully event-driven: a Redis Pub/Sub pipeline moves data through discrete stages (Discovery → Validation → DB Write → Change Detection), each independently subscribing and publishing without shared state. When a change is detected, the pipeline publishes to an app channel that bridges into the WebSocket hub, delivering a targeted push to every connected iOS client.

## Architecture

```mermaid
flowchart TD
    subgraph iOS ["iOS App (SwiftUI / Combine)"]
        LM[LocationManager]
        EB_iOS[EventBus\nPassthroughSubject]
        LVM[ListViewModel]
        WSM[WebSocketManager]
        LM -->|locationUpdated event| EB_iOS
        EB_iOS -->|filtersChanged / appForegrounded| LVM
        EB_iOS -->|appForegrounded / appBackgrounded| WSM
    end

    subgraph API ["FastAPI (Python 3.11)"]
        REST[REST Endpoints\n/restaurants, /happy-hours]
        WS[WebSocket Hub\n/ws]
    end

    subgraph DB ["PostgreSQL 16 + PostGIS"]
        PG[(Restaurants\nHappyHourVersions\nPipelineEvents)]
    end

    subgraph Pipeline ["Redis Pub/Sub Pipeline"]
        CH_SCRAPE[channel: pipeline.scrape]
        CH_VALIDATE[channel: pipeline.validate]
        CH_DBWRITE[channel: pipeline.db_write]
        CH_CHANGE[channel: pipeline.change_detect]
        CH_APP[channel: app.changes]

        CH_SCRAPE -->|scrape_completed| CH_VALIDATE
        CH_VALIDATE -->|validation_completed| CH_DBWRITE
        CH_DBWRITE -->|db_updated| CH_CHANGE
        CH_CHANGE -->|happy_hour_changed| CH_APP
    end

    subgraph Workers ["Background Workers"]
        CW[Celery Worker\nscrape_restaurant task]
        CB[Celery Beat\nmonthly_rescrape_all]
        PW[Pipeline Worker\nasync event loop]
    end

    subgraph Notifications ["Push Notifications"]
        APNS[APNs\n"starting soon" alerts]
    end

    LVM -->|HTTP GET| REST
    REST --> PG
    WSM <-->|WebSocket| WS
    CH_APP -->|bridge| WS
    WS -->|broadcast| WSM
    CB -->|publishes scrape_triggered| CH_SCRAPE
    CW --> CH_SCRAPE
    PW -->|subscribes all channels| Pipeline
    PW --> PG
    CB -->|schedules APNs task| APNS
    APNS -->|"starting soon" push| iOS
```

## Tech Stack

| Layer | Technologies |
|---|---|
| iOS Client | Swift 5.9, SwiftUI (iOS 17+), Combine, CoreLocation, URLSessionWebSocketTask |
| API Layer | Python 3.11, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2 |
| Database | PostgreSQL 16 with PostGIS 3.4 — geospatial radius queries via `ST_DWithin` |
| Cache / Broker | Redis 7 — Pub/Sub event bus + Celery message broker |
| Background Workers | Celery 5 (task execution), Celery Beat (cron scheduling), async pipeline worker |
| Real-time | WebSocket hub (FastAPI) bridged from Redis `app.changes` channel |
| Notifications | Apple Push Notification service (APNs) for "starting soon" alerts |
| Containerization | Docker Compose — postgres, redis, api, celery-worker, celery-beat, pipeline-worker |

## Key Engineering Highlights

- **iOS Combine EventBus** — A `PassthroughSubject`-backed singleton decouples all producers (LocationManager, AppLifecycleMonitor, FilterSheet) from consumers (ListViewModel, WebSocketManager). No component holds a direct reference to another; communication is entirely through typed `AppEvent` cases.

- **Event-driven backend pipeline via Redis Pub/Sub** — Each pipeline stage (Discovery, Validation, DB Write, Change Detection) runs as an independent subscriber that publishes to the next channel on success. Stages have no shared in-process state, enabling horizontal scaling and clean retry/dead-letter handling per event.

- **PostGIS geospatial queries** — Happy hour lookups use `ST_DWithin` on a geography column for efficient radius filtering. Indexes on the geography column keep per-request query time under 10 ms even across thousands of venue rows.

- **Multi-source confidence scoring with recency decay** — Each scraped version receives a confidence score weighted across source count, cross-source agreement, field completeness (times, days, prices), and a linear recency decay over 90 days. The score gates which version is surfaced to users. Algorithm is proprietary.

- **WebSocket hub with per-connection lifecycle management** — The `WebSocketHub` singleton maintains all active connections and handles dead-socket cleanup on broadcast. It is fed directly by a Redis subscriber bridging the `app.changes` channel, so pipeline events reach connected iOS clients within one Redis round-trip of a DB write.

- **APNs "starting soon" notifications via Celery Beat** — A scheduled Celery Beat task queries for happy hours starting within a configurable window and dispatches APNs payloads, giving users ambient awareness without requiring an active app session.

## Showcase Files

| File | Pattern demonstrated |
|---|---|
| `showcase/ios/EventBus.swift` | Combine `PassthroughSubject` singleton for fully decoupled in-process pub/sub |
| `showcase/ios/WebSocketManager.swift` | `@MainActor` WebSocket lifecycle — connect/disconnect on app lifecycle events, exponential backoff reconnect loop |
| `showcase/ios/LocationManager.swift` | `CLLocationManagerDelegate` bridged into Combine via EventBus emissions |
| `showcase/ios/ListViewModel.swift` | `@MainActor` MVVM + EventBus subscription for filter changes and stale-data foreground refresh |
| `showcase/backend/event_bus.py` | Async Redis Pub/Sub wrapper with structured structured logging and lazy singleton |
| `showcase/backend/ws.py` | FastAPI WebSocket hub — connection registry, broadcast with dead-socket cleanup |
| `showcase/backend/pipeline_worker.py` | Long-running async event loop routing pipeline channels to handlers with idempotency guards, retry logic, and dead-letter tracking |
| `showcase/backend/scoring.py` | Confidence scoring interface — input shape documented, proprietary implementation omitted |
| `showcase/infra/docker-compose.yml` | Full local stack: PostGIS, Redis, FastAPI, Celery worker, Celery Beat, pipeline worker with health-check dependencies |
| `showcase/infra/tasks.py` | Celery task definitions — sync-to-async bridge, multi-agent orchestration, versioned DB writes |

---

*Core scraping, parsing, and scoring logic is proprietary and omitted from this showcase.*
