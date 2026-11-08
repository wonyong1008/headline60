import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app import studio_store
from app.config import Settings
from app.content_modes import mode_metadata, sensitive_flags, validation_issues
from app.jobs import store
from app.jobs.pipeline import exclusive, execute, job_folder, prepare_news
from app.llm.news_writer import generate_news
from app.llm.ollama import LLMError
from app.news.feeds import CATEGORIES, NewsError, article_id
from app.process import cancellation, save_json
from app.script.models import Script, VideoRequest
from app.tts.macos import korean_voices


class Manager:
    def __init__(self, config: Settings):
        self.config = config
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="headline-production")
        self.events: dict[str, threading.Event] = {}

    def sample_voice(self, profile_id: str, text: str) -> dict:
        from app.tts.profiles import get, sample_record
        profile = get(self.config, profile_id)
        key = uuid4().hex
        record = {"id": key, "profile": profile, "text": text, "status": "queued", "error": None}
        sample_record(self.config, key, record)
        self.events[key] = threading.Event()
        self.pool.submit(self._sample_voice, record)
        return record

    def _sample_voice(self, record: dict) -> None:
        from app.script.models import Scene
        from app.tts.engine import SpeechDocument, join_wavs, synthesize
        from app.tts.profiles import sample_record
        record["status"] = "running"
        sample_record(self.config, record["id"], record)
        folder = self.config.data / "voice_samples" / record["id"]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with cancellation(self.events[record["id"]]), exclusive(self.config):
                synthesize(SpeechDocument([Scene(headline="음성 미리듣기", narration=record["text"])]),
                           folder, self.config, record["profile"])
                join_wavs([folder / "speech-00.wav"], folder / "sample.wav")
            record.update(status="completed", path=str(folder / "sample.wav"),
                          metrics=json.loads((folder / "tts-metrics.json").read_text()))
        except (RuntimeError, ValueError, OSError) as exc:
            record.update(status="failed", error=str(exc))
        self.events.pop(record["id"], None)
        sample_record(self.config, record["id"], record)

    def prepare(self, key: str, revision: int) -> dict:
        """One button: fetch source/evidence, write script, then find licensed visuals."""
        with self.lock:
            draft = studio_store.get(self.config, key)
            if draft["revision"] != revision or draft["status"] in {"researching", "generating", "rendering"}:
                raise ValueError("이미 실행 중이거나 버전이 변경되었습니다.")
            draft.update(status="researching", automatic=True, error=None, approved_revision=None, content_review=None)
            studio_store.save(self.config, draft, revision)
            self.pool.submit(self._prepare, draft)
            return draft

    def _prepare(self, draft: dict) -> None:
        import httpx

        from app.news.automatic import SOURCE_EXTRACT_VERSION, category_for, gather
        from app.news.sources import source_page
        from app.studio_models import Evidence, evidence_ready
        try:
            source = None
            unreviewed_only = not any(e.get("reviewed") for e in draft.get("evidence", []))
            # Automatic (URL-queue) drafts always re-read the source: evidence must reflect the
            # current extraction rules, not whatever an earlier attempt left behind.
            if draft.get("source_url") and (not draft.get("source_loaded") or draft.get("source_extract_version") != SOURCE_EXTRACT_VERSION
                                            or (unreviewed_only and (not draft.get("evidence") or draft.get("automatic")))):
                source = source_page(draft["source_url"])
                draft["evidence"] = [e for e in draft.get("evidence", []) if e.get("verification") != "source_matched" or e.get("reviewed")]
                draft.update(topic=source["title"][:200], category=category_for(source["title"]),
                             source_loaded=True, source_outline_loaded=True, source_extract_version=SOURCE_EXTRACT_VERSION)
                draft["articles"] = [{"id": article_id(source["url"]), "url": source["url"], "title": source["title"],
                    "publisher": source["publisher"], "published_at": source["published_date"] + "T00:00:00+09:00" if source["published_date"] else None,
                    "origin": "url_import", "category": draft["category"]}]
                draft["article_ids"] = [draft["articles"][0]["id"]]
            if draft.get("content_mode") != "adult_satire":
                if source is not None or evidence_ready([Evidence.model_validate(e) for e in draft.get("evidence", [])], draft["category"]):
                    draft["evidence"], draft["evidence_search"] = gather(draft, source)
                issues = validation_issues(draft)
                draft["content_warnings"] = []
                if issues and draft.get("automatic") and draft.get("evidence"):
                    # Automatic drafts become private previews the user reviews before publishing;
                    # evidence gaps are recorded as warnings instead of stopping production.
                    draft["content_warnings"] = issues
                elif issues:
                    raise ValueError("자동 자료 확인 보류: " + "; ".join(issues))
            draft.update(status="generating", error=None)
            with self.lock:
                expected = draft["revision"]
                draft["revision"] += 1
                studio_store.save(self.config, draft, expected)
            self._generate(draft)
        except (httpx.HTTPError, RuntimeError, ValueError, OSError, NewsError) as exc:
            draft.update(status="needs_evidence", error=str(exc))
            with self.lock:
                expected = draft["revision"]
                draft["revision"] += 1
                studio_store.save(self.config, draft, expected)

    def generate(self, key: str, revision: int) -> dict:
        with self.lock:
            draft = studio_store.get(self.config, key)
            if draft["revision"] != revision:
                raise ValueError("대본 버전이 변경되었습니다. 새로고침하세요.")
            if draft["status"] in {"researching", "generating", "rendering"}:
                raise ValueError("이미 실행 중인 작업입니다.")
            issues = validation_issues(draft)
            if issues:
                raise ValueError("; ".join(issues))
            draft.update(status="generating", error=None, approved_revision=None, content_review=None)
            studio_store.save(self.config, draft, revision)
            self.pool.submit(self._generate, draft)
            return draft

    def _generate(self, draft: dict) -> None:
        start = time.perf_counter()
        folder = self.config.data / "drafts" / draft["id"]
        folder.mkdir(parents=True, exist_ok=True)
        response_file = folder / ("ollama-" + datetime.now().strftime("%Y%m%dT%H%M%S%f") + ".json")
        try:
            with exclusive(self.config):
                script, raw = generate_news(draft, self.config)
            save_json(response_file, raw)
            draft.setdefault("metrics", {})["llm_seconds"] = round(time.perf_counter() - start, 3)
            if draft.get("automatic"):
                from app.media.discovery import ensure_media
                draft["media_search"] = ensure_media(self.config, script, draft["category"])
            if draft.get("automatic"):
                draft["content_warnings"] = list(dict.fromkeys(
                    [*draft.get("content_warnings", []), *(raw.get("warnings") or [])]))
            draft.update(script=script.model_dump(), status="review", error=None)
        except (RuntimeError, ValueError, OSError) as exc:
            if isinstance(exc, LLMError) and exc.raw is not None:
                save_json(response_file, exc.raw)
            draft.update(status="failed", error=str(exc))
        draft.setdefault("metrics", {}).setdefault("llm_seconds", round(time.perf_counter() - start, 3))
        with self.lock:
            expected = draft["revision"]
            draft["revision"] += 1
            studio_store.save(self.config, draft, expected)

    def _validate_media(self, draft: dict) -> None:
        from app.media.library import resolve_asset
        scenes = (draft.get("script") or {}).get("scenes", [])
        if draft.get("automatic"):
            # A photo retired from the library after it was pinned must not stop production:
            # drop it and let discovery find another one for that scene.
            dropped = False
            for scene in scenes:
                if scene.get("asset_id"):
                    try:
                        resolve_asset(self.config, scene["asset_id"])
                    except ValueError:
                        scene["asset_id"] = None
                        dropped = True
            if dropped:
                from app.media.discovery import ensure_media
                script = Script.model_validate(draft["script"])
                draft["media_search"] = ensure_media(self.config, script, draft["category"])
                draft["script"] = script.model_dump()
                scenes = draft["script"]["scenes"]
        for scene in scenes:
            if scene.get("asset_id"):
                asset = resolve_asset(self.config, scene["asset_id"])
                if asset["media_type"] not in {"image", "video"}:
                    raise ValueError("장면에는 사진 또는 영상 자료를 선택하세요.")
        if draft.get("music_asset_id"):
            resolve_asset(self.config, draft["music_asset_id"], "audio")

    def approve(self, key: str, revision: int, *, content_reviewed=False, medical_reviewed=False, sexual_reviewed=False) -> dict:
        with self.lock:
            draft = studio_store.get(self.config, key)
            if draft["revision"] != revision or draft["status"] in {"researching", "generating", "rendering"}:
                raise ValueError("저장한 최신 대본이 아니거나 실행 중입니다. 새로고침하세요.")
            if not draft.get("script"):
                raise ValueError("대본을 먼저 생성하세요.")
            review = {"revision": revision, "content_reviewed": bool(content_reviewed),
                      "medical_reviewed": bool(medical_reviewed), "sexual_reviewed": bool(sexual_reviewed),
                      "sensitive_flags": sensitive_flags(draft), "reviewed_at": studio_store.now()}
            reviewed = {**draft, "content_review": review}
            issues = validation_issues(reviewed, Script.model_validate(draft["script"]), require_review=True)
            if issues:
                raise ValueError("; ".join(issues))
            self._validate_media(draft)
            draft.update(approved_revision=revision, status="approved", error=None, validation_sample=False, content_review=review)
            draft.update(mode_metadata(draft))
            return studio_store.save(self.config, draft, revision)

    def render(self, key: str, revision: int, *, private_preview=False, schedule=True) -> dict:
        with self.lock:
            draft = studio_store.get(self.config, key)
            if draft["revision"] != revision or (not private_preview and draft.get("approved_revision") != revision):
                raise ValueError("수정 후 최신 대본·출처 검토 승인이 필요합니다.")
            for job in store.recent(self.config):
                details = job["details"]
                if (details.get("draft_id") == key and details.get("revision") == revision
                        and bool(details.get("automation_private_preview")) == private_preview):
                    if job["status"] in {"completed", "queued", "running"}:
                        return job
                    raise ValueError("같은 버전의 실패 작업이 있습니다. 제작 내역에서 재개하세요.")
            issues = validation_issues(draft, Script.model_validate(draft["script"]), require_review=not private_preview)
            if issues and private_preview and draft.get("automatic"):
                # Private automatic previews proceed; the user judges facts before publishing.
                draft["content_warnings"] = list(dict.fromkeys([*draft.get("content_warnings", []), *issues]))
            elif issues:
                raise ValueError("; ".join(issues))
            self._validate_media(draft)
            profile = None
            if draft.get("voice_profile_id"):
                from app.tts.profiles import get
                profile = get(self.config, draft["voice_profile_id"])
            config = self.config.model_copy(update={"voice": profile["speaker"] if profile else draft["voice"], "tts_profile": profile})
            if not profile and config.voice not in korean_voices():
                raise ValueError("선택한 한국어 음성이 설치되어 있지 않습니다.")
            request = VideoRequest(topic=draft["script"]["title"], seconds=draft["seconds"],
                                   category=CATEGORIES[draft["category"]][0],
                                   resolution=draft["resolution"], style=draft["style"],
                                   template=draft.get("template", "legacy"),
                                   subtitle_style=draft.get("subtitle_style", "clean"),
                                   music_asset_id=draft.get("music_asset_id"),
                                   edit_style=draft.get("edit_style", "classic"),
                                   text_style=draft.get("text_style", "classic"),
                                   narration_speed=draft.get("narration_speed", 1.0),
                                   content_mode=draft.get("content_mode", "news"),
                                   anchor_id=draft.get("anchor_id"))
            context = draft
            if private_preview:
                context = {**draft, "preview_only": True, "automation_private_preview": True,
                           "render_authorized_revision": revision, "approved_revision": None,
                           "content_review": None}
            job_id = prepare_news(config, request, Script.model_validate(draft["script"]), context)
            draft.update(status="rendering", error=None, latest_job_id=job_id)
            studio_store.save(self.config, draft, revision)
            if schedule:
                self._schedule(job_id, config)
            return store.get(self.config, job_id)

    def config_for_job(self, job_id: str) -> Settings:
        manifest = json.loads((job_folder(self.config, job_id) / "job.json").read_text())
        original = manifest["settings"]
        values = {name: original[name] for name in ("model", "voice", "tts_rate", "font", "encoder")}
        values["font"] = Path(values["font"])
        values["tts_profile"] = original.get("tts_profile")
        return self.config.model_copy(update=values)

    def run_prepared(self, job_id: str) -> dict:
        """Run synchronously inside the production pool, retaining stage caches on restart."""
        with self.lock:
            job = store.get(self.config, job_id)
            if job["status"] == "completed":
                return job
            if job_id in self.events:
                raise ValueError("이미 실행 중인 영상 작업입니다.")
            config = self.config_for_job(job_id)
            event = threading.Event()
            self.events[job_id] = event
        self._render(job_id, config, event)
        return store.get(self.config, job_id)

    def _schedule(self, job_id: str, config: Settings) -> None:
        event = threading.Event()
        self.events[job_id] = event
        self.pool.submit(self._render, job_id, config, event)

    def _render(self, job_id: str, config: Settings, event: threading.Event) -> None:
        try:
            with cancellation(event):
                execute(config, resume=job_id)
        except (RuntimeError, ValueError, OSError) as exc:
            job = store.get(config, job_id)
            if job["status"] in {"queued", "running"}:
                store.update(config, job_id, job["topic"], "failed", job["stage"],
                             str(exc), job["details"])
        finally:
            with self.lock:
                self.events.pop(job_id, None)
                job = store.get(config, job_id)
                key = job["details"].get("draft_id")
                if key:
                    draft = studio_store.get(config, key)
                    if draft["revision"] == job["details"]["revision"]:
                        draft.update(status=job["status"] if job["status"] in {"completed", "cancelled", "interrupted"} else "failed",
                                     error=job.get("error"), latest_job_id=job_id)
                        studio_store.save(config, draft, draft["revision"])

    def resume(self, job_id: str) -> dict:
        with self.lock:
            if job_id in self.events:
                raise ValueError("이미 실행 중인 영상 작업입니다.")
            job = store.get(self.config, job_id)
            if job["status"] == "completed":
                raise ValueError("완료 결과는 덮어쓰지 않습니다. 대본을 수정해 새 버전을 만드세요.")
            folder = job_folder(self.config, job_id)
            manifest = json.loads((folder / "job.json").read_text())
            config = self.config.model_copy(update={"voice": manifest["settings"]["voice"],
                "tts_profile": manifest["settings"].get("tts_profile")})
            if manifest["settings"].get("encoder") != config.encoder:
                raise ValueError("기존 작업의 인코더 설정과 현재 설정이 다릅니다.")
            store.update(config, job_id, job["topic"], "queued", job["stage"], details=job["details"])
            key = job["details"].get("draft_id")
            if key:
                draft = studio_store.get(config, key)
                if draft["revision"] == job["details"]["revision"]:
                    draft.update(status="rendering", error=None)
                    studio_store.save(config, draft, draft["revision"])
            self._schedule(job_id, config)
            return store.get(config, job_id)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            event = self.events.get(job_id)
            if event is None:
                raise ValueError("현재 서버가 실행 중인 영상 작업만 취소할 수 있습니다.")
            event.set()
            return {"id": job_id, "status": "cancelling"}

    def recover(self) -> None:
        """A new process cannot own work left running by the previous server."""
        from app.tts.profiles import recover_samples
        recover_samples(self.config)
        for job in store.recent(self.config):
            if job["status"] in {"running", "queued"} and job["details"].get("draft_id"):
                store.update(self.config, job["id"], job["topic"], "interrupted", job["stage"],
                             "서버가 다시 시작되었습니다. 작업을 재개하세요.", job["details"])
        for draft in studio_store.listing(self.config):
            if draft["status"] in {"researching", "generating", "rendering"}:
                draft.update(status="failed", error="서버 재시작으로 작업이 중단되었습니다. 재생성·재개하세요.")
                studio_store.save(self.config, draft, draft["revision"])
