import io
import json
import re
import secrets
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import studio_store
from app.anchors import experiments as anchor_experiments
from app.config import settings
from app.content_modes import creation_issues, mode_metadata, validation_issues
from app.doctor import diagnose
from app.integrations.youtube import YoutubeService
from app.jobs import store
from app.jobs.manager import Manager
from app.jobs.pipeline import job_folder
from app.jobs.submissions import SubmissionQueue
from app.media.api import make_router
from app.news.evidence import preview
from app.news.feeds import CATEGORIES, NewsError, article_id, collect, group_articles, public_url
from app.process import save_json
from app.script.models import CaptionEdit, Script
from app.studio_models import DraftCreate, DraftEdit, Evidence, StrictModel
from app.tts import profiles
from app.tts.macos import korean_voices
from app.tts.text import spoken_parts

manager = Manager(settings)
youtube = YoutubeService(settings)
submissions = SubmissionQueue(settings, manager, youtube)
youtube.on_connected = submissions.start
csrf_token = secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(application: FastAPI):
    manager.recover()
    submissions.recover()
    submissions.start()
    yield
    for event in list(manager.events.values()):
        event.set()


app = FastAPI(title="HEADLINE 60", version="0.2.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver", *settings.allowed_hosts])
app.mount("/static", StaticFiles(directory=settings.root / "static"), name="static")
templates = Jinja2Templates(directory=settings.root / "templates")
app.include_router(make_router(settings))


@app.middleware("http")
async def local_mutations(request: Request, call_next):
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        if not secrets.compare_digest(request.headers.get("x-headline-token", ""), csrf_token):
            return JSONResponse({"detail": "현재 관리 화면에서 다시 시도하세요. 요청 토큰이 없습니다."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({"detail": "다른 웹사이트에서 보낸 변경 요청은 차단됩니다."}, status_code=403)
        try:
            body_size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "잘못된 요청 길이입니다."}, status_code=400)
        limit = 50 * 1024**2 if request.url.path == "/api/media/upload" else 262144
        if body_size > limit:
            return JSONResponse({"detail": "요청 데이터가 너무 큽니다."}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def bad_input(request: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def missing(request: Request, exc: KeyError):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(NewsError)
async def news_failed(request: Request, exc: NewsError):
    return JSONResponse({"detail": str(exc), "type": "news_collection"}, status_code=502)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "headline60", "version": "0.2.0"}


@app.get("/api/anchor-experiments")
def list_anchor_experiments() -> list[dict]:
    return anchor_experiments.listing(settings)


@app.get("/anchor-experiments/{experiment_id}/output.mp4")
def anchor_experiment_video(experiment_id: str):
    try:
        path = anchor_experiments.video_path(settings, experiment_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "진행자 실험 영상을 찾을 수 없습니다.") from None
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/diagnostics")
def diagnostics() -> dict:
    return diagnose(settings)


@app.get("/api/jobs")
def jobs() -> list[dict]:
    return store.recent(settings)


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    return store.get(settings, job_id)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    return manager.resume(job_id)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    return manager.cancel(job_id)


class NewsQuery(StrictModel):
    category: str = "economy"
    keyword: str = Field(default="", max_length=120)
    days: int = 1


@app.post("/api/news/collect")
def fetch_news(query: NewsQuery):
    items = collect(query.category, query.keyword, query.days)
    studio_store.save_articles(settings, items)
    return {"collected": len(items), "groups": group_articles(items),
            "notice": "자체 추천 점수: 기사 수·출처명 수·최신성 기준. 실제 조회수·검색량이 아닙니다."}


@app.get("/api/news")
def cached_news():
    return {"groups": group_articles(studio_store.articles(settings))}


class ManualURL(StrictModel):
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=400)
    publisher: str = Field(default="", max_length=120)
    category: str = "economy"
    published_date: date | None = None


