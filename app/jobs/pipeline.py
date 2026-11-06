import contextvars
import fcntl
import json
import re
import shutil
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.content_modes import mode_metadata, validation_issues
from app.jobs import store
from app.llm.ollama import LLMError, generate
from app.media.cards import create_cards
from app.process import JobCancelled, check_cancel, save_json
from app.renderer.ffmpeg import render, timeline, verify, write_subtitles
from app.renderer.storyboard import build_plan, natural_timeline
from app.script.models import Script, VideoRequest, validate_explainer
from app.tts.macos import synthesize

STAGES = ("script", "tts", "cards", "render", "verify")


def config_signature(config: Settings) -> dict:
    result = {"model": config.model, "voice": config.voice, "tts_rate": config.tts_rate,
              "font": str(config.font), "encoder": config.encoder}
    if config.tts_profile:
        result["tts_profile"] = config.tts_profile
    return result


def private_preview(context: dict) -> bool:
    """Only server-authorized previews may omit human approval; evidence gates remain."""
    return (context.get("preview_only") is True and context.get("automation_private_preview") is True
            and context.get("render_authorized_revision") == context.get("revision")
            and isinstance(context.get("revision"), int) and context["revision"] > 0
            and context.get("approved_revision") is None and context.get("content_review") is None)


def prepare_news(config: Settings, request: VideoRequest, script: Script, context: dict) -> str:
    context = {"seconds": request.seconds, **context}
    if request.content_mode != context.get("content_mode", "news"):
        raise ValueError("콘텐츠 모드와 영상 제작 설정이 일치해야 합니다.")
    preview = private_preview(context)
    issues = validation_issues(context, script, require_review=not preview)
    if preview and context.get("automatic"):
        # Automatic private previews are judged by the user before publishing; keep issues as warnings.
        context["content_warnings"] = list(dict.fromkeys([*context.get("content_warnings", []), *issues]))
        issues = []
    if issues or (not preview and (not context.get("approved_revision") or context.get("approved_revision") != context.get("revision"))):
        raise ValueError("; ".join(issues) or "대본 검토 승인이 필요합니다.")
    job_id = datetime.now().strftime("%Y%m%dT%H%M%S-") + uuid4().hex[:8]
    folder = job_folder(config, job_id)
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {"id": job_id, "request": request.model_dump(), "completed_stages": ["script"],
                "status": "queued", "stage": "tts", "settings": config_signature(config),
                "created_at": datetime.now(UTC).isoformat(), "content_type": context.get("content_mode", "news"),
                "draft_id": context["id"], "revision": context["revision"], "timings": {},
                "validation_sample": bool(context.get("validation_sample")),
                "automation_private_preview": preview, "preview_only": preview,
                "render_authorized_revision": context.get("render_authorized_revision") if preview else None,
                **mode_metadata(context)}
    save_json(folder / "job.json", manifest)
    save_json(folder / "script.json", script.model_dump())
    save_json(folder / "news-context.json", context)
    save_json(folder / "sources.json", context["evidence"])
    store.update(config, job_id, request.topic, "queued", "tts", details=job_details(manifest))
    return job_id


def job_details(manifest: dict) -> dict:
    return {"content_mode": manifest.get("content_mode", "news"), "is_fiction": manifest.get("is_fiction", False),
            "category": manifest["request"]["category"],
            "created_at": manifest.get("created_at"),
            "draft_id": manifest.get("draft_id"), "revision": manifest.get("revision"),
            "template": manifest["request"].get("template", "legacy"),
            "actual_seconds": manifest.get("actual_seconds"),
            "edit_style": manifest["request"].get("edit_style", "classic"),
            "narration_speed": manifest["request"].get("narration_speed", 1.0),
            "resolution": manifest["request"]["resolution"],
            "seconds": manifest["request"]["seconds"], "timings": manifest.get("timings", {}),
            "completed_stages": manifest["completed_stages"],
            "content_type": manifest.get("content_type", "general_explainer"),
            "automation_private_preview": bool(manifest.get("automation_private_preview")),
            "review_required": bool(manifest.get("automation_private_preview")),
            "review_status": "unreviewed_preview" if manifest.get("automation_private_preview") else "sample_pending_user_review" if manifest.get("validation_sample") else "human_reviewed" if manifest.get("draft_id") else "unreviewed",
            "voice_profile_id": (manifest.get("settings", {}).get("tts_profile") or {}).get("voice_profile_id"),
            "has_phrase_captions": bool(manifest.get("settings", {}).get("tts_profile")),
            "output": f"/jobs/{manifest['id']}/output.mp4" if manifest["status"] == "completed" else None}


# Seconds to wait for the lock. Interactive calls fail fast (0); the URL queue sets a
# longer wait so a briefly busy lock never turns a queued item into a permanent failure.
LOCK_WAIT = contextvars.ContextVar("headline_lock_wait", default=0.0)


