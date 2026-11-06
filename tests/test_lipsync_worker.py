"""MuseTalk worker failure guards; no model imports, inference or media processes."""
import builtins
import importlib.util
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKER = Path(__file__).parents[1] / "runtime/lipsync/worker.py"
WEIGHTS = [
    "musetalk/models/vae.py", "musetalk/models/unet.py",
    "models/musetalkV15/unet.pth", "models/musetalkV15/musetalk.json",
    "models/sd-vae/config.json", "models/sd-vae/diffusion_pytorch_model.bin",
    "models/whisper/config.json", "models/whisper/preprocessor_config.json",
    "models/whisper/pytorch_model.bin", "models/face-parse-bisent/79999_iter.pth",
    "models/face-parse-bisent/resnet18-5c106cde.pth",
]


def load_worker():
    spec = importlib.util.spec_from_file_location("headline_lipsync_worker_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_wav(path, *, rate=48000, channels=1, width=2, seconds=5):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\0" * round(rate * seconds) * channels * width)


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def no_neural_import(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "cv2", "numpy", "musetalk", "transformers"}:
            raise AssertionError("Test must not import neural runtime: " + name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_neural_import)
    worker = load_worker()
    monkeypatch.setattr(worker, "checked", lambda *args, **kwargs: pytest.fail("No media subprocess expected"))
    source = tmp_path / "portrait.png"
    source.write_bytes(b"fixture source; not decoded in these failure tests")
    audio = tmp_path / "sample.wav"
    write_wav(audio)
    request = {
        "metrics_path": str(tmp_path / "metrics.json"),
        "source_root": str(tmp_path / "vendor"),
        "audio_path": str(audio), "output_path": str(tmp_path / "output.mp4"),
        "reference_image": str(source), "fps": 25, "target_seconds": 5,
    }
    return worker, request, tmp_path


def invoke(experiment, monkeypatch, capsys):
    worker, request, folder = experiment
    request_path = folder / "request.json"
    request_path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, "argv", ["worker.py", "--request", str(request_path)])
    exit_code = worker.main()
    printed = json.loads(capsys.readouterr().out)
    metrics = json.loads(Path(request["metrics_path"]).read_text())
    assert exit_code == 1
    assert printed["status"] == metrics["status"] == "failed"
    assert printed["error"] == metrics["error"]
    assert metrics["total_seconds"] >= 0
    return metrics


def assert_failure(experiment, monkeypatch, capsys, code):
    metrics = invoke(experiment, monkeypatch, capsys)
    assert metrics["error"]["code"] == code
    assert not Path(experiment[1]["output_path"]).exists()
    return metrics


def test_import_does_not_load_neural_modules_or_start_cli(experiment, capsys):
    worker, _, _ = experiment
    assert callable(worker.run_request)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["neither", "both", "absent_file"])
def test_exactly_one_existing_source_is_required(experiment, monkeypatch, capsys, mode):
    _, request, folder = experiment
    if mode == "neither":
        request.pop("reference_image")
    elif mode == "both":
        request["base_video"] = request["reference_image"]
    else:
        request["base_video"] = str(folder / "absent.mp4")
        request.pop("reference_image")
    assert_failure(experiment, monkeypatch, capsys, "missing_source")


def test_missing_audio(experiment, monkeypatch, capsys):
    Path(experiment[1]["audio_path"]).unlink()
    assert_failure(experiment, monkeypatch, capsys, "missing_audio")


def test_existing_output_and_original_inputs_are_preserved(experiment, monkeypatch, capsys):
    _, request, _ = experiment
    output, source, audio = (Path(request[key]) for key in
                             ("output_path", "reference_image", "audio_path"))
    output.write_bytes(b"previous completed result must stay unchanged")
    before = {path: path.read_bytes() for path in (output, source, audio)}
    metrics = invoke(experiment, monkeypatch, capsys)
    assert metrics["error"]["code"] == "output_exists"
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(("field", "value"), [
    ("fps", True), ("fps", 14), ("fps", 31), ("fps", 25.0),
    ("target_seconds", False), ("target_seconds", 0), ("target_seconds", 16),
    ("target_seconds", float("nan")), ("target_seconds", float("inf")),
    ("target_seconds", 5.01),
])
def test_invalid_duration_or_frame_rate_fails_before_import(experiment, monkeypatch, capsys, field, value):
    experiment[1][field] = value
    assert_failure(experiment, monkeypatch, capsys, "invalid_duration")


@pytest.mark.parametrize("value", [True, 127, 721, 512.0, "512"])
def test_max_side_is_bounded_integer(experiment, monkeypatch, capsys, value):
    experiment[1]["max_side"] = value
    assert_failure(experiment, monkeypatch, capsys, "invalid_source_size")


def test_precision_must_be_boolean(experiment, monkeypatch, capsys):
    experiment[1]["vae_fp16"] = "true"
    assert_failure(experiment, monkeypatch, capsys, "invalid_precision")


@pytest.mark.parametrize("params", [
    {"rate": 16000}, {"channels": 2}, {"width": 1}, {"seconds": 4.9}, {"seconds": 5.1},
])
def test_wrong_wav_format_or_duration(experiment, monkeypatch, capsys, params):
    write_wav(Path(experiment[1]["audio_path"]), **params)
    assert_failure(experiment, monkeypatch, capsys, "invalid_audio")


