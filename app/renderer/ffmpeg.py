import json
import math
from itertools import pairwise
from pathlib import Path

from app.config import Settings
from app.process import (
    cache_path,
    check_cancel,
    digest,
    file_digest,
    probe,
    restore_cached,
    run,
    save_json,
    store_cached,
)
from app.script.models import Script


def timeline(lengths: list[float], target: int) -> tuple[list[int], float]:
    total_frames = target * 30
    if not lengths or any(not math.isfinite(x) or x <= 0 for x in lengths):
        raise ValueError("유효한 음성 길이가 필요합니다.")
    speed = sum(lengths) / target
    if not 0.5 <= speed <= 2.0:
        raise ValueError(f"음성이 목표 길이에 맞지 않습니다 (배속 {speed:.2f}). 대본을 조정하세요.")
    boundaries = [0]
    elapsed = 0.0
    for length in lengths:
        elapsed += length
        boundaries.append(round(elapsed / sum(lengths) * total_frames))
    frames = [b - a for a, b in pairwise(boundaries)]
    if min(frames) < 12:
        raise ValueError("장면이 지나치게 짧습니다.")
    return frames, speed


def stamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    sec, ms = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02},{ms:03}"


def write_subtitles(script: Script, frames: list[int], path: Path) -> None:
    start = 0
    lines = []
    for index, (scene, count) in enumerate(zip(script.scenes, frames, strict=True)):
        lines.append(f"{index + 1}\n{stamp(start / 30)} --> {stamp((start + count) / 30)}\n"
                     f"{scene.narration}\n")
        start += count
    path.write_text("\n".join(lines), encoding="utf-8")


def encoder_args(config: Settings) -> list[str]:
    if config.encoder == "h264_videotoolbox":
        return ["-c:v", config.encoder, "-b:v", "5M", "-allow_sw", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "2"]


def caption_frame_ranges(cues: list[dict], scene_start: float,
                         count: int) -> list[tuple[int, int]]:
    """Sample half-open cue intervals on the renderer's 30fps frame grid.

    captions.json stores global times to four decimal places. Only that rounding
    tolerance is snapped to a nearby frame boundary; other manual times retain
    their first eligible frame (ceil), including deliberate gaps. Shared cue
    endpoints map identically, so adjacent captions cannot flash or overlap.
    """
    def frame_at(seconds: float) -> int:
        if not math.isfinite(seconds):
            raise ValueError("자막 시간은 유한한 숫자여야 합니다.")
        nearest = round(seconds * 30)
        # Half of the 0.0001s serialization step, plus floating point tolerance.
        if abs(seconds - nearest / 30) <= 0.000050001:
            frame = nearest
        else:
            frame = math.ceil(seconds * 30)
        return max(0, min(count, frame))

    return [(frame_at(cue["start"] - scene_start),
             frame_at(cue["end"] - scene_start)) for cue in cues]


def render(folder: Path, frames: list[int], lengths: list[float], width: int,
           config: Settings) -> Path:
    height = width * 16 // 9
    reused = 0
    captions = json.loads((folder / "captions.json").read_text())["cues"] if (folder / "captions.json").is_file() else []
    scene_start_frame = 0
    for index, (count, length) in enumerate(zip(frames, lengths, strict=True)):
        check_cancel()
        segment = folder / f"segment-{index:02}.mov"
        scene_cues = [cue for cue in captions if cue["scene"] == index]
        cue_frames = caption_frame_ranges(scene_cues, scene_start_frame / 30, count)
        key = digest(["segment-v3-frame-captions", file_digest(folder / f"card-{index:02}.png"),
                      file_digest(folder / f"speech-{index:02}.aiff"),
                      count, length, width, config.encoder,
                      [(start, end, file_digest(folder / cue["image"]))
                       for cue, (start, end) in zip(scene_cues, cue_frames, strict=True)]])
        cached = cache_path(config.data, "segments", key, ".mov")
        if restore_cached(cached, segment):
            reused += 1
            scene_start_frame += count
            continue
        seconds = count / 30
        fade_out = max(0, seconds - 0.12)
        filters = (
            f"zoompan=z='min(1+on*0.00003,1.025)':"
            f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={count}:s={width}x{height}:fps=30,"
            f"fade=t=in:st=0:d=0.12,fade=t=out:st={fade_out:.6f}:d=0.12,format=yuv420p"
        )
        caption_inputs = []
        filter_args = ["-vf", filters]
        if scene_cues:
            chain = [f"[0:v]{filters}[base]"]
            previous = "base"
            for cue_index, (cue, (start, end)) in enumerate(
                    zip(scene_cues, cue_frames, strict=True)):
                caption_inputs.extend(["-loop", "1", "-i", str(folder / cue["image"])])
                output = f"sub{cue_index}"
                chain.append(f"[{previous}][{cue_index + 2}:v]overlay=0:0:enable='gte(n,{start})*lt(n,{end})'[{output}]")
                previous = output
            chain.append(f"[{previous}]format=yuv420p[out]")
            filter_args = ["-filter_complex_threads", "1", "-filter_complex", ";".join(chain),
                           "-map", "[out]", "-map", "1:a"]
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(folder / f"card-{index:02}.png"),
             "-i", str(folder / f"speech-{index:02}.aiff"),
             *caption_inputs, *filter_args, "-af", f"atempo={length / seconds:.8f},apad,atrim=duration={seconds:.8f}",
             *encoder_args(config), "-c:a", "pcm_s16le", "-ar", "48000",
             "-ac", "1", "-t", f"{seconds:.8f}", "-movflags", "+faststart",
             str(segment)], timeout=240)
        store_cached(segment, cached)
        scene_start_frame += count
    save_json(folder / "render-cache.json", {"reused": reused, "total": len(frames)})
    listing = folder / "segments.txt"
    listing.write_text("".join(f"file 'segment-{i:02}.mov'\n" for i in range(len(frames))),
                       encoding="utf-8")
    temporary = folder / "output.partial.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
         "-i", str(listing), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-t", f"{sum(frames) / 30:.8f}", "-movflags", "+faststart", str(temporary)])
    output = folder / "output.mp4"
    temporary.replace(output)
    return output


def verify(path: Path, width: int, seconds: int) -> dict:
    info = probe(path)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    if (video.get("codec_name") != "h264" or video.get("width") != width
            or video.get("height") != width * 16 // 9
            or video.get("avg_frame_rate") != "30/1" or audio.get("codec_name") != "aac"
            or abs(float(info["format"]["duration"]) - seconds) > 0.2):
        raise RuntimeError("영상 규격 검증 실패: H.264 / AAC / 9:16 / 30fps / 지정 길이를 확인하세요.")
    # Decode all frames and audio, beyond checking only the container header.
    run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], timeout=180)
    return info
