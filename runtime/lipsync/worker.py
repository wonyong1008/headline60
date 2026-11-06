"""Isolated, offline MuseTalk 1.5 smoke-test worker (no server or TTS API)."""

import argparse
import gc
import json
import math
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path


class WorkerError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def save_metrics(path, metrics):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def checked(args, timeout=60):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            shell=False, check=False)
    if result.returncode:
        raise WorkerError("media_command_failed", result.stderr[-4000:])
    return result.stdout


class MemorySampler:
    """Observed MPS maxima are samples, not allocator lifetime peak counters."""

    def __init__(self, torch, metrics):
        self.torch, self.metrics = torch, metrics
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.poll, daemon=True)

    def observe(self):
        current = int(self.torch.mps.current_allocated_memory())
        driver = int(self.torch.mps.driver_allocated_memory())
        self.metrics["mps_current_allocated_bytes"] = current
        self.metrics["mps_driver_allocated_bytes"] = driver
        self.metrics["mps_max_observed_allocated_bytes"] = max(
            current, self.metrics.get("mps_max_observed_allocated_bytes", 0))
        self.metrics["mps_max_observed_driver_bytes"] = max(
            driver, self.metrics.get("mps_max_observed_driver_bytes", 0))

    def poll(self):
        while not self.stop.wait(0.1):
            try:
                self.observe()
            except Exception as exc:  # noqa: BLE001 - sampling must not stop the render thread.
                self.metrics["memory_sampling_error"] = str(exc)
                return

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=1)
        self.observe()


