"""Read-only anchor diagnostics without downloading or loading models."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.anchors import preflight
from app.config import Settings


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda _: SimpleNamespace(free=20 * 1024**3))
    monkeypatch.setattr(preflight.shutil, "which", lambda binary: "/fake/" + binary)
    monkeypatch.setattr(preflight, "run", lambda args, timeout: args[0] + " fixture")
    monkeypatch.setattr(preflight, "probe", lambda _: {"streams": [
        {"codec_type": "video", "width": 512, "height": 720, "codec_name": "h264"}],
        "format": {"duration": "5.0"}})
    runtime = tmp_path / "runtime/lipsync/.venv/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("fixture: never execute")
    runtime.chmod(0o700)
    for module in ("vae.py", "unet.py"):
        path = tmp_path / preflight.VENDOR / "musetalk/models" / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture: never import")
    model = tmp_path / preflight.VENDOR / "models/musetalkV15/unet.pth"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"test")
    manifest = tmp_path / preflight.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"required": [{"path": "musetalkV15/unet.pth", "size_bytes": 4}]}))
    image = tmp_path / "reference.png"
    Image.new("RGB", (16, 16)).save(image)
    video = tmp_path / "base.mp4"
    video.write_bytes(b"ffprobe mocked media fixture")
    return Settings(root=tmp_path), image, video


def diagnose(environment, **kwargs):
    config, image, video = environment
    return preflight.diagnose(config, reference_image=image, base_video=video, **kwargs)


def test_ready_checks_only_files_headers_and_leaves_inputs_unchanged(environment):
    config, _, _ = environment
    before = {p.relative_to(config.root): p.read_bytes() for p in config.root.rglob("*") if p.is_file()}
    report = diagnose(environment)
    after = {p.relative_to(config.root): p.read_bytes() for p in config.root.rglob("*") if p.is_file()}
    assert report["ready"] and not report["missing"]
    assert report["models"][0]["size_bytes"] == 4
    assert report["target_seconds"] == 5
    assert before == after


def test_unprovided_assets_are_explicit_not_claimed_to_be_missing_models(environment):
    config, _, _ = environment
    report = preflight.diagnose(config)
    assert not report["ready"]
    assert not report["checks"]["reference_image"]["supplied"]
    assert not report["checks"]["base_video"]["supplied"]
    assert report["checks"]["model_manifest"]["ok"]
    assert len(report["missing"]) == 2


@pytest.mark.parametrize("name", ["vae.py", "unet.py"])
def test_missing_actual_upstream_modules_are_blockers(environment, name):
    config, _, _ = environment
    (config.root / preflight.VENDOR / "musetalk/models" / name).unlink()
    report = diagnose(environment)
    assert not report["ready"]
    assert not report["checks"]["source_" + name[:-3]]["ok"]


@pytest.mark.parametrize("required", [[], ["../escape.pth"], ["/absolute.pth"],
                                      [{"path": "a.pth", "size_bytes": True}], [None]])
def test_invalid_manifest_fails_clearly(environment, required):
    config, _, _ = environment
    (config.root / preflight.MANIFEST).write_text(json.dumps({"required": required}))
    report = diagnose(environment)
    assert not report["ready"]
    assert not report["checks"]["model_manifest"]["ok"]


def test_absent_manifest_and_zero_or_wrong_size_models(environment):
    config, _, _ = environment
    model = config.root / preflight.VENDOR / "models/musetalkV15/unet.pth"
    for content in (b"", b"wrongsize"):
        model.write_bytes(content)
        assert not diagnose(environment)["checks"]["model_0"]["ok"]
    model.unlink()
    assert diagnose(environment)["models"][0]["size_bytes"] is None
    (config.root / preflight.MANIFEST).unlink()
    assert not diagnose(environment)["checks"]["model_manifest"]["ok"]


def test_platform_disk_runtime_and_ffprobe_failures(environment, monkeypatch):
    config, _, _ = environment
    monkeypatch.setattr(preflight.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(preflight.shutil, "disk_usage", lambda _: SimpleNamespace(free=12 * 1024**3 - 1))
    monkeypatch.setattr(preflight.shutil, "which", lambda _: None)
    (config.root / "runtime/lipsync/.venv/bin/python").unlink()
    report = diagnose(environment)
    assert all(not report["checks"][key]["ok"] for key in
               ("platform", "disk", "runtime_python", "ffmpeg", "ffprobe", "base_video"))


def test_invalid_asset_paths_and_corrupt_image_are_reported(environment):
    config, image, _ = environment
    image.write_text("not an image")
    report = preflight.diagnose(config, reference_image=image, base_video=config.root)
    assert not report["checks"]["reference_image"]["ok"]
    assert not report["checks"]["base_video"]["ok"]
    report = preflight.diagnose(config, reference_image=config.root / "absent.png",
                               base_video=config.root / "absent.mp4")
    assert not report["ready"]


@pytest.mark.parametrize("duration", ["4.9", "nan", "inf"])
def test_too_short_or_nonfinite_video_cannot_pass(environment, monkeypatch, duration):
    monkeypatch.setattr(preflight, "probe", lambda _: {"streams": [
        {"codec_type": "video", "width": 512, "height": 720}], "format": {"duration": duration}})
    assert not diagnose(environment)["checks"]["base_video"]["ok"]


def test_long_audio_does_not_hide_short_video_stream(environment, monkeypatch):
    monkeypatch.setattr(preflight, "probe", lambda _: {"streams": [
        {"codec_type": "video", "width": 512, "height": 720, "duration": "2.0"}],
        "format": {"duration": "10.0"}})
    report = diagnose(environment)
    assert not report["checks"]["base_video"]["ok"]
    assert report["checks"]["base_video"]["duration_seconds"] == 2


def test_cli_json_and_not_ready_exit_code_without_import_execution(monkeypatch, capsys):
    path = Path(__file__).parents[1] / "scripts/anchor_preflight.py"
    spec = importlib.util.spec_from_file_location("anchor_preflight_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(module, "diagnose", lambda *args, **kwargs: {"ready": False, "missing": ["fixture"]})
    assert module.main(["--json"]) == 2
    assert json.loads(capsys.readouterr().out)["missing"] == ["fixture"]