@app.post("/api/news/manual")
def manual_news(value: ManualURL):
    url = public_url(value.url)
    if value.category not in CATEGORIES:
        raise ValueError("지원 카테고리를 선택하세요.")
    if value.published_date and value.published_date > date.today():
        raise ValueError("발행일이 미래입니다.")
    item = {"id": article_id(url), "url": url, "title": value.title or urlsplit(url).hostname,
            "publisher": value.publisher or urlsplit(url).hostname, "category": value.category,
            "published_at": value.published_date.isoformat() + "T00:00:00+09:00" if value.published_date else None,
            "origin": "manual", "verification": "metadata_only", "collected_at": studio_store.now()}
    studio_store.save_articles(settings, [item])
    return item


class PreviewURL(StrictModel):
    url: str = Field(max_length=2000)


@app.post("/api/evidence/preview")
def preview_evidence(value: PreviewURL):
    return preview(value.url)


@app.get("/api/drafts")
def drafts():
    return [{**draft, **mode_metadata(draft)} for draft in studio_store.listing(settings)]


@app.post("/api/drafts")
def new_draft(value: DraftCreate):
    articles = studio_store.articles(settings, value.article_ids)
    if len(articles) != len(set(value.article_ids)):
        raise ValueError("선택한 뉴스가 없습니다. 다시 수집·선택하세요.")
    draft = studio_store.create(settings, {**value.model_dump(), "articles": articles,
        "evidence": [], "script": None, "voice": settings.voice, "voice_profile_id": "reels-natural-sohee" if any(p["voice_profile_id"] == "reels-natural-sohee" and p["available"] for p in profiles.listing(settings)) else "macos-yuna-news", "metrics": {}})
    return {**draft, **mode_metadata(draft)}


class URLCreate(StrictModel):
    url: str = Field(max_length=2000)


@app.post("/api/source-drafts")
def source_draft(value: URLCreate):
    url = public_url(value.url)
    default = DraftCreate(article_ids=["url-import"], category="trending", subtitle_style="dynamic", template="story_explainer")
    draft = studio_store.create(settings, {**default.model_dump(), "source_url": url, "source_loaded": False,
        "articles": [], "evidence": [], "script": None, "voice": settings.voice,
        "voice_profile_id": "reels-natural-sohee" if any(p["voice_profile_id"] == "reels-natural-sohee" and p["available"] for p in profiles.listing(settings)) else "macos-yuna-news", "metrics": {}})
    return manager.prepare(draft["id"], draft["revision"])


@app.get("/api/drafts/{key}")
def draft(key: str):
    value = studio_store.get(settings, key)
    return {**value, **mode_metadata(value)}


@app.get("/api/drafts/{key}/storyboard-media")
def storyboard_media(key: str):
    from app.renderer.storyboard import preview_visuals
    return preview_visuals(settings, studio_store.get(settings, key))


@app.put("/api/drafts/{key}")
def edit_draft(key: str, value: DraftEdit):
    with manager.lock:
        draft = studio_store.get(settings, key)
        if draft["revision"] != value.revision or draft["status"] in {"researching", "generating", "rendering"}:
            raise ValueError("실행 중이거나 버전이 다릅니다. 완료 후 새로고침하세요.")
        changes = value.model_dump(mode="json", exclude={"revision"})
        if all(draft.get(field) == content for field, content in changes.items()):
            return draft
        original_evidence = {e["id"]: Evidence.model_validate(e).model_dump(mode="json") for e in draft.get("evidence", [])}
        for item in changes["evidence"]:
            if item.get("verification") == "source_matched":
                previous = original_evidence.get(item["id"], {})
                fields = ("url", "fact", "excerpt", "published_date", "publisher", "title", "conditions", "date_precision", "source_sha256", "scope")
                if previous.get("verification") != "source_matched" or any(item.get(f) != previous.get(f) for f in fields):
                    item.update(verification="manual", source_sha256="", reviewed=False)
        changed_mode = changes.get("content_mode", "news") != draft.get("content_mode", "news")
        issues = creation_issues({**draft, **changes}) if changed_mode or changes.get("content_mode", "news") != "news" else []
        if issues:
            raise ValueError("; ".join(issues))
        # Store an immutable revision snapshot before changing the current draft.
        folder = settings.data / "drafts" / key
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / f"revision-{draft['revision']:04}.json", draft)
        draft.update(**changes, revision=draft["revision"] + 1, status="review" if value.script else "needs_evidence",
                     approved_revision=None, content_review=None, error=None)
        draft.update(mode_metadata(draft))
        return studio_store.save(settings, draft, value.revision)


