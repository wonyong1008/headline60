"""검색 결과에서 눈에 띄는 16:9 화면(=썸네일). 배경은 밤에 틀어도 눈부시지 않게 어둡게 유지한다."""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
NAVY = (10, 14, 32)


def face(style: str, size: int) -> ImageFont.FreeTypeFont:
    for i in range(24):
        try:
            f = ImageFont.truetype(FONT, size, index=i)
        except OSError:
            break
        if f.getname()[1].lower() == style.lower():
            return f
    return ImageFont.truetype(FONT, size, index=0)


def fit(draw: ImageDraw.ImageDraw, text: str, style: str, size: int, max_w: int, stroke: int = 0):
    while size > 20:
        f = face(style, size)
        box = draw.textbbox((0, 0), text, font=f, stroke_width=stroke)
        if box[2] - box[0] <= max_w:
            return f, box
        size -= 4
    return f, box


def main(out_png: Path, out_jpg: Path) -> None:
    top, bottom = (12, 17, 40), (30, 38, 74)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)))

    stars = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    rnd = random.Random(60)
    for _ in range(110):
        x, y = rnd.randrange(W), rnd.randrange(H)
        r = rnd.choice((1, 1, 1, 2))
        sd.ellipse([x - r, y - r, x + r, y + r], fill=(235, 238, 255, rnd.randrange(50, 160)))
    img.paste(stars, (0, 0), stars)

    cx, cy, r = 1120, 150, 70
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    md.ellipse([cx - r + 32, cy - r - 22, cx + r + 24, cy + r - 28], fill=0)
    cream = Image.new("RGB", (W, H), (246, 236, 208))
    img.paste(cream, (0, 0), mask.filter(ImageFilter.GaussianBlur(34)).point(lambda v: int(v * 0.5)))
    img.paste(cream, (0, 0), mask)

    d = ImageDraw.Draw(img)
    x = 96
    badge_font = face("Bold", 50)
    bb = d.textbbox((0, 0), "1시간", font=badge_font)
    bw, bh = bb[2] - bb[0] + 52, bb[3] - bb[1] + 30
    d.rounded_rectangle([x, 92, x + bw, 92 + bh], radius=bh // 2, fill=(255, 205, 60))
    d.text((x + 26 - bb[0], 92 + 15 - bb[1]), "1시간", font=badge_font, fill=NAVY)
    d.text((x + bw + 22, 92 + (bh - 44) // 2 - 4), "연속 재생", font=face("Medium", 40), fill=(210, 218, 240))

    hook, hb = fit(d, "아기 울음 뚝!", "Heavy", 150, 900, stroke=8)
    d.text((x - hb[0], 214 - hb[1]), "아기 울음 뚝!", font=hook, fill=(255, 222, 89), stroke_width=8, stroke_fill=NAVY)
    y2 = 214 + (hb[3] - hb[1]) + 34
    main_font, mb = fit(d, "부스럭 소리", "Bold", 132, 900, stroke=6)
    d.text((x - mb[0], y2 - mb[1]), "부스럭 소리", font=main_font, fill=(255, 255, 255), stroke_width=6, stroke_fill=NAVY)
    y3 = y2 + (mb[3] - mb[1]) + 46
    d.text((x, y3), "신생아 백색소음 · 비닐 바스락 · 아기 재우는 소리", font=face("Medium", 40), fill=(176, 189, 222))
    img.save(out_png)
    img.save(out_jpg, quality=92)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
