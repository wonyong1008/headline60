"""Durable URL inbox, drained by the existing single production worker."""
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from app import studio_store
from app.config import Settings
from app.jobs import store

UPLOAD_WAIT_SECONDS = 3 * 3600
from app.jobs.pipeline import job_folder
from app.news.evidence import public_url
from app.news.feeds import CATEGORIES
from app.studio_models import DraftCreate
from app.tts import profiles


@contextmanager
def keep_awake():
    """Hold a process-scoped idle-sleep assertion only while this worker is active."""
    process = None
    if sys.platform == "darwin":
        try:
            process = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    try:
        yield
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


class SubmissionQueue:
    def __init__(self, config: Settings, manager, youtube):
        self.config, self.manager, self.youtube = config, manager, youtube
        self.lock = threading.RLock()
        self._draining = False
        self._wake_version = 0
        with store.connect(config) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS submissions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                draft_id TEXT, job_id TEXT, youtube_url TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, anchor_id TEXT)""")

    def listing(self) -> list[dict]:
        with store.connect(self.config) as db:
            return [dict(row) for row in db.execute("SELECT * FROM submissions ORDER BY sequence")]

    def get(self, key: str) -> dict:
        with store.connect(self.config) as db:
            row = db.execute("SELECT * FROM submissions WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError("등록한 URL 작업을 찾을 수 없습니다.")
        return dict(row)

    def _update(self, key: str, **changes) -> dict:
        allowed = {"title", "status", "stage", "draft_id", "job_id", "youtube_url", "error"}
        if not changes or not set(changes) <= allowed:
            raise ValueError("잘못된 큐 상태 변경입니다.")
        changes["updated_at"] = datetime.now(UTC).isoformat()
        with store.connect(self.config) as db:
            db.execute("UPDATE submissions SET " + ", ".join(f"{field}=?" for field in changes)
                       + " WHERE id=?", (*changes.values(), key))
        return self.get(key)

    def _draft(self, row: dict, articles: list[dict] | None = None, category="trending") -> dict:
        defaults = DraftCreate(article_ids=[a["id"] for a in articles] if articles else ["url-import"],
                               category=category, subtitle_style="dynamic", template="story_explainer")
        available = profiles.listing(self.config)
        payload = {**defaults.model_dump(), "articles": articles or [], "evidence": [], "script": None,
                   "voice": self.config.voice, "voice_profile_id": "reels-natural-sohee" if any(
                       p["voice_profile_id"] == "reels-natural-sohee" and p["available"] for p in available
                   ) else "macos-yuna-news", "metrics": {}, "automatic": True,
                   "submission_id": row["id"], "anchor_id": row["anchor_id"]}
        if articles:
            payload["topic"] = articles[0]["title"][:200]
        else:
            payload.update(source_url=row["url"], source_loaded=False)
        draft = studio_store.create(self.config, payload)
        self._update(row["id"], draft_id=draft["id"])
        return draft

    def _create(self, url: str, anchor_id=None, articles=None, category="trending") -> dict:
        with self.lock:
            return self._register(url, anchor_id, articles, category)

    def _register(self, url, anchor_id, articles, category):
        key, now = uuid4().hex, datetime.now(UTC).isoformat()
        with store.connect(self.config) as db:
            db.execute("""INSERT INTO submissions
                (id,url,title,status,stage,created_at,updated_at,anchor_id)
                VALUES (?,?,?,'queued','queued',?,?,?)""",
                       (key, url, articles[0]["title"] if articles else url, now, now, anchor_id))
        try:
            self._draft(self.get(key), articles, category)
        except Exception as exc:
            self._update(key, status="failed", error=str(exc))
            raise
        result = self.get(key)
        self.start()
        return result

    def create(self, url: str, anchor_id=None) -> dict:
        return self._create(public_url(url), anchor_id)

    def create_selected(self, article_ids: list[str], category: str, anchor_id=None) -> dict:
        if not article_ids or category not in CATEGORIES:
            raise ValueError("기사와 올바른 카테고리를 선택하세요.")
        articles = studio_store.articles(self.config, article_ids)
        if len(articles) != len(set(article_ids)):
            raise ValueError("선택한 뉴스가 없습니다. 다시 수집·선택하세요.")
        return self._create(public_url(articles[0]["url"]), anchor_id, articles, category)

    def retry(self, key: str) -> dict:
        with self.lock:
            row = self.get(key)
            if row["status"] not in {"failed", "needs_auth", "needs_review", "cancelled", "upload_wait"}:
                raise ValueError("중단되거나 실패한 작업만 다시 시도할 수 있습니다.")
            result = self._update(key, status="queued", stage="queued", error=None)
            self.start()
            return result

    def rebuild(self, key: str) -> dict:
        """Make a fresh script/video for a finished item and upload it as a new private video."""
        with self.lock:
            row = self.get(key)
            if row["status"] != "completed" or not row["draft_id"]:
                raise ValueError("완료된 항목만 다시 만들 수 있습니다.")
            draft = studio_store.get(self.config, row["draft_id"])
            expected = draft["revision"]
            draft.setdefault("previous_outputs", []).append(
                {"job_id": row["job_id"], "youtube_url": row["youtube_url"], "rebuilt_at": datetime.now(UTC).isoformat()})
            # A new revision keeps the old job (and its YouTube upload record) untouched.
            draft.update(script=None, latest_job_id=None, status="failed", error=None,
                         approved_revision=None, content_review=None, content_warnings=[])
            draft["revision"] += 1
            studio_store.save(self.config, draft, expected)
            with store.connect(self.config) as db:
                db.execute("UPDATE submissions SET job_id=NULL, youtube_url=NULL WHERE id=?", (key,))
            result = self._update(key, status="queued", stage="queued", error=None)
            self.start()
            return result

    def _schedule_wake(self) -> None:
        if getattr(self, "_wake_timer", None) is None or not self._wake_timer.is_alive():
            self._wake_timer = threading.Timer(UPLOAD_WAIT_SECONDS + 5, self.start)
            self._wake_timer.daemon = True
            self._wake_timer.start()

    def cancel(self, key: str) -> dict:
        with self.lock:
            if self.get(key)["status"] != "queued":
                raise ValueError("아직 시작하지 않은 대기 작업만 취소할 수 있습니다.")
            return self._update(key, status="cancelled", stage="cancelled", error=None)

    def recover(self) -> None:
        """Return interrupted submissions to the inbox; completed jobs remain reusable."""
        if any(row["status"] == "upload_wait" for row in self.listing()):
            self._schedule_wake()
        with self.lock, store.connect(self.config) as db:
            db.execute("""UPDATE submissions SET status='queued', updated_at=?,
                error='서버가 다시 시작되어 저장된 단계부터 재개합니다.'
                WHERE status IN ('preparing','rendering','uploading')""",
                       (datetime.now(UTC).isoformat(),))

    def start(self) -> None:
        with self.lock:
            self._wake_version += 1
            if self._draining:
                return
            self._draining = True
            try:
                self.manager.pool.submit(self._drain)
            except Exception:
                self._draining = False
                raise

    def _claim(self, attempted: set[str]) -> dict | None:
        try:
            connected = self.youtube.status()["connected"]
        except (RuntimeError, ValueError, OSError, KeyError):
            connected = False
        with self.lock, store.connect(self.config) as db:
            db.execute("BEGIN IMMEDIATE")
            for candidate in db.execute("SELECT * FROM submissions ORDER BY sequence"):
                row = dict(candidate)
                waited = (row["status"] == "upload_wait" and row["id"] not in attempted
                          and datetime.fromisoformat(row["updated_at"]).timestamp() <= time.time() - UPLOAD_WAIT_SECONDS)
                if row["status"] == "queued" or waited or (connected and row["status"] == "needs_auth" and row["id"] not in attempted):
                    db.execute("UPDATE submissions SET status='preparing', updated_at=?, error=NULL WHERE id=?",
                               (datetime.now(UTC).isoformat(), row["id"]))
                    return row
        return None

    def _drain(self) -> None:
        from app.jobs.pipeline import LOCK_WAIT
        token = LOCK_WAIT.set(1800.0)  # pool threads are shared; restore after draining
        try:
            with keep_awake():
                self._drain_items()
        except BaseException:
            with self.lock:
                self._draining = False
            raise
        finally:
            LOCK_WAIT.reset(token)

    def _drain_items(self) -> None:
        attempted = set()
        observed_wake = self._wake_version
        while True:
            # The same lock covers the empty check and releasing ownership so create/start
            # cannot miss newly registered work at the end of a drain.
            with self.lock:
                row = self._claim(attempted)
                if row is None:
                    if observed_wake != self._wake_version:
                        observed_wake = self._wake_version
                        attempted.clear()
                        continue
                    self._draining = False
                    return
            attempted.add(row["id"])
            try:
                self._process(row)
            except Exception as exc:  # noqa: BLE001 - isolate a failed item and continue the inbox
                from app.integrations.youtube import YoutubeAuthError
                current = self.get(row["id"])
                if re.search(r"uploadLimitExceeded|quotaExceeded|rateLimitExceeded", str(exc)):
                    # YouTube's daily cap: the video is finished; upload again later automatically.
                    self._update(row["id"], status="upload_wait", stage="youtube_upload",
                                 error="YouTube 일일 업로드 한도에 걸렸습니다. 영상은 완성됐고 몇 시간 뒤 자동으로 다시 올립니다.")
                    self._schedule_wake()
                    continue
                self._update(row["id"], status="needs_auth" if isinstance(exc, YoutubeAuthError) else "failed",
                             stage=current["stage"], error=str(exc) or type(exc).__name__)

    def _process(self, row: dict) -> None:
        key = row["id"]
        draft = studio_store.get(self.config, row["draft_id"]) if row["draft_id"] else self._draft(row)
        job_id = row["job_id"] or draft.get("latest_job_id")
        if not job_id:
            # A process can stop after prepare_news writes the job and before its ID is
            # attached to the draft/submission. Recover that frozen version as well.
            for existing in store.recent(self.config):
                details = existing["details"]
                if (details.get("draft_id") == draft["id"] and details.get("revision") == draft["revision"]
                        and details.get("automation_private_preview")):
                    job_id = existing["id"]
                    break
        if not job_id:
            if not draft.get("script"):
                self._update(key, status="preparing", stage="source_and_script")
                draft.update(status="researching", automatic=True, approved_revision=None,
                             content_review=None, error=None)
                studio_store.save(self.config, draft, draft["revision"])
                self.manager._prepare(draft)
                draft = studio_store.get(self.config, draft["id"])
                self._update(key, title=draft.get("topic") or row["title"])
                if draft["status"] != "review" or not draft.get("script"):
                    self._update(key, status="needs_review" if draft["status"] == "needs_evidence" else "failed",
                                 stage="source_and_script", error=draft.get("error") or "대본 생성 결과를 확인하세요.")
                    return
            self._update(key, status="rendering", stage="render")
            try:
                job = self.manager.render(draft["id"], draft["revision"], private_preview=True, schedule=False)
            except ValueError as exc:
                self._update(key, status="needs_review", stage="render_validation", error=str(exc))
                return
            job_id = job["id"]
        self._update(key, job_id=job_id, title=(draft.get("script") or {}).get("title") or row["title"],
                     status="rendering", stage="render")
        job = self.manager.run_prepared(job_id)
        if job["status"] != "completed":
            self._update(key, status="failed", stage=job["stage"], error=job.get("error") or "영상 제작이 중단되었습니다.")
            return
        self._update(key, status="uploading", stage="youtube_upload")
        if not self.youtube.status()["connected"]:
            self._update(key, status="needs_auth", stage="youtube_upload", error="YouTube 계정을 연결하면 완성 영상을 업로드합니다.")
            return
        result = self.youtube.upload(job_folder(self.config, job_id))
        if result.get("privacy") != "private":
            raise RuntimeError("자동 제작 영상은 비공개 업로드만 허용합니다.")
        # Check results stay in the draft data only; the user judges content and is not shown memos.
        self._update(key, status="completed", stage="completed", youtube_url=result["url"], error=None)