class Revision(StrictModel):
    revision: int = Field(ge=1)


@app.post("/api/drafts/{key}/prepare")
def auto_prepare_draft(key: str, value: Revision):
    return manager.prepare(key, value.revision)


@app.post("/api/drafts/{key}/generate")
def generate_draft(key: str, value: Revision):
    return manager.generate(key, value.revision)


class ReviewApproval(Revision):
    content_reviewed: bool = False
    medical_reviewed: bool = False
    sexual_reviewed: bool = False


@app.post("/api/drafts/{key}/approve")
def approve_draft(key: str, value: ReviewApproval):
    return manager.approve(key, value.revision, content_reviewed=value.content_reviewed,
                           medical_reviewed=value.medical_reviewed, sexual_reviewed=value.sexual_reviewed)


@app.post("/api/drafts/{key}/render")
def render_draft(key: str, value: Revision):
    return manager.render(key, value.revision)


class SubmissionCreate(StrictModel):
    url: str = Field(max_length=2000)
    anchor_id: Literal["headline60-brown-adult-01"] | None = None


class SelectedSubmission(StrictModel):
    article_ids: list[str] = Field(min_length=1, max_length=20)
    category: str = "economy"
    anchor_id: Literal["headline60-brown-adult-01"] | None = None


@app.get("/api/submissions")
def list_submissions():
    return submissions.listing()


@app.post("/api/submissions")
def register_submission(value: SubmissionCreate):
    return submissions.create(value.url, value.anchor_id)


@app.post("/api/submissions/selected")
def register_selected_submission(value: SelectedSubmission):
    if value.category not in CATEGORIES:
        raise ValueError("지원하는 카테고리를 선택하세요.")
    return submissions.create_selected(value.article_ids, value.category, value.anchor_id)


@app.post("/api/submissions/{key}/retry")
def retry_submission(key: str):
    return submissions.retry(key)


@app.post("/api/submissions/{key}/cancel")
def cancel_submission(key: str):
    return submissions.cancel(key)


@app.get("/api/anchor-profiles")
def anchor_profiles():
    from app.anchors.production import profiles as anchor_profiles_list
    return {"profiles": [{key: value for key, value in profile.items() if key != "thumbnail_path"}
                         for profile in anchor_profiles_list(settings)]}


@app.get("/api/anchors/{key}/thumbnail")
def anchor_thumbnail(key: str):
    from app.anchors.production import thumbnail_path
    return FileResponse(thumbnail_path(settings, key))


@app.get("/api/youtube/status")
def youtube_status():
    return youtube.status()


class YoutubeClient(StrictModel):
    client: dict


@app.post("/api/youtube/client")
def configure_youtube(value: YoutubeClient):
    return youtube.configure(value.client)


@app.post("/api/youtube/connect")
def connect_youtube():
    return youtube.begin()


@app.get("/api/settings")
def app_settings():
    return {"model": settings.model, "voice": settings.voice, "voices": korean_voices(),
            "tts_rate": settings.tts_rate, "encoder": settings.encoder, "root": str(settings.root),
            "ollama_url": settings.ollama_url, "anchors": [],
            "voice_profiles": profiles.listing(settings),
            "features": {"news": True, "macos_tts": True, "neural_tts": any(p["available"] and p["engine"] == "qwen3_mlx" for p in profiles.listing(settings)), "lipsync": False}}


@app.get("/api/voice-profiles")
def voice_profiles():
    return profiles.listing(settings)


@app.get("/api/voice-profiles/{key}/sample")
def preset_sample(key: str):
    profile = profiles.get(settings, key)
    path = Path(profile.get("sample_path") or "")
    if not path.is_file() or not path.resolve().is_relative_to(settings.data.resolve()):
        raise HTTPException(404, "샘플을 먼저 생성하세요.")
    return FileResponse(path, media_type="audio/wav")


