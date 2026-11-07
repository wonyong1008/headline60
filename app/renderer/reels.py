"""Local news reels with credited media, three templates and reusable silent cuts."""
import math
import os
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from app.config import Settings
from app.media.cards import load_font, wrap
from app.process import (
    cache_path,
    check_cancel,
    digest,
    file_digest,
    probe,
    restore_cached,
    run,
    save_json,
    store_cached,
)
from app.renderer.ffmpeg import caption_frame_ranges, encoder_args

WIDTH, HEIGHT, FPS = 720, 1280, 30
LEFT, RIGHT = 48, 638
NAVY, CORAL, PAPER, MUTED = '#101B2D', '#F25B52', '#F5F6F8', '#8B98AA'
TEMPLATES = {'headline_news', 'story_explainer', 'data_comparison'}
ANIMATIONS = {'fade', 'slide_up', 'slide_down', 'scale', 'pop', 'words', 'number'}
VERSION = 'news-reel-v3.5.1-presenter-cutout-captions'
CONTENT_LABELS = {'adult_satire': '허구 · 성인 공감 풍자', 'myth_check': '속설 검증 · 근거 확인'}


def layout_lines(lines, font_path):
    if not isinstance(lines, list) or not 1 <= len(lines) <= 3:
        raise ValueError('문장은 1~3줄로 지정하세요.')
    if any(not isinstance(line, str) or not line.strip() or len(line) > 90 for line in lines):
        raise ValueError('빈 문장 또는 지나치게 긴 문장은 사용할 수 없습니다.')
    for size in range(80, 61, -2):
        font = load_font(font_path, size, True)
        fitted = [(part, index) for index, line in enumerate(lines)
                  for part in wrap(line.strip(), font, RIGHT - LEFT).splitlines()]
        if len(fitted) <= 3:
            return fitted, size
    raise ValueError('큰 글씨 안전영역을 넘습니다. 문장을 세 줄 이내로 줄여 주세요.')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def hook_layout(scene, config):
    text = scene.get('hook_headline_text') or scene.get('hook_text') or ' '.join(scene['lines'])
    if not isinstance(text, str) or not text.strip():
        raise ValueError('첫 장면 후킹 문장이 필요합니다.')
    for size in range(72, 55, -2):
        lines = wrap(text.strip(), load_font(config.font, size, True), 590).splitlines()
        if len(lines) <= 2:
            return [(line, index) for index, line in enumerate(lines)], size
    raise ValueError('후킹 제목이 56px 두 줄 안전영역을 넘습니다. 의미를 보존하여 제목을 직접 줄여 주세요.')


def caption_layout(text, scene, plan, config):
    v3 = plan.get('edit_style', 'classic') == 'reels_v3'
    style = plan.get('subtitle_style', 'clean')
    visual = scene.get('visual_first') and scene.get('media_type') != 'graphic' and not scene.get('chart')
    base = (44 if style == 'dynamic' else 40 if style == 'boxed' else 38) if v3 else (42 if visual else 34)
    limit = 542 if v3 and style == 'dynamic' else 558 if v3 else 560
    sizes = range(base, 37, -2) if base >= 38 else [base]
    for size in sizes:
        lines = wrap(text, load_font(config.font, size, True), limit).splitlines()
        if len(lines) <= 2:
            return '\n'.join(lines), size, limit
    raise ValueError('내레이션 자막은 실제 출력 글꼴의 두 줄 안전영역을 넘습니다. 구절을 더 짧게 나눠 주세요.')


def validate_chart(chart):
    if not isinstance(chart, dict):
        raise ValueError('차트 형식을 확인하세요.')  # noqa: TRY004 - user plan validation uses ValueError.
    labels, values = chart.get('labels', []), chart.get('values', [])
    if (not isinstance(labels, list) or not isinstance(values, list)
            or not 1 <= len(labels) <= 6 or len(labels) != len(values)
            or any(not isinstance(label, str) or not label.strip() or len(label) > 24 for label in labels)
            or any(not finite(value) for value in values)):
        raise ValueError('차트 라벨과 유한한 실제 수치는 같은 수로 최대 6개까지 지정하세요.')
    if (not chart.get('source_label') or not chart.get('as_of')
            or not isinstance(chart.get('source_refs'), list) or not chart['source_refs']
            or any(not isinstance(ref, str) or not ref.strip() for ref in chart['source_refs'])):
        raise ValueError('차트 출처·기준일·근거 연결이 필요합니다.')


