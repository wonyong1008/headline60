"""Translate reviewed scene metadata and measured speech into a render plan."""
import json
import math
import re
import wave
from array import array
from pathlib import Path

from app.config import Settings
from app.media.cards import load_font, wrap
from app.process import run, save_json
from app.script.models import Script
from app.tts.engine import join_wavs
from app.tts.text import normalize, phrases


def natural_timeline(lengths: list[float], speed: float = 1.0) -> tuple[list[int], float]:
    if not lengths or any(not math.isfinite(x) or x <= 0 for x in lengths):
        raise ValueError("유효한 음성 길이가 필요합니다.")
    if not math.isfinite(speed) or not 1 <= speed <= 1.35:
        raise ValueError("음성 속도는1.00~1.35 범위여야 합니다.")
    frames = [math.ceil(x / speed * 30 - 1e-8) for x in lengths]
    if sum(frames) / 30 > 90:
        raise ValueError("음성이 90초를 넘습니다. 30~60초 목표에 맞게 대본을 줄여 주세요.")
    return frames, speed


def pause_points(path: Path) -> list[float]:
    """Measured quiet intervals, not speech recognition or word alignment."""
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            return []
        rate = audio.getframerate()
        samples = array("h", audio.readframes(audio.getnframes()))
    step = max(1, rate // 50)
    quiet = [sum(abs(x) for x in samples[i:i + step]) / max(1, len(samples[i:i + step])) < 180
             for i in range(0, len(samples), step)]
    result, begin = [], None
    for i, silent in enumerate([*quiet, False]):
        if silent and begin is None:
            begin = i
        elif not silent and begin is not None:
            if i - begin >= 3 and begin > 0 and i < len(quiet):
                result.append((begin + i) * .01)
            begin = None
    return result


def scene_captions(scene, duration: float, audio: Path | None = None,
                   speed: float = 1, config: Settings | None = None, dynamic: bool = False) -> list[dict]:
    if scene.caption_overrides:
        ratio = scene.caption_reference_speed / speed
        cues = [{**cue.model_dump(), "start": cue.start * ratio, "end": cue.end * ratio}
                for cue in scene.caption_overrides]
        if any(cue["end"] > duration + 1 / 30 for cue in cues):
            raise ValueError("편집한 자막이 실제 음성 장면 길이를 넘습니다. 자막 시간을 조정하세요.")
        return cues
    chunks = phrases(scene.subtitle or scene.narration, limit=22 if dynamic else 24)
    if config:
        font = load_font(config.font, 42 if dynamic else 38, True)
        if any(len(wrap(text, font, 560).splitlines()) > 2 for text in chunks):
            raise ValueError("자동 자막이 두 줄을 넘습니다. 긴 고유명사·띄어쓰기를 검토하세요.")
    weights = [max(1, len(normalize(text).replace(" ", ""))) for text in chunks]
    pauses = pause_points(audio) if audio else []
    cues, cursor, cumulative = [], 0.0, 0
    for i, (text, weight) in enumerate(zip(chunks, weights, strict=True)):
        cumulative += weight
        target = duration * cumulative / sum(weights)
        if i == len(chunks) - 1:
            end = duration
        else:
            nearby = [mark for mark in pauses if abs(mark - target) <= .28 and mark > cursor + .25
                      and duration - mark > (len(chunks) - i - 1) * .20]
            end = min(nearby, key=lambda mark: abs(mark - target)) if nearby else target
        emphasis = [word for word in scene.emphasis_words if word in text]
        if dynamic:
            emphasis += re.findall(r"\d[\d,.]*(?:%p|%|퍼센트포인트|퍼센트|만호|억원|원|호)?", text)
        cues.append({"text": text, "start": round(cursor, 4), "end": round(end, 4),
                     "highlight": list(dict.fromkeys(emphasis))[:6]})
        cursor = end
    return cues


VISUAL_TERMS = (
    ("은행", "금리", "기준금리", "bank", "banking", "interest", "finance", "financial"),
    ("인출", "인출기", "현금", "atm", "withdrawal", "cash"),
    ("대출", "상담", "loan", "consultation", "advisor", "mortgage"),
    ("아파트", "주택", "단지", "apartment", "housing", "residential"),
    ("자동차", "차량", "vehicle", "automobile"),
)


def select_visual(scene, assets: list[dict], index: int = 0) -> dict | None:
    """Rank registered footage by visual intent; category alone never supplies relevance."""
    query = f"{scene.visual_query} {scene.headline_text} {scene.narration}".lower()
    words = set(re.findall(r"[a-z0-9가-힣]{2,}", scene.visual_query.lower()))
    ranked = []
    for asset in assets:
        if scene.visual_type == "stock_video" and asset["media_type"] != "video":
            continue
        text = " ".join([asset.get("name", ""), asset.get("description", ""), *asset.get("tags", [])]).lower()
        score = sum(4 for word in words if word in text)
        for group in VISUAL_TERMS:
            if any(term in query for term in group) and any(term in text for term in group):
                score += 2
        if score:
            score += int(asset.get("license_type") in {"user-owned", "permission-granted"})
            ranked.append((score, asset))
    if not ranked:
        return None
    best = max(score for score, _ in ranked)
    matches = [asset for score, asset in ranked if score == best]
    return matches[index % len(matches)]


def resolve_visuals(config: Settings, script: Script, category: str, modern: bool = True) -> list[dict | None]:
    from app.media.library import listing, resolve_asset
    assets = [a for a in listing(config) if a["usage_status"] == "approved"
              and a["media_type"] in {"image", "video"} and a["category"] == category]
    result, usage, previous = [], {}, None
    for i, scene in enumerate(script.scenes):
        asset = None
        if scene.asset_id:
            asset = resolve_asset(config, scene.asset_id)
            if asset["media_type"] not in {"image", "video"}:
                raise ValueError("장면에는 사진 또는 영상 자료를 선택하세요.")
        else:
            available = [a for a in assets if not modern or usage.get(a["asset_id"], 0) < 2]
            candidate = select_visual(scene, [a for a in available if a["asset_id"] != previous], i)
            candidate = candidate or select_visual(scene, available, i)
            if candidate:
                asset = resolve_asset(config, candidate["asset_id"])
        result.append(asset)
        previous = asset["asset_id"] if asset else None
        if asset:
            usage[asset["asset_id"]] = usage.get(asset["asset_id"], 0) + 1
    return result


def preview_visuals(config: Settings, draft: dict) -> list[dict]:
    if not draft.get("script"):
        return []
    script = Script.model_validate(draft["script"])
    assets = resolve_visuals(config, script, draft["category"], draft.get("edit_style") == "reels_v3")
    return [{"scene": i, "asset_id": asset["asset_id"] if asset else None,
             "name": asset["name"] if asset else None,
             "url": f"/api/media/{asset['asset_id']}/file" if asset else None,
             "media_type": asset["media_type"] if asset else None,
             "mode": "manual" if scene.asset_id else "automatic" if asset else "graphic"}
            for i, (scene, asset) in enumerate(zip(script.scenes, assets, strict=True))]


def build_plan(script: Script, folder: Path, request, config: Settings, news: dict | None = None) -> dict:
    from app.media.library import resolve_asset
    timing = json.loads((folder / "timing.json").read_text())
    frames = timing["frames"]
    category = (news or {}).get("category", request.category)
    modern = request.edit_style == "reels_v3"
    assets = resolve_visuals(config, script, category, modern)
    used, scenes, global_cues = {}, [], []
    audio_files = []
    cursor = 0.0
    evidence = {e["id"]: e for e in (news or {}).get("evidence", [])}
    for i, (scene, count) in enumerate(zip(script.scenes, frames, strict=True)):
        duration = count / 30
        audio = folder / f"reel-speech-{i:02}.wav"
        run(["ffmpeg", "-v", "error", "-y", "-i", str(folder / f"speech-{i:02}.wav"),
             "-af", f"atempo={request.narration_speed:.8f},apad,atrim=duration={duration:.8f}", "-ar", "48000", "-ac", "1",
             "-c:a", "pcm_s16le", str(audio)])
        audio_files.append(audio)
        asset = assets[i]
        credit = ""
        if asset:
            used[asset["asset_id"]] = asset
            credit = f"{asset['attribution'].split(' · ')[0]} · {asset['license_type']}"
        chart = scene.chart.model_dump() if scene.chart else None
        if chart:
            refs = [evidence[key] for key in chart["source_refs"] if key in evidence]
            if len(refs) != len(set(chart["source_refs"])):
                raise ValueError("차트 근거가 없습니다. 출처를 연결하거나 설명형 화면을 사용하세요.")
            chart["source_label"] = " · ".join(dict.fromkeys(e["publisher"] for e in refs))[:100]
        captions = scene_captions(scene, duration, audio, request.narration_speed, config, request.subtitle_style == "dynamic")
        lines = (scene.headline_text or scene.headline).splitlines()[:2]
        item = {"asset_path": asset["local_path"] if asset else None,
                "media_type": asset["media_type"] if asset else "graphic", "duration": duration,
                "visual_first": bool(asset) and not chart,
                "hook_scene": modern and i == 0,
                "hook_headline_text": scene.headline_text or scene.headline,
                "eyebrow": "창작 장면" if request.content_mode == "adult_satire" else "속설 검증 · 근거 확인" if request.content_mode == "myth_check" else "해외 자료화면" if asset and "해외" in asset.get("description", "") else "핵심 요약" if i == len(script.scenes) - 1 else request.category,
                "lines": lines, "accent_line": 0, "credit": credit,
                "narration": scene.narration, "captions": captions,
                "visual_type": scene.visual_type,
                "animation_type": ("pop" if i == 0 else "number" if chart else "slide_up" if scene.story_role == "meaning" else "fade") if modern else scene.animation_type,
                "transition_type": "cut" if modern and (i == 0 or chart) else "fade" if modern and asset else scene.transition_type,
                "chart": chart,
                "emphasis_words": scene.emphasis_words, "source_refs": scene.source_refs or scene.claim_ids,
                "source_label": " · ".join(dict.fromkeys(evidence[k]["publisher"] for k in scene.claim_ids if k in evidence)),
                "as_of": " · ".join(dict.fromkeys(evidence[k]["published_date"] for k in scene.claim_ids if k in evidence))}
        scenes.append(item)
        global_cues.extend({**cue, "scene": i, "start": round(cursor + cue["start"], 4),
                            "end": round(cursor + cue["end"], 4)} for cue in captions)
        cursor += duration
    narration = folder / "narration.wav"
    join_wavs(audio_files, narration)
    music = resolve_asset(config, request.music_asset_id, "audio") if request.music_asset_id else None
    if music:
        used[music["asset_id"]] = music
    plan = {"width": request.resolution, "fps": 30, "template": request.template,
            "category": "성인 공감 코미디" if request.content_mode == "adult_satire" else "속설 검증" if request.content_mode == "myth_check" else request.category, "title": script.title, "subtitle_style": request.subtitle_style,
            "edit_style": request.edit_style, "narration_speed": request.narration_speed,
            "text_style": getattr(request, "text_style", "classic"),
            "content_mode": request.content_mode,
            "audio_path": str(narration), "music_path": music["local_path"] if music else None,
            "scenes": scenes}
    from app.anchors.production import prepare_presenter
    prepare_presenter(plan, folder, config, getattr(request, "anchor_id", None))
    save_json(folder / "reels-plan.json", plan)
    save_json(folder / "captions.json", {"alignment": "estimated spoken-length timing snapped to measured quiet intervals within 280ms; manually editable, not word alignment", "cues": global_cues})
    save_json(folder / "media-sources.json", list(used.values()))
    still = request.edit_style == "reels_v3"
    # Reels v3 keeps photos fixed; credits must describe the edits actually applied.
    credits = [f"{a.get('name', a['asset_id'])}: {a['attribution'].replace(', zoom/pan', '') if still else a['attribution']} / {a['license_type']}\n{a['source_url']}\n{a.get('license_url', '')}" for a in used.values()]
    edits = "크롭·자막·텍스트 모션을 추가했습니다(사진은 고정)." if still else "크롭·줌/팬·자막·모션을 추가했습니다."
    (folder / "CREDITS.md").write_text(f"# 자료 출처\n\n사진·영상은 자료 화면입니다. {edits}\n\n" + "\n\n".join(credits) + ("\n\nCC BY-SA 자료를 사용한 영상 편집물은 CC BY-SA 4.0으로 제공합니다.\n" if any(a["license_type"].startswith("CC-BY-SA") for a in used.values()) else ""))
    return plan
