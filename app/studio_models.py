import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.content_modes import ContentMode, creation_issues
from app.news.feeds import CATEGORIES, public_url
from app.script.models import Script, TemplateName


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(StrictModel):
    id: str = Field(pattern=r"^[a-f0-9]{12}$")
    url: str = Field(max_length=2000)
    publisher: str = Field(min_length=2, max_length=100)
    title: str = Field(min_length=2, max_length=300)
    published_date: date
    accessed_date: date
    excerpt: str = Field(min_length=10, max_length=600)
    fact: str = Field(min_length=10, max_length=300)
    conditions: str = Field(min_length=2, max_length=300)
    reviewed: bool = False
    scope: Literal["facts", "source_outline", "list_item"] = "facts"
    verification: Literal["manual", "source_matched"] = "manual"
    source_sha256: str = Field(default="", max_length=64)
    date_precision: Literal["day", "month"] = "day"
    permission: Literal["official", "allowed"] = "official"

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return public_url(value)


class ReelOptions(StrictModel):
    template: TemplateName = "headline_news"
    edit_style: Literal["classic", "reels_v3"] = "reels_v3"
    narration_speed: float = Field(default=1.18, ge=1, le=1.35)
    subtitle_style: Literal["clean", "boxed", "dynamic"] = "clean"
    music_asset_id: str | None = Field(default=None, max_length=60)
    anchor_id: Literal["headline60-brown-adult-01"] | None = None


class DraftCreate(ReelOptions):
    content_mode: ContentMode = "news"
    topic: str = Field(default="", max_length=200)
    fiction_acknowledged: bool = False
    article_ids: list[str] = Field(default_factory=list, max_length=20)
    seconds: Literal[30, 60] = 30
    resolution: Literal[720, 1080] = 720
    category: str = "economy"
    style: str = Field(default="차분하고 명확한 한국어 뉴스 해설", max_length=100)

    @model_validator(mode="after")
    def mode_requirements(self):
        issues = creation_issues(self.model_dump())
        if issues:
            raise ValueError("; ".join(issues))
        return self

    @field_validator("category")
    @classmethod
    def valid_category(cls, value):
        if value not in CATEGORIES:
            raise ValueError("지원하지 않는 카테고리입니다.")
        return value


class DraftEdit(ReelOptions):
    content_mode: ContentMode = "news"
    topic: str = Field(default="", max_length=200)
    fiction_acknowledged: bool = False
    edit_style: Literal["classic", "reels_v3"] = "classic"
    narration_speed: float = Field(default=1.0, ge=1, le=1.35)
    template: Literal["legacy", "headline_news", "story_explainer", "data_comparison"] = "legacy"
    revision: int = Field(ge=1)
    script: Script | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    seconds: Literal[30, 60] = 30
    resolution: Literal[720, 1080] = 720
    voice: str = Field(default="Yuna", min_length=1, max_length=100)
    voice_profile_id: str | None = Field(default=None, max_length=60)


def evidence_ready(evidence: list[Evidence], category: str) -> list[str]:
    from app.news.evidence import official_url

    issues = []
    if not evidence:
        return ["RSS 제목만으로 대본을 만들 수 없습니다. 원문을 확인한 핵심 주장과 근거를 추가하세요."]
    ids = [e.id for e in evidence]
    if len(set(ids)) != len(ids):
        issues.append("근거 ID가 중복됩니다.")
    today = date.today()
    for e in evidence:
        auto_matched = (e.verification == "source_matched" and re.fullmatch(r"[a-f0-9]{64}", e.source_sha256)
                        and e.fact in e.excerpt)
        if not e.reviewed and not auto_matched:
            issues.append(f"{e.id}: 원문과 핵심 주장 확인이 필요합니다.")
        if e.published_date > today or e.accessed_date > today:
            issues.append(f"{e.id}: 미래 날짜를 근거로 사용할 수 없습니다.")
        if e.accessed_date < e.published_date:
            issues.append(f"{e.id}: 확인일이 발표일보다 빠릅니다.")
        if e.permission == "official" and not official_url(e.url):
            issues.append(f"{e.id}: 공식 도메인이 아닙니다. 사용 허용 자료인지 직접 확인하세요.")
        if category in {"health", "realestate", "auto"} and not official_url(e.url) and e.scope != "source_outline":
            issues.append(f"{e.id}: 이 카테고리는 공식 자료 확인이 필요합니다.")
    if category in {"health", "realestate", "auto"} and not any(official_url(e.url) and e.scope == "facts" for e in evidence):
        issues.append("이 카테고리의 사실·효과를 뒷받침하는 공식 자료가 필요합니다.")
    return issues


