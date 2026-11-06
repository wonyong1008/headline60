"""Local media rights/path/format guards, with small real FFmpeg fixtures."""
import asyncio
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

import app.media
from app.config import Settings
from app.jobs.store import connect

# Also permits this standalone staging file to use the existing project's app.
app.media.__path__.insert(0, str(Path(__file__).parents[1] / "app/media"))
from app.media import api, library


def image_bytes(orientation=None):
    data = io.BytesIO()
    image = Image.new("RGB", (20, 10), "red")
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
        exif[315] = "metadata must be removed"
    image.save(data, format="JPEG", exif=exif)
    return data.getvalue()


@pytest.fixture
def config(tmp_path):
    return Settings(root=tmp_path)


def upload(config, **kwargs):
    return library.register_upload(config, image_bytes(), name="../../news.jpg",
                                   rights_confirmed=True, **kwargs)


def test_approval_paths_hash_and_display_name(config):
    asset = upload(config)
    assert asset["name"] == "news.jpg"
    assert asset["is_reference"] and asset["usage_status"] == "approved"
    assert Path(asset["local_path"]).parent == config.data / "media/assets"
    assert Path(asset["local_path"]).stem == asset["asset_id"]
    assert library.resolve_asset(config, asset["asset_id"], "image") == asset
    assert library.listing(config) == [asset]


def test_no_rights_confirmation_blocks_renderer(config):
    asset = library.register_upload(config, image_bytes(), name="photo.jpg")
    assert asset["usage_status"] == "needs_review"
    with pytest.raises(ValueError, match="권리"):
        library.resolve_asset(config, asset["asset_id"])


def test_orientation_corrected_and_exif_stripped(config):
    asset = library.register_upload(config, image_bytes(6), name="rotated.jpg", rights_confirmed=True)
    with Image.open(asset["local_path"]) as image:
        assert image.size == (10, 20)
        assert not image.getexif()


@pytest.mark.parametrize("kwargs", [
    {"source_url": "http://127.0.0.1/private"},
    {"license_url": "file:///private/license"},
    {"license_type": "unknown"}, {"license_type": "CC-BY-4.0"},
])
def test_invalid_rights_and_urls(config, kwargs):
    with pytest.raises(ValueError):
        upload(config, **kwargs)


def test_oversize_empty_and_giant_pixels(config, monkeypatch):
    for data in (b"", b"x" * (library.MAX_BYTES + 1)):
        with pytest.raises(ValueError, match="50MiB"):
            library.register_upload(config, data, name="photo.jpg")
    monkeypatch.setattr(library, "MAX_PIXELS", 20)
    with pytest.raises(ValueError, match="픽셀"):
        upload(config)
    assert not list((config.data / "media/assets").glob("*"))


def test_arbitrary_paths_and_symlink_uploads_are_rejected(config):
    outside = config.root / "outside.jpg"
    outside.write_bytes(image_bytes())
    with pytest.raises(ValueError, match="경로 밖"):
        library.register_upload(config, outside, name="photo.jpg")
    incoming = config.data / "media/.incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    link = incoming / "alias.jpg"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="심볼릭"):
        library.register_upload(config, link, name="photo.jpg")


def test_modified_hash_symlink_and_wrong_kind_block_render(config):
    asset = upload(config)
    path = Path(asset["local_path"])
    with pytest.raises(ValueError, match="종류"):
        library.resolve_asset(config, asset["asset_id"], "audio")
    original = path.read_bytes()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="변경"):
        library.resolve_asset(config, asset["asset_id"])
    outside = config.root / "outside.jpg"
    outside.write_bytes(original)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="심볼릭"):
        library.resolve_asset(config, asset["asset_id"])


def test_database_cannot_redirect_asset_to_external_file(config):
    asset = upload(config)
    outside = config.root / "outside.jpg"
    outside.write_bytes(Path(asset["local_path"]).read_bytes())
    asset["local_path"] = str(outside)
    with connect(config) as db:
        db.execute("UPDATE media_assets SET payload=? WHERE id=?",
                   (json.dumps(asset), asset["asset_id"]))
    with pytest.raises(ValueError, match="경로 밖"):
        library.resolve_asset(config, asset["asset_id"])


