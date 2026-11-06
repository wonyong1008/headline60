import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app import main
from app.anchors.experiments import experiment_directory, listing, video_path
from app.config import Settings

KEY = "lipsync-5s-20261030T010123"


def fixture_experiment(root: Path) -> tuple[Settings, Path]:
    config = Settings(root=root)
    folder = root / "data/benchmarks" / KEY
    folder.mkdir(parents=True)
    (folder / "output.mp4").write_bytes(b"local test video")
    (folder / "metrics.json").write_text(json.dumps({
        "status": "completed", "target_seconds": 5, "width": 512, "height": 512,
        "fps": 25, "rendered_frames": 125, "total_seconds": 89.866,
        "peak_rss_bytes": 7062257664, "mps_max_observed_driver_bytes": 4647518208,
        "static_portrait_mouth_only": True, "body_motion_generated": False,
        "source_path": "/private/hidden/reference.png", "output_path": "/private/hidden/output.mp4",
    }))
    return config, folder


def test_experiment_projection_is_not_a_production_profile(tmp_path):
    config, _ = fixture_experiment(tmp_path)
    result = listing(config)
    assert len(result) == 1
    assert result[0]["id"] == KEY
    assert result[0]["production_ready"] is False
    assert result[0]["body_motion_generated"] is False
    assert result[0]["video_url"] == f"/anchor-experiments/{KEY}/output.mp4"
    assert result[0]["peak_rss_bytes"] == 7062257664
    assert "source_path" not in result[0] and "output_path" not in result[0]
    assert "/private/hidden" not in json.dumps(result)


@pytest.mark.parametrize("key", ["../secret", "lipsync-60s-20261030T010123", "lipsync-5s-20261330T010123", "lipsync-5s-20261030T010123-extra"])
def test_invalid_experiment_id_cannot_access_files(tmp_path, key):
    with pytest.raises(ValueError):
        experiment_directory(Settings(root=tmp_path), key)


def test_symlink_output_and_metrics_are_not_served(tmp_path):
    config, folder = fixture_experiment(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private bytes")
    (folder / "output.mp4").unlink()
    (folder / "output.mp4").symlink_to(outside)
    with pytest.raises(ValueError):
        video_path(config, KEY)
    assert listing(config)[0]["video_url"] is None
    metadata = folder / "metrics.json"
    copied = tmp_path / "outside.json"
    copied.write_text(metadata.read_text())
    metadata.unlink()
    metadata.symlink_to(copied)
    assert listing(config) == []


def test_symlink_experiment_folder_is_rejected(tmp_path):
    config = Settings(root=tmp_path)
    root = tmp_path / "data/benchmarks"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / KEY).symlink_to(outside, target_is_directory=True)
    assert listing(config) == []
    with pytest.raises(ValueError):
        experiment_directory(config, KEY)


def test_read_only_experiment_api_and_preview(tmp_path, monkeypatch):
    config, _ = fixture_experiment(tmp_path)
    monkeypatch.setattr(main, "settings", config)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
            result = await client.get("/api/anchor-experiments")
            assert result.status_code == 200 and result.json()[0]["seconds"] == 5
            video = await client.get(f"/anchor-experiments/{KEY}/output.mp4")
            assert video.status_code == 200 and video.content == b"local test video"
            assert video.headers["content-type"] == "video/mp4"
            assert (await client.get("/anchor-experiments/not-valid/output.mp4")).status_code == 404
            page = await client.get("/anchors")
            assert page.status_code == 200
            assert "進行" not in page.text
            assert "임시 가상 얼굴" in page.text and "입 주변만 합성" in page.text
            assert "/static/app.js?v=" in page.text
            mutation = await client.post("/api/anchor-experiments", headers={"X-Headline-Token": main.csrf_token})
            assert mutation.status_code == 405
    asyncio.run(check())
