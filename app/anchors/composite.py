"""Isolated runtime CLI: feather synthesized mouth pixels into the approved still."""
import argparse
import json
import subprocess
from pathlib import Path


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
    capture = cv2.VideoCapture(str(folder / "mouth.mp4"))
    output = folder / "presenter.mp4"
    if output.exists():
        raise RuntimeError("기존 진행자 합성 결과는 덮어쓰지 않습니다.")
    encoder_args = (["-allow_sw", "0", "-b:v", "5M"] if args.encoder == "h264_videotoolbox"
                    else ["-preset", "veryfast", "-crf", "20"])
    frames = 0
    with (folder / "compose-ffmpeg.log").open("w") as log:
        encoder = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{base.shape[1]}x{base.shape[0]}", "-r", "25", "-i", "pipe:0", "-an", "-vf", "fps=30",
            "-c:v", args.encoder, *encoder_args, "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
            stdin=subprocess.PIPE, stderr=log)
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                patch = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
                final = base.copy()
                final[top:bottom, left:right] = np.clip(patch * alpha + base_roi * (1 - alpha), 0, 255).astype(np.uint8)
                encoder.stdin.write(final.tobytes())
                frames += 1
            encoder.stdin.close()
            if encoder.wait(timeout=60):
                raise RuntimeError("진행자 합성 인코딩에 실패했습니다.")
        finally:
            capture.release()
            if encoder.poll() is None:
                encoder.kill()
                encoder.wait()
    if frames != round(args.seconds * 25):
        raise RuntimeError(f"진행자 프레임 수가 다릅니다: {frames}")


if __name__ == "__main__":
    main()
