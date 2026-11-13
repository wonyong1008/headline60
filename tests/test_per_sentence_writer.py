"""The default news writer: code picks distinct article sentences, the model rewrites one at a time."""
import hashlib
import json
import re
from datetime import date

import httpx

from app.config import Settings
from app.llm import automatic_writer
from app.llm.automatic_writer import to_haeyo
from app.news import automatic
from app.studio_models import numeric_tokens

LINES = ['정부는 내년까지 수도권에 주택 12만 가구를 공급하는 계획을 발표했다. 수도권 공공임대 주택은 3만 가구로, 청년과 신혼부부에게 먼저 배정된다.',
         '국토교통부는 주택 착공을 앞당기기 위해 수도권 인허가 절차를 6개월 단축한다고 밝혔다. 전문가들은 주택 실제 입주까지는 최소 4년이 걸릴 것으로 내다봤다.',
         '수도권 아파트 매매가격은 지난달 0.3% 올라 주택 상승세를 이어갔다. 정부는 다음 달 수도권 주택 세부 입지를 공개할 예정이다.']
TOPIC = '수도권 주택 12만 가구 공급'
REAL_CLIENT = httpx.Client


def evidence():
    page = {'url': 'https://www.korea.kr/news/x', 'resolved_url': 'https://www.korea.kr/news/x', 'title': TOPIC,
            'publisher': '정책브리핑', 'published_date': date.today().isoformat(), 'date_precision': 'day',
            'sha256': hashlib.sha256(b'x').hexdigest(), 'lines': LINES}
    return automatic.excerpts(page, '수도권 주택 공급', fallback=True, limit=8)


def run(monkeypatch, line_for, hook=None):
    """line_for(source_sentence, attempt) -> model answer for one rewrite call."""
    original, calls, attempts = REAL_CLIENT, [], {}

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if 'hook' in body['format']['properties']:
            answer = hook or {'hook': '12만 가구가 풀린다고요?', 'title': '수도권 주택 12만 가구 공급'}
        else:
            sentence = re.search(r'기사 문장: "(.+?)"', body['prompt']).group(1)
            attempts[sentence] = attempts.get(sentence, 0) + 1
            answer = line_for(sentence, attempts[sentence])
        return httpx.Response(200, json={'done': True, 'response': json.dumps(answer, ensure_ascii=False)})

    monkeypatch.setattr(automatic_writer.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(respond)))
    draft = {'category': 'realestate', 'seconds': 60, 'evidence': evidence(), 'topic': TOPIC}
    script, raw = automatic_writer.generate_automatic(draft, Settings())
    return script, raw, calls, draft


def spoken(sentence, attempt):
    words = sentence.split()
    return {'narration': ' '.join(words[:6]).rstrip('.,') + ' 소식이에요.', 'headline': words[1][:10],
            'key_phrase': words[1], 'visual_en': 'apartment construction site'}


def test_each_scene_rewrites_one_distinct_article_sentence(monkeypatch):
    script, raw, calls, draft = run(monkeypatch, spoken)
    assert raw['writer'] == 'per_sentence'
    body = [s for s in script.scenes if s.story_role not in ('hook',) and s.scene_id != 'scene-cta']
    assert len(body) == len(raw['lines']) >= 5
    assert script.scenes[0].narration == '12만 가구가 풀린다고요?' and script.scenes[-1].scene_id == 'scene-cta'
    assert sum(s.duration_seconds for s in script.scenes) == 60
    narrations = [s.narration for s in body]
    assert len(set(narrations)) == len(narrations)
    ids = {e['id'] for e in draft['evidence']}
    assert all(s.claim_ids and set(s.claim_ids) <= ids for s in script.scenes)
    # Prompts carry one source sentence only (no title, no previous narration) so the model cannot repeat itself.
    rewrites = [c for c in calls if 'narration' in c['format']['properties']]
    assert all(c['prompt'].count('기사 문장:') == 1 and TOPIC not in c['prompt'] for c in rewrites)
    assert rewrites[-1]['keep_alive'] == 0


def test_invented_numbers_are_retried_then_replaced_by_the_source_sentence(monkeypatch):
    def invent(sentence, attempt):
        return {'narration': '무려 99만 가구가 한꺼번에 쏟아진다고 해요.', 'headline': '99만 가구',
                'key_phrase': '가구', 'visual_en': 'apartment'}
    script, raw, calls, draft = run(monkeypatch, invent)
    source = ' '.join(e['excerpt'] for e in draft['evidence'])
    for scene in script.scenes:
        assert not numeric_tokens(scene.narration + ' ' + scene.headline) - numeric_tokens(source)
        assert scene.narration.endswith(('요.', '요?', '요!', '요'))
    assert len([c for c in calls if 'narration' in c['format']['properties']]) == 2 * len(raw['lines'])


