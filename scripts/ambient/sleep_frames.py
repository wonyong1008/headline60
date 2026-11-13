"""10시간 수면 영상용 화면 2종: 영상 화면(거의 검은 화면, 방을 밝히지 않음)과 검색용 썸네일(비 오는 밤 도시)."""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
NAVY = (8, 12, 28)


def face(style: str, size: int) -> ImageFont.FreeTypeFont:
    for i in range(24):
        try:
            f = ImageFont.truetype(FONT, size, index=i)
        except OSError:
            break
        if f.getname()[1].lower() == style.lower():
            return f
    return ImageFont.truetype(FONT, size, index=0)


def gradient(top, bottom):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        f = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3)))
    return img


def moon(img, cx, cy, r, alpha):
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    md.ellipse([cx - r + int(r * 0.45), cy - r - int(r * 0.3), cx + r + int(r * 0.35), cy + r - int(r * 0.4)], fill=0)
    cream = Image.new("RGB", (W, H), (246, 236, 208))
    img.paste(cream, (0, 0), mask.filter(ImageFilter.GaussianBlur(r * 0.5)).point(lambda v: int(v * alpha * 0.5)))
    img.paste(cream, (0, 0), mask.point(lambda v: int(v * alpha)))


def video_frame(out: Path) -> None:
    img = gradient((3, 4, 7), (7, 9, 14))
    moon(img, 1150, 110, 34, 0.22)
    d = ImageDraw.Draw(img)
    d.text((80, 618), "빗소리 · 계곡 물소리 · 빗길 차 소리", font=face("Medium", 30), fill=(46, 52, 66))
    d.text((80, 660), "10시간 · 화면은 어둡게 유지돼요", font=face("Regular", 22), fill=(36, 41, 53))
    img.save(out)


def thumbnail(out_png: Path, out_jpg: Path) -> None:
    img = gradient((10, 20, 48), (22, 40, 80))
    moon(img, 1110, 120, 64, 1.0)
    rnd = random.Random(10)
    sky = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sky)
    x = 0
    while x < W:  # 비 오는 밤 도시 지붕선 + 켜진 창문
        w, h = rnd.randrange(50, 130), rnd.randrange(70, 190)
        sd.rectangle([x, H - h, x + w, H], fill=(6, 10, 24, 255))
        for _ in range(rnd.randrange(1, 5)):
            wx, wy = x + rnd.randrange(8, max(9, w - 14)), H - h + rnd.randrange(12, max(13, h - 16))
            sd.rectangle([wx, wy, wx + 7, wy + 9], fill=(255, 205, 110, rnd.randrange(120, 220)))
        x += w + rnd.randrange(0, 8)
    for _ in range(320):  # 빗줄기
        rx, ry, ln = rnd.randrange(-100, W), rnd.randrange(-60, H), rnd.randrange(18, 55)
        sd.line([(rx, ry), (rx + ln * 0.27, ry + ln)], fill=(200, 220, 255, rnd.randrange(35, 95)), width=2)
    img.paste(sky, (0, 0), sky)

    d = ImageDraw.Draw(img)
    x = 90
    for i, (label, fill, color) in enumerate((("10시간", (255, 205, 60), NAVY), ("검은화면", (235, 240, 255), NAVY))):
        f = face("Bold", 44)
        bb = d.textbbox((0, 0), label, font=f)
        bw, bh = bb[2] - bb[0] + 44, bb[3] - bb[1] + 26
        d.rounded_rectangle([x, 70, x + bw, 70 + bh], radius=bh // 2, fill=fill)
        d.text((x + 22 - bb[0], 70 + 13 - bb[1]), label, font=f, fill=color)
        x += bw + 16
    big = face("Heavy", 150)
    d.text((86, 168), "신생아 꿀잠", font=big, fill=(255, 222, 89), stroke_width=9, stroke_fill=NAVY)
    d.text((92, 360), "빗소리 · 물소리 · 차 소리", font=face("Bold", 76), fill=(255, 255, 255), stroke_width=6, stroke_fill=NAVY)
    d.text((96, 478), "아기 재우는 백색소음", font=face("Medium", 46), fill=(196, 212, 244), stroke_width=4, stroke_fill=NAVY)
    img.save(out_png)
    img.save(out_jpg, quality=92)


if __name__ == "__main__":
    o = Path(sys.argv[1])
    video_frame(o / "frame.png")
    thumbnail(o / "thumbnail.png", o / "thumbnail.jpg")
