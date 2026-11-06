"""Media selection must follow the scene, not just the news category."""
from app.renderer.storyboard import select_visual
from app.script.models import Scene


def scene(query, kind="stock_image"):
    return Scene(headline="자료화면 선택", narration="장면에 맞는 실제 자료화면을 확인합니다.",
                 visual_query=query, visual_type=kind)


def test_atm_query_prefers_atm_over_unrelated_apartments():
    assets = [{"asset_id": "apartment", "name": "apartment tower", "tags": ["아파트"], "media_type": "image"},
              {"asset_id": "atm", "name": "Cash withdrawal", "tags": ["ATM", "현금 인출"], "media_type": "video"}]
    assert select_visual(scene("ATM 현금 인출"), assets)["asset_id"] == "atm"


def test_korean_consultation_query_matches_english_stock_tags():
    assets = [{"asset_id": "teller", "name": "Bank teller", "tags": ["은행 창구"], "media_type": "image"},
              {"asset_id": "advisor", "name": "financial advisor", "tags": ["consultation", "loan"], "media_type": "image"}]
    assert select_visual(scene("대출 상담받는 모습"), assets)["asset_id"] == "advisor"


def test_category_alone_never_selects_an_unrelated_photo():
    assets = [{"asset_id": "car", "name": "sports automobile", "tags": ["자동차"], "media_type": "image"}]
    assert select_visual(scene("은행 창구"), assets) is None


def test_video_request_does_not_pretend_a_photo_is_a_video():
    assets = [{"asset_id": "atm", "name": "ATM", "tags": ["현금 인출"], "media_type": "image"}]
    assert select_visual(scene("ATM 현금 인출", "stock_video"), assets) is None
