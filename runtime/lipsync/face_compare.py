"""Side-by-side face sheet for an outfit draft: a visual check aid, not biometric verification.

Runs in this runtime because it reuses the installed MediaPipe face detector.
"""

import argparse
import json
from pathlib import Path

import mediapipe as mp
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


def face_box(image: Image.Image) -> tuple[tuple[float, float, float, float], float, float]:
    rgb = np.asarray(image.convert("RGB"))
    with mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.5) as detector:
        found = detector.process(rgb).detections
    if not found:
        raise SystemExit("얼굴을 찾지 못했습니다.")
    best = max(found, key=lambda item: item.score[0])
    box = best.location_data.relative_bounding_box
    width, height = image.size
    face_width = box.width * width
    side = max(face_width, box.height * height) * 1.7
    center_x = (box.xmin + box.width / 2) * width
    center_y = (box.ymin + box.height / 2) * height - side * 0.06
    return ((center_x - side / 2, center_y - side / 2, center_x + side / 2, center_y + side / 2),
            float(best.score[0]), face_width)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"기존 파일은 덮어쓰지 않습니다: {args.output}")
    reference = Image.open(args.reference).convert("RGB")
    draft = Image.open(args.draft).convert("RGB")
    reference_box, reference_score, reference_width = face_box(reference)
    draft_box, draft_score, draft_width = face_box(draft)

    title, label = ImageFont.truetype(FONT, 30), ImageFont.truetype(FONT, 22)
    canvas = Image.new("RGB", (1210, 900), "#15171c")
    pen = ImageDraw.Draw(canvas)
    pen.text((24, 18), "의상 시안 1 · 원본 얼굴 비교 (육안 확인용, 생체 인증 아님)", font=title, fill="#f2f2f2")
    canvas.paste(draft.resize((432, 768), Image.LANCZOS), (24, 72))
    pen.text((24, 852), f"시안 전체 {draft.width}×{draft.height}", font=label, fill="#c9ccd3")
    for index, (image, box, caption) in enumerate((
            (reference, reference_box, f"원본 얼굴 (얼굴 폭 {reference_width:.0f}px)"),
            (draft, draft_box, f"시안 얼굴 확대 (얼굴 폭 {draft_width:.0f}px)"))):
        left = 480 + index * 362
        canvas.paste(image.crop(box).resize((350, 350), Image.LANCZOS), (left, 72))
        pen.text((left, 430), caption, font=label, fill="#c9ccd3")
    canvas.paste(reference.resize((350, 350), Image.LANCZOS), (480, 476))
    pen.text((480, 834), "원본 전체 (Syn-Vis CC0 001-0030)", font=label, fill="#c9ccd3")
    canvas.save(args.output)
    print(json.dumps({"output": str(args.output), "reference_face_width_px": round(reference_width),
                      "draft_face_width_px": round(draft_width),
                      "detector_scores": [round(reference_score, 3), round(draft_score, 3)]}))


if __name__ == "__main__":
    main()