@pytest.fixture(scope="module")
def real_media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/ffprobe fixtures unavailable")
    root = tmp_path_factory.mktemp("media-fixtures")
    fixtures = {}
    for suffix in ("mp4", "mov", "mp3", "wav"):
        path = root / ("fixture." + suffix)
        if suffix in {"mp4", "mov"}:
            command = ["-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10", "-t", "0.2",
                       "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
        else:
            command = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.2"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", *command, str(path)],
                       capture_output=True, check=True, timeout=20)
        fixtures[suffix] = path.read_bytes()
    return fixtures


@pytest.mark.parametrize(("suffix", "kind"), [("mp4", "video"), ("mov", "video"),
                                             ("mp3", "audio"), ("wav", "audio")])
def test_real_ffprobe_formats(config, real_media, suffix, kind):
    asset = library.register_upload(config, real_media[suffix], name="fixture." + suffix,
                                    rights_confirmed=True)
    assert asset["media_type"] == kind
    assert library.resolve_asset(config, asset["asset_id"], kind)["asset_id"] == asset["asset_id"]


def test_fake_media_and_long_video_are_rejected(config, monkeypatch):
    with pytest.raises(RuntimeError):
        library.register_upload(config, b"not a media file", name="fake.mp4")
    monkeypatch.setattr(library, "probe", lambda _: {"format": {"duration": "301", "format_name": "mov"},
        "streams": [{"codec_type": "video", "width": 720, "height": 1280}]})
    with pytest.raises(ValueError, match="300초"):
        library.register_upload(config, b"header fixture", name="long.mp4")


def bundle(config):
    root = config.data / "reels-assets"
    root.mkdir(parents=True)
    content = image_bytes()
    (root / "photo.jpg").write_bytes(content)
    entry = {"file": "photo.jpg", "author": "Fixture Author", "license": "CC-BY-SA-4.0",
        "source_url": "https://commons.wikimedia.org/wiki/File:Fixture.jpg",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "sha256": hashlib.sha256(content).hexdigest()}
    manifest = root / "sources.json"
    manifest.write_text(json.dumps([entry]))
    return manifest, entry


def test_bundled_import_is_idempotent_and_keeps_attribution(config):
    bundle(config)
    assets = library.import_bundled(config)
    assert assets == library.import_bundled(config)
    assert len(library.listing(config)) == 1
    assert assets[0]["license_type"] == "CC-BY-SA-4.0"
    assert "Fixture Author" in assets[0]["attribution"]
    assert "변경:" in assets[0]["attribution"]


@pytest.mark.parametrize("change", ["hash", "source", "license", "path"])
def test_bundled_tamper_or_rights_mismatch_is_rejected(config, change):
    manifest, entry = bundle(config)
    if change == "hash":
        entry["sha256"] = "0" * 64
    elif change == "source":
        entry["source_url"] = "http://127.0.0.1/private"
    elif change == "license":
        entry["license_url"] = "https://example.com/not-a-license"
    else:
        entry["file"] = "../../outside.jpg"
    manifest.write_text(json.dumps([entry]))
    with pytest.raises(ValueError):
        library.import_bundled(config)
    assert library.listing(config) == []


def test_api_stream_limits_approval_and_download(config, monkeypatch):
    app = FastAPI()
    app.include_router(api.make_router(config))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/media/upload", params={"name": "photo.jpg"}, content=image_bytes())
            assert response.status_code == 200
            assert response.json()["usage_status"] == "needs_review"
            assert (await client.get("/api/media/" + response.json()["asset_id"] + "/file")).status_code == 400
            response = await client.post("/api/media/upload", params={"name": "photo.jpg", "rights_confirmed": True}, content=image_bytes())
            assert response.status_code == 200
            assert (await client.get("/api/media/" + response.json()["asset_id"] + "/file")).status_code == 200
            listing = (await client.get("/api/media")).json()
            assert isinstance(listing, list) and len(listing) == 2
            monkeypatch.setattr(library, "MAX_BYTES", 1000)

            async def oversized():
                yield b"x" * 600
                yield b"x" * 600

            assert (await client.post("/api/media/upload?name=big.mp4", content=oversized())).status_code == 413
            assert (await client.post("/api/media/upload?name=big.mp4", headers={"content-length": "1001"}, content=b"x")).status_code == 413
            assert not list((config.data / "media/.incoming").glob("*"))

    asyncio.run(exercise())