def run_request(request_path, metrics):
    request = json.loads(request_path.read_text(encoding="utf-8"))
    metrics_path = Path(request["metrics_path"]).expanduser().resolve()
    # Diagnostics must never replace inputs or a previously published result.
    # Do not register this path until validated: main() also writes it on failure.
    protected_paths = {request_path.expanduser().resolve()}
    for key in ("audio_path", "base_video", "reference_image", "output_path"):
        if request.get(key):
            protected_paths.add(Path(request[key]).expanduser().resolve())
    metrics_temporary = metrics_path.with_suffix(metrics_path.suffix + ".tmp").resolve()
    if metrics_path in protected_paths or metrics_temporary in protected_paths:
        raise WorkerError("path_collision", "metrics_path는 요청·음성·원본 자산·출력 경로와 달라야 합니다.")
    if metrics_path.is_dir() or metrics_temporary.is_dir() or metrics_temporary == metrics_path:
        raise WorkerError("invalid_metrics_path", "metrics_path는 디렉터리가 아닌 JSON 파일 경로여야 합니다.")
    metrics["metrics_path"] = str(metrics_path)
    metrics["stage"] = "validate"
    save_metrics(metrics_path, metrics)
    source_root = Path(request["source_root"]).expanduser().resolve()
    audio = Path(request["audio_path"]).expanduser().resolve()
    output = Path(request["output_path"]).expanduser().resolve()
    selected = [(key, request.get(key)) for key in ("base_video", "reference_image")
                if request.get(key)]
    if len(selected) != 1:
        raise WorkerError("missing_source", "base_video 또는 reference_image 한 개가 필요합니다.")
    kind, source_value = selected[0]
    source = Path(source_value).expanduser().resolve()
    if not source.is_file():
        raise WorkerError("missing_source", f"진행자 이미지/영상이 없습니다: {source}")
    if not audio.is_file():
        raise WorkerError("missing_audio", f"음성 파일이 없습니다: {audio}")
    if output.exists():
        raise WorkerError("output_exists", f"기존 결과는 덮어쓰지 않습니다: {output}")
    fps = request.get("fps", 25)
    seconds = request.get("target_seconds", 5)
    if (isinstance(fps, bool) or not isinstance(fps, int) or not 15 <= fps <= 30
            or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or not 0 < seconds <= 15
            or not math.isclose(seconds * fps, round(seconds * fps), abs_tol=1e-8)):
        raise WorkerError("invalid_duration", "15~30 정수 fps 및 15초 이하의 완전한 프레임 길이가 필요합니다.")
    frame_count = round(seconds * fps)
    vae_fp16 = request.get("vae_fp16", True)
    if not isinstance(vae_fp16, bool):
        raise WorkerError("invalid_precision", "vae_fp16은 boolean이어야 합니다.")
    max_side = request.get("max_side", 512)
    if isinstance(max_side, bool) or not isinstance(max_side, int) or not 128 <= max_side <= 720:
        raise WorkerError("invalid_source_size", "max_side는 128~720 정수여야 합니다.")
    with wave.open(str(audio), "rb") as wav:
        audio_seconds = wav.getnframes() / wav.getframerate()
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (48000, 1, 2):
            raise WorkerError("invalid_audio", "48kHz/mono/PCM16 WAV가 필요합니다.")
        if abs(audio_seconds - seconds) > 1 / 48000 + 1e-8:
            raise WorkerError("invalid_audio", "음성이 지정한 시험 길이와 일치하지 않습니다.")
    needed = ["musetalk/models/vae.py", "musetalk/models/unet.py",
              "models/musetalkV15/unet.pth", "models/musetalkV15/musetalk.json",
              "models/sd-vae/config.json", "models/sd-vae/diffusion_pytorch_model.bin",
              "models/whisper/config.json", "models/whisper/preprocessor_config.json",
              "models/whisper/pytorch_model.bin", "models/face-parse-bisent/79999_iter.pth",
              "models/face-parse-bisent/resnet18-5c106cde.pth"]
    missing = [name for name in needed if not (source_root / name).is_file()]
    if missing:
        raise WorkerError("missing_local_assets", "로컬 소스/가중치 누락: " + ", ".join(missing))
    source_info = json.loads(checked(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                      "-show_entries", "stream=width,height", "-of", "json", str(source)]))
    streams = source_info.get("streams", [])
    if (not streams or not streams[0].get("width") or not streams[0].get("height")
            or max(streams[0]["width"], streams[0]["height"]) > 4096):
        raise WorkerError("invalid_source_size", "긴 변 4096px 이하 진행자 자산이 필요합니다.")
    original_width, original_height = streams[0]["width"], streams[0]["height"]
    resize_ratio = min(1.0, max_side / max(original_width, original_height))
    resized_width = max(2, int(original_width * resize_ratio) // 2 * 2)
    resized_height = max(2, int(original_height * resize_ratio) // 2 * 2)
    metrics.update({"source_kind": kind, "source_path": str(source), "audio_path": str(audio),
                    "output_path": str(output), "source_root": str(source_root),
                    "fps": fps, "target_seconds": seconds, "target_frames": frame_count,
                    "batch_size": 1, "unet_dtype": "float32",
                    "vae_dtype": "float16" if vae_fp16 else "float32",
                    "body_motion_generated": False,
                    "static_portrait_mouth_only": kind == "reference_image",
                    "motion_note": "정지 이미지는 입 주변만 합성; 영상은 원본 움직임 재사용.",
                    "source_width": original_width, "source_height": original_height,
                    "max_side": max_side, "inference_width": resized_width,
                    "inference_height": resized_height, "source_asset_modified": False,
                    "mps_peak_method": "100ms sampling + stage/frame boundaries; observed maximum",
                    "cpu_fallback_enabled": False})
    # Set before importing torch/transformers: models must already exist locally.
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "HF_HUB_DISABLE_TELEMETRY": "1", "PYTORCH_ENABLE_MPS_FALLBACK": "0",
                       "GLOG_minloglevel": "2", "TOKENIZERS_PARALLELISM": "false"})
    metrics["stage"] = "imports"
    save_metrics(metrics_path, metrics)
    import cv2
    import numpy as np
    import torch

    if not torch.backends.mps.is_available():
        raise WorkerError("mps_unavailable", "MPS를 사용할 수 없습니다. CPU 추론으로 대체하지 않습니다.")
    torch.set_num_threads(2)
    torch.manual_seed(42)
    sys.path.insert(0, str(source_root))
    os.chdir(source_root)  # Vendor's face parser has relative local weight paths.
    from musetalk.utils.audio_processor import AudioProcessor
    from musetalk.utils.blending import get_image_blending, get_image_prepare_material
    from musetalk.utils.face_parsing import FaceParsing
    from musetalk.utils.preprocessing import coord_placeholder, get_landmark_and_bbox
    from musetalk.utils.utils import load_all_model
    from transformers import WhisperModel

    device = torch.device("mps")
    metrics.update({"device": "mps", "torch_version": torch.__version__, "seed": 42})
    sampler = MemorySampler(torch, metrics)
    sampler.thread.start()
    process = None
    try:
        def stage(name):
            torch.mps.synchronize()
            sampler.observe()
            metrics["stage"] = name
            save_metrics(metrics_path, metrics)
            return time.perf_counter()

        started = stage("model_load")
        # CPU deserialization avoids mapping .pth storages onto MPS before the
        # live module is populated. All neural inference below is explicitly MPS.
        vae, unet, pe = load_all_model(device=torch.device("cpu"))
        unet.model = unet.model.eval().to(device=device, dtype=torch.float32)
        pe = pe.eval().to(device=device, dtype=torch.float32)
        vae.vae = vae.vae.eval().to(device=device, dtype=torch.float16 if vae_fp16 else torch.float32)
        vae.device = device
        whisper = WhisperModel.from_pretrained(str(source_root / "models/whisper"),
                                               local_files_only=True)
        whisper = whisper.eval().to(device=device, dtype=torch.float32)
        whisper.requires_grad_(False)
        audio_processor = AudioProcessor(feature_extractor_path=str(source_root / "models/whisper"))
        fp = FaceParsing(left_cheek_width=90, right_cheek_width=90)
        if fp.device.type != "mps":
            raise WorkerError("device_mismatch", "FaceParsing 모델이 MPS에 올라가지 않았습니다.")
        torch.mps.synchronize()
        metrics["model_load_seconds"] = time.perf_counter() - started
        sampler.observe()
        with tempfile.TemporaryDirectory(prefix="headline60-lipsync-") as temporary:
            temp = Path(temporary)
            started = stage("audio_features")
            with torch.inference_mode():
                features, samples = audio_processor.get_audio_feature(str(audio))
                prompts = audio_processor.get_whisper_chunk(features, device, torch.float32,
                                                            whisper, samples, fps=fps)
            if len(prompts) != frame_count:
                raise WorkerError("audio_frame_mismatch", f"음성특징 {len(prompts)} != {frame_count}프레임")
            # Audio encoder is no longer needed while rendering mouth frames.
            prompts = prompts.detach().cpu()
            whisper.to("cpu")
            del whisper, audio_processor, features
            gc.collect()
            torch.mps.empty_cache()
            torch.mps.synchronize()
            metrics["audio_features_seconds"] = time.perf_counter() - started

            started = stage("face_preparation")
            if kind == "reference_image":
                original = cv2.imread(str(source))
                if original is None:
                    raise WorkerError("invalid_source_image", "참조 이미지를 읽을 수 없습니다.")
                resized = cv2.resize(original, (resized_width, resized_height),
                                     interpolation=cv2.INTER_AREA)
                resized_path = temp / "reference-resized.png"
                if not cv2.imwrite(str(resized_path), resized):
                    raise WorkerError("image_write_failed", "임시 참조 이미지를 저장할 수 없습니다.")
                del original, resized
                inputs = [str(resized_path)]
            else:
                checked(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                         "-i", str(source), "-t", str(seconds), "-vf",
                         f"scale={resized_width}:{resized_height},fps={fps}",
                         "-frames:v", str(frame_count), str(temp / "input-%05d.png")])
                inputs = [str(path) for path in sorted(temp.glob("input-*.png"))]
            if not inputs:
                raise WorkerError("missing_frames", "기본 영상에서 프레임을 얻지 못했습니다.")
            coords, frames = get_landmark_and_bbox(inputs, 0)
            latents, masks, crops, boxes = [], [], [], []
            shape = None
            with torch.inference_mode():
                for index, (bbox, frame) in enumerate(zip(coords, frames, strict=True)):
                    if frame is None or bbox == coord_placeholder:
                        raise WorkerError("face_not_found", f"원본 {index}번 프레임에서 얼굴을 찾지 못했습니다.")
                    if shape is None:
                        shape = frame.shape
                    if frame.shape != shape or shape[0] % 2 or shape[1] % 2:
                        raise WorkerError("invalid_frame_size", "동일한 짝수 크기의 프레임이 필요합니다.")
                    if max(shape[:2]) > 1280:
                        raise WorkerError("oversized_source", "5초 시험은 긴 변 1280px 이하 자산을 사용하세요.")
                    x1, y1, x2, y2 = map(int, bbox)
                    y2 = min(y2 + 10, frame.shape[0])
                    face = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                    latents.append(vae.get_latents_for_unet(face).detach().cpu())
                    mask, crop = get_image_prepare_material(frame, [x1, y1, x2, y2], fp=fp, mode="jaw")
                    masks.append(mask)
                    crops.append(crop)
                    boxes.append((x1, y1, x2, y2))
                    sampler.observe()
            fp.net.to("cpu")
            del fp
            gc.collect()
            torch.mps.empty_cache()
            torch.mps.synchronize()
            metrics.update({"face_preparation_seconds": time.perf_counter() - started,
                            "source_frames": len(frames), "width": shape[1], "height": shape[0]})
            # Index the existing frames in a ping-pong cycle without duplicating arrays.
            cycle = list(range(len(frames))) + list(reversed(range(len(frames))))
            silent = temp / "silent.mp4"
            stderr = temp / "ffmpeg.stderr"
            started = stage("inference_and_render")
            infer_seconds, blend_seconds, pipe_seconds = 0.0, 0.0, 0.0
            timesteps = torch.tensor([0], device=device)
            with stderr.open("wb") as error_stream:
                process = subprocess.Popen([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{shape[1]}x{shape[0]}",
                    "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-threads", "2",
                    "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", str(silent),
                ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error_stream, shell=False)
                with torch.inference_mode():
                    for index in range(frame_count):
                        source_index = cycle[index % len(cycle)]
                        tick = time.perf_counter()
                        feature = pe(prompts[index:index + 1].to(device=device, dtype=torch.float32))
                        latent = latents[source_index].to(device=device, dtype=torch.float32)
                        predicted = unet.model(latent, timesteps, encoder_hidden_states=feature).sample
                        reconstructed = vae.decode_latents(predicted)[0]
                        torch.mps.synchronize()
                        infer_seconds += time.perf_counter() - tick
                        tick = time.perf_counter()
                        x1, y1, x2, y2 = boxes[source_index]
                        face = cv2.resize(reconstructed, (x2 - x1, y2 - y1))
                        combined = get_image_blending(frames[source_index], face, boxes[source_index],
                                                      masks[source_index], crops[source_index])
                        blend_seconds += time.perf_counter() - tick
                        tick = time.perf_counter()
                        try:
                            process.stdin.write(np.ascontiguousarray(combined).tobytes())
                        except BrokenPipeError as exc:
                            raise WorkerError("ffmpeg_failed", stderr.read_text(errors="replace")[-4000:]) from exc
                        pipe_seconds += time.perf_counter() - tick
                        del feature, latent, predicted, reconstructed, combined, face
                        sampler.observe()
                        metrics["rendered_frames"] = index + 1
                        if (index + 1) % fps == 0:
                            save_metrics(metrics_path, metrics)
                process.stdin.close()
                if process.wait(timeout=60):
                    raise WorkerError("ffmpeg_failed", stderr.read_text(errors="replace")[-4000:])
                process = None
            metrics.update({"inference_seconds": infer_seconds, "blend_seconds": blend_seconds,
                            "ffmpeg_pipe_seconds": pipe_seconds,
                            "inference_and_render_seconds": time.perf_counter() - started})
            started = stage("mux_and_verify")
            final = temp / "final.mp4"
            checked(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-i", str(silent), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                     "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-b:a", "160k",
                     "-t", str(seconds), "-movflags", "+faststart", str(final)])
            info = json.loads(checked(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
                                       "-show_format", "-of", "json", str(final)]))
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            audio_info = next(stream for stream in info["streams"] if stream["codec_type"] == "audio")
            if (int(video.get("nb_read_frames", 0)) != frame_count
                    or video["codec_name"] != "h264" or audio_info["codec_name"] != "aac"
                    or (video["width"], video["height"]) != (resized_width, resized_height)
                    or video.get("avg_frame_rate") != f"{fps}/1"
                    or audio_info.get("sample_rate") != "48000" or audio_info.get("channels") != 1
                    or abs(float(info["format"]["duration"]) - seconds) > 1 / fps):
                raise WorkerError("output_verification_failed", "출력 프레임/코덱/길이가 시험 조건과 다릅니다.")
            checked(["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(final),
                     "-f", "null", "-"])
            # Publish a complete file exclusively, including across filesystems.
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".",
                                             suffix=".partial") as publish:
                with final.open("rb") as src:
                    shutil.copyfileobj(src, publish)
                publish.flush()
                os.fsync(publish.fileno())
                os.link(publish.name, output)  # fails if another result now exists
            metrics.update({"mux_and_verify_seconds": time.perf_counter() - started,
                            "output_bytes": output.stat().st_size, "ffprobe": info,
                            "status": "completed", "stage": "completed"})
            metrics["render_seconds"] = (metrics["inference_and_render_seconds"] - infer_seconds
                                         + metrics["mux_and_verify_seconds"])
            metrics["render_seconds_scope"] = "blend + encoder piping/wait + mux + validation; excludes inference"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        try:
            sampler.finish()
        except Exception as exc:  # noqa: BLE001 - preserve the original failure, if any.
            metrics["memory_sampling_error"] = str(exc)
    return metrics_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    metrics = {"status": "running", "stage": "request", "worker_version": "musetalk-smoke-v1"}
    exit_code = 0
    try:
        run_request(args.request.expanduser().resolve(), metrics)
    except BaseException as exc:  # noqa: BLE001 - vendor code may raise SystemExit; always record failure.
        exit_code = 1
        metrics.update({"status": "failed", "error": {"code": getattr(exc, "code", "worker_failed"),
                        "type": type(exc).__name__, "message": str(exc),
                        "traceback": traceback.format_exc()[-12000:]}})
    finally:
        metrics["total_seconds"] = time.perf_counter() - started
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        metrics["peak_rss_bytes"] = int(peak if sys.platform == "darwin" else peak * 1024)
        metrics["peak_rss_scope"] = "worker process; child FFmpeg RSS excluded"
        save_metrics(Path(metrics["metrics_path"]) if metrics.get("metrics_path") else None, metrics)
        print(json.dumps({"status": metrics["status"], "metrics_path": metrics.get("metrics_path"),
                          "error": metrics.get("error")}, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
