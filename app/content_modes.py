"""Mode-specific evidence and human review gates; heuristics are not fact verification."""
import re
from typing import Literal

ContentMode = Literal["news", "myth_check", "adult_satire"]
FICTION_LABEL = "허구 · 성인 공감 풍자"
SEXUAL_TERMS = re.compile(r"성관계|성행위|성적|섹스|성욕|피임|임신|정력|콘돔|연애|불륜|키스|스킨십|자위|발기|오르가즘")
MEDICAL_TERMS = re.compile(r"발기|정력|케겔|의학|의약|약물|약을|약이|약은|복용|치료|임신|피임|정력|건강|효능|부작용|다이어트|성형|탈모|여드름|영양제")


def sensitive_flags(draft: dict, script=None) -> dict:
    if script is None:
        text = str(draft.get("script") or "")
    else:
        text = script.model_dump_json()
    text = str(draft.get("topic", "")) + " " + text
    mode = draft.get("content_mode", "news")
    return {"medical": mode == "myth_check" or draft.get("category") == "health" or bool(MEDICAL_TERMS.search(text)),
            "sexual": bool(SEXUAL_TERMS.search(text)), "is_fiction": mode == "adult_satire"}


def creation_issues(draft: dict) -> list[str]:
    mode = draft.get("content_mode", "news")
    if mode not in {"news", "myth_check", "adult_satire"}:
        return ["지원하지 않는 콘텐츠 모드입니다."]
    if mode == "news" and not draft.get("article_ids", draft.get("articles")):
        return ["뉴스 모드는 기사를 하나 이상 선택하세요."]
    if mode == "myth_check" and not (draft.get("article_ids", draft.get("articles")) or draft.get("topic", "").strip()):
        return ["속설 검증 모드는 기사 또는 검증할 주제가 필요합니다."]
    if mode == "adult_satire":
        if not draft.get("topic", "").strip():
            return ["성인 공감 풍자의 주제를 입력하세요."]
        if draft.get("fiction_acknowledged") is not True:
            return ["실제 사건이 아닌 허구 콘텐츠임을 확인하세요."]
    return []


def validation_issues(draft: dict, script=None, *, require_review=False) -> list[str]:
    from app.studio_models import Evidence, evidence_ready, script_issues
    mode = draft.get("content_mode", "news")
    # Legacy news payloads may predate article_ids; evidence remains the actual gate.
    issues = creation_issues(draft) if mode != "news" else []
    if mode != "news" and draft.get("template") == "legacy":
        issues.append("새 콘텐츠 모드는 허구·검증 표시가 있는 릴스 템플릿을 선택하세요.")
    evidence = [Evidence.model_validate(e) for e in draft.get("evidence", [])]
    if mode != "adult_satire":
        issues += evidence_ready(evidence, "health" if mode == "myth_check" else draft["category"])
        if script is not None:
            issues += script_issues(script, evidence, draft["seconds"])
    elif script is not None:
        if abs(sum(s.duration_seconds or 0 for s in script.scenes) - draft["seconds"]) > .05:
            issues.append("장면 길이 합계가 목표 시간과 다릅니다.")
        if not script.hook or script.hook != script.scenes[0].narration:
            issues.append("후킹 문장과 첫 내레이션을 동일하게 입력하세요.")
        if script.scenes[0].duration_seconds != 3:
            issues.append("첫 후킹 장면은 3초로 설정하세요.")
        for scene in script.scenes:
            if not scene.duration_seconds or not 1 <= scene.duration_seconds <= 20:
                issues.append("허구 장면 길이는 1~20초여야 합니다.")
            if scene.chart or scene.claim_ids or scene.source_refs:
                issues.append("허구 풍자에는 사실 차트·뉴스 근거 연결을 사용할 수 없습니다.")
            if scene.caption_overrides:
                previous = 0
                for cue in scene.caption_overrides:
                    if cue.end <= cue.start or cue.start < previous:
                        issues.append("허구 자막 시간이 겹치거나 종료가 시작보다 빠릅니다.")
                    previous = cue.end
        if re.search(r"완치|부작용[이가은는 ]*없|효과[가를는 ]*(입증|보장)|치료[가를는 ]*(보장|된다)|실제 사건(?:입니다|이다|을 재현)|실명 폭로", script.model_dump_json()):
            issues.append("허구 풍자에 의학 효과나 실제 사건으로 오인할 표현을 넣지 마세요.")
    if require_review:
        review = draft.get("content_review") or {}
        flags = sensitive_flags(draft, script)
        if mode != "news" and (review.get("revision") != draft.get("revision") or not review.get("content_reviewed")):
            issues.append("현재 버전의 콘텐츠 수동 검토가 필요합니다.")
        if flags["medical"] and (review.get("revision") != draft.get("revision") or not review.get("medical_reviewed")):
            issues.append("속설·의학 내용의 근거와 표현을 검토하세요.")
        if flags["sexual"] and (review.get("revision") != draft.get("revision") or not review.get("sexual_reviewed")):
            issues.append("성·연애 관련 표현의 수동 검토가 필요합니다.")
    return list(dict.fromkeys(issues))


def mode_metadata(draft: dict) -> dict:
    mode = draft.get("content_mode", "news")
    fiction = mode == "adult_satire"
    flags = sensitive_flags(draft)
    return {"content_mode": mode, "is_fiction": fiction,
            "fiction_label": FICTION_LABEL if fiction else "",
            "sensitive_flags": flags, "content_review": draft.get("content_review"),
            "review_requirements": {"content_reviewed": mode != "news",
                "medical_reviewed": flags["medical"], "sexual_reviewed": flags["sexual"]}}
