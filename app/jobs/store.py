import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from app.config import Settings


@contextmanager
def connect(config: Settings):
    config.data.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.data / "jobs.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT NOT NULL,
        stage TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL)""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if "details" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
    connection.commit()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def update(config: Settings, job_id: str, topic: str, status: str,
           stage: str, error: str | None = None, details: dict | None = None) -> None:
    import json
    with connect(config) as db:
        db.execute("""INSERT INTO jobs (id, topic, status, stage, error, updated_at, details)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET topic=excluded.topic,
            status=excluded.status, stage=excluded.stage, error=excluded.error,
            updated_at=excluded.updated_at, details=COALESCE(?, jobs.details)""",
            (job_id, topic, status, stage, error, datetime.now(UTC).isoformat(),
             json.dumps(details or {}, ensure_ascii=False),
             json.dumps(details, ensure_ascii=False) if details is not None else None))


def recent(config: Settings) -> list[dict]:
    import json
    with connect(config) as db:
        result = [dict(row) for row in db.execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 100"
        )]
    for row in result:
        row["details"] = json.loads(row["details"])
    return result


def get(config: Settings, job_id: str) -> dict:
    import json
    with connect(config) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError("영상 작업을 찾을 수 없습니다.")
    result = dict(row)
    result["details"] = json.loads(result["details"])
    return result
