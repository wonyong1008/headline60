from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CaptionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    highlight: list[str] = Field(default_factory=list, max_length=6)


TemplateName = Literal["headline_news", "story_explainer", "data_comparison"]


class ChartData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    labels: list[str] = Field(min_length=2, max_length=6)
    values: list[float] = Field(min_length=2, max_length=6)
    unit: str = Field(min_length=1, max_length=12)
    source_label: str = Field(default="", max_length=100)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_refs: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_values(self):
        import math
        if len(self.labels) != len(self.values) or any(not math.isfinite(x) or x < 0 for x in self.values):
            raise ValueError("차트의 항목·수치 개수와 0 이상의 유한한 값을 확인하세요.")
        if any(not x.strip() or len(x) > 24 for x in self.labels):
            raise ValueError("차트 항목은 1~24자여야 합니다.")
        return self


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    headline: str = Field(min_length=2, max_length=30)
    narration: str = Field(min_length=8, max_length=120)
    explanation: str = Field(default="", max_length=300)
    visual_type: Literal["card", "concept", "quote", "stock_video", "stock_image", "uploaded_media", "animated_headline", "data_chart", "comparison_card", "key_point", "summary_card"] = "card"
    scene_id: str = Field(default="", max_length=60)
    subtitle: str = Field(default="", max_length=120)
    headline_text: str = Field(default="", max_length=90)
    visual_query: str = Field(default="", max_length=150)
    animation_type: Literal["fade", "slide_up", "slide_down", "scale", "pop", "words", "number"] = "slide_up"
    transition_type: Literal["cut", "fade", "wipe"] = "cut"
    emphasis_words: list[str] = Field(default_factory=list, max_length=8)
    source_refs: list[str] = Field(default_factory=list, max_length=20)
    asset_id: str | None = Field(default=None, max_length=60)
    chart: ChartData | None = None
    duration_seconds: float | None = Field(default=None, ge=1, le=20)
    claim_ids: list[str] = Field(default_factory=list, max_length=20)
    kind: Literal["fact", "analysis"] = "fact"
    tts_text: str | None = Field(default=None, max_length=700)
    caption_reference_speed: float = Field(default=1, ge=1, le=1.35)
    story_role: Literal["hook", "context", "fact", "meaning", "conclusion"] = "fact"
    caption_overrides: list[CaptionEdit] = Field(default_factory=list, max_length=30)


    @field_validator("animation_type", mode="before")
    @classmethod
    def supported_animation(cls, value):
        return value if value in {"fade", "slide_up", "slide_down", "scale", "pop", "words", "number"} else "slide_up"

    @field_validator("transition_type", mode="before")
    @classmethod
    def supported_transition(cls, value):
        return value if value in {"cut", "fade", "wipe"} else "cut"


class Script(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=2, max_length=65)
    description: str = Field(min_length=5, max_length=400)
    scenes: list[Scene] = Field(min_length=4, max_length=12)
    hook: str = Field(default="", max_length=120)
    hashtags: list[str] = Field(default_factory=list, max_length=12)


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    template: Literal["legacy", "headline_news", "story_explainer", "data_comparison"] = "legacy"
    content_mode: Literal["news", "myth_check", "adult_satire"] = "news"
    edit_style: Literal["classic", "reels_v3"] = "classic"
    text_style: Literal["classic", "punch"] = "classic"
    narration_speed: float = Field(default=1.0, ge=1, le=1.35)
    subtitle_style: Literal["clean", "boxed", "dynamic"] = "clean"
    music_asset_id: str | None = Field(default=None, max_length=60)
    topic: str = Field(min_length=2, max_length=200)
    seconds: Literal[30, 60] = 30
    category: str = Field(default="경제", min_length=1, max_length=30)
    style: str = Field(default="차분한 설명형", min_length=1, max_length=100)
    resolution: Literal[720, 1080] = 720


def validate_explainer(request: VideoRequest) -> None:
    if any(word in request.topic for word in ("속보", "최신", "오늘", "어제", "현재", "실시간")):
        raise ValueError("현재 MVP는 일반 설명형 주제만 지원합니다. 최신 뉴스는 출처 수집 후 지원합니다.")
