"""Bounded local media library. Rights approval records a user's confirmation only."""
import json
import math
import re
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image, ImageOps
from pydantic import BaseModel, Field, field_validator

from app.config import Settings
from app.jobs.store import connect
from app.news.feeds import public_url
from app.process import file_digest, probe

MAX_BYTES = 50 * 1024**2
MAX_PIXELS = 24_000_000
MAX_SECONDS = 300
LICENSES = {"CC0", "CC-BY-4.0", "CC-BY-SA-4.0", "user-owned", "permission-granted", "Pexels"}
RIGHTS_NOTE = "approved는 사용자 확인 또는 원본 라이선스 메타데이터 기록입니다. 초상권·상표권 등 모든 권리를 자동 증명하지 않습니다."
_bundle_lock = threading.RLock()


class MediaAsset(BaseModel):
    asset_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=200)
    source_url: str = Field(default="", max_length=2000)
    license_type: Literal["CC0", "CC-BY-4.0", "CC-BY-SA-4.0", "user-owned", "permission-granted", "Pexels"]
    attribution: str = Field(default="", max_length=2000)
    category: str = Field(default="general", min_length=1, max_length=80)
    local_path: str
    usage_status: Literal["approved", "needs_review"]
    media_type: Literal["image", "video", "audio"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str
    rights_basis: Literal["user_confirmation", "license_metadata"] = "user_confirmation"
    is_reference: bool = True
    tags: list[str] = Field(default_factory=list, max_length=24)
    description: str = Field(default="", max_length=500)
    license_url: str | None = Field(default=None, max_length=2000)

    @field_validator("source_url", "license_url")
    @classmethod
    def external_url(cls, value):
        return public_url(value) if value else value


def _root(config: Settings) -> Path:
    root = config.data / "media"
    if root.is_symlink() or config.data.is_symlink():
        raise ValueError("미디어 저장 경로에 심볼릭 링크를 사용할 수 없습니다.")
    return root


def _safe_file(root: Path, path: Path) -> Path:
    absolute = path.absolute()
    if not absolute.is_relative_to(root.absolute()):
        raise ValueError("허용된 미디어 경로 밖의 파일입니다.")
    # macOS /var and /tmp aliases are outside the configured data tree.
    for part in (absolute, *absolute.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError("미디어 파일에 심볼릭 링크를 사용할 수 없습니다.")
    resolved = absolute.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("미디어 파일이 없거나 경로가 올바르지 않습니다.")
    return resolved


def _save(config: Settings, asset: dict) -> dict:
    value = MediaAsset.model_validate(asset).model_dump()
    with connect(config) as db:
        db.execute("CREATE TABLE IF NOT EXISTS media_assets (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("INSERT INTO media_assets VALUES (?, ?)",
                   (value["asset_id"], json.dumps(value, ensure_ascii=False)))
    return value


def listing(config: Settings) -> list[dict]:
    with connect(config) as db:
        db.execute("CREATE TABLE IF NOT EXISTS media_assets (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        rows = db.execute("SELECT payload FROM media_assets ORDER BY rowid DESC LIMIT 500").fetchall()
    return [MediaAsset.model_validate_json(row[0]).model_dump() for row in rows]


def resolve_asset(config: Settings, asset_id: str, kind: str | None = None) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
        raise ValueError("잘못된 미디어 자산 ID입니다.")
    with connect(config) as db:
        db.execute("CREATE TABLE IF NOT EXISTS media_assets (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        row = db.execute("SELECT payload FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    if row is None:
        raise KeyError("미디어 자산을 찾을 수 없습니다.")
    asset = MediaAsset.model_validate_json(row[0]).model_dump()
    if asset["usage_status"] != "approved":
        raise ValueError("사용 권리 확인이 필요한 미디어입니다. 렌더링에 사용할 수 없습니다.")
    if kind is not None and asset["media_type"] != kind:
        raise ValueError("요청한 미디어 종류와 일치하지 않습니다.")
    path = _safe_file(_root(config), Path(asset["local_path"]))
    if path.stat().st_size > MAX_BYTES or file_digest(path) != asset["sha256"]:
        raise ValueError("미디어 파일이 변경되었거나 크기 제한을 초과했습니다.")
    return {**asset, "local_path": str(path)}


def _inspect_and_copy(source: Path, target: Path) -> tuple[str, str]:
    try:
        with Image.open(source) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("PNG/JPEG/WEBP 이미지만 지원합니다.")
            if image.width * image.height > MAX_PIXELS or max(image.size) > 12000:
                raise ValueError("이미지가 너무 큽니다 (최대 2400만 픽셀).")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("움직이는 이미지는 지원하지 않습니다. 영상으로 등록하세요.")
            image.load()
            oriented = ImageOps.exif_transpose(image)
            output = target.with_suffix(".png" if "A" in oriented.getbands() else ".jpg")
            clean = Image.new("RGBA" if output.suffix == ".png" else "RGB", oriented.size)
            clean.paste(oriented.convert(clean.mode))
            clean.save(output, **({"quality": 95} if output.suffix == ".jpg" else {}))
            return "image", str(output)
    except Image.UnidentifiedImageError:
        pass
    except Image.DecompressionBombError as exc:
        raise ValueError("이미지 픽셀 수 제한을 초과했습니다.") from exc
    info = probe(source)
    streams = info.get("streams", [])
    formats = set(info.get("format", {}).get("format_name", "").split(","))
    seconds = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:
        raise ValueError("미디어 길이는 0초 초과 300초 이하여야 합니다.")
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video and formats & {"mov", "mp4"}:
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if min(width, height) <= 0 or max(width, height) > 4096 or width * height > 16_777_216:
            raise ValueError("영상 크기가 허용 범위를 초과했습니다 (긴 변 최대 4096px).")
        media_type, suffix = "video", ".mp4"
    elif audio and not video and formats & {"mp3", "wav"}:
        if not 1 <= int(audio.get("channels", 0)) <= 8 or not 8000 <= int(audio.get("sample_rate", 0)) <= 192000:
            raise ValueError("오디오 채널/샘플레이트가 허용 범위를 벗어났습니다.")
        media_type, suffix = "audio", ".wav" if "wav" in formats else ".mp3"
    else:
        raise ValueError("지원 형식은 PNG/JPEG/WEBP, MP4/MOV, MP3/WAV입니다.")
    output = target.with_suffix(suffix)
    shutil.copyfile(source, output)
    return media_type, str(output)


def register_upload(config: Settings, content: bytes | Path, *, name: str,
                    category: str = "general", license_type: str = "user-owned",
                    attribution: str = "", source_url: str = "", rights_confirmed: bool = False,
                    license_url: str | None = None, tags: list[str] | None = None,
                    description: str = "", rights_basis: str = "user_confirmation") -> dict:
    if license_type not in LICENSES:
        raise ValueError("지원하지 않는 라이선스입니다.")
    if license_type in {"CC-BY-4.0", "CC-BY-SA-4.0"} and not attribution.strip():
        raise ValueError("CC BY 자료에는 저작자·출처 표시가 필요합니다.")
    if type(rights_confirmed) is not bool:
        raise ValueError("권리 확인은 boolean이어야 합니다.")
    source_url = public_url(source_url) if source_url else ""
    license_url = public_url(license_url) if license_url else None
    if license_type == "Pexels":
        from urllib.parse import urlsplit
        if urlsplit(source_url).hostname not in {"pexels.com", "www.pexels.com"}:
            raise ValueError("Pexels 자료는 원본 Pexels 페이지 URL을 기록하세요.")
        license_url = "https://www.pexels.com/license/"
    if tags is not None and (len(tags) > 24 or any(not isinstance(t, str) or not t.strip() or len(t) > 60 for t in tags)):
        raise ValueError("미디어 키워드는 1~60자, 최대24개여야 합니다.")
    root = _root(config)
    incoming, assets = root / ".incoming", root / "assets"
    for folder in (incoming, assets):
        if folder.is_symlink():
            raise ValueError("미디어 경로에 심볼릭 링크를 사용할 수 없습니다.")
        folder.mkdir(parents=True, exist_ok=True)
    own_temp = None
    output = None
    key = uuid4().hex
    try:
        if isinstance(content, Path):
            source = _safe_file(incoming, content)
        elif isinstance(content, bytes):
            if not 0 < len(content) <= MAX_BYTES:
                raise ValueError("미디어 파일은 비어 있지 않은 50MiB 이하 파일이어야 합니다.")
            with tempfile.NamedTemporaryFile(dir=incoming, suffix=".upload", delete=False) as handle:
                handle.write(content)
                own_temp = Path(handle.name)
            source = own_temp
        else:
            raise TypeError("업로드 내용 또는 내부 임시 파일만 사용할 수 있습니다.")
        if not 0 < source.stat().st_size <= MAX_BYTES:
            raise ValueError("미디어 파일은 비어 있지 않은 50MiB 이하 파일이어야 합니다.")
        media_type, path = _inspect_and_copy(source, assets / key)
        output = Path(path)
        if output.stat().st_size > MAX_BYTES:
            raise ValueError("정규화한 미디어가 50MiB를 초과했습니다.")
        return _save(config, {"asset_id": key, "name": name.replace("\\", "/").split("/")[-1],
            "source_url": source_url, "license_type": license_type, "attribution": attribution,
            "category": category, "local_path": str(output.resolve()),
            "usage_status": "approved" if rights_confirmed else "needs_review", "media_type": media_type,
            "sha256": file_digest(output), "created_at": datetime.now(UTC).isoformat(),
            "is_reference": True, "license_url": license_url, "rights_basis": rights_basis,
            "tags": tags or [], "description": description})
    except BaseException:
        if output is not None:
            output.unlink(missing_ok=True)
        for partial in assets.glob(key + ".*"):
            partial.unlink(missing_ok=True)
        raise
    finally:
        if own_temp is not None:
            own_temp.unlink(missing_ok=True)


def import_bundled(config: Settings) -> list[dict]:
    # Concurrent page requests must not duplicate the same bundled photographs.
    with _bundle_lock:
        return _import_bundled(config)


def _import_bundled(config: Settings) -> list[dict]:
    root = config.data / "reels-assets"
    manifest = root / "sources.json"
    if not manifest.is_file():
        return []
    if root.is_symlink() or manifest.is_symlink() or manifest.stat().st_size > 100_000:
        raise ValueError("기본 미디어 출처 목록 경로/크기가 올바르지 않습니다.")
    entries = json.loads(manifest.read_text())
    if not isinstance(entries, list) or len(entries) > 20:
        raise ValueError("기본 미디어 출처 목록 형식이 올바르지 않습니다.")
    imported = []
    for entry in entries:
        source = _safe_file(root, root / entry["file"])
        expected = entry.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", expected) or source.stat().st_size > MAX_BYTES or file_digest(source) != expected:
            raise ValueError("기본 미디어 원본 해시가 출처 기록과 다릅니다.")
        license_type = entry.get("license")
        if license_type not in {"CC0", "CC-BY-4.0", "CC-BY-SA-4.0"}:
            raise ValueError("기본 미디어의 공개 라이선스를 확인할 수 없습니다.")
        source_url, license_url = public_url(entry["source_url"]), public_url(entry["license_url"])
        expected_license_url = {"CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
            "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
            "CC-BY-SA-4.0": "https://creativecommons.org/licenses/by-sa/4.0/"}[license_type]
        if license_url != expected_license_url:
            raise ValueError("기본 미디어 라이선스 URL과 종류가 일치하지 않습니다.")
        author = entry.get("author", "").strip()
        if not author:
            raise ValueError("기본 미디어의 저작자 표시가 없습니다.")
        existing = next((item for item in listing(config) if item["source_url"] == source_url), None)
        if existing:
            resolve_asset(config, existing["asset_id"])
            imported.append(existing)
            continue
        imported.append(register_upload(config, source.read_bytes(), name=entry["file"],
            category="realestate", license_type=license_type, source_url=source_url,
            attribution=f"{author} · {source_url} · {license_type} · 변경: 방향 보정·메타데이터 제거; {entry.get('modifications', '')}", license_url=license_url,
            rights_confirmed=True))
    return imported
