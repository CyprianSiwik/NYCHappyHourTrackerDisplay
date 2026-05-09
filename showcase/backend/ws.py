import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class WebSocketHub:
    """Manages all active WebSocket connections and broadcasts messages to them."""

    def __init__(self) -> None:
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)
        logger.info("ws_connected total=%d", len(self.connections))

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self.connections.remove(ws)
        except ValueError:
            pass  # already removed
        logger.info("ws_disconnected total=%d", len(self.connections))

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# Module-level singleton — shared between the WS endpoint and the Redis bridge
hub = WebSocketHub()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Unauthenticated WebSocket endpoint for real-time push to iOS clients.

    Intentionally unauthenticated for MVP per the EDA PRD. Post-MVP: pass a
    short-lived JWT as a query param on connect.

    Clients should send periodic pings (any text) to keep the connection alive.
    The server echoes nothing — it only pushes data via broadcast().
    """
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive / ping drain
    except WebSocketDisconnect:
        hub.disconnect(ws)
