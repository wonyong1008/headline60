import importlib.util
import json
import math
import shutil
import struct
import subprocess
import threading
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.config import Settings
from app.process import JobCancelled, cancellation

spec = importlib.util.spec_from_file_location('reels_v2', Path(__file__).parents[1] / 'app/renderer/reels.py')
reels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reels)


def audio(path, seconds=1, frequency=300):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i * frequency * 2 * math.pi / 48000)))
                                   for i in range(round(48000 * seconds))))


@pytest.fixture
def plan(tmp_path):
    voice = tmp_path / 'voice.wav'
    audio(voice)
    return {'audio_path': str(voice), 'category': '경제', 'scenes': [
        {'media_type': 'graphic', 'duration': 2, 'lines': ['호가와 실거래가'], 'accent_line': 0,
         'narration': '서로 다른 가격을 구분합니다.',
         'captions': [{'text': '서로 다른 가격을 구분합니다.', 'start': 0, 'end': 2, 'highlight': ['가격']}]}]}


def test_sixty_seconds_twelve_scenes_and_fallback_graphics(plan, tmp_path):
    scene = {**plan['scenes'][0], 'duration': 5}
    plan['scenes'] = [scene.copy() for _ in range(12)]
    scenes = reels.validate_plan(plan, Settings(root=tmp_path))
    assert sum(scene['frames'] for scene in scenes) == 1800
    assert all(scene['media_type'] == 'graphic' for scene in scenes)


@pytest.mark.parametrize('captions', [
    [{'text': '범위 밖', 'start': 0, 'end': 2.1}],
    [{'text': '첫 문장', 'start': 0, 'end': 1.4}, {'text': '겹침', 'start': 1.2, 'end': 2}],
])
def test_bad_caption_boundaries_rejected(plan, tmp_path, captions):
    plan['scenes'][0]['captions'] = captions
    with pytest.raises(ValueError, match='자막'):
        reels.validate_plan(plan, Settings(root=tmp_path))


def test_chart_requires_evidence_and_rejects_nan(plan, tmp_path):
    chart = {'labels': ['A', 'B'], 'values': [1, 2], 'unit': '%'}
    plan['scenes'][0]['chart'] = chart
    with pytest.raises(ValueError, match='출처'):
        reels.validate_plan(plan, Settings(root=tmp_path))
    chart.update({'source_label': '검증된 테스트 자료', 'as_of': '2026-10-30', 'source_refs': ['evidence-1']})
    assert reels.validate_plan(plan, Settings(root=tmp_path))[0]['chart']['values'] == [1, 2]
    chart['values'][0] = float('nan')
    with pytest.raises(ValueError, match='수치'):
        reels.validate_plan(plan, Settings(root=tmp_path))


def test_three_layouts_differ_and_captions_stay_separate(plan, tmp_path):
    config = Settings(root=tmp_path)
    scene = reels.validate_plan(plan, config)[0]
    pixels = []
    for index, template in enumerate(sorted(reels.TEMPLATES)):
        folder = tmp_path / template
        folder.mkdir()
        fixed, layers = reels.build_layers(scene, {**plan, 'template': template}, folder, index, config)
        pixels.append(Image.open(fixed).tobytes())
        captions = [layer for layer in layers if layer['caption']]
        headlines = [layer for layer in layers if not layer['caption']]
        assert len(captions) == 1 and captions[0]['y'] >= 920
        assert max(layer['y'] + layer['size'][1] for layer in headlines) < captions[0]['y']
    assert len(set(pixels)) == 3


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for real chart reveal')
def test_chart_panel_reveals_then_holds_exact_sourced_pixels(plan, tmp_path):
    config = Settings(root=tmp_path)
    plan['template'] = 'data_comparison'
    plan['scenes'][0]['chart'] = {'labels': ['A', 'B'], 'values': [10, 20], 'unit': '%',
        'source_label': 'synthetic fixture', 'as_of': '2026-10-30', 'source_refs': ['fixture-1']}
    scene = reels.validate_plan(plan, config)[0]
    _fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
    chart = next(layer for layer in layers if layer['animation'] == 'chart_reveal')
    assert chart['start'] == .3 and chart['end'] - .7 >= 1.3
    chain, x, y, enable = reels.motion_filter(chart, 1, 'chart', 1)
    # The real filter scales a fixed raster panel and holds it after the reveal.
    output = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=black:s=720x1280:r=30:d=2', '-loop', '1', '-framerate', '30', '-i', str(chart['path']),
        '-filter_complex_threads', '1', '-filter_complex',
        chain + f";[0:v][chart]overlay=x='{x}':y='{y}':enable='{enable}',"
        "select='eq(n,10)+eq(n,15)+eq(n,30)+eq(n,45)',crop=590:350:48:505[out]",
        '-map', '[out]', '-frames:v', '4', '-fps_mode', 'vfr', '-pix_fmt', 'rgb24',
        '-f', 'rawvideo', '-'], check=True, capture_output=True, timeout=15).stdout
    size = 590 * 350 * 3
    assert len(output) == 4 * size
    early, middle, full, held = [output[i * size:(i + 1) * size] for i in range(4)]
    assert early != middle and middle != full
    assert full == held