def captions_for(scene, config, plan=None):
    plan = plan or {}
    cues, estimated = scene.get('captions') or [], False
    if not cues and scene.get('narration', '').strip():
        v3 = plan.get('edit_style', 'classic') == 'reels_v3'
        base = 44 if v3 and plan.get('subtitle_style') == 'dynamic' else 38 if v3 else 42 if scene.get('visual_first') else 34
        limit = 542 if v3 and plan.get('subtitle_style') == 'dynamic' else 558 if v3 else 560
        lines = wrap(scene['narration'].strip(), load_font(config.font, base, True), limit).splitlines()
        chunks = ['\n'.join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
        cues = [{'text': text, 'start': i * scene['duration'] / len(chunks),
                 'end': (i + 1) * scene['duration'] / len(chunks), 'highlight': []}
                for i, text in enumerate(chunks)]
        estimated = True
    if not isinstance(cues, list):
        raise ValueError('자막은 목록이어야 합니다.')  # noqa: TRY004 - public validation contract.
    previous_end = 0
    for cue in cues:
        if (not isinstance(cue, dict) or not isinstance(cue.get('text'), str)
                or not cue['text'].strip() or len(cue['text']) > 180
                or not finite(cue.get('start')) or not finite(cue.get('end'))
                or not 0 <= cue['start'] < cue['end'] <= scene['duration'] + .0001
                or cue['start'] < previous_end - .0001
                or not isinstance(cue.get('highlight', []), list)
                or any(not isinstance(word, str) for word in cue.get('highlight', []))):
            raise ValueError('자막 시간·문장·강조 또는 장면 범위를 확인하세요.')
        previous_end = cue['end']
        caption_layout(cue['text'], scene, plan, config)
    return cues, estimated


def validate_plan(plan: dict, config: Settings) -> list[dict]:
    if plan.get('width', 720) not in (720, 1080) or plan.get('fps', 30) != 30:
        raise ValueError('720/1080 세로 영상과 30fps를 사용하세요.')
    if plan.get('template', 'headline_news') not in TEMPLATES:
        raise ValueError('지원하지 않는 영상 템플릿입니다.')
    if plan.get('edit_style', 'classic') not in {'classic', 'reels_v3'}:
        raise ValueError('편집 스타일은 classic 또는 reels_v3입니다.')
    if plan.get('subtitle_style', 'clean') not in {'clean', 'boxed', 'dynamic'}:
        raise ValueError('자막 스타일은 clean, boxed 또는 dynamic입니다.')
    if not Path(plan.get('audio_path', '')).is_file():
        raise ValueError('준비된 음성 파일이 필요합니다.')
    if plan.get('music_path') and not Path(plan['music_path']).is_file():
        raise ValueError('선택한 배경음악 파일이 없습니다.')
    scenes = plan.get('scenes', [])
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 24:
        raise ValueError('장면을 1~24개 지정하세요.')
    normalized = []
    for scene in scenes:
        check_cancel()
        if not isinstance(scene, dict):
            raise ValueError('장면 형식을 확인하세요.')  # noqa: TRY004 - public validation contract.
        kind = scene.get('media_type', 'image' if scene.get('asset_path') else 'graphic')
        asset = Path(scene['asset_path']).resolve() if scene.get('asset_path') else None
        if kind not in {'image', 'video', 'graphic'}:
            raise ValueError('미디어 형식은 image/video/graphic입니다.')
        if asset is not None and not asset.is_file():
            raise ValueError(f'실제 자료사진/영상이 없습니다: {asset}')
        if kind != 'graphic' and asset is None:
            raise ValueError('사진·영상 장면에는 실제 자료 파일이 필요합니다.')
        if kind == 'image':
            try:
                with Image.open(asset) as image:
                    image.verify()
            except (OSError, ValueError) as exc:
                raise ValueError(f'사진 파일을 읽을 수 없습니다: {asset}') from exc
        if kind == 'video' and not any(s.get('codec_type') == 'video' for s in probe(asset)['streams']):
            raise ValueError('영상 스트림이 없는 자료입니다.')
        duration = scene.get('duration', 3)
        # Scene length follows the spoken narration; only absurd values are technical errors.
        if not finite(duration) or not .8 <= duration <= 40:
            raise ValueError('장면 길이는 0.8~40초여야 합니다.')
        if kind != 'graphic' and (not isinstance(scene.get('credit'), str) or not scene['credit'].strip()):
            raise ValueError('자료사진·영상 저작자·라이선스 크레딧이 필요합니다.')
        fitted, size = layout_lines(scene.get('lines'), config.font)
        if plan.get('edit_style') == 'reels_v3' and scene.get('hook_scene'):
            fitted, size = hook_layout(scene, config)
        accent = scene.get('accent_line', -1)
        if isinstance(accent, bool) or not isinstance(accent, int) or not -1 <= accent < len(scene['lines']):
            raise ValueError('강조 줄 번호가 문장 범위를 벗어났습니다.')
        if scene.get('animation_type', 'slide_up') not in ANIMATIONS:
            raise ValueError('지원하지 않는 문자 모션입니다.')
        if scene.get('transition_type', 'cut') not in {'cut', 'fade', 'wipe'}:
            raise ValueError('지원하지 않는 장면 전환입니다.')
        if scene.get('chart') is not None:
            validate_chart(scene['chart'])
            if duration < 2:
                raise ValueError('차트 장면은 펼침 이후 읽을 수 있도록 최소 2초를 확보하세요.')
        cues, estimated = captions_for({**scene, 'duration': duration, 'media_type': kind}, config, plan)
        normalized.append({**scene, 'media_type': kind, 'asset_path': str(asset) if asset else None,
                           'duration': duration, 'frames': round(duration * 30), 'captions': cues,
                           'estimated_captions': estimated, 'fitted_lines': fitted, 'font_size': size})
    if not 2 <= sum(scene['frames'] for scene in normalized) / 30 <= 120:
        raise ValueError('영상 전체 길이는 2~120초여야 합니다.')
    return normalized


def text_layer(text, font, color, path, background=None, highlights=(), only_highlights=False):
    lines = text.splitlines()
    step = round(font.size * 1.4)
    image = Image.new('RGBA', (math.ceil(max(font.getlength(line) for line in lines)) + 32,
                               step * len(lines) + 22))
    draw = ImageDraw.Draw(image)
    if background:
        draw.rounded_rectangle((0, 0, image.width - 1, image.height - 1), radius=12, fill=background)
    for row, line in enumerate(lines):
        x = 16
        for token in re.split(r'(\s+)', line):
            marked = any(word and word in token for word in highlights)
            ink = CORAL if marked else color
            if not only_highlights or marked:
                draw.text((x, 4 + row * step), token, font=font, fill=ink,
                          stroke_width=1, stroke_fill=(0, 0, 0, 130))
            x += font.getlength(token)
    image.save(path)
    return image.size


def chart_panel(chart, width, config, translucent=False):
    scale = width / 720
    image = Image.new('RGBA', (int(590 * scale), int(350 * scale)),
                      (16, 27, 45, 178) if translucent else PAPER)
    ink = PAPER if translucent else NAVY
    draw = ImageDraw.Draw(image)
    font = load_font(config.font, int((14 if len(chart['values']) > 4 else 18) * scale), True)
    low, high = min(0, min(chart['values'])), max(0, max(chart['values']))
    span, left, right = high - low or 1, 145 * scale, 446 * scale
    zero = left + (0 - low) / span * (right - left)
    row_height = 240 * scale / len(chart['values'])
    draw.line((zero, 18 * scale, zero, 258 * scale), fill=MUTED, width=max(1, int(scale)))
    for index, (label, value) in enumerate(zip(chart['labels'], chart['values'], strict=True)):
        y = 25 * scale + index * row_height
        label_lines = wrap(label, font, int(127 * scale)).splitlines()
        if len(label_lines) > 2:
            raise ValueError('차트 라벨을 두 줄 이내로 줄여 주세요. 원문 라벨을 임의로 생략하지 않습니다.')
        draw.multiline_text((12 * scale, y), '\n'.join(label_lines), font=font, fill=ink, spacing=2)
        end = left + (value - low) / span * (right - left)
        if abs(end - zero) >= 1:
            draw.rounded_rectangle((min(zero, end), y + 4 * scale, max(zero, end), y + 24 * scale),
                                   radius=4 * scale, fill=CORAL if index == 0 else MUTED)
        number = f"{value}{chart.get('unit', '')}"
        number_font = load_font(config.font, int(18 * scale), True)
        if number_font.getlength(number) > 120 * scale:
            number_font = load_font(config.font, int(13 * scale), True)
        if number_font.getlength(number) > 120 * scale:
            raise ValueError('차트 수치 표기가 너무 깁니다. 단위를 명시해 자료를 다시 구성하세요.')
        draw.text((458 * scale, y), number, font=number_font, fill=ink)
    note_font = load_font(config.font, int(15 * scale))
    note = f"{chart['source_label']} · {chart['as_of']} 기준"
    draw.multiline_text((12 * scale, 291 * scale), wrap(note, note_font, int(562 * scale)),
                        font=note_font, fill=ink, spacing=4)
    return image


# Cut-out presenter: scaled and shifted right so the headline keeps the top-left of the photo.
PRESENTER_SCALE, PRESENTER_X, PRESENTER_Y = .8, 160, 256


def build_layers(scene, plan, folder, index, config):
    width = plan.get('width', 720)
    height, scale = width * 16 // 9, width / 720
    template = plan.get('template', 'headline_news')
    v3 = plan.get('edit_style', 'classic') == 'reels_v3'
    hook = v3 and bool(scene.get('hook_scene'))
    has_presenter = bool(scene.get('presenter_windows'))
    context_chart = v3 and scene['media_type'] != 'graphic' and bool(scene.get('chart'))
    visual_first = (bool(scene.get('visual_first')) or v3) and scene['media_type'] != 'graphic' and not scene.get('chart')
    fixed = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(fixed)
    chart_layers = []
    for y in range(height):
        alpha = int(12 + 180 * max(0, min(1, (y / scale - 470) / 700))) if visual_first else int(25 + 165 * max(0, min(1, (y / scale - 130) / 950)))
        draw.line((0, y, width, y), fill=(16, 27, 45, alpha))
    hero_y, hero_step, hero_size = (610, 82, min(56, scene['font_size'])) if visual_first else (300, 106, scene['font_size'])
    if template == 'story_explainer' and not visual_first and not hook and not context_chart:
        draw.rounded_rectangle((28 * scale, 462 * scale, 654 * scale, 876 * scale),
                               radius=24 * scale, fill=(16, 27, 45, 238))
        draw.rectangle((48 * scale, 497 * scale, 93 * scale, 502 * scale), fill=CORAL)
        draw.text((114 * scale, 482 * scale), f"{index + 1:02} / {len(plan['scenes']):02}",
                  font=load_font(config.font, int(23 * scale), True), fill=PAPER)
        hero_y, hero_step, hero_size = 536, 96, min(72, scene['font_size'])
    elif (template == 'data_comparison' or context_chart) and not visual_first and (not hook or context_chart):
        if not context_chart:
            draw.rectangle((0, 195 * scale, width, 890 * scale), fill=(245, 246, 248, 242))
        elif not has_presenter:
            # With a presenter the panel would sit on the person; text keeps its own plate.
            draw.rounded_rectangle((32 * scale, 224 * scale, 654 * scale, 442 * scale),
                                   radius=20 * scale, fill=(16, 27, 45, 150))
        draw.rectangle((48 * scale, 212 * scale, 102 * scale, 218 * scale), fill=CORAL)
        hero_y, hero_step, hero_size = ((182, 62, min(48, scene['font_size'])) if has_presenter
                                             else (238, 83, min(64, scene['font_size'])))
        if scene.get('chart'):
            panel = chart_panel(scene['chart'], width, config, translucent=context_chart)
            chart_path = folder / f'chart-{index:02}.png'
            panel.save(chart_path)
            chart_layers.append({'path': chart_path, 'x': 48 * scale, 'y': 505 * scale,
                                 'start': .3, 'end': scene['frames'] / 30,
                                 'animation': 'chart_reveal', 'size': panel.size, 'caption': False,
                                 'hide_with_presenter': True})
        else:
            for item, label in enumerate(scene['lines'][:2]):
                y = (545 + item * 125) * scale
                draw.rounded_rectangle((48 * scale, y, 638 * scale, y + 102 * scale),
                                       radius=15 * scale, fill=NAVY if item == 0 else '#dde2e9')
                font = load_font(config.font, int(30 * scale), True)
                draw.text((68 * scale, y + 28 * scale), wrap(label, font, int(540 * scale)),
                          font=font, fill=PAPER if item == 0 else NAVY)
    if has_presenter and (hook or context_chart):
        # Headline sits on the photo next to the cut-out presenter; a soft plate keeps it readable.
        draw.rounded_rectangle((32 * scale, 166 * scale, 654 * scale, 318 * scale),
                               radius=18 * scale, fill=(16, 27, 45, 150))
    if hook and has_presenter:
        hero_y, hero_step, hero_size = 182, 62, min(48, scene['font_size'])
    elif hook:
        hero_y, hero_step, hero_size = 228, 100, scene['font_size']
        plate = Image.new('RGBA', (int(622 * scale), int(274 * scale)))
        ImageDraw.Draw(plate).rounded_rectangle((0, 0, plate.width, plate.height),
                                               radius=20 * scale, fill=(16, 27, 45, 160))
        plate_path = folder / f'hook-plate-{index:02}.png'
        plate.save(plate_path)
        chart_layers.append({'path': plate_path, 'x': 32 * scale, 'y': 194 * scale,
                             'start': 0, 'end': min(2.0, scene['frames'] / 30),
                             'animation': 'fade', 'size': plate.size, 'caption': False})
    draw.text((48 * scale, 65 * scale), 'HEADLINE 60', font=load_font(config.font, int(27 * scale), True), fill=PAPER)
    content_label = CONTENT_LABELS.get(plan.get('content_mode'))
    if content_label:
        # Editorial disclosure is renderer-owned, never accepted from model text.
        # It lives in the always-on fixed layer, outside every headline animation.
        label_font = load_font(config.font, int(20 * scale), True)
        right = 48 * scale + label_font.getlength(content_label) + 24 * scale
        draw.rounded_rectangle((48 * scale, 111 * scale, right, 155 * scale),
                               radius=8 * scale, fill=(16, 27, 45, 242), outline=CORAL,
                               width=max(1, int(scale)))
        draw.text((60 * scale, 117 * scale), content_label, font=label_font, fill=PAPER)
    else:
        draw.text((48 * scale, 115 * scale), str(plan.get('category') or '뉴스 · 이슈 해설')[:36],
                  font=load_font(config.font, int(20 * scale)), fill='#d9dfe6')
    eyebrow_y = 252 if template == 'headline_news' else 425 if template == 'story_explainer' else 168
    eyebrow_y = 558 if visual_first else eyebrow_y
    if v3 and visual_first:
        eyebrow_y = 172
    draw.text((48 * scale, eyebrow_y * scale), str(scene.get('eyebrow', ''))[:40],
              font=load_font(config.font, int(23 * scale), True), fill=CORAL)
    label = {'image': '자료사진 · 크롭 적용' if v3 else '자료사진 · 크롭/줌 적용', 'video': '자료영상 · 원본 음성 제외', 'graphic': '설명 그래픽'}[scene['media_type']]
    if has_presenter:
        label += ' · AI 합성 진행자'  # disclosure stays on screen, out of the headline area
    font = load_font(config.font, int(16 * scale))
    source_note = str(scene.get('source_label') or '').strip()
    if scene.get('as_of'):
        source_note += f" · {scene['as_of']} 기준"
    credit_text = f"{label}\n{scene.get('credit', '')}"
    if source_note:
        credit_text += '\n' + source_note
    credit_lines = wrap(credit_text.strip(), font, int(590 * scale)).splitlines()
    if len(credit_lines) > 4:
        raise ValueError('화면 크레딧은 네 줄 이내로 줄이고 상세 출처는 메타데이터에 기록하세요.')
    draw.multiline_text((48 * scale, 1084 * scale), '\n'.join(credit_lines), font=font,
                        fill='#d6dce4', spacing=4 * scale, stroke_width=1, stroke_fill=NAVY)
    fixed_path = folder / f'fixed-{index:02}.png'
    fixed.save(fixed_path)
    default_motion = {'headline_news': 'pop', 'story_explainer': 'words', 'data_comparison': 'number'}
    layers, motion = chart_layers, scene.get('animation_type', default_motion[template])
    if hook:
        motion = 'pop'
    font = load_font(config.font, int(hero_size * scale), True)
    emphasis_words = scene.get('emphasis_words') or []
    if (not isinstance(emphasis_words, list)
            or any(not isinstance(word, str) or not word.strip() for word in emphasis_words)):
        raise ValueError('emphasis_words는 강조할 단어의 목록이어야 합니다.')
    hero_lines = [] if v3 and visual_first and not hook else scene['fitted_lines']
    for row, (line, original) in enumerate(hero_lines):
        # Keep the first readable state neutral, then crossfade only the
        # emphasized glyphs. Light comparison panels start with navy type.
        color = NAVY if template == 'data_comparison' and not hook and not context_chart else PAPER
        tokens = re.findall(r'\S+\s*', line) if motion == 'words' else [line]
        x = 32 * scale
        for token_index, token in enumerate(tokens):
            path = folder / f'hero-{index:02}-{row:02}-{token_index:02}.png'
            size = text_layer(token, font, color, path)
            layer = {'path': path, 'x': x, 'y': (hero_y + row * hero_step) * scale,
                     'start': .05 + row * .085 + token_index * .075,
                     'end': min(2.0, scene['frames'] / 30) if hook else scene['frames'] / 30,
                     'animation': motion, 'size': size, 'caption': False}
            layers.append(layer)
            marked = any(word in token for word in emphasis_words)
            if marked or (not emphasis_words and original == scene.get('accent_line', -1)):
                accent_path = folder / f'color-{index:02}-{row:02}-{token_index:02}.png'
                text_layer(token, font, CORAL, accent_path,
                           highlights=emphasis_words, only_highlights=bool(emphasis_words))
                layers.append({**layer, 'path': accent_path, 'color_start': layer['start'] + .18})
            x += font.getlength(token)
    ranges = caption_frame_ranges(scene['captions'], 0, scene['frames'])
    for cue_index, (cue, (start, end)) in enumerate(zip(scene['captions'], ranges, strict=True)):
        path = folder / f'caption-{index:02}-{cue_index:03}.png'
        text, font_size, limit = caption_layout(cue['text'], scene, plan, config)
        font = load_font(config.font, int(font_size * scale), True)
        text = wrap(cue['text'], font, int(limit * scale))
        if len(text.splitlines()) > 2:
            raise ValueError('실제 출력 해상도에서 자막이 두 줄을 넘습니다. 구절을 더 짧게 나눠 주세요.')
        style = plan.get('subtitle_style', 'clean')
        dynamic = v3 and style == 'dynamic'
        highlights = cue.get('highlight', []) + (re.findall(r'\d[\d,.]*(?:%|억|만|원|건|명|초)?', cue['text']) if dynamic else [])
        plate = (16, 27, 45, 225) if dynamic else NAVY if style == 'boxed' else None
        size = text_layer(text, font, PAPER, path, background=plate, highlights=highlights)
        caption_x = 56 if dynamic else 48 if v3 else 32
        caption_y = 866 if dynamic else 892 if v3 else 922
        layers.append({'path': path, 'x': caption_x * scale, 'y': caption_y * scale, 'start': start / 30,
                       'end': end / 30, 'animation': 'fade', 'size': size, 'caption': True,
                       'dynamic_caption': dynamic,
                       'start_frame': start, 'end_frame': end})
    return fixed_path, layers


def motion_filter(layer, input_index, label, scale):
    start, end, animation = layer['start'], layer['end'], layer['animation']
    progress = f'min(1,max(0,(t-{start:.6f})/0.20))'
    width, height = layer['size']
    x, y = f"{layer['x']:.3f}", f"{layer['y']:.3f}"
    chain = f'[{input_index}:v]format=rgba'
    if layer['caption']:
        enable = f"gte(n,{layer['start_frame']})*lt(n,{layer['end_frame']})"
        if layer.get('dynamic_caption'):
            factor = (f'0.94+0.08*min(1,max(0,(t-{start:.6f})/0.08))'
                      f'-0.02*min(1,max(0,(t-{start + .08:.6f})/0.08))')
            chain += f",scale=w='max(2,trunc(iw*({factor})/2)*2)':h=-2:eval=frame"
            x += f'+({width}-overlay_w)/2'
            y += f'+({height}-overlay_h)/2'
    else:
        enable = f'gte(t,{start:.6f})*lt(t,{end:.6f})'
        if animation == 'chart_reveal':
            # Scale-X reveals the immutable, sourced panel; no synthetic numeric
            # interpolation. At 0.7s the exact original width is held to the end.
            reveal = f'min(1,max(0.001,(t-{start:.6f})/0.40))'
            chain += f",scale=w='max(1,ceil(iw*({reveal})))':h=ih:eval=frame"
        else:
            fade_start = layer.get('color_start', start)
            chain += (f',fade=t=in:st={fade_start:.6f}:d=0.18:alpha=1'
                      f',fade=t=out:st={max(start, end - .12):.6f}:d=0.12:alpha=1')
        if animation in {'slide_up', 'slide_down', 'words'}:
            direction = -1 if animation == 'slide_down' else 1
            y += f'+{direction * 28 * scale:.3f}*(1-{progress})'
        elif animation in {'scale', 'pop'}:
            factor = (f'0.84+0.22*min(1,max(0,(t-{start:.6f})/0.12))'
                      f'-0.06*min(1,max(0,(t-{start + .12:.6f})/0.10))' if animation == 'pop'
                      else f'0.88+0.12*{progress}')
            exit_progress = f'min(1,max(0,(t-{end - .12:.6f})/0.12))'
            factor = f'({factor})*(1-0.12*{exit_progress})'
            chain += f",scale=w='max(2,trunc(iw*({factor})/2)*2)':h=-2:eval=frame"
            x += f'+({width}-overlay_w)/2'
            y += f'+({height}-overlay_h)/2'
        elif animation == 'number':
            # Reveal the sourced digits, never intermediate fabricated amounts.
            x += f'-18*(1-{progress})'
    return chain + f'[{label}]', x, y, enable


def background(scene, folder, index, width):
    height = width * 16 // 9
    path = folder / f'background-{index:02}.png'
    if scene['media_type'] == 'video':
        return Path(scene['asset_path'])
    if scene['media_type'] == 'image':
        with Image.open(scene['asset_path']) as source:
            ImageOps.fit(ImageOps.exif_transpose(source).convert('RGB'), (width, height),
                         method=Image.Resampling.LANCZOS, centering=(.5, .45)).save(path)
    else:
        image = Image.new('RGB', (width, height), NAVY)
        draw = ImageDraw.Draw(image)
        draw.polygon([(width * .60, 0), (width, 0), (width, height * .80), (width * .12, height)], fill='#1d304b')
        draw.ellipse((width * .2, height * .5, width * 1.4, height * 1.15), outline='#304762', width=max(1, width // 140))
        image.save(path)
    return path


def segment(scene, plan, folder, index, config, previous_tail=None):
    width, count = plan.get('width', 720), scene['frames']
    height, seconds = width * 16 // 9, count / 30
    media = background(scene, folder, index, width)
    fixed, layers = build_layers(scene, plan, folder, index, config)
    inputs = ['-stream_loop', '-1', '-i', str(media)] if scene['media_type'] == 'video' else ['-i', str(media)]
    for path in [fixed] + [layer['path'] for layer in layers]:
        inputs.extend(['-loop', '1', '-framerate', '30', '-i', str(path)])
    presenter_windows = scene.get('presenter_windows', [])
    presenter_inputs = []
    next_index = len(layers) + 2
    for window in presenter_windows:
        inputs.extend(['-i', window['path']])
        entry = {'video': next_index, 'matte': None}
        next_index += 1
        if window.get('matte'):
            inputs.extend(['-i', window['matte']])
            entry['matte'] = next_index
            next_index += 1
        presenter_inputs.append(entry)
    crossfade = (plan.get('edit_style', 'classic') == 'reels_v3' and index > 0
                 and scene.get('transition_type', 'cut') == 'fade' and previous_tail is not None)
    if crossfade:
        inputs.extend(['-loop', '1', '-framerate', '30', '-i', str(previous_tail)])
    if scene['media_type'] == 'video':
        base = (f'[0:v]fps=30,scale={width}:{height}:force_original_aspect_ratio=increase,'
                f'crop={width}:{height},setsar=1,setpts=PTS-STARTPTS,trim=duration={seconds:.8f}')
    elif plan.get('edit_style', 'classic') == 'reels_v3':
        # Still backgrounds stay pixel-stable. Output-sized moving crops step
        # across integer pixels, making buildings and graphic lines jitter.
        base = f'[0:v]zoompan=z=1:x=0:y=0:d={count}:s={width}x{height}:fps=30,setsar=1'
    else:
        progress = f'on/{max(1, count - 1)}'
        zoom = f'1.02+0.028*{progress}' if index % 2 == 0 else f'1.048-0.028*{progress}'
        pan = f'0.3+0.4*{progress}' if index % 2 == 0 else f'0.7-0.4*{progress}'
        base = (f"[0:v]zoompan=z='{zoom}':x='(iw-iw/zoom)*({pan})':y='ih/2-ih/zoom/2':"
                f'd={count}:s={width}x{height}:fps=30,setsar=1')
    if scene.get('blur_background') is True:
        base += ',gblur=sigma=1.2:steps=1'
    transition = scene.get('transition_type', 'cut')
    tail = folder / f'media-tail-{index:02}.png'
    filters = [base + '[pure]', '[pure]split=2[media][tail_source]',
               f"[tail_source]select='eq(n,{count - 1})',setpts=PTS-STARTPTS[tail]"]
    if presenter_windows:
        previous_media = 'media'
        for window_index, (window, entry) in enumerate(zip(presenter_windows, presenter_inputs, strict=True)):
            start, end = window['start'], window['end']
            clip = f"trim=start={window['source_start']:.8f}:duration={end-start:.8f},fps=30,setpts=PTS-STARTPTS+{start:.8f}/TB"
            if entry['matte'] is not None:
                # Person cut out of the studio still and placed on the scene photo, slightly smaller.
                size = f'{round(width * PRESENTER_SCALE / 2) * 2}:{round(height * PRESENTER_SCALE / 2) * 2}'
                filters.append(f"[{entry['video']}:v]{clip}[pv{window_index}]")
                filters.append(f"[{entry['matte']}:v]{clip},format=gray[pm{window_index}]")
                filters.append(f'[pv{window_index}][pm{window_index}]alphamerge,scale={size}[presenter{window_index}]')
                position = f'{round(PRESENTER_X * width / 720)}:{round(PRESENTER_Y * width / 720)}'
            else:
                filters.append(f"[{entry['video']}:v]{clip},scale={width}:{height},setsar=1[presenter{window_index}]")
                position = '0:0'
            out = f'presented{window_index}'
            filters.append(f"[{previous_media}][presenter{window_index}]overlay={position}:eof_action=pass:"
                           f"enable='gte(t,{start:.8f})*lt(t,{end:.8f})'[{out}]")
            previous_media = out
        filters.append(f'[{previous_media}]null[presented_media]')
    else:
        filters.append('[media]null[presented_media]')
    if crossfade:
        previous_input = next_index
        filters.extend([f'[{previous_input}:v]format=rgba,fade=t=out:st=0:d=0.16:alpha=1[previous_photo]',
                        '[presented_media][previous_photo]overlay=0:0[bg]'])
    elif transition == 'fade':
        filters.append('[presented_media]fade=t=in:st=0:d=0.18[bg]')
    elif transition == 'wipe':
        filters.extend([f'color=c=0x101B2D:s={width}x{height}:r=30:d={seconds:.8f}[canvas]',
                        "[canvas][presented_media]overlay=x='-W+W*min(1,t/0.22)':y=0[bg]"])
    else:
        filters.append('[presented_media]null[bg]')
    filters.append('[bg][1:v]overlay=0:0[fixed]')
    previous = 'fixed'
    for index_layer, layer in enumerate(layers):
        label = f'motion{index_layer}'
        chain, x, y, enable = motion_filter(layer, index_layer + 2, label, width / 720)
        if layer.get('hide_with_presenter'):
            # Charts never cover the presenter; they appear only outside presenter windows.
            for window in presenter_windows:
                enable = f"({enable})*not(between(t,{window['start']:.8f},{window['end']:.8f}))"
        filters.append(chain)
        out = f'layer{index_layer}'
        filters.append(f"[{previous}][{label}]overlay=x='{x}':y='{y}':enable='{enable}'[{out}]")
        previous = out
    filters.append(f'[{previous}]format=yuv420p[out]')
    target = folder / f'reel-{index:02}.mp4'
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1',
         *inputs, '-filter_complex_threads', '1', '-filter_complex', ';'.join(filters),
         '-map', '[out]', '-an', '-frames:v', str(count), *encoder_args(config), '-threads', '1',
         str(target), '-map', '[tail]', '-frames:v', '1', '-c:v', 'png', '-threads', '1',
         '-update', '1', str(tail)], timeout=max(180, int(seconds * 25)))
    return target


def verify(path, width, seconds, decode=True):
    info = probe(path)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), {})
    if ((video.get('width'), video.get('height'), video.get('avg_frame_rate')) != (width, width * 16 // 9, '30/1')
            or video.get('codec_name') != 'h264'
            or abs(float(info['format']['duration']) - seconds) > .07):
        raise RuntimeError('릴스 출력 규격 검증에 실패했습니다.')
    if decode:
        run(['ffmpeg', '-v', 'error', '-xerror', '-nostdin', '-threads', '1', '-i', str(path),
             '-f', 'null', '-'], timeout=max(180, int(seconds * 4)))
    return info


def render_reel(plan: dict, folder: Path, config: Settings) -> Path:
    started = time.perf_counter()
    scenes = validate_plan(plan, config)
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / 'output.mp4'
    if output.exists():
        raise ValueError('기존 영상은 덮어쓰지 않습니다. 새 버전 폴더를 사용하세요.')
    width = plan.get('width', 720)
    seconds = sum(scene['frames'] for scene in scenes) / 30
    audio = Path(plan['audio_path']).resolve()
    audio_info = probe(audio)
    if not any(s.get('codec_type') == 'audio' for s in audio_info['streams']):
        raise ValueError('내레이션 오디오 스트림이 없습니다.')
    audio_seconds = float(audio_info['format']['duration'])
    if audio_seconds > seconds + .10:
        raise ValueError('내레이션이 영상보다 깁니다. 음성이 잘리지 않도록 장면 길이를 늘려 주세요.')
    save_json(folder / 'reel-plan.json', {**plan, 'renderer': VERSION})
    font_hash, assets, reused, timings = file_digest(config.font), {}, 0, []
    report = {'reused': 0, 'total': len(scenes), 'segments': []}
    cursor = 0
    for index, scene in enumerate(scenes):
        check_cancel()
        tick, asset = time.perf_counter(), scene.get('asset_path')
        if asset and asset not in assets:
            assets[asset] = file_digest(Path(asset))
        presenter_hashes = [[file_digest(Path(window['path'])), file_digest(Path(window['matte'])) if window.get('matte') else None]
                            for window in scene.get('presenter_windows', [])]
        visual = {key: value for key, value in scene.items() if key != 'asset_path'}
        # Story scene counters depend on index AND the total scene count.
        previous_tail = folder / f'media-tail-{index - 1:02}.png' if index else None
        blend_dependency = (file_digest(previous_tail) if plan.get('edit_style') == 'reels_v3'
                            and index and scene.get('transition_type') == 'fade' else None)
        key = digest([VERSION, plan.get('edit_style', 'classic'), plan.get('template', 'headline_news'), plan.get('category'),
                      plan.get('subtitle_style', 'clean'), plan.get('content_mode', 'news'), width, config.encoder, font_hash,
                      index, len(scenes), assets.get(asset), visual, blend_dependency, presenter_hashes])
        cached = cache_path(config.data, 'reel-segments', key, '.mp4')
        tail_key = digest(['pure-media-tail-v2', plan.get('edit_style', 'classic'), assets.get(asset), scene['media_type'], width,
                           scene['frames'], index % 2, scene.get('blur_background') is True])
        cached_tail = cache_path(config.data, 'reel-media-tails', tail_key, '.png')
        target = folder / f'reel-{index:02}.mp4'
        tail = folder / f'media-tail-{index:02}.png'
        hit = restore_cached(cached, target) and restore_cached(cached_tail, tail)
        if hit:
            try:
                verify(target, width, scene['frames'] / 30, decode=False)
            except (RuntimeError, KeyError, ValueError):
                check_cancel()
                hit = False
        if not hit:
            target = segment(scene, plan, folder, index, config, previous_tail)
            verify(target, width, scene['frames'] / 30)
            store_cached(target, cached)
            store_cached(tail, cached_tail)
        reused += int(hit)
        elapsed = time.perf_counter() - tick
        report['reused'] = reused
        report['segments'].append({'index': index, 'key': key, 'reused': hit, 'seconds': elapsed})
        save_json(folder / 'render-cache.json', report)
        timings.append({'scene': index, 'start': cursor / 30, 'end': (cursor + scene['frames']) / 30,
                        'frames': scene['frames'], 'render_seconds': elapsed, 'cached': hit,
                        'captions_alignment': 'estimated evenly from narration' if scene['estimated_captions'] else 'explicit scene-relative timestamps',
                        'captions': [{**cue, 'start': cursor / 30 + cue['start'],
                                      'end': cursor / 30 + cue['end']} for cue in scene['captions']]})
        cursor += scene['frames']
        save_json(folder / 'reel-timing.json', {'renderer': VERSION, 'scenes': timings})
    listing = folder / 'reel-segments.txt'
    listing.write_text(''.join(f"file 'reel-{i:02}.mp4'\n" for i in range(len(scenes))), encoding='utf-8')
    partial = folder / 'output.partial.mp4'
    inputs = ['-f', 'concat', '-safe', '1', '-i', str(listing), '-i', str(audio)]
    if plan.get('music_path'):
        inputs.extend(['-stream_loop', '-1', '-i', str(Path(plan['music_path']).resolve())])
        af = (f'[1:a]apad,atrim=duration={seconds:.8f},asplit=2[voice][side];'
              f'[2:a]volume=0.10,atrim=duration={seconds:.8f}[music];'
              '[music][side]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=250[ducked];'
              '[voice][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.89:level=false:latency=true[aout]')
    else:
        af = f'[1:a]apad,atrim=duration={seconds:.8f}[aout]'
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1',
         *inputs, '-filter_complex_threads', '1', '-filter_complex', af, '-map', '0:v:0', '-map', '[aout]',
         '-c:v', 'copy', '-c:a', 'aac', '-threads', '1', '-b:a', '160k', '-ar', '48000', '-ac', '1',
         '-t', f'{seconds:.8f}', '-movflags', '+faststart', str(partial)], timeout=180)
    info = verify(partial, width, seconds)
    if not any(s.get('codec_name') == 'aac' for s in info['streams'] if s.get('codec_type') == 'audio'):
        raise RuntimeError('AAC 내레이션 검증에 실패했습니다.')
    save_json(folder / 'reel-verification.json', info)
    save_json(folder / 'reel-timing.json', {'renderer': VERSION, 'scenes': timings,
        'total_frames': cursor, 'duration': seconds, 'audio_seconds': audio_seconds,
        'render_seconds': time.perf_counter() - started, 'reused_segments': reused})
    check_cancel()
    os.link(partial, output)
    partial.unlink()
    return output
