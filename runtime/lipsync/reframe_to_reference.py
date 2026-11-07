"""Isolated runtime CLI: re-frame a generated outfit image so its eyes land exactly where the
approved presenter's eyes are (same position and inter-eye distance). The whole image gets one
similarity transform, so a full-body draft becomes the approved head-to-knees framing and the
fixed lip-sync crop box fits again."""
import argparse
import json
from pathlib import Path


def eye_centres(image_bgr):
    import cv2
    import mediapipe as mp
    import numpy as np
    with mp.solutions.face_mesh.FaceMesh(static_image_mode=True, max_num_faces=1,
                                       refine_landmarks=True, min_detection_confidence=.4) as mesh:
        faces = mesh.process(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)).multi_face_landmarks
    if not faces:
        raise RuntimeError("얼굴을 찾지 못했습니다.")
    h, w = image_bgr.shape[:2]
    pts = np.array([(p.x * w, p.y * h) for p in faces[0].landmark])
    def centre(connections):
        ids = sorted({i for edge in connections for i in edge})
        return pts[ids].mean(axis=0)
    return centre(mp.solutions.face_mesh.FACEMESH_LEFT_EYE), centre(mp.solutions.face_mesh.FACEMESH_RIGHT_EYE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    import cv2
    import numpy as np
    reference, draft = cv2.imread(str(args.reference)), cv2.imread(str(args.draft))
    if reference is None or draft is None:
        raise SystemExit("이미지를 읽지 못했습니다.")
    ref_l, ref_r = eye_centres(reference)
    drf_l, drf_r = eye_centres(draft)
    ref_mid, drf_mid = (ref_l + ref_r) / 2, (drf_l + drf_r) / 2
    scale = float(np.linalg.norm(ref_r - ref_l) / max(1e-6, np.linalg.norm(drf_r - drf_l)))
    scale = min(max(scale, .6), 2.5)  # sanity bounds; never explode a tiny detection
    transform = np.array([[scale, 0, ref_mid[0] - scale * drf_mid[0]],
                          [0, scale, ref_mid[1] - scale * drf_mid[1]]], dtype=np.float64)
    h, w = reference.shape[:2]
    out = cv2.warpAffine(draft, transform, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    cv2.imwrite(str(args.output), out)
    args.output.with_suffix(".reframe.json").write_text(json.dumps({
        "scale": round(scale, 4), "draft_eye_mid": [round(float(v), 1) for v in drf_mid],
        "reference_eye_mid": [round(float(v), 1) for v in ref_mid], "method": "eye-aligned similarity transform, replicate borders"}))


if __name__ == "__main__":
    main()
