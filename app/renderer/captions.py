import json
import re
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import Settings
from app.media.cards import load_font, wrap
from app.process import save_json
from app.renderer.ffmpeg import stamp
from app.script.models import Script


def create_captions(script: Script, folder: Path, frames: list[int], config: Settings, width: int) -> list[dict]:
    utterances = json.loads((folder / "utterances.json").read_text())
    height = width * 16 // 9
    scale = width / 720
    cues, scene_start = [], 0.0
    for index, (scene, count) in enumerate(zip(script.scenes, frames, strict=True)):
        scene_seconds = count / 30
        raw = utterances[index]
        ratio = scene_seconds / raw["duration"]
        if scene.caption_overrides:
            parts = [cue.model_dump() for cue in scene.caption_overrides]
        else:
            parts = [{"text": part["text"], "start": part["start"] * ratio,
                      "end": part["end"] * ratio, "highlight": re.findall(r"\d[\d.,]*\s*(?:%|억|만|원|건)?", part["text"])}
                     for part in raw["parts"]]
        for part in parts:
            if part["start"] < 0 or part["end"] > scene_seconds + .02 or part["end"] <= part["start"]:
                raise ValueError(f"장면 {index + 1}: 자막 시간이 실제 장면 {scene_seconds:.2f}초 범위를 벗어났습니다.")
            font = load_font(config.font, int(35 * scale), True)
            lines = wrap(part["text"], font, int(580 * scale)).splitlines()
            if len(lines) > 2:
                font = load_font(config.font, int(33 * scale), True)
                lines = wrap(part["text"], font, int(580 * scale)).splitlines()
            if len(lines) > 2:
                raise ValueError(f"장면 {index + 1}: 자막이 두 줄을 넘습니다. 구절을 더 짧게 나눠 주세요.")
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            left, top, right, bottom = [int(x * scale) for x in (44, 920, 666, 1084)]
            draw.rounded_rectangle((left, top, right, bottom), radius=int(17 * scale),
                                   fill=(23, 29, 37, 248), outline=(62, 74, 88, 255), width=max(1, int(scale)))
            draw.rounded_rectangle((left + int(10 * scale), top + int(27 * scale),
                                    left + int(14 * scale), bottom - int(27 * scale)),
                                   radius=max(1, int(2 * scale)), fill=(37, 78, 255, 255))
            line_height = int(49 * scale)
            text_height = len(lines) * line_height
            text_top = top + ((bottom - top) - text_height) / 2 - int(2 * scale)
            for line_index, line in enumerate(lines):
                x = (left + right - font.getlength(line)) / 2
                y = text_top + line_index * line_height
                for token in re.split(r"(\s+)", line):
                    emphasis = any(word and word in token for word in part.get("highlight", []))
                    draw.text((x, y), token, font=font, fill="#eaff72" if emphasis else "#ffffff")
                    x += font.getlength(token)
            cue_index = len(cues)
            image.save(folder / f"caption-{cue_index:03}.png")
            cues.append({**part, "scene": index, "scene_start": scene_start,
                         "start": round(scene_start + part["start"], 4),
                         "end": round(scene_start + part["end"], 4),
                         "image": f"caption-{cue_index:03}.png"})
        scene_start += scene_seconds
    save_json(folder / "captions.json", {"alignment": "measured synthesized phrase boundaries, scaled with audio tempo; not word alignment",
        "safe_area": {"top": 920, "bottom": 1084, "base_height": 1280}, "cues": cues})
    (folder / "subtitles.srt").write_text("\n".join(
        f"{i + 1}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['text']}\n"
        for i, cue in enumerate(cues)), encoding="utf-8")
    return cues
