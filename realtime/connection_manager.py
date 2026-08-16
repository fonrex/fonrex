"""WebSocket client groups for realtime price broadcasting."""

from collections.abc import Mapping

from starlette.websockets import WebSocketDisconnect

from technical.contracts import WebSocketPort


class ConnectionManager:
    """Manage active WebSocket clients grouped by ticker."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocketPort]] = {}

    async def connect(self, ticker: str, websocket: WebSocketPort) -> None:
        await websocket.accept()
        self._connections.setdefault(ticker, set()).add(websocket)

    def disconnect(self, ticker: str, websocket: WebSocketPort) -> None:
        group = self._connections.get(ticker)
        if group:
            group.discard(websocket)
            if not group:
                del self._connections[ticker]

    async def broadcast(self, ticker: str, message: Mapping[str, object]) -> None:
        group = self._connections.get(ticker)
        if not group:
            return
        dead_connections: set[WebSocketPort] = set()
        for websocket in list(group):
            try:
                await websocket.send_json(message)
            except (ConnectionError, RuntimeError, WebSocketDisconnect):
                dead_connections.add(websocket)
        for websocket in dead_connections:
            self.disconnect(ticker, websocket)

    async def broadcast_ping(self, ticker: str) -> None:
        await self.broadcast(ticker, {"type": "ping"})

    def get_subscriber_count(self, ticker: str) -> int:
        return len(self._connections.get(ticker, set()))

    def all_tickers(self) -> list[str]:
        return list(self._connections.keys())
