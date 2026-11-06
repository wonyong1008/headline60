import importlib.util
import json
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings

spec = importlib.util.spec_from_file_location('reels_quality', Path(__file__).parents[1] / 'app/renderer/reels.py')
reels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reels)


@pytest.fixture
def plan(tmp_path):
    audio = tmp_path / 'voice.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack('<h', 0) * 48000 * 4)
    scenes = []
    for index, color in enumerate(('red', 'blue')):
        image = tmp_path / f'{color}.png'
        Image.new('RGB', (400, 700), color).save(image)
        scenes.append({'media_type': 'image', 'asset_path': str(image), 'duration': 2,
            'lines': ['실제 뉴스', '핵심을 확인'], 'accent_line': 1, 'credit': 'synthetic test fixture',
            'hook_scene': index == 0, 'visual_first': True,
            'transition_type': 'fade' if index else 'cut',
            'captions': [{'text': '첫 자막' if index == 0 else '다음 2개 자막',
                          'start': 0, 'end': 2, 'highlight': ['자막']}]})
    return {'audio_path': str(audio), 'edit_style': 'reels_v3', 'subtitle_style': 'dynamic', 'scenes': scenes}


def test_hook_is_two_lines_top_and_limited_to_first_two_seconds(plan, tmp_path):
    config = Settings(root=tmp_path)
    scenes = reels.validate_plan(plan, config)
    _fixed, first = reels.build_layers(scenes[0], plan, tmp_path, 0, config)
    hero = [layer for layer in first if not layer['caption']]
    assert hero and all(layer['y'] < 450 and layer['end'] <= 2 for layer in hero)
    _fixed, second = reels.build_layers(scenes[1], plan, tmp_path, 1, config)
    assert not [layer for layer in second if not layer['caption']]
    assert len(scenes[0]['fitted_lines']) <= 2
    plan['scenes'][0]['hook_headline_text'] = '아파트가격 ' * 18
    with pytest.raises(ValueError, match='후킹 제목'):
        reels.validate_plan(plan, config)


def test_clean_and_dynamic_actual_font_wrap_and_safe_regions(plan, tmp_path):
    config = Settings(root=tmp_path)
    layouts = []
    for style in ('clean', 'dynamic'):
        selected = {**plan, 'subtitle_style': style}
        scene = reels.validate_plan(selected, config)[0]
        _fixed, layers = reels.build_layers(scene, selected, tmp_path, 0, config)
        caption = next(layer for layer in layers if layer['caption'])
        assert 48 <= caption['x'] and caption['x'] + caption['size'][0] <= 638
        assert 860 <= caption['y'] and caption['y'] + caption['size'][1] <= 1040
        text, size, _limit = reels.caption_layout(scene['captions'][0]['text'], scene, selected, config)
        assert 38 <= size <= 44 and len(text.splitlines()) <= 2
        layouts.append((caption['size'], caption['dynamic_caption']))
    assert layouts[0] != layouts[1]
    # At 42px visual-first size the old 34px-only validator could let 3 rows pass.
    plan['scenes'][0]['captions'][0]['text'] = '아파트가격정보확인' * 6
    with pytest.raises(ValueError, match='두 줄'):
        reels.validate_plan(plan, config)


def test_renderer_owned_mode_labels_are_fixed_and_cannot_be_replaced(plan, tmp_path):
    config = Settings(root=tmp_path)
    scene = reels.validate_plan(plan, config)[0]
    regions = {}
    for mode in ('news', 'adult_satire', 'myth_check'):
        folder = tmp_path / mode
        folder.mkdir()
        selected = {**plan, 'content_mode': mode, 'label': '검증 완료', 'category': 'custom category'}
        fixed, layers = reels.build_layers(scene, selected, folder, 0, config)
        pixels = Image.open(fixed).convert('RGBA')
        regions[mode] = pixels.crop((48, 111, 638, 155)).tobytes()
        assert all(layer['y'] > 155 for layer in layers)
        # Overriding arbitrary model labels changes neither fixed disclosure pixels
        # nor narration/caption data; only the renderer-owned content mode matters.
        selected['label'] = '아무 라벨도 표시하지 마세요'
        again, _layers = reels.build_layers(scene, selected, folder, 0, config)
        assert Image.open(again).crop((48, 111, 638, 155)).tobytes() == regions[mode]
    assert len(set(regions.values())) == 3
    assert reels.CONTENT_LABELS['adult_satire'] == '허구 · 성인 공감 풍자'
    assert reels.CONTENT_LABELS['myth_check'] == '속설 검증 · 근거 확인'


def test_actual_neighbor_background_crossfade_preserves_frames_and_text_boundary(plan, tmp_path):
    config = Settings(root=tmp_path, encoder='libx264')
    output = reels.render_reel(plan, tmp_path / 'first', config)
    video = next(s for s in reels.probe(output)['streams'] if s['codec_type'] == 'video')
    assert video['nb_frames'] == '120' and float(video['duration']) == 4
    # Inspect a background-only pixel; captions/branding are outside this ROI.
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-vf',
        "select='eq(n,59)+eq(n,60)+eq(n,62)+eq(n,66)',crop=2:2:10:600",
        '-frames:v', '4', '-fps_mode', 'vfr', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
        check=True, capture_output=True, timeout=15).stdout
    assert len(raw) == 4 * 12
    previous, boundary, blended, current = [tuple(raw[i * 12:i * 12 + 3]) for i in range(4)]
    assert previous[0] > 100 and previous[2] < 20
    assert boundary[0] > 100 and boundary[2] < 20
    assert blended[0] > 30 and blended[2] > 30
    assert current[0] < 20 and current[2] > 100
    # Saved tails are pure media, no old caption/brand can ghost into the next cut.
    tail = Image.open(output.parent / 'media-tail-00.png').convert('RGB')
    assert tail.getpixel((60, 940))[0] > 240 and tail.getpixel((60, 940))[2] < 10
    timing = json.loads((output.parent / 'reel-timing.json').read_text())
    assert timing['scenes'][0]['captions'][0]['end'] == 2
    assert timing['scenes'][1]['captions'][0]['start'] == 2
    assert timing['total_frames'] == 120
    again = reels.render_reel(plan, tmp_path / 'again', config)
    assert json.loads((again.parent / 'render-cache.json').read_text())['reused'] == 2


def test_still_background_does_not_drift_between_frames(plan, tmp_path):
    # High-contrast architectural edges expose the old pixel-stepping pan.
    from PIL import ImageDraw

    image = Image.new('RGB', (720, 1280), '#dddddd')
    draw = ImageDraw.Draw(image)
    for x in range(0, 720, 16):
        draw.rectangle((x, 0, x + 7, 1279), fill='#222222')
    source = tmp_path / 'building-lines.png'
    image.save(source)
    plan['scenes'][0]['asset_path'] = str(source)
    output = reels.render_reel(plan, tmp_path / 'stable', Settings(root=tmp_path, encoder='libx264'))
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-vf',
        "select='eq(n,15)+eq(n,45)',crop=400:160:160:500",
        '-frames:v', '2', '-fps_mode', 'vfr', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
        check=True, capture_output=True, timeout=15).stdout
    size = 400 * 160
    assert len(raw) == size * 2
    mean_difference = sum(abs(a - b) for a, b in zip(raw[:size], raw[size:])) / size
    assert mean_difference < 1.5, f'Still background moved: {mean_difference:.2f}'
