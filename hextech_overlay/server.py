"""FastAPI HTTP & WebSocket server for OBS Browser Source and streamer control panel."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel

from .engine import OverlayEngine

STATIC_DIR = Path(__file__).resolve().parent / "static"

FALLBACK_SVG_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">'
    '<polygon points="32,4 58,20 58,44 32,60 6,44 6,20" stroke="#ffffff" stroke-width="4" fill="#ffffff" fill-opacity="0.16"/>'
    '<polygon points="32,16 46,24 46,40 32,48 18,40 18,24" fill="#ffffff"/>'
    "</svg>"
).encode("utf-8")


class AddPickRequest(BaseModel):
    augment_id: int
    level: int | None = None


class ReplacePickRequest(BaseModel):
    uid: str
    augment_id: int


class DemoStartRequest(BaseModel):
    auto_play: bool = True


class WsHub:
    """Manages active WebSocket subscribers and schedules thread-safe broadcasts."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def broadcast_json(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def schedule_broadcast(self, engine: OverlayEngine) -> None:
        if self.loop is None or self.loop.is_closed():
            return

        def _dispatch() -> None:
            state, cfg = engine.snapshot()
            payload = {
                "type": "snapshot",
                "state": state.model_dump(),
                "config": cfg.model_dump(),
            }
            asyncio.create_task(self.broadcast_json(payload))

        try:
            self.loop.call_soon_threadsafe(_dispatch)
        except RuntimeError:
            pass


def create_app(engine: OverlayEngine) -> FastAPI:
    hub = WsHub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.bind_loop(asyncio.get_running_loop())
        engine.set_on_change(lambda: hub.schedule_broadcast(engine))
        engine.start()
        try:
            yield
        finally:
            engine.set_on_change(None)
            engine.stop()

    app = FastAPI(title="Hextech ARAM Live Overlay", lifespan=lifespan)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/control")

    @app.get("/overlay")
    async def overlay_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "overlay.html")

    @app.get("/control")
    async def control_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "control.html")

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        state, cfg = engine.snapshot()
        return {"state": state.model_dump(), "config": cfg.model_dump()}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        _, cfg = engine.snapshot()
        return cfg.model_dump()

    @app.post("/api/config")
    async def update_config(patch: dict[str, Any]) -> dict[str, Any]:
        cfg = engine.update_config(patch)
        return cfg.model_dump()

    @app.get("/api/augments")
    async def search_augments(
        q: str = "",
        rarity: str | None = None,
        only_mayhem: bool = False,
        limit: int = Query(default=30, ge=1, le=200),
    ) -> dict[str, Any]:
        items = engine.catalog.search(query=q, rarity=rarity, only_mayhem=only_mayhem, limit=limit)
        return {"items": [item.model_dump() for item in items]}

    @app.post("/api/picks/add")
    async def add_pick(req: AddPickRequest) -> dict[str, Any]:
        pick = engine.add_pick(req.augment_id, level=req.level, source="manual")
        if pick is None:
            raise HTTPException(status_code=404, detail="Augment not found")
        return {"pick": pick.model_dump()}

    @app.post("/api/picks/replace")
    async def replace_pick(req: ReplacePickRequest) -> dict[str, Any]:
        pick = engine.replace_pick(req.uid, req.augment_id)
        if pick is None:
            raise HTTPException(status_code=404, detail="Pick or augment not found")
        return {"pick": pick.model_dump()}

    @app.delete("/api/picks/{uid}")
    async def delete_pick(uid: str) -> dict[str, Any]:
        if not engine.remove_pick(uid):
            raise HTTPException(status_code=404, detail="Pick not found")
        return {"ok": True}

    @app.post("/api/picks/undo")
    async def undo_pick() -> dict[str, Any]:
        removed = engine.undo_pick()
        return {"ok": True, "removed": removed.model_dump() if removed else None}

    @app.post("/api/picks/clear")
    async def clear_picks() -> dict[str, Any]:
        engine.clear_picks()
        return {"ok": True}

    @app.post("/api/demo/start")
    async def demo_start(req: DemoStartRequest | None = None) -> dict[str, Any]:
        engine.start_demo(auto_play=req.auto_play if req else True)
        return {"ok": True}

    @app.post("/api/demo/next")
    async def demo_next() -> dict[str, Any]:
        finished = engine.next_demo_step()
        return {"ok": True, "finished": finished}

    @app.post("/api/demo/reset")
    async def demo_reset() -> dict[str, Any]:
        engine.reset_demo()
        return {"ok": True}

    @app.post("/api/catalog/refresh")
    async def refresh_catalog() -> dict[str, Any]:
        ok, message = await asyncio.to_thread(engine.refresh_catalog)
        return {"ok": ok, "message": message}

    @app.get("/api/icon/{icon_path:path}")
    async def get_icon(icon_path: str) -> Response:
        clean = icon_path.strip()
        if not clean or ".." in clean or "\\" in clean or "\0" in clean:
            raise HTTPException(status_code=400, detail="Invalid icon path")
        if clean == "fallback" or "/" not in clean:
            return Response(
                content=FALLBACK_SVG_ICON,
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=300"},
            )
        _, cfg = engine.snapshot()
        data = await asyncio.to_thread(
            engine.catalog.get_icon_bytes,
            clean,
            engine.lcu,
            cfg.proxy_url,
        )
        if data:
            return Response(
                content=data,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        return Response(
            content=FALLBACK_SVG_ICON,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        hub.clients.add(ws)
        try:
            state, cfg = engine.snapshot()
            await ws.send_json(
                {
                    "type": "snapshot",
                    "state": state.model_dump(),
                    "config": cfg.model_dump(),
                }
            )
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=25.0)
                    if msg == "ping":
                        await ws.send_text("pong")
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.clients.discard(ws)

    return app
