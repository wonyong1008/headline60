"""앰비언트 영상 1회 업로드.

기존 업로더(app.integrations.youtube)의 채널 재확인·이어올리기·실제 공개범위 조회·썸네일 로직을 그대로 쓴다.
다른 점: 메타데이터를 폴더의 youtube-metadata.json에서 읽고, 구독자 알림을 끄고, 영어 제목(localizations)을 함께 넣는다.
토큰·키는 출력하지 않는다.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

import httpx

import app.integrations.youtube as yt
from app.config import Settings


def tag_chars(tags: list[str]) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def main(folder: Path) -> None:
    meta = json.loads((folder / "youtube-metadata.json").read_text())
    settings = Settings()
    privacy = settings.youtube_privacy
    snippet = dict(meta["snippet"])
    title, desc, tags = snippet["title"], snippet["description"], snippet["tags"]
    assert 0 < len(title) <= 100 and not set("<>") & set(title), "제목 1~100자, 꺾쇠 금지"
    assert len(desc) <= 5000 and not set("<>") & set(desc), "설명 5000자 이하, 꺾쇠 금지"
    assert tag_chars(tags) <= 500, f"태그 합계 {tag_chars(tags)}자 > 500"
    payload = {
        "snippet": snippet,
        "status": {"privacyStatus": privacy, **meta["status"]},
        "localizations": meta.get("localizations", {}),
    }
    if not payload["localizations"]:
        payload.pop("localizations")
    print(json.dumps({"title_len": len(title), "desc_len": len(desc), "tag_chars": tag_chars(tags),
                      "privacy": privacy, "notify": meta.get("notifySubscribers", True)}, ensure_ascii=False))

    # 기존 _upload()가 뉴스 대본(script.json) 대신 이 메타데이터를 쓰게 한다.
    yt.video_metadata = lambda _folder, _privacy="private": payload

    service = yt.YoutubeService(settings)
    journal, path = folder / "youtube-upload.json", folder / "output.mp4"
    with (folder / "youtube-upload.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            record = yt.read_json(journal)
            if not record.get("session_url") and not record.get("video_id"):
                total = path.stat().st_size
                headers = {"Authorization": "Bearer " + service.access_token()}
                params = {"uploadType": "resumable", "part": "snippet,status" + (",localizations" if "localizations" in payload else "")}
                if meta.get("notifySubscribers") is False:
                    params["notifySubscribers"] = "false"
                with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
                    service._verify_channel(http, headers)
                    upload_headers = {**headers, "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(total)}
                    response = http.post(yt.UPLOAD_URL, params=params, headers=upload_headers, json=payload)
                    if response.status_code == 400 and "localizations" in payload:
                        print("영어 제목(localizations) 거부됨 → 한국어 메타데이터만으로 다시 시도")
                        payload.pop("localizations")
                        params["part"] = "snippet,status"
                        response = http.post(yt.UPLOAD_URL, params=params, headers=upload_headers, json=payload)
                    yt.checked(response, (200, 201))
                record.update(session_url=yt.session_url(response.headers["location"]), offset=0,
                              sha256=yt.file_digest(path), requested_privacy=privacy,
                              notify_subscribers=meta.get("notifySubscribers", True),
                              localizations="localizations" in payload)
                yt.private_json(journal, record)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

    result = service.upload(folder)  # 채널 재확인 → 세션 이어올리기 → 실제 공개 범위 조회 → 썸네일
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