class VoiceSampleRequest(StrictModel):
    voice_profile_id: str
    text: str = Field(default="안녕하세요. 헤드라인 육십입니다. 확인한 사실을 정확하고 차분하게 전해 드립니다.", min_length=8, max_length=120)


@app.post("/api/voice-samples")
def voice_sample(value: VoiceSampleRequest):
    return manager.sample_voice(value.voice_profile_id, value.text)


@app.get("/api/voice-samples/{key}")
def sample_status(key: str):
    return profiles.sample_record(settings, key)


@app.get("/api/voice-samples/{key}/audio")
def sample_audio(key: str):
    record = profiles.sample_record(settings, key)
    path = Path(record.get("path") or "")
    if record["status"] != "completed" or not path.is_file() or not path.resolve().is_relative_to(settings.data.resolve()):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


class NormalizeText(StrictModel):
    texts: list[str] = Field(max_length=12)


@app.post("/api/tts/normalize")
def normalize_text(value: NormalizeText):
    if any(len(text) > 120 for text in value.texts):
        raise ValueError("장면별 원문은 120자 이내여야 합니다.")
    return [spoken_parts(text) for text in value.texts]


def current_reels_preview() -> str | None:
    pointer = settings.data / "reels-current.json"
    try:
        if pointer.is_symlink() or pointer.stat().st_size > 65536:
            return None
        record = json.loads(pointer.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("status") != "completed":
            return None
        job_id = record.get("job_id")
        if not isinstance(job_id, str):
            return None
        folder = job_folder(settings, job_id)
        video = folder / "output.mp4"
        if settings.outputs.is_symlink() or folder.is_symlink() or video.is_symlink():
            return None
        if not video.is_file() or not video.resolve().is_relative_to(settings.outputs.resolve()):
            return None
        return f"/jobs/{job_id}/output.mp4"
    except (OSError, ValueError, TypeError):
        return None


def current_reels_samples() -> list[dict]:
    labels = {"headline_news": "기사형", "story_explainer": "설명형", "data_comparison": "비교형"}
    folder = settings.data / "reels-validation-v2"
    pointer = folder / "samples.json"
    try:
        if settings.data.is_symlink() or folder.is_symlink() or pointer.is_symlink():
            return []
        if pointer.stat().st_size > 65536:
            return []
        records = json.loads(pointer.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            return []
    except (OSError, ValueError, TypeError):
        return []
    samples = {}
    for record in records[:50]:
        try:
            if not isinstance(record, dict):
                continue
            template = record.get("template")
            job_id = record.get("job_id")
            if not isinstance(template, str) or template not in labels or not isinstance(job_id, str):
                continue
            output_folder = job_folder(settings, job_id)
            video, manifest_path = output_folder / "output.mp4", output_folder / "job.json"
            if any(path.is_symlink() for path in (settings.outputs, output_folder, video, manifest_path)):
                continue
            if not video.is_file() or not video.resolve().is_relative_to(settings.outputs.resolve()):
                continue
            if not manifest_path.is_file() or not manifest_path.resolve().is_relative_to(settings.outputs.resolve()):
                continue
            if manifest_path.stat().st_size > 1048576:
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("status") != "completed":
                continue
            request = manifest.get("request")
            if not isinstance(request, dict) or request.get("template") != template or manifest.get("id") != job_id:
                continue
            draft_id = manifest.get("draft_id")
            if not isinstance(draft_id, str) or not re.fullmatch(r"[a-f0-9]{32}", draft_id):
                continue
            if record.get("draft_id") not in (None, draft_id):
                continue
            samples[template] = {"template": template, "label": labels[template],
                                 "url": f"/jobs/{job_id}/output.mp4", "studio_url": f"/studio?draft={draft_id}"}
            before_id = record.get("before_job_id")
            if isinstance(before_id, str):
                before_folder = job_folder(settings, before_id)
                before = before_folder / "output.mp4"
                if (before.is_file() and not before.is_symlink() and not before_folder.is_symlink()
                        and before.resolve().is_relative_to(settings.outputs.resolve())):
                    samples[template]["before_url"] = f"/jobs/{before_id}/output.mp4"
            comparison = output_folder / "comparison.png"
            if comparison.is_file() and not comparison.is_symlink():
                samples[template]["comparison_url"] = f"/jobs/{job_id}/comparison.png"
            samples[template]["hook_url"] = f"/jobs/{job_id}/hook-preview.mp4" if (output_folder / "hook-preview.mp4").is_file() else None
        except (OSError, ValueError, TypeError):
            continue
    return [samples[key] for key in labels if key in samples]


@app.get("/")
@app.get("/news")
@app.get("/studio")
@app.get("/history")
@app.get("/settings")
@app.get("/anchors")
def home(request: Request):
    reels_samples = current_reels_samples() if request.url.path == "/" else []
    return templates.TemplateResponse(request=request, name="index.html", context={
        "token": csrf_token, "page": request.url.path, "categories": CATEGORIES,
        "reels_samples": reels_samples,
        "reels_preview_url": current_reels_preview() if request.url.path == "/" and not reels_samples else None,
    })


def stored_script(job_id: str) -> tuple:
    try:
        folder = job_folder(settings, job_id)
    except ValueError:
        raise HTTPException(404) from None
    path = folder / "script.json"
    if not path.is_file() or not path.resolve().is_relative_to(settings.outputs.resolve()):
        raise HTTPException(404)
    script = Script.model_validate_json(path.read_text())
    manifest = json.loads((folder / "job.json").read_text())
    return script, manifest


@app.get("/jobs/{job_id}/script")
def script_view(request: Request, job_id: str):
    script, manifest = stored_script(job_id)
    return templates.TemplateResponse(request=request, name="script_view.html", context={
        "script": script, "job_id": job_id, "seconds": manifest["request"]["seconds"],
        "voice": manifest["settings"]["voice"], "tags": " ".join("#" + t.lstrip("#") for t in script.hashtags),
    })


@app.get("/jobs/{job_id}/narration.txt")
def narration_text(job_id: str):
    script, _ = stored_script(job_id)
    return Response(script.title + "\n\n" + "\n\n".join(s.narration for s in script.scenes),
                    media_type="text/plain", headers={"Content-Disposition": f'attachment; filename="{job_id}-narration.txt"'})


@app.get("/jobs/{job_id}/captions")
def caption_edit_view(request: Request, job_id: str):
    script, manifest = stored_script(job_id)
    path = job_folder(settings, job_id) / "captions.json"
    if not path.is_file():
        raise HTTPException(400, "이 영상은 이전 장면 단위 자막입니다. 음성 프로필을 선택해 새 버전으로 제작하면 구절 자막 편집을 사용할 수 있습니다.")
    return templates.TemplateResponse(request=request, name="caption_edit.html", context={
        "token": csrf_token, "job_id": job_id, "script": script, "revision": manifest.get("revision"),
        "cues": json.loads(path.read_text())["cues"],
    })


class CaptionChange(CaptionEdit):
    scene: int = Field(ge=0, le=11)


class CaptionBatch(StrictModel):
    cues: list[CaptionChange] = Field(min_length=1, max_length=100)


@app.post("/api/jobs/{job_id}/captions")
def save_captions(job_id: str, value: CaptionBatch):
    _, manifest = stored_script(job_id)
    if not manifest.get("draft_id") or not (job_folder(settings, job_id) / "captions.json").is_file():
        raise ValueError("이 영상은 구절 자막 편집을 지원하지 않습니다.")
    with manager.lock:
        draft = studio_store.get(settings, manifest["draft_id"])
        if draft["revision"] != manifest["revision"]:
            raise ValueError("대본이 이미 다른 버전으로 수정되었습니다. 최신 영상을 기준으로 편집하세요.")
        script = Script.model_validate(draft["script"])
        timing = json.loads((job_folder(settings, job_id) / "timing.json").read_text())
        boundaries = [sum(timing["frames"][:i]) / 30 for i in range(len(script.scenes) + 1)]
        for scene in script.scenes:
            scene.caption_overrides = []
            scene.caption_reference_speed = manifest["request"].get("narration_speed", 1.0)
        for cue in value.cues:
            if cue.scene >= len(script.scenes):
                raise ValueError("잘못된 장면 번호입니다.")
            start, end = boundaries[cue.scene], boundaries[cue.scene + 1]
            if cue.start < start - .001 or cue.end > end + .001:
                raise ValueError(f"장면 {cue.scene + 1}의 {start:.2f}~{end:.2f}초 안에서 자막 시간을 조정하세요.")
            script.scenes[cue.scene].caption_overrides.append(CaptionEdit(
                text=cue.text, start=max(0, cue.start - start), end=cue.end - start, highlight=cue.highlight))

        from app.media.cards import load_font, wrap
        if draft.get("edit_style") == "reels_v3":
            from app.renderer.reels import caption_layout
            for cue in value.cues:
                caption_layout(cue.text, {"media_type": "graphic"}, draft, settings)
        else:
            font = load_font(settings.font, round(33 * draft["resolution"] / 720), True)
            if any(len(wrap(cue.text, font, round(580 * draft["resolution"] / 720)).splitlines()) > 2 for cue in value.cues):
                raise ValueError("자막은 두 줄 이내로 나눠 주세요. 아직 새 버전으로 저장하지 않았습니다.")
        issues = validation_issues(draft, script)
        if issues:
            raise ValueError("; ".join(issues))
        body = DraftEdit(revision=draft["revision"], script=script, evidence=draft["evidence"],
            seconds=draft["seconds"], resolution=draft["resolution"], voice=draft["voice"],
            voice_profile_id=draft.get("voice_profile_id"), anchor_id=draft.get("anchor_id"), template=draft.get("template", "legacy"),
            edit_style=draft.get("edit_style", "classic"), narration_speed=draft.get("narration_speed", 1.0),
            content_mode=draft.get("content_mode", "news"), topic=draft.get("topic", ""),
            fiction_acknowledged=draft.get("fiction_acknowledged", False),
            subtitle_style=draft.get("subtitle_style", "clean"), music_asset_id=draft.get("music_asset_id"))
        result = edit_draft(draft["id"], body)
        return {"draft_id": draft["id"], "revision": result["revision"], "url": f"/studio?draft={draft['id']}"}


ARTIFACTS = {"output.mp4", "script.json", "metadata.json", "subtitles.srt", "sources.json", "ffprobe.json", "poster.png", "captions.json", "spoken-script.json", "tts-metrics.json", "media-sources.json", "CREDITS.md", "reels-plan.json", "timing.json", "job.json", "comparison.png", "hook-preview.mp4"}


@app.get("/jobs/{job_id}/package.zip")
def download_package(job_id: str):
    try:
        folder = job_folder(settings, job_id)
    except ValueError:
        raise HTTPException(404) from None
    if store.get(settings, job_id)["status"] != "completed":
        raise ValueError("완성된 영상만 내려받을 수 있습니다.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ARTIFACTS:
            path = folder / name
            if path.is_file() and path.resolve().is_relative_to(settings.outputs.resolve()):
                archive.write(path, name)
        metadata = json.loads((folder / "metadata.json").read_text())
        archive.writestr("publication.txt", metadata["title"] + "\n\n" + metadata["description"]
                         + "\n\n" + " ".join("#" + tag.lstrip("#") for tag in metadata["hashtags"]))
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="headline60-{job_id}.zip"'})


@app.get("/jobs/{job_id}/{filename}")
def artifact(job_id: str, filename: str):
    if filename not in ARTIFACTS:
        raise HTTPException(404)
    try:
        path = job_folder(settings, job_id) / ("card-00.png" if filename == "poster.png" else filename)
    except ValueError:
        raise HTTPException(404) from None
    if not path.is_file() or not path.resolve().is_relative_to(settings.outputs.resolve()):
        raise HTTPException(404)
    return FileResponse(path)