def test_split_digits_and_written_endings_are_repaired(monkeypatch):
    def broken(sentence, attempt):
        if '12만' in sentence:
            return {'narration': '정부가 수도권에 주택 1:2만 가구를 공급한다', 'headline': '1:2만 가구', 'key_phrase': '주택', 'visual_en': 'apartment'}
        return spoken(sentence, attempt)
    script, *_ = run(monkeypatch, broken)
    first = script.scenes[1]
    assert first.narration == '정부가 수도권에 주택 12만 가구를 공급해요.' and first.headline == '12만 가구'


def test_invented_hook_numbers_fall_back_to_the_article_title(monkeypatch):
    script, *_ = run(monkeypatch, spoken, hook={'hook': '무려 500만 명이 몰렸다고요?', 'title': '500만 명의 선택'})
    assert '500' not in script.scenes[0].narration and '500' not in script.title


def test_short_articles_keep_the_whole_script_writer(monkeypatch):
    import tests.test_automatic_creation as legacy
    source = automatic.excerpts(legacy.page(), '주택 공급')
    picked = automatic_writer.pick_sentences("주택 공급", [automatic_writer.Evidence.model_validate(e) for e in source], 7)
    assert len(picked) < 3


def test_written_style_endings_become_spoken():
    assert to_haeyo('협회는 서한을 받은 것으로 전해졌다.') == '협회는 서한을 받은 것으로 전해졌어요.'
    assert to_haeyo('시한은 내년 3월까지다.') == '시한은 내년 3월까지예요.'
    assert to_haeyo('수리 대란을 겪고 있다.') == '수리 대란을 겪고 있어요.'
    assert to_haeyo('조사를 진행한다') == '조사를 진행해요.'
    assert to_haeyo('불가능한 상황인 것이다.') == '불가능한 상황인 거예요.'
    assert to_haeyo('누적 인원은 8785명요') == '누적 인원은 8785명이에요.'
    assert to_haeyo('업무가 과부하 상태에 빠져 있어요') == '업무가 과부하 상태에 빠져 있어요.'


def test_blog_asides_and_title_restatements_are_skipped_but_ordinary_words_are_not():
    from app.studio_models import Evidence
    topic = '꿈에서 고양이를 만났다면？ 상황별 고양이 꿈 해몽'
    text = ('꿈에서 고양이를 만났다면 상황별 고양이 꿈 해몽 생각보다 많은 분들이 궁금해한다고 해요. '
            '심지어 저도 고양이가 꿈에서 문다면 살짝 무섭다는 생각이 들었어요. '
            '검은 고양이가 따라오는 꿈은 신경 쓰이는 관계나 문제가 있다는 해몽으로 볼 수 있다고 해요. '
            '✅ 고양이를 품에 안는 꿈은 애정과 안정감을 상징하는 꿈으로 풀이한다고 해요.')
    source = Evidence.model_validate({**evidence()[0], 'excerpt': text, 'fact': text[:40]})
    kept = [s for s, _i in automatic_writer.spoken_sentences([source], topic)]
    assert kept == ['검은 고양이가 따라오는 꿈은 신경 쓰이는 관계나 문제가 있다는 해몽으로 볼 수 있다고 해요.',
                    '고양이를 품에 안는 꿈은 애정과 안정감을 상징하는 꿈으로 풀이한다고 해요.']


def test_rejected_hooks_fall_back_to_a_readable_part_of_the_headline(monkeypatch):
    monkeypatch.setattr(automatic_writer, '_ask', lambda *a, **k: {'hook': '대한축구협회, 올해 안에 새 회장 선출 무산…FIFA가 제동'})
    hook, subject = automatic_writer.hook_line(None, '대한축구협회, 올해 안에 새 회장 선출 무산…FIFA가 제동', 'a', 'b')
    assert hook == '대한축구협회, 올해 안에 새 회장 선출 무산?' and subject == ''
    hook, _ = automatic_writer.hook_line(None, '갤럭시 별거 아니라며？ 계속된 하자에 역대급 위기', 'a', 'b')
    assert hook == '갤럭시 별거 아니라며?'