def test_missing_photo_does_not_silently_become_graphic(plan, tmp_path):
    plan['scenes'][0].update({'media_type': 'image', 'asset_path': '/missing/image.png'})
    with pytest.raises(ValueError, match='자료사진'):
        reels.validate_plan(plan, Settings(root=tmp_path))


def test_cancel_stops_before_rendering(plan, tmp_path):
    event = threading.Event()
    event.set()
    with cancellation(event), pytest.raises(JobCancelled):
        reels.render_reel(plan, tmp_path / 'cancelled', Settings(root=tmp_path))
    assert not (tmp_path / 'cancelled').exists()


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for actual color crossfade')
def test_hero_color_crossfade_and_exit_keep_caption_timing(plan, tmp_path):
    config = Settings(root=tmp_path)
    plan['scenes'][0]['lines'] = ['가격 확인']
    plan['scenes'][0]['emphasis_words'] = ['가격']
    plan['scenes'][0]['animation_type'] = 'pop'
    scene = reels.validate_plan(plan, config)[0]
    _fixed, layers = reels.build_layers(scene, plan, tmp_path, 0, config)
    heroes = [layer for layer in layers if not layer['caption']]
    caption = next(layer for layer in layers if layer['caption'])
    assert len(heroes) == 2 and heroes[1]['color_start'] > heroes[0]['start']
    caption_filter, _, _, _ = reels.motion_filter(caption, 3, 'caption', 1)
    assert 'fade=' not in caption_filter
    assert (caption['start_frame'], caption['end_frame']) == (0, 60)
    inputs, filters, previous = [], [], '0:v'
    for i, layer in enumerate(heroes):
        inputs.extend(['-loop', '1', '-framerate', '30', '-i', str(layer['path'])])
        chain, x, y, enable = reels.motion_filter(layer, i + 1, f'm{i}', 1)
        filters.extend([chain, f"[{previous}][m{i}]overlay=x='{x}':y='{y}':enable='{enable}'[o{i}]"])
        previous = f'o{i}'
    filters.append(f"[{previous}]select='eq(n,6)+eq(n,21)+eq(n,59)',crop=650:200:0:290[out]")
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'color=black:s=720x1280:r=30:d=2', *inputs, '-filter_complex_threads', '1',
        '-filter_complex', ';'.join(filters), '-map', '[out]', '-frames:v', '3',
        '-fps_mode', 'vfr', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
        check=True, capture_output=True, timeout=15).stdout
    count = 650 * 200 * 3
    frames = [raw[i * count:(i + 1) * count] for i in range(3)]
    assert len(raw) == 3 * count
    def coral_pixels(frame):
        return sum(r > 150 and r > g + 50 for r, g in zip(frame[0::3], frame[1::3], strict=True))
    assert coral_pixels(frames[0]) < 10
    assert coral_pixels(frames[1]) > 500
    assert sum(frames[2]) < sum(frames[1]) * .5


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for real synthesis fixture')
def test_real_ffmpeg_motions_video_loop_music_and_segment_reuse(plan, tmp_path):
    clip = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=160x96:rate=15:duration=0.4', '-c:v', 'libx264',
                    '-threads', '1', str(clip)], check=True, capture_output=True, timeout=15)
    scene = plan['scenes'][0]
    plan['scenes'] = [{**scene, 'duration': .8, 'animation_type': motion,
                       'transition_type': ('cut', 'fade', 'wipe')[i % 3],
                       'captions': [{'text': '실제 자막', 'start': 0, 'end': .8, 'highlight': ['자막']}]}
                      for i, motion in enumerate(sorted(reels.ANIMATIONS))]
    plan['scenes'].append({**plan['scenes'][-1], 'media_type': 'video', 'asset_path': str(clip),
                           'credit': 'generated FFmpeg test fixture'})
    plan['music_path'] = plan['audio_path']
    config = Settings(root=tmp_path, encoder='libx264')
    first = reels.render_reel(plan, tmp_path / 'first', config)
    info = reels.probe(first)
    stream = next(item for item in info['streams'] if item['codec_type'] == 'video')
    assert (stream['width'], stream['height'], stream['nb_frames']) == (720, 1280, '192')
    assert float(info['format']['duration']) == pytest.approx(6.4, abs=.02)
    assert json.loads((first.parent / 'render-cache.json').read_text())['reused'] == 0
    second = reels.render_reel(plan, tmp_path / 'second', config)
    assert second.is_file()
    assert json.loads((second.parent / 'render-cache.json').read_text())['reused'] == 8
    with pytest.raises(ValueError, match='덮어쓰지'):
        reels.render_reel(plan, second.parent, config)