def test_corrupt_wav_is_a_failed_json_result(experiment, monkeypatch, capsys):
    Path(experiment[1]["audio_path"]).write_bytes(b"corrupt WAV")
    metrics = assert_failure(experiment, monkeypatch, capsys, "worker_failed")
    assert metrics["error"]["type"] in {"Error", "EOFError"}


def test_missing_local_weights_before_any_runtime_import(experiment, monkeypatch, capsys):
    metrics = assert_failure(experiment, monkeypatch, capsys, "missing_local_assets")
    assert "models/musetalkV15/unet.pth" in metrics["error"]["message"]
    assert metrics["stage"] == "validate"


def fill_assets(request):
    for relative in WEIGHTS:
        path = Path(request["source_root"]) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture only; no neural loaders are allowed")


def test_oversize_source_is_rejected_before_neural_import(experiment, monkeypatch, capsys):
    worker, request, _ = experiment
    fill_assets(request)
    monkeypatch.setattr(worker, "checked", lambda *args: json.dumps({"streams": [{"width": 4097, "height": 512}]}))
    assert_failure(experiment, monkeypatch, capsys, "invalid_source_size")


def test_unavailable_mps_is_reported_without_cpu_or_model_fallback(experiment, monkeypatch, capsys):
    worker, request, _ = experiment
    fill_assets(request)
    monkeypatch.setattr(worker, "checked", lambda *args: json.dumps({"streams": [{"width": 512, "height": 720}]}))
    original_import = builtins.__import__
    fake_torch = SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)))

    def fake_neural_import(name, *args, **kwargs):
        if name in {"cv2", "numpy"}:
            return SimpleNamespace()
        if name == "torch":
            return fake_torch
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_neural_import)
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY",
                "PYTORCH_ENABLE_MPS_FALLBACK", "GLOG_minloglevel", "TOKENIZERS_PARALLELISM"):
        monkeypatch.setenv(key, "fixture")
    metrics = assert_failure(experiment, monkeypatch, capsys, "mps_unavailable")
    assert metrics["stage"] == "imports"
    assert metrics["cpu_fallback_enabled"] is False
    assert metrics["static_portrait_mouth_only"] is True


def test_missing_request_prints_failed_json_without_loading_models(experiment, monkeypatch, capsys):
    worker, _, folder = experiment
    monkeypatch.setattr(sys, "argv", ["worker.py", "--request", str(folder / "absent.json")])
    assert worker.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["metrics_path"] is None
    assert report["error"]["type"] == "FileNotFoundError"


@pytest.mark.parametrize("target", ["audio_path", "reference_image", "base_video", "output_path", "request"])
@pytest.mark.parametrize("symlink", [False, True])
def test_metrics_cannot_overwrite_any_input_or_output(experiment, monkeypatch, capsys, target, symlink):
    worker, request, folder = experiment
    request_path = folder / "request.json"
    if target == "request":
        protected = request_path
    elif target == "base_video":
        protected = folder / "source.mp4"
        protected.write_bytes(b"original base video")
        request["base_video"] = str(protected)
        request.pop("reference_image")
    else:
        protected = Path(request[target])
        if target == "output_path":
            protected.write_bytes(b"previous completed output")
    metrics_path = protected
    if symlink:
        metrics_path = folder / "metrics-alias.json"
        metrics_path.symlink_to(protected)
    request["metrics_path"] = str(metrics_path)
    request_path.write_text(json.dumps(request))
    originals = {path: path.read_bytes() for path in (request_path, protected)}
    monkeypatch.setattr(sys, "argv", ["worker.py", "--request", str(request_path)])
    assert worker.main() == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "failed"
    assert printed["error"]["code"] == "path_collision"
    assert printed["metrics_path"] is None
    assert all(path.read_bytes() == content for path, content in originals.items())
    if symlink:
        assert metrics_path.is_symlink()


def test_metrics_directory_is_rejected_without_finally_writing(experiment, monkeypatch, capsys):
    worker, request, folder = experiment
    request["metrics_path"] = str(folder)
    request_path = folder / "request.json"
    request_path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, "argv", ["worker.py", "--request", str(request_path)])
    assert worker.main() == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["error"]["code"] == "invalid_metrics_path"
    assert printed["metrics_path"] is None
    assert folder.is_dir()


def test_safe_existing_metrics_file_can_be_updated(experiment, monkeypatch, capsys):
    _, request, _ = experiment
    path = Path(request["metrics_path"])
    path.write_text(json.dumps({"status": "old benchmark state"}))
    metrics = assert_failure(experiment, monkeypatch, capsys, "missing_local_assets")
    assert metrics["status"] == "failed"
    assert json.loads(path.read_text()) == metrics


@pytest.mark.parametrize("symlink", [False, True])
def test_atomic_metrics_temporary_file_cannot_clobber_output(experiment, monkeypatch, capsys, symlink):
    worker, request, folder = experiment
    temporary = Path(request["metrics_path"] + ".tmp")
    output = Path(request["output_path"]) if symlink else temporary
    output.write_bytes(b"previous completed video")
    request["output_path"] = str(output)
    if symlink:
        temporary.symlink_to(output)
    request_path = folder / "request.json"
    request_path.write_text(json.dumps(request))
    monkeypatch.setattr(sys, "argv", ["worker.py", "--request", str(request_path)])
    assert worker.main() == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["error"]["code"] == "path_collision"
    assert printed["metrics_path"] is None
    assert output.read_bytes() == b"previous completed video"