def test_a_writer_error_falls_back_to_the_whole_script_writer_and_is_recorded(monkeypatch):
    def broken(*args):
        raise ValueError('bad hook')
    monkeypatch.setattr(automatic_writer, 'generate_per_sentence', broken)
    monkeypatch.setattr(automatic_writer, '_generate_whole', lambda *a: ('script', {}))
    _script, raw = automatic_writer.generate_automatic({'category': 'realestate', 'seconds': 60, 'evidence': evidence(), 'topic': TOPIC}, Settings())
    assert 'bad hook' in raw['per_sentence_error']


def test_trailing_quotes_do_not_break_spoken_endings():
    assert to_haeyo('상황이 생길 수 있단 걸 암시해요"') == '상황이 생길 수 있단 걸 암시해요.'


def test_model_ending_glitches_are_repaired_sentence_by_sentence():
    from app.llm.automatic_writer import spoken_line
    assert spoken_line('상황이 생길 수 있단 걸 암시해요요') == '상황이 생길 수 있단 걸 암시해요.'
    assert spoken_line('개인정보를 빼내는 데 악용한 사이트다요') == '개인정보를 빼내는 데 악용한 사이트예요.'
    assert spoken_line('지원 부서가 과부하 상태라고 말했다요') == '지원 부서가 과부하 상태라고 말했어요.'
    assert spoken_line('나타낼 수 있다고 해요. 요') == '나타낼 수 있다고 해요.'
    assert spoken_line('선거일이 바뀌었어요. 이거예요.') == '선거일이 바뀌었어요.'
    assert spoken_line('개인정보가 노출됐다. 주소가 드러났어요.') == '개인정보가 노출됐어요. 주소가 드러났어요.'


def test_a_broken_model_reply_only_affects_its_own_scene(monkeypatch):
    original, state = httpx.Client, {'broken': 0}

    def respond(request):
        body = json.loads(request.content)
        if 'hook' in body['format']['properties']:
            answer = json.dumps({'hook': '12만 가구가 풀린다고요?'}, ensure_ascii=False)
        elif '12만' in body['prompt']:
            state['broken'] += 1
            answer = '{\n  "narration": "정부가 수도권에'  # cut off mid-string, every time
        else:
            sentence = re.search(r'기사 문장: "(.+?)"', body['prompt']).group(1)
            answer = json.dumps(spoken(sentence, 1), ensure_ascii=False)
        return httpx.Response(200, json={'done': True, 'response': answer})

    monkeypatch.setattr(automatic_writer.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(respond)))
    draft = {'category': 'realestate', 'seconds': 60, 'evidence': evidence(), 'topic': TOPIC}
    script, raw = automatic_writer.generate_automatic(draft, Settings())
    assert raw['writer'] == 'per_sentence' and state['broken'] >= 2
    assert script.scenes[1].narration == '정부는 내년까지 수도권에 주택 12만 가구를 공급하는 계획을 발표했어요.'


def test_added_asides_without_any_source_words_are_dropped():
    from app.llm.automatic_writer import spoken_line
    source = '축구협회 차기 회장 선거는 12월 말로 예정돼 있었다.'
    assert spoken_line('축구협회 차기 회장 선거가 12월 말로 예정됐는데요. 이거 요즘 관심이 많아요.', source) \
        == '축구협회 차기 회장 선거가 12월 말로 예정됐는데요.'
    assert spoken_line('회장 선거가 12월 말이에요. 선거 일정이 바뀌었어요.', source) == '회장 선거가 12월 말이에요. 선거 일정이 바뀌었어요.'


def test_the_first_screen_searches_for_the_story_subject_not_a_generic_street(monkeypatch):
    script, raw, *_ = run(monkeypatch, spoken, hook={'hook': '12만 가구가 풀린다고요?', 'visual_en': 'apartment construction site'})
    assert script.scenes[0].visual_query.endswith('/ apartment construction site') and raw['subject'] == 'apartment construction site'
    script, raw, *_ = run(monkeypatch, spoken, hook={'hook': '12만 가구가 풀린다고요?', 'visual_en': '위기 상황'})
    assert raw['subject'] == '' and 'city street' not in script.scenes[0].visual_query
    assert script.scenes[0].visual_query.endswith('/ apartment construction site')  # first fact scene's subject


def test_overlong_lines_keep_whole_sentences_or_are_skipped():
    from app.llm.automatic_writer import fit_narration
    long_sentence = '가' * 130 + '요.'
    assert fit_narration(long_sentence) == ''
    assert fit_narration('짧은 첫 문장이에요. ' + long_sentence) == '짧은 첫 문장이에요.'