def numeric_tokens(text: str) -> set[str]:
    # Link identifiers (e.g. newsId=148948818) are attribution, not stated figures.
    return set(re.findall(r"\d+(?:[.,]\d+)*", re.sub(r"https?://\S+", " ", text)))


def script_issues(script: Script, evidence: list[Evidence], seconds: int) -> list[str]:
    issues = []
    ids = {e.id for e in evidence}
    scopes = {e.id: e.scope for e in evidence}
    facts = {e.id: e.fact + " " + e.excerpt + " " + e.conditions + " " + (str(e.published_date)[:7] if e.date_precision == "month" else str(e.published_date)) for e in evidence}
    if abs(sum(s.duration_seconds or 0 for s in script.scenes) - seconds) > 0.05:
        issues.append(f"장면 길이의 합은 {seconds}초여야 합니다.")
    if not script.hook or script.scenes[0].narration != script.hook:
        issues.append("후킹 문장과 첫 장면 내레이션을 동일하게 입력하세요.")
    if script.scenes[0].duration_seconds != 3:
        issues.append("첫 후킹 장면은 3초로 설정하세요.")
    for index, scene in enumerate(script.scenes):
        linked = set(scene.claim_ids)
        if not linked or not linked <= ids:
            issues.append(f"장면 {index + 1}: 유효한 근거를 하나 이상 연결하세요.")
        supporting = " ".join(facts.get(key, "") for key in linked)
        if linked and all(scopes.get(key) == "source_outline" for key in linked):
            text = scene.narration + " " + scene.headline_text
            if re.search(r"(?:발기|정력|치료|혈액순환|혈류|효능).*(?:개선|강화|향상|회복|도움|증가|보장)", text) and not re.search(r"보장하지|보장되지|입증하지|단정할 수 없", text):
                issues.append(f"장면 {index + 1}: 원문의 항목 목록은 건강 효과를 입증하는 근거가 아닙니다.")
        unsupported = numeric_tokens(scene.narration + " " + scene.headline + " " + scene.headline_text + " " + scene.subtitle + " " + " ".join(scene.emphasis_words)) - numeric_tokens(supporting)
        if unsupported:
            issues.append(f"장면 {index + 1}: 근거에 없는 수치 {', '.join(sorted(unsupported))}")
        if scene.source_refs and not set(scene.source_refs) <= linked:
            issues.append(f"장면 {index + 1}: 화면 출처는 연결된 근거 안에서 지정하세요.")
        if scene.chart:
            chart = scene.chart
            refs = set(chart.source_refs)
            if not refs <= linked or not refs:
                issues.append(f"장면 {index + 1}: 차트 출처를 장면 근거에 연결하세요.")
            chart_facts = " ".join(facts.get(key, "") for key in refs)
            from decimal import Decimal
            known = {Decimal(n.replace(",", "")) for n in numeric_tokens(chart_facts)}
            if any(Decimal(str(value)) not in known for value in chart.values):
                issues.append(f"장면 {index + 1}: 근거에 없는 차트 수치입니다.")
            if chart.as_of not in chart_facts:
                issues.append(f"장면 {index + 1}: 차트 기준일을 출처와 대조하세요.")
        if re.search(r"[\u3400-\u9fff]", scene.narration):
            issues.append(f"장면 {index + 1}: 한자 혼입을 확인하세요.")
        if not scene.duration_seconds or not 1 <= scene.duration_seconds <= 20:
            issues.append(f"장면 {index + 1}: 길이는 1~20초여야 합니다.")
        if scene.caption_overrides:
            previous = 0
            for cue in scene.caption_overrides:
                if cue.end <= cue.start or cue.start < previous:
                    issues.append(f"장면 {index + 1}: 자막 시간이 겹치거나 종료가 시작보다 빠릅니다.")
                if numeric_tokens(cue.text) - numeric_tokens(supporting):
                    issues.append(f"장면 {index + 1}: 자막의 수치도 근거와 대조하세요.")
                previous = cue.end
    all_facts = " ".join(facts.values())
    if numeric_tokens(script.title + " " + script.description + " ".join(tag for tag in script.hashtags if tag not in {"헤드라인60", "헤드라인 60", "HEADLINE60", "HEADLINE 60"})) - numeric_tokens(all_facts):
        issues.append("제목·설명·해시태그에 근거 없는 수치가 있습니다.")
    return issues
