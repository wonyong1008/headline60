"""Caption frame boundary regressions; synthetic media only, no AI inference."""

import json
import math
import shutil
import subprocess

import pytest
from PIL import Image

from app.config import Settings
from app.renderer.ffmpeg import caption_frame_ranges, render


@pytest.mark.parametrize("start_frame", [0, 1, 2, 108, 270, 452, 598, 744])
def test_rounded_scene_edges_keep_first_and_last_frames(start_frame):
    start, count = start_frame / 30, 146
    cues = [{"start": round(start, 4), "end": round(start + count / 30, 4)}]
    assert caption_frame_ranges(cues, start, count) == [(0, count)]


@pytest.mark.parametrize("boundary", [1 / 30, 2 / 30, 0.1234, 0.5678])
def test_adjacent_cues_share_half_open_frame_boundary(boundary):
    scene_start = 452 / 30
    common = round(scene_start + boundary, 4)
    cues = [
        {"start": round(scene_start, 4), "end": common},
        {"start": common, "end": round(scene_start + 1, 4)},
    ]
    first, second = caption_frame_ranges(cues, scene_start, 30)
    assert first[0] == 0 and first[1] == second[0] and second[1] == 30
    active = [sum(start <= n < end for start, end in (first, second)) for n in range(30)]
    assert active == [1] * 30


def test_manual_times_and_deliberate_gaps_keep_their_sampled_frames():
    cues = [
        {"start": 0.001, "end": 0.06666},
        {"start": 0.1002, "end": 0.173},
    ]
    # No nearest-frame quantization of user times: ceil preserves the first
    # visible sample at/after each true boundary, including the intended gap.
    assert caption_frame_ranges(cues, 0, 6) == [(1, 2), (4, 6)]


def test_only_serialization_sized_noise_is_snapped():
    cues = [
        {"start": 0.00005, "end": 0.10005},
        {"start": 0.0001, "end": 0.1001},
    ]
    assert caption_frame_ranges(cues, 0, 6) == [(0, 3), (1, 4)]


def test_cues_cannot_extend_beyond_the_scene_frame_range():
    assert caption_frame_ranges([{"start": -0.00003, "end": 0.20003}], 0, 6) == [(0, 6)]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_caption_times_fail_before_ffmpeg(bad):
    with pytest.raises(ValueError, match="유한"):
        caption_frame_ranges([{"start": bad, "end": 1}], 0, 30)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_actual_ffmpeg_overlay_covers_rounded_scene_edges_and_adjacent_cues(tmp_path):
    """Reproduce a +33us scene offset and inspect every decoded RGB frame."""
    width, height = 72, 128
    for index, frames in enumerate((2, 6)):
        Image.new("RGB", (width, height), "black").save(tmp_path / f"card-{index:02}.png")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", str(frames / 30), "-c:a", "pcm_s16be",
            str(tmp_path / f"speech-{index:02}.aiff"),
        ], check=True, capture_output=True, timeout=15)
    for name, color in (("red.png", "red"), ("green.png", "lime")):
        Image.new("RGBA", (width, height), color).save(tmp_path / name)
    # Scene 1 starts at 2/30, whose serialized 0.0667 is 33us too late.
    (tmp_path / "captions.json").write_text(json.dumps({"cues": [
        {"scene": 1, "start": 0.0667, "end": 0.1667, "image": "red.png"},
        {"scene": 1, "start": 0.1667, "end": 0.2667, "image": "green.png"},
    ]}))
    config = Settings(root=tmp_path, encoder="libx264")
    output = render(tmp_path, [2, 6], [2 / 30, 6 / 30], width, config)
    decoded = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(output),
        "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ], check=True, capture_output=True, timeout=15).stdout
    frame_bytes = width * height * 3
    assert len(decoded) == frame_bytes * 8
    for number in range(2, 8):
        # The centre is inside the opaque test caption, away from codec edges.
        offset = number * frame_bytes + ((height // 2) * width + width // 2) * 3
        red, green, blue = decoded[offset:offset + 3]
        if number < 5:
            assert red > 220 and green < 20 and blue < 20, (number, red, green, blue)
        else:
            assert green > 220 and red < 20 and blue < 20, (number, red, green, blue)
    # Cache reuse must preserve the corrected media and avoid fresh segment work.
    render(tmp_path, [2, 6], [2 / 30, 6 / 30], width, config)
    assert json.loads((tmp_path / "render-cache.json").read_text()) == {"reused": 2, "total": 2}
