"""Bounded RSS metadata collection. Article bodies and press photos are never harvested."""
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

CATEGORIES = {
    "realestate": ("부동산", "부동산 주택"),
    "economy": ("경제", "경제 금융"),
    "health": ("건강·다이어트·의약품", "건강 다이어트 의약품"),
    "auto": ("자동차", "자동차"),
    "tech": ("IT·기술", "인공지능 기술"),
    "trending": ("일반 화제성 뉴스", "사회 문화"),
}


class NewsError(RuntimeError):
    pass


def public_url(value: str) -> str:
    import ipaddress

    parts = urlsplit(value.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("공개 HTTP(S) URL을 입력하세요.")
    host = parts.hostname.lower()
    if host == "localhost" or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("로컬·내부 주소는 사용할 수 없습니다.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("내부 IP 주소는 사용할 수 없습니다.")
    if parts.port not in {None, 80, 443}:
        raise ValueError("표준 HTTP(S) 포트만 허용합니다.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       urlencode(query), ""))


def article_id(url: str) -> str:
    return hashlib.sha256(public_url(url).encode()).hexdigest()[:24]


def parse_rss(content: bytes, category: str, days: int, now: datetime | None = None) -> list[dict]:
    if len(content) > 2_000_000 or b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise NewsError("RSS 크기 또는 XML 선언이 허용 범위를 벗어났습니다.")
    now = now or datetime.now(UTC)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise NewsError("Google News RSS를 해석할 수 없습니다.") from exc
    articles = {}
    for item in root.findall("./channel/item")[:100]:
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(UTC)
            if published < now - timedelta(days=days) or published > now + timedelta(minutes=15):
                continue
            url = public_url(item.findtext("link", ""))
            publisher = (item.findtext("source") or "출처 확인 필요").strip()[:120]
            title = (item.findtext("title") or "").strip()
            suffix = " - " + publisher
            title = title.removesuffix(suffix)
            if not title:
                continue
            key = article_id(url)
            articles[key] = {"id": key, "title": title[:400], "url": url,
                             "publisher": publisher, "published_at": published.isoformat(),
                             "category": category, "origin": "google_rss",
                             "verification": "metadata_only", "collected_at": now.isoformat()}
        except (ValueError, TypeError, OverflowError):
            continue
    return list(articles.values())


def collect(category: str, keyword: str, days: int) -> list[dict]:
    if category not in CATEGORIES or days not in {1, 3, 7}:
        raise ValueError("지원 카테고리와 최근 1·3·7일을 선택하세요.")
    if len(keyword) > 120:
        raise ValueError("검색어는 120자 이내로 입력하세요.")
    query = (keyword.strip() or CATEGORIES[category][1]) + f" when:{days}d"
    try:
        with (
            httpx.Client(timeout=25, trust_env=False, follow_redirects=False) as client,
            client.stream("GET", "https://news.google.com/rss/search", params={
                "q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko",
            }) as response,
        ):
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise NewsError("RSS 응답이 너무 큽니다.")
        return parse_rss(bytes(data), category, days)
    except httpx.HTTPError as exc:
        raise NewsError(f"뉴스 RSS 수집 실패: {exc}. 인터넷 연결 또는 Google 응답을 확인하세요.") from exc


def normalized_title(title: str) -> str:
    return re.sub(r"[^가-힣a-z0-9]", "", title.lower())


def group_articles(articles: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(UTC)
    groups: list[dict] = []
    for article in sorted(articles, key=lambda a: a["published_at"] or "", reverse=True):
        normalized = normalized_title(article["title"])
        group = next((g for g in groups if SequenceMatcher(
            None, normalized, normalized_title(g["title"])
        ).ratio() >= 0.58), None)
        if group is None:
            group = {"id": article["id"], "title": article["title"], "articles": []}
            groups.append(group)
        group["articles"].append(article)
    for group in groups:
        members = group["articles"]
        # Publisher labels are a proxy, not proof of independent original reporting.
        sources = len({a["publisher"].casefold() for a in members})
        dated = [datetime.fromisoformat(a["published_at"]) for a in members if a["published_at"]]
        age = max(0, (now - max(dated)).total_seconds() / 3600) if dated else 168
        group.update(article_count=len(members), publisher_count=sources,
                     score=round(min(100, len(members) * 5 + sources * 12 + max(0, 40 - age * 0.5))))
    return sorted(groups, key=lambda g: g["score"], reverse=True)
