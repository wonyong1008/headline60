import json
from datetime import UTC, datetime
from uuid import uuid4

from app.config import Settings
from app.jobs.store import connect


def now() -> str:
    return datetime.now(UTC).isoformat()


def init(config: Settings) -> None:
    with connect(config) as db:
        db.execute("CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")


def save_articles(config: Settings, articles: list[dict]) -> None:
    init(config)
    with connect(config) as db:
        for article in articles:
            db.execute("INSERT INTO articles VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (article["id"], json.dumps(article, ensure_ascii=False)))


def articles(config: Settings, ids: list[str] | None = None) -> list[dict]:
    init(config)
    with connect(config) as db:
        if ids is None:
            rows = db.execute("SELECT payload FROM articles ORDER BY rowid DESC LIMIT 300").fetchall()
        else:
            rows = []
            for key in dict.fromkeys(ids):
                row = db.execute("SELECT payload FROM articles WHERE id=?", (key,)).fetchone()
                if row:
                    rows.append(row)
    return [json.loads(row[0]) for row in rows]


def create(config: Settings, payload: dict) -> dict:
    init(config)
    draft = {**payload, "id": uuid4().hex, "revision": 1, "status": "needs_evidence",
             "created_at": now(), "updated_at": now(), "approved_revision": None, "error": None}
    with connect(config) as db:
        db.execute("INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?)",
                   (draft["id"], 1, draft["status"], json.dumps(draft, ensure_ascii=False),
                    draft["created_at"], draft["updated_at"]))
    return draft


def get(config: Settings, key: str) -> dict:
    init(config)
    with connect(config) as db:
        row = db.execute("SELECT payload FROM drafts WHERE id=?", (key,)).fetchone()
    if not row:
        raise KeyError("대본 작업을 찾을 수 없습니다.")
    return json.loads(row[0])


def save(config: Settings, draft: dict, expected_revision: int) -> dict:
    draft["updated_at"] = now()
    with connect(config) as db:
        result = db.execute("""UPDATE drafts SET revision=?, status=?, payload=?, updated_at=?
            WHERE id=? AND revision=?""", (draft["revision"], draft["status"],
            json.dumps(draft, ensure_ascii=False), draft["updated_at"], draft["id"], expected_revision))
        if result.rowcount != 1:
            raise ValueError("다른 화면에서 수정되었습니다. 페이지를 새로고침하세요.")
    return draft


def listing(config: Settings) -> list[dict]:
    init(config)
    with connect(config) as db:
        rows = db.execute("SELECT payload FROM drafts ORDER BY updated_at DESC LIMIT 50").fetchall()
    return [json.loads(row[0]) for row in rows]
