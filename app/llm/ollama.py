import json
import re

import httpx

from app.config import Settings
from app.script.models import Script, VideoRequest


class LLMError(RuntimeError):
    def __init__(self, message: str, raw: dict | None = None):
        super().__init__(message)
        self.raw = raw


def review_script(script: Script) -> None:
    text = script.model_dump_json()
    if re.search(r"[\u3400-\u9fff]", text):
        raise ValueError("한국어 대본에 한자가 섞여 있습니다. 대본을 다시 생성하세요.")
    if any(term in text for term in ("통계적으로", "무조건", "반드시 하락", "반드시 상승")):
        raise ValueError("출처 없는 통계·단정적 인과 표현이 발견되었습니다. 재생성 또는 검토가 필요합니다.")
    for scene in script.scenes:
        for sentence in re.split(r"[.!?\n]", scene.narration):
            if "반비례" in sentence and not re.search(
                r"반비례.{0,25}(?:단정할 수 없|아닙|아니|단정하지)", sentence
            ):
                raise ValueError("금리·주택 가격의 반비례 단정은 출처 검증이 필요합니다.")
    if re.search(r"\d+(?:[.,]\d+)?\s*(?:%|퍼센트|원|억원|만원|명|배|년|월|일)", text):
        raise ValueError("출처 없는 수치가 발견되었습니다. 현재 설명형 MVP에서는 허용하지 않습니다.")


def generate(request: VideoRequest, config: Settings) -> tuple[Script, dict]:
    count = 6 if request.seconds == 30 else 10
    chars = "180~240" if request.seconds == 30 else "370~460"
    notes = ""
    if "금리" in request.topic and "부동산" in request.topic:
        notes = """이 주제는 다음 기본 개념만 설명하라:
금리는 돈을 빌리는 비용과 관련된다. 금리가 오르면 신규 대출과 변동금리 대출의
이자 부담이 커질 수 있다. 고정금리 대출은 계약 조건에 따라 영향 시점이 다르다.
주택 구입 자금 조달 부담은 수요에 영향을 줄 수 있다. 주택 가격은 공급, 소득,
정책, 지역 수요에도 영향을 받으므로 금리 하나로 주택 가격을 예측할 수 없다.
실증 통계나 가격 하락을 확인된 사실처럼 제시하지 마라."""
    prompt = f"""한국어 설명형 쇼츠 대본을 JSON으로 작성하라. /no_think
주제: {request.topic}. 분류: {request.category}. 문체: {request.style}.
이 영상은 {request.seconds}초 길이의 일반 개념 설명이며 실제 최신 뉴스가 아니다.
정확히 {count}개 장면. 전체 narration 총 {chars}자. 각 장면 headline은 16자 이내.
각 narration은 소리내어 읽을 자연스러운 1~2문장으로 30~48자 정도.
가상의 사건, 날짜, 통계, 금리 수치, 의학적 효능, 투자 권유를 만들지 마라.
단정적인 인과관계 대신 조건과 한계를 설명하라. 출처를 지어내지 마라.
한자와 중국어를 섞지 말고 한국어 표기만 사용하라. '통계적으로', '반비례', '무조건' 금지.
마지막 장면에서 단순한 예측이나 개인 투자 판단에 쓸 수 없음을 말하라.
title과 description도 한국어. description에 '일반 설명 · 최신 뉴스 아님 · AI 초안 검토 필요' 명시.
{notes}
JSON 스키마: {json.dumps(Script.model_json_schema(), ensure_ascii=False)}"""
    raw = None
    try:
        with httpx.Client(timeout=240, trust_env=False) as client:
            response = client.post(config.ollama_url + "/api/generate", json={
                "model": config.model, "prompt": prompt, "stream": False, "think": False,
                "format": Script.model_json_schema(), "keep_alive": 0,
                "options": {"temperature": 0.3, "num_ctx": 4096, "num_predict": 1800},
            })
            response.raise_for_status()
            raw = response.json()
        if not raw.get("done") or raw.get("done_reason") == "length":
            raise ValueError("모델 응답이 토큰 제한으로 잘렸습니다.")
        script = Script.model_validate_json(raw["response"])
        review_script(script)
        return script, raw
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise LLMError(
            f"Ollama 대본 생성 실패: {exc}. 연결 오류라면 scripts/ollama.sh와 모델 설치를 확인하세요.",
            raw=raw,
        ) from exc
