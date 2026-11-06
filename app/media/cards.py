"""Editorial news cards using native typography and non-statistical diagrams."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.process import cache_path, check_cancel, digest, restore_cached, store_cached
from app.script.models import Script

PAPER = "#f3f5f1"
INK = "#171d25"
COBALT = "#254eff"
MUTED = "#68727d"
LINE = "#d7dedc"
LIME = "#eaff72"


def load_font(path: Path, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    index = 6 if bold else 2
    if path.name != "AppleSDGothicNeo.ttc":
        index = 0
    return ImageFont.truetype(str(path), max(1, size), index=index)


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    lines, current = [], ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if font.getlength(current + char) > width and current:
            # Prefer a word boundary without letting a long proper name overflow.
            split = current.rfind(" ")
            if split > len(current) // 3:
                lines.append(current[:split].rstrip())
                current = current[split + 1:] + char
            else:
                lines.append(current.rstrip())
                current = char.lstrip()
        else:
            current += char
    if current:
        lines.append(current.rstrip())
    return "\n".join(lines)


def draw_steps(draw, box, steps, font, scale):
    """A sequence of planned steps, with equal nodes and no invented magnitude."""
    left, top, right, bottom = box
    centers = [left + (right - left) * (i + .5) / len(steps) for i in range(len(steps))]
    line_y = top + 48 * scale
    radius = int(22 * scale)
    draw.line((centers[0], line_y, centers[-1], line_y), fill=LINE, width=max(1, int(2 * scale)))
    for index, (x, label) in enumerate(zip(centers, steps, strict=True)):
        draw.ellipse((x - radius, line_y - radius, x + radius, line_y + radius),
                     fill="#ffffff", outline=COBALT, width=max(1, int(2 * scale)))
        number = str(index + 1)
        number_font = font(21, True)
        draw.text((x - number_font.getlength(number) / 2, line_y - 12 * scale),
                  number, font=number_font, fill=COBALT)
        label_font = font(25, True)
        label_lines = wrap(label, label_font, int((right - left) / len(steps) - 14 * scale))
        for offset, line in enumerate(label_lines.splitlines()):
            draw.text((x - label_font.getlength(line) / 2, line_y + (49 + offset * 33) * scale),
                      line, font=label_font, fill=INK)
    note_font = font(16)
    note = "발표된 추진 계획 · 실제 성과를 나타내는 그래프가 아닙니다"
    draw.text((left, bottom - 25 * scale), note, font=note_font, fill=MUTED)


def draw_paragraph(draw, text, left, top, width, height, font, scale, *, bold=False):
    for size in range(38, 25, -1):
        face = font(size, bold)
        lines = wrap(text, face, int(width)).splitlines()
        step = int((size + 11) * scale)
        if len(lines) * step <= height:
            draw.multiline_text((left, top), "\n".join(lines), font=face, fill=INK,
                                spacing=int(11 * scale))
            return True
    return False


def create_cards(script: Script, folder: Path, width: int, font_path: Path) -> None:
    if not font_path.is_file():
        raise RuntimeError(f"한국어 글꼴 파일을 찾을 수 없습니다: {font_path}")
    height = width * 16 // 9
    scale = width / 720
    font = lambda size, bold=False: load_font(font_path, int(size * scale), bold)
    context = folder / "news-context.json"
    news = json.loads(context.read_text()) if context.is_file() else {}
    evidence = news.get("evidence", [])
    category = {"tech": "IT · 기술", "economy": "경제", "realestate": "부동산",
                "health": "건강", "auto": "자동차", "trending": "오늘의 이슈"}.get(news.get("category"), "이슈 해설")
    phrase_captions = (folder / "utterances.json").is_file()
    for index, scene in enumerate(script.scenes):
        check_cancel()
        selected = [item for item in evidence if item["id"] in scene.claim_ids]
        publishers = list(dict.fromkeys(item["publisher"] for item in selected))
        dates = list(dict.fromkeys(item["published_date"] for item in selected))
        target = folder / f"card-{index:02}.png"
        visible = scene.model_dump(exclude={"duration_seconds", "claim_ids", "tts_text", "caption_overrides"})
        if phrase_captions:
            visible.pop("narration", None)
        related = " ".join(item.get("fact", "") for item in selected) + " " + scene.narration
        diagram_features = [(word, word in related) for word in ("해운", "항만", "수산", "해양", "실증", "확산", "넓힐", "넓히", "검증", "안전성", "신뢰성")]
        key = digest(["card-v5-editorial", visible, script.title, selected, category, diagram_features,
                      phrase_captions, index, len(script.scenes), width,
                      str(font_path), font_path.stat().st_mtime_ns])
        cached = cache_path(folder.parent.parent / "data", "cards", key, ".png")
        if restore_cached(cached, target):
            continue
        image = Image.new("RGB", (width, height), PAPER)
        draw = ImageDraw.Draw(image)

        def rect(coords, _draw=draw, **kwargs):
            _draw.rectangle(tuple(int(value * scale) for value in coords), **kwargs)

        def text(x, y, value, size=20, fill=INK, bold=False, _draw=draw):
            _draw.text((int(x * scale), int(y * scale)), value, font=font(size, bold), fill=fill)

        # A quiet masthead gives the issue and scene their own typographic hierarchy.
        rect((0, 0, 720, 140), fill=INK)
        text(44, 35, "HEADLINE", 32, "#ffffff", True)
        draw.rounded_rectangle(tuple(int(v * scale) for v in (234, 31, 298, 78)),
                               radius=int(10 * scale), fill=LIME)
        text(246, 36, "60", 31, INK, True)
        text(528, 44, "@headline.60", 18, "#b7c0c9")
        text(44, 94, "세상의 이슈, 60초면 충분해.", 18, "#bec7ce")
        text(44, 169, category, 18, COBALT, True)
        issue_font = font(20)
        issue_lines = wrap(script.title, issue_font, int(452 * scale)).splitlines()
        issue = issue_lines[0] if len(issue_lines) == 1 else issue_lines[0].rstrip() + "…"
        draw.text((int(210 * scale), int(169 * scale)), issue, font=issue_font, fill=MUTED)
        text(44, 215, "해설" if scene.kind == "analysis" else "발표 자료" if selected else "개념 설명", 19, INK, True)
        text(600, 217, f"{index + 1:02} / {len(script.scenes):02}", 17, MUTED)
        gap = 7 * scale
        segment_width = (632 * scale - gap * (len(script.scenes) - 1)) / len(script.scenes)
        for tick in range(len(script.scenes)):
            x = 44 * scale + tick * (segment_width + gap)
            draw.rounded_rectangle((x, 253 * scale, x + segment_width, 257 * scale),
                                   radius=max(1, int(2 * scale)),
                                   fill=COBALT if tick == index else INK if tick < index else LINE)

        title_font = font(78, True)
        for size in range(78, 57, -1):
            title_font = font(size, True)
            title_lines = wrap(scene.headline, title_font, int(632 * scale)).splitlines()
            if len(title_lines) <= 3:
                break
        step = int((title_font.size / scale + 12) * scale)
        title_top = int(292 * scale)
        for line_index, line in enumerate(title_lines):
            draw.text((int(44 * scale), title_top + line_index * step), line,
                      font=title_font, fill=COBALT if line_index == len(title_lines) - 1 else INK)
        title_bottom = title_top + len(title_lines) * step
        panel_top = max(int(473 * scale), title_bottom + int(34 * scale))
        panel_bottom = int(802 * scale)
        draw.rounded_rectangle((44 * scale, panel_top, 676 * scale, panel_bottom),
                               radius=int(21 * scale), fill="#ffffff", outline=LINE, width=max(1, int(scale)))
        inner_left, inner_right = int(73 * scale), int(647 * scale)
        conditions = selected[0].get("conditions", "") if selected else ""
        fact = selected[0].get("fact", "") if selected else scene.explanation
        panel_label = "핵심 내용" if scene.kind == "fact" else "이렇게 읽어야 합니다"
        draw.text((inner_left, panel_top + 20 * scale), panel_label, font=font(17, True), fill=COBALT)

        if scene.visual_type == "concept":
            steps = ["기술 실증", "검증", "확산 계획"] if "실증" in related and any(word in related for word in ("확산", "넓힐", "넓히")) else ["발표 자료", "조건 확인", "후속 검토"]
            draw_steps(draw, (inner_left, panel_top + 45 * scale, inner_right, panel_bottom - 9 * scale), steps, font, scale)
        elif "계획" in scene.headline and "성과" in scene.headline:
            y = panel_top + 66 * scale
            for x, label in ((inner_left, "계획"), (inner_left + 338 * scale, "성과")):
                face = font(45, True)
                draw.text((x + 18 * scale, y), label, font=face, fill=INK if label == "계획" else COBALT)
            mid_x, mid_y = 359 * scale, y + 35 * scale
            for offset in (-8, 8):
                draw.line((mid_x - 28 * scale, mid_y + offset * scale,
                           mid_x + 28 * scale, mid_y + offset * scale), fill=MUTED, width=max(2, int(3 * scale)))
            draw.line((mid_x + 19 * scale, mid_y - 27 * scale, mid_x - 19 * scale, mid_y + 27 * scale),
                      fill=COBALT, width=max(2, int(3 * scale)))
            draw_paragraph(draw, conditions or scene.explanation, inner_left, y + 91 * scale,
                           inner_right - inner_left, panel_bottom - y - 110 * scale, font, scale)
        elif "확인" in scene.headline and "검증" in related and "확산" in related:
            y = panel_top + 74 * scale
            for item_index, label in enumerate(("기술 검증", "현장 확산")):
                row_y = y + item_index * 77 * scale
                draw.rounded_rectangle((inner_left, row_y + 3 * scale, inner_left + 29 * scale, row_y + 32 * scale),
                                       radius=int(6 * scale), outline=COBALT, width=max(1, int(2 * scale)))
                draw.text((inner_left + 52 * scale, row_y), label, font=font(33, True), fill=INK)
        elif "검증" in scene.headline and "안전성" in related and "신뢰성" in related:
            draw_steps(draw, (inner_left, panel_top + 45 * scale, inner_right, panel_bottom - 9 * scale),
                       ["핵심 기술 확보", "안전성·신뢰성 검증"], font, scale)
        elif ("AI" in scene.headline or "인공지능" in scene.headline) and all(word in related for word in ("해운", "항만", "수산")):
            # Actual named application fields, connected equally rather than as a numerical chart.
            draw.text((inner_left, panel_top + 50 * scale), "인공지능 적용 분야", font=font(25, True), fill=INK)
            labels = [word for word in ("해운", "항만", "수산", "해양") if word in related]
            center_y = panel_top + 185 * scale
            gap_width = (inner_right - inner_left) / len(labels)
            draw.line((inner_left + gap_width / 2, center_y, inner_right - gap_width / 2, center_y),
                      fill=LINE, width=max(1, int(2 * scale)))
            for position, label in enumerate(labels):
                x = inner_left + gap_width * (position + .5)
                radius = int(43 * scale)
                draw.ellipse((x - radius, center_y - radius, x + radius, center_y + radius),
                             fill="#f4f6ff", outline="#bdcaff", width=max(1, int(scale)))
                face = font(25, True)
                draw.text((x - face.getlength(label) / 2, center_y - 16 * scale), label, font=face, fill=COBALT)
        else:
            body_top = panel_top + 69 * scale
            displayed = draw_paragraph(draw, fact or scene.explanation or scene.headline,
                                       inner_left, body_top, inner_right - inner_left,
                                       panel_bottom - body_top - 23 * scale, font, scale, bold=True)
            if not displayed:
                draw_paragraph(draw, scene.headline, inner_left, body_top,
                               inner_right - inner_left, panel_bottom - body_top - 23 * scale, font, scale, bold=True)

        # The complete references remain in sources.json; publication date is visible here.
        if publishers:
            source = " · ".join(publishers)
            source_font = font(18)
            while source_font.getlength(source) > 568 * scale and source_font.size > 14 * scale:
                source_font = load_font(font_path, source_font.size - 1)
            draw.text((44 * scale, 832 * scale), "출처", font=font(16, True), fill=COBALT)
            draw.text((99 * scale, 831 * scale), source, font=source_font, fill=MUTED)
            text(44, 863, "발표일  " + " · ".join(date.replace("-", ".") for date in dates), 17, MUTED)
        else:
            text(44, 835, "일반 개념 설명 · 실제 통계를 나타내지 않습니다", 17, MUTED)
        if not phrase_captions:
            face = font(31, True)
            caption = wrap(scene.narration, face, int(580 * scale))
            while len(caption.splitlines()) > 4 and face.size > 24 * scale:
                face = load_font(font_path, face.size - 1, True)
                caption = wrap(scene.narration, face, int(580 * scale))
            draw.rounded_rectangle((44 * scale, 920 * scale, 666 * scale, 1134 * scale),
                                   radius=int(16 * scale), fill=INK)
            draw.multiline_text((63 * scale, 946 * scale), caption, font=face, fill="#ffffff", spacing=int(10 * scale))
        text(44, 1144, "AI 음성 · 자료 기반 요약" if selected else "AI 음성 · 개념 설명", 16, MUTED)
        image.save(target)
        store_cached(target, cached)
