"""Raw-body bounded uploads; the app's existing request guards apply to this router."""
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.media import library


def make_router(config: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/media")
    def media_list():
        try:
            library.import_bundled(config)
            return library.listing(config)
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/api/media/upload")
    async def upload(request: Request, name: str = Query(min_length=1, max_length=200),
                     category: str = Query(default="general", min_length=1, max_length=80),
                     license_type: str = Query(default="user-owned"),
                     attribution: str = Query(default="", max_length=2000),
                     source_url: str = Query(default="", max_length=2000),
                     license_url: str | None = Query(default=None, max_length=2000),
                     rights_confirmed: bool = False):
        temporary = None
        try:
            length = request.headers.get("content-length")
            if length is not None and (int(length) < 0 or int(length) > library.MAX_BYTES):
                raise HTTPException(413, "업로드는 최대 50MiB입니다.")
            folder = library._root(config) / ".incoming"
            if folder.is_symlink():
                raise ValueError("업로드 임시 경로에 심볼릭 링크를 사용할 수 없습니다.")
            folder.mkdir(parents=True, exist_ok=True)
            size = 0
            with tempfile.NamedTemporaryFile(dir=folder, suffix=".upload", delete=False) as handle:
                temporary = Path(handle.name)
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > library.MAX_BYTES:
                        raise HTTPException(413, "업로드는 최대 50MiB입니다.")
                    handle.write(chunk)
            return await run_in_threadpool(library.register_upload, config, temporary, name=name,
                category=category, license_type=license_type, attribution=attribution,
                source_url=source_url, license_url=license_url, rights_confirmed=rights_confirmed)
        except HTTPException:
            raise
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @router.get("/api/media/{asset_id}/file")
    def media_file(asset_id: str):
        try:
            asset = library.resolve_asset(config, asset_id)
            return FileResponse(asset["local_path"])
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
