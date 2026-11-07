"""Isolated runtime CLI: feather synthesized mouth pixels into the approved still,
cut the presenter out of the studio image, and add gentle body sway.

Outputs next to the mouth clip:
  presenter.mp4        full-frame colour clip (studio background kept for clean edges)
  presenter-matte.mp4  matching grey matte; the renderer alpha-merges the two so the
                       presenter stands on the scene's real photo instead of the AI studio.
"""
import argparse
import json
import math
import subprocess
from pathlib import Path


def sway(t: float) -> tuple[float, float, float, float]:
    """Breathing-like scale, slow drift and a hint of rotation; sub-pixel so nothing jitters."""
    scale = 1 + .010 * math.sin(2 * math.pi * t / 4.7)
    dx = 5 * math.sin(2 * math.pi * t / 6.1)
    dy = 2.5 * math.sin(2 * math.pi * t / 3.7 + 1)
    angle = .35 * math.sin(2 * math.pi * t / 8.3)
    return scale, dx, dy, angle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--encoder", choices=("libx264", "h264_videotoolbox"), required=True)
    args = parser.parse_args()
    # Available only in the existing lip-sync runtime, never imported by the app.
    import cv2
    import mediapipe as mp
    import numpy as np

    folder = args.folder.resolve()
    source = json.loads((folder / "source.json").read_text())
    base = cv2.imread(source["approved_image"])
    crop = cv2.imread(str(folder / "portrait-crop.png"))
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=1,
                                       refine_landmarks=True, min_detection_confidence=.5) as detector:
        faces = detector.process(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).multi_face_landmarks
    if not faces:
        raise RuntimeError("진행자 입술 위치를 찾지 못했습니다.")
    points = np.array([(p.x * 512, p.y * 512) for p in faces[0].landmark])
    indices = sorted({i for edge in mp.solutions.face_mesh.FACEMESH_LIPS for i in edge})
    lips = points[indices]
    center, width, height = lips.mean(axis=0), np.ptp(lips[:, 0]), np.ptp(lips[:, 1])
    mask = np.zeros((512, 512), dtype=np.uint8)
    axes = (round(width * .86), round(max(height * 2, width * .52)))
    cv2.ellipse(mask, tuple(np.round(center).astype(int)), axes, 0, 0, 360, 255, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=7)
    left, top, right, bottom = source["crop_box"]
    size = (right - left, bottom - top)
    alpha = (cv2.resize(mask, size, interpolation=cv2.INTER_LINEAR) / 255)[..., None]
    base_roi = base[top:bottom, left:right].astype(np.float32)

    # Person matte from the still (the body never moves, only the mouth is synthesized).
    with mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=1) as segmenter:
        probability = segmenter.process(cv2.cvtColor(base, cv2.COLOR_BGR2RGB)).segmentation_mask
    matte = np.clip((probability - .35) / .3, 0, 1).astype(np.float32)
    matte = cv2.erode(matte, np.ones((3, 3), np.uint8))
    matte = cv2.GaussianBlur(matte, (0, 0), sigmaX=1.2)
    if matte.mean() < .08:
        raise RuntimeError("진행자 인물 영역을 분리하지 못했습니다.")
    matte8 = np.clip(matte * 255, 0, 255).astype(np.uint8)
    height_px, width_px = base.shape[:2]
    pivot = (width_px / 2, height_px * .42)

    capture = cv2.VideoCapture(str(folder / "mouth.mp4"))
    output, matte_output = folder / "presenter.mp4", folder / "presenter-matte.mp4"
    if output.exists() or matte_output.exists():
        raise RuntimeError("기존 진행자 합성 결과는 덮어쓰지 않습니다.")
    encoder_args = (["-allow_sw", "0", "-b:v", "5M"] if args.encoder == "h264_videotoolbox"
                    else ["-preset", "veryfast", "-crf", "20"])

    def encoder_for(path: Path, pix_fmt: str, log):
        return subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", pix_fmt,
            "-s", f"{width_px}x{height_px}", "-r", "25", "-i", "pipe:0", "-an", "-vf", "fps=30",
            "-c:v", args.encoder, *encoder_args, "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
            stdin=subprocess.PIPE, stderr=log)

    frames = 0
    with (folder / "compose-ffmpeg.log").open("w") as log:
        colour, grey = encoder_for(output, "bgr24", log), encoder_for(matte_output, "gray", log)
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                patch = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                final = base.copy()
                final[top:bottom, left:right] = np.clip(patch * alpha + base_roi * (1 - alpha), 0, 255).astype(np.uint8)
                scale, dx, dy, angle = sway(frames / 25)
                transform = cv2.getRotationMatrix2D(pivot, angle, scale)
                transform[:, 2] += (dx, dy)
                moved = cv2.warpAffine(final, transform, (width_px, height_px), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)
                moved_matte = cv2.warpAffine(matte8, transform, (width_px, height_px), flags=cv2.INTER_LINEAR,
                                             borderMode=cv2.BORDER_CONSTANT, borderValue=0)
                colour.stdin.write(moved.tobytes())
                grey.stdin.write(moved_matte.tobytes())
                frames += 1
            colour.stdin.close()
            grey.stdin.close()
            if colour.wait(timeout=60) or grey.wait(timeout=60):
                raise RuntimeError("진행자 합성 인코딩에 실패했습니다.")
        finally:
            capture.release()
            for process in (colour, grey):
                if process.poll() is None:
                    process.kill()
                    process.wait()
    if frames != round(args.seconds * 25):
        raise RuntimeError(f"진행자 프레임 수가 다릅니다: {frames}")
    (folder / "matte.json").write_text(json.dumps({"coverage": round(float(matte.mean()), 4), "sway": "scale±1%, drift≤5px, rotation≤0.35°"}))


if __name__ == "__main__":
    main()
