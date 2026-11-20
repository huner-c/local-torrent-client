import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .models import FileSelection, MagnetRequest, Snapshot, SpeedLimits, TorrentFile
from .torrent_manager import TorrentError, TorrentManager


ASSET_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ASSET_ROOT
INDEX = ASSET_ROOT / "static" / "index.html"
MAX_TORRENT_SIZE = 64 * 1024 * 1024


def same_origin(headers, scheme: str) -> bool:
    origin = headers.get("origin")
    host = headers.get("host", "").lower()
    return origin is None or origin.lower() == f"{scheme}://{host}"


def create_app(root: Path = PROJECT_ROOT, network_settings: dict | None = None) -> FastAPI:
    manager = TorrentManager(root, network_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            manager.start()
            yield
        finally:
            await manager.close()

    app = FastAPI(title="Local Torrent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(request.headers, request.url.scheme):
            return JSONResponse({"detail": "İstek uygulamanın kendi adresinden gönderilmeli."}, status_code=403)
        if request.url.path == "/api/torrents/upload":
            length = request.headers.get("content-length", "")
            if length.isdecimal() and int(length) > MAX_TORRENT_SIZE + 1024 * 1024:
                return JSONResponse({"detail": ".torrent dosya sınırı 64 MiB."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(TorrentError)
    async def torrent_error(_: Request, exc: TorrentError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(INDEX, media_type="text/html")

    @app.get("/api/torrents", response_model=Snapshot)
    async def torrents():
        return manager.snapshot()

    @app.post("/api/torrents/magnet", status_code=201)
    async def add_magnet(body: MagnetRequest):
        return {"info_hash": manager.add_magnet(body.magnet, body.save_path, body.select_first)}

    @app.post("/api/torrents/upload", status_code=201)
    async def upload_torrent(
        file: UploadFile = File(), save_path: str | None = Form(None, max_length=4096),
        select_first: bool = Form(False),
    ):
        if not file.filename or not file.filename.lower().endswith(".torrent"):
            raise TorrentError(".torrent uzantılı bir dosya seçin.")
        if file.size is not None and file.size > MAX_TORRENT_SIZE:
            raise TorrentError(".torrent dosya sınırı 64 MiB.", 413)
        data = await file.read(MAX_TORRENT_SIZE + 1)
        if len(data) > MAX_TORRENT_SIZE:
            raise TorrentError(".torrent dosya sınırı 64 MiB.", 413)
        return {"info_hash": manager.add_torrent(data, save_path, select_first)}

    @app.get("/api/torrents/{key}/files", response_model=list[TorrentFile])
    async def files(key: str):
        return manager.list_files(key)

    @app.put("/api/torrents/{key}/files", status_code=204)
    async def select_files(key: str, body: FileSelection):
        await manager.select_files(key, body.selected)
        return Response(status_code=204)

    @app.post("/api/torrents/{key}/pause", status_code=204)
    async def pause(key: str):
        await manager.set_paused(key, True)
        return Response(status_code=204)

    @app.post("/api/torrents/{key}/resume", status_code=204)
    async def resume(key: str):
        await manager.set_paused(key, False)
        return Response(status_code=204)

    @app.delete("/api/torrents/{key}", status_code=204)
    async def remove(key: str, delete_files: bool = Query(False)):
        await manager.remove(key, delete_files)
        return Response(status_code=204)

    @app.put("/api/settings/limits", response_model=SpeedLimits)
    async def limits(body: SpeedLimits):
        return manager.set_limits(body)

    @app.websocket("/ws")
    async def events(websocket: WebSocket):
        scheme = "https" if websocket.url.scheme == "wss" else "http"
        if not same_origin(websocket.headers, scheme):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(manager.snapshot().model_dump_json())
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    return
                await websocket.close(code=1008)
                return
        except (WebSocketDisconnect, OSError):
            return

    return app


app = create_app()