@contextmanager
def exclusive(config: Settings):
    config.data.mkdir(parents=True, exist_ok=True)
    with (config.data / "pipeline.lock").open("a") as handle:
        deadline = time.monotonic() + LOCK_WAIT.get()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("다른 영상 제작이 실행 중입니다. 완료 후 다시 시도하세요.") from exc
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def job_folder(config: Settings, job_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}-[a-f0-9]{8}", job_id):
        raise ValueError("잘못된 작업 ID입니다.")
    return config.outputs / job_id


def log(folder: Path, **record: object) -> None:
    event = {"time": datetime.now(UTC).isoformat(), **record}
    line = json.dumps(event, ensure_ascii=False)
    with (folder / "job.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    print(line, flush=True)


def execute(config: Settings, request: VideoRequest | None = None,
            resume: str | None = None) -> Path:
    with exclusive(config):
        if resume:
            folder = job_folder(config, resume)
            manifest = json.loads((folder / "job.json").read_text(encoding="utf-8"))
            request = VideoRequest.model_validate(manifest["request"])
            if manifest["settings"].get("tts_profile") and not config.tts_profile:
                config = config.model_copy(update={"tts_profile": manifest["settings"]["tts_profile"],
                                                  "voice": manifest["settings"]["voice"]})
            if manifest["status"] == "completed":
                raise ValueError("완료된 작업은 덮어쓰지 않습니다. 새 작업을 생성하세요.")
            if manifest["settings"] != config_signature(config):
                raise ValueError("재개 시 설정이 변경되었습니다. 원래 설정 또는 새 작업을 사용하세요.")
            job_id = resume
        else:
            if request is None:
                raise ValueError("주제와 영상 설정이 필요합니다.")
            validate_explainer(request)
            job_id = datetime.now().strftime("%Y%m%dT%H%M%S-") + uuid4().hex[:8]
            folder = job_folder(config, job_id)
            folder.mkdir(parents=True, exist_ok=False)
            manifest = {"id": job_id, "request": request.model_dump(), "completed_stages": [],
                        "status": "created", "stage": "script", "settings": {
                            **config_signature(config),
                        }, "created_at": datetime.now(UTC).isoformat(), "timings": {}}
            save_json(folder / "job.json", manifest)
        stage = manifest.get("stage", "script")
        manifest.setdefault("timings", {})
        news = None
        if (folder / "news-context.json").is_file():
            news = json.loads((folder / "news-context.json").read_text())
        overall_start = time.perf_counter()
        try:
            if shutil.disk_usage(config.root).free < 1024**3:
                raise RuntimeError("디스크 여유 공간이 1GiB 미만입니다. 로컬 결과나 캐시를 정리하세요.")
            for stage in STAGES:
                check_cancel()
                if stage in manifest["completed_stages"]:
                    continue
                manifest.update(status="running", stage=stage, error=None)
                save_json(folder / "job.json", manifest)
                store.update(config, job_id, request.topic, "running", stage, details=job_details(manifest))
                log(folder, event="stage_started", stage=stage, job_id=job_id)
                started = time.perf_counter()
                if stage == "script":
                    try:
                        script, raw = generate(request, config)
                    except LLMError as exc:
                        if exc.raw is not None:
                            save_json(folder / "ollama-response.json", exc.raw)
                        raise
                    save_json(folder / "ollama-response.json", raw)
                    save_json(folder / "script.json", script.model_dump())
                    save_json(folder / "sources.json", [])
                else:
                    script = Script.model_validate_json((folder / "script.json").read_text())
                    if stage == "tts":
                        if config.tts_profile:
                            from app.tts.engine import synthesize as synthesize_profile
                            lengths = synthesize_profile(script, folder, config, config.tts_profile)
                        else:
                            lengths = synthesize(script, folder, config)
                        frames, speed = natural_timeline(lengths, request.narration_speed) if request.template != "legacy" else timeline(lengths, request.seconds)
                        if config.tts_profile and not .65 <= speed <= 1.5:
                            raise ValueError(f"실제 음성 {sum(lengths):.1f}초를 목표 {request.seconds}초로 맞추려면 과도한 배속이 필요합니다. 대본 길이를 조정하세요.")
                        if news and not config.tts_profile and request.template == "legacy":
                            durations = [scene.duration_seconds for scene in script.scenes]
                            boundaries = [round(sum(durations[:i]) * 30) for i in range(len(durations) + 1)]
                            frames = [b - a for a, b in pairwise(boundaries)]
                            if any(not 0.5 <= length / (count / 30) <= 2 for length, count in zip(lengths, frames, strict=True)):
                                raise ValueError("장면 길이에 비해 음성이 너무 길거나 짧습니다. 편집 화면에서 장면 길이·내레이션을 조정하세요.")
                        save_json(folder / "timing.json", {"audio_seconds": lengths,
                                                          "frames": frames, "speed": speed})
                        write_subtitles(script, frames, folder / "subtitles.srt")
                    elif stage == "cards" and request.template != "legacy":
                        build_plan(script, folder, request, config, news)
                    elif stage == "cards":
                        create_cards(script, folder, request.resolution, config.font)
                        if (folder / "utterances.json").exists():
                            from app.renderer.captions import create_captions
                            timing = json.loads((folder / "timing.json").read_text())
                            create_captions(script, folder, timing["frames"], config, request.resolution)
                    elif stage == "render" and request.template != "legacy":
                        from app.renderer.reels import render_reel
                        plan = json.loads((folder / "reels-plan.json").read_text())
                        from app.media.library import resolve_asset
                        for asset in json.loads((folder / "media-sources.json").read_text()):
                            current = resolve_asset(config, asset["asset_id"])
                            if current["sha256"] != asset["sha256"]:
                                raise ValueError("저장된 작업의 미디어 자료가 변경되었습니다. 새 버전으로 제작하세요.")
                        if not (folder / "output.mp4").exists():
                            render_reel(plan, folder, config)
                    elif stage == "render":
                        timing = json.loads((folder / "timing.json").read_text())
                        render(folder, timing["frames"], timing["audio_seconds"],
                               request.resolution, config)
                    elif stage == "verify":
                        expected = request.seconds if request.template == "legacy" else sum(json.loads((folder / "timing.json").read_text())["frames"]) / 30
                        info = verify(folder / "output.mp4", request.resolution, expected)
                        manifest["actual_seconds"] = float(info["format"]["duration"])
                        save_json(folder / "ffprobe.json", info)
                        if request.template != "legacy":
                            from app.process import run
                            run(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(folder / "output.mp4"), "-frames:v", "1", str(folder / "poster.png")])
                        save_json(folder / "metadata.json", {
                            "brand": "HEADLINE 60", "title": script.title,
                            **mode_metadata(news or {}),
                            "automation_private_preview": bool(manifest.get("automation_private_preview")),
                            "preview_only": bool(manifest.get("automation_private_preview")),
                            "publication_status": "private_preview_only" if manifest.get("automation_private_preview") else "review_required" if manifest.get("validation_sample") else "manual_upload_only",
                            "posting_note": "미검수 자동 제작 프리뷰입니다. 비공개 YouTube 업로드만 허용하며 공개 전 수동 검토가 필요합니다." if manifest.get("automation_private_preview") else "현재 버전의 수동 검토 후 사용자가 직접 게시합니다.",
                            "description": script.description,
                            "hashtags": script.hashtags or ["헤드라인60", request.category.replace(" ", "")],
                            "content_type": manifest.get("content_mode", "news") if news else "general_explainer", "verified_news": False,
                            "review_required": bool(manifest.get("automation_private_preview")) or not bool(news) or manifest.get("validation_sample", False), "review_status": "unreviewed_preview" if manifest.get("automation_private_preview") else "sample_pending_user_review" if manifest.get("validation_sample") else "human_reviewed" if news else "unreviewed",
                            "sources": news["evidence"] if news else [],
                            "model": config.model, "voice": config.voice, "voice_profile": config.tts_profile,
                            "asset_license_record": "docs/ASSETS.md",
                            "template": request.template, "target_seconds": request.seconds,
                            "timing_policy": "user selected speech speed; visual timing follows audio" if request.template != "legacy" and request.narration_speed != 1 else "natural speech, no time stretching" if request.template != "legacy" else "fit target",
                            "narration_speed": request.narration_speed, "edit_style": request.edit_style,
                            "media_sources": json.loads((folder / "media-sources.json").read_text()) if (folder / "media-sources.json").is_file() else [],
                            "duration_seconds": float(info["format"]["duration"]),
                            "subtitle_alignment": "estimated within continuous scene speech; manually editable" if request.template != "legacy" else "measured synthesized phrase boundaries" if (folder / "captions.json").exists() else "scene-level, narration paced to scene duration",
                        })
                manifest["timings"][stage] = round(time.perf_counter() - started, 3)
                manifest["completed_stages"].append(stage)
                save_json(folder / "job.json", manifest)
                log(folder, event="stage_completed", stage=stage, job_id=job_id)
            manifest.update(status="completed", stage="verify", error=None)
            manifest["timings"]["run_total"] = round(time.perf_counter() - overall_start, 3)
            store.update(config, job_id, request.topic, "completed", "verify", details=job_details(manifest))
            save_json(folder / "job.json", manifest)
            return folder
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (KeyboardInterrupt, JobCancelled)) else "failed"
            manifest.update(status=status, stage=stage, error=str(exc) or type(exc).__name__)
            save_json(folder / "job.json", manifest)
            store.update(config, job_id, request.topic, status, stage, manifest["error"], details=job_details(manifest))
            log(folder, event=status, stage=stage, error=manifest["error"], job_id=job_id)
            raise
