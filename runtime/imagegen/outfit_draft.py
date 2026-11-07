"""One outfit draft from the fixed reference face with local FLUX.2 [klein] 4B (mflux).

Experiment only: never a production anchor/outfit profile. Each run uses a fresh folder
and never overwrites earlier drafts.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "data/models/flux2-klein-4b"
REFERENCE = ROOT / "data/anchors/benchmark-input/reference.png"
LOOKS = {
    "shirt-dress-studio": (
        "Use the reference image ONLY for camera framing and pose: a head-to-knees photograph of a "
        "fictional ADULT Korean female news presenter in her mid-twenties, standing upright facing "
        "the camera, arms relaxed, full head visible with space above, both knees visible with space "
        "below. A NEW natural face: soft oval face, clear gentle eyes, light natural makeup, calm "
        "confident expression with a slight smile, long dark-brown hair neatly styled. Outfit: an "
        "elegant, modest tailored shirt dress in soft ivory or light beige, collared with a buttoned "
        "front, three-quarter or long sleeves, a slim fabric belt at the natural waist, A-line skirt "
        "ending just below the knee. Polished broadcast-journalist look, no exposed midriff, no "
        "shorts. Minimal small earrings. Modern news studio background with soft blue screens and "
        "flattering natural studio light. Photorealistic editorial portrait, natural anatomy, sharp "
        "focus on the face. No text, no logos."
    ),
    "brown-anchor-identity-lowrise": (
        "Extend the REFERENCE PORTRAIT into a head-to-knees fashion photograph of the EXACT SAME "
        "fictional ADULT female news presenter. Her approved face is the highest priority: preserve "
        "the exact facial identity, eyes, eyelids, eyebrows, nose, lip shape, cheeks, jawline, smile, "
        "skin tone and light makeup. Keep her warm chestnut-brown long hair and hairstyle exactly. "
        "Do not redesign, age or replace her face. She is an adult in her early twenties. "
        "Camera pulled back to include the entire head, both hands, hips, legs and BOTH KNEES with "
        "space below the knees. Natural upright presenter pose, arms relaxed at her sides. "
        "Outfit: fitted opaque ivory V-neck sleeveless crop top ending at the lower ribs, shoulders "
        "and arms bare, full midriff and bellybutton visible. Black denim LOW-RISE short shorts with "
        "belt loops, slanted pockets and a visible metal button fly. Narrow waistband resting LOW "
        "on the hip bones, a broad area of lower abdomen exposed between the bellybutton and the "
        "waistband; the metal button is about seven centimetres BELOW the bellybutton. Short front "
        "rise, unmistakable 2000s low-waist jeans styling, not high-waisted shorts. Opaque denim "
        "fully covers the pelvis and buttocks, tidy upper-thigh hems. Sophisticated modern news "
        "studio, soft blue screens and flattering natural studio light. Photorealistic adult "
        "fashion lookbook, natural anatomy. Preserve the reference face, hair and makeup above "
        "all. No text, no logos."
    ),
    "approved23-lowrise-denim": (
        "Use the reference image ONLY for the identity of this fictional 23-year-old ADULT woman. "
        "Preserve her exact youthful adult face, eyes, brows, nose, mouth, soft cheek shape and long "
        "dark wavy hair. Make a photorealistic vertical fashion lookbook picture, showing her complete "
        "head down to just below BOTH KNEES. Camera pulled back, natural adult body proportions, "
        "standing upright facing forward, hands relaxed at her sides. Outfit: a short fitted ivory "
        "V-neck halter crop top, bare shoulders and arms, hem ending at the lower ribs. Her full "
        "midriff and bellybutton are uncovered. Black LOW-RISE DENIM SHORTS with a visible metal "
        "button fly, belt loops, diagonal pockets and a very narrow waistband. The waistband sits "
        "on her hip bones, approximately seven centimetres BELOW the bellybutton, leaving a broad "
        "visible area of lower abdomen between the bellybutton and waistband. This is a 2000s-style "
        "low-waist denim cut with a short front rise. Her bellybutton must sit clearly ABOVE the "
        "waistband and metal button, not at the waistband. The opaque shorts fully cover the pelvis "
        "and buttocks, with upper-thigh hems. Modern blue television news studio background, soft "
        "flattering light. Same approved face as the reference, no mature formal makeup, no facial "
        "aging. Adult outerwear fashion, no lingerie or transparent cloth. Both knees in frame."
    ),
    "adult25-extra-lowrise": (
        "Edit image 1, using image 2 as the facial reference. The subject is a fictional ADULT woman "
        "aged 25 with clear adult facial and body proportions. Give her a fresh mid-twenties adult "
        "appearance: a softly refined oval face, gentle cheek contours, light natural makeup and a "
        "relaxed subtle smile. Match the facial reference in image 2, especially its eyes, brows, "
        "nose, lips and face outline. Keep image 1's long dark wavy hair, ivory V-neck halter crop "
        "top, body proportions, upright pose, hands, blue news studio, lighting and head-to-knees "
        "composition unchanged. Adjust the black tailored shorts only: make them genuinely VERY "
        "LOW-RISE, with a short front rise and a narrow waistband sitting on the hip bones, "
        "approximately 8 centimetres BELOW the navel. The whole navel and a broad area of lower "
        "abdomen below it must be clearly visible above the waistband. The waistband must not touch "
        "or cover the navel. Remove the high-waist panel from the original shorts. Keep the shorts "
        "opaque, structured and fully covering the pelvis and buttocks, with neat upper-thigh hems. "
        "Contemporary adult fashion lookbook. Natural anatomy, no lingerie or transparent fabric, "
        "no nudity. Preserve the studio, top, pose and framing; change only the adult facial "
        "appearance and the much lower waistline of the shorts."
    ),
    "halter-lowrise-shorts": (
        "Fashion editorial photograph of the SAME fictional adult woman, age around 30, from the "
        "reference photograph. Preserve her approved face, facial proportions, eyes, nose, lips, "
        "expression, long dark wavy hairstyle and skin tone. Keep the same blue TV news studio and "
        "soft lighting. Change only her outfit and pull the camera back slightly so her complete head, "
        "torso, hips, thighs and both knees are visible. She stands upright facing the camera with arms "
        "relaxed at her sides, natural body proportions. New outfit: an opaque ivory halter-neck crop "
        "top with a deep V neckline, bare shoulders and bare arms. The top is short, ending just below "
        "the ribcage, leaving the full midriff and navel clearly visible. Pair it with opaque black "
        "low-rise tailored short shorts, waistband resting on the hips well below the navel, visibly "
        "lower than in the reference trousers. The short shorts fully cover the pelvis and buttocks; "
        "they are structured outerwear with a neat finished hem. Contemporary glamorous fashion "
        "lookbook styling, not lingerie, no transparent fabric, no nudity. Keep her original face and "
        "pose unchanged. Include both knees and the full silhouette of the shorts in the frame."
    ),
    "crop-lowrise-studio": (
        "Photorealistic vertical photo of the same woman as in the reference image. Keep her identity "
        "exactly: same face shape, eyes, eyebrows, nose, lips, skin tone and long dark wavy hair. "
        "Framing: from the top of her head down to just below her knees, she stands upright facing the "
        "camera with a calm slight smile, arms relaxed at her sides. Outfit: an ivory off-the-shoulder "
        "cropped knit top that shows her shoulders and collarbones and ends above the waist, so her bare "
        "midriff and navel are visible; low-rise black wide-leg trousers with the waistband sitting low on "
        "the hips, clearly below the navel. Setting: a modern TV news studio with soft blue screens and "
        "warm key light. Natural body proportions, sharp focus on the face, fashion editorial style."
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--look", choices=sorted(LOOKS), default="crop-lowrise-studio")
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument("--face-reference", type=Path)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--quantize", type=int, choices=[4, 8], default=8)
    parser.add_argument("--prompt", help="Custom prompt (overrides --look); used by the wardrobe generator.")
    parser.add_argument("--output-dir", type=Path, help="Write draft.png here instead of a new outfit-drafts folder.")
    args = parser.parse_args()
    prompt = args.prompt or LOOKS[args.look]
    reference = args.reference.expanduser().resolve()
    if not reference.is_file():
        raise SystemExit("Reference image file is missing.")
    references = [reference]
    if args.face_reference:
        face_reference = args.face_reference.expanduser().resolve()
        if not face_reference.is_file():
            raise SystemExit("Face reference image file is missing.")
        references.append(face_reference)
    if not (MODEL / "headline60-model.json").is_file():
        raise SystemExit("모델이 없습니다. runtime/imagegen/download_model.py를 먼저 실행하세요.")
    record = json.loads((MODEL / "headline60-model.json").read_text(encoding="utf-8"))
    folder = args.output_dir.expanduser().resolve() if args.output_dir else (
        ROOT / "data/anchors/outfit-drafts" / f"draft-{datetime.now():%Y%m%dT%H%M%S}")
    folder.mkdir(parents=True, exist_ok=False)  # never reuse or overwrite a draft folder
    output = folder / "draft.png"
    request = {
        "kind": "outfit_draft_experiment_not_production",
        "look": args.look if not args.prompt else "custom", "prompt": prompt, "seed": args.seed,
        "width": args.width, "height": args.height, "steps": 4, "guidance": 1.0,
        "quantize": args.quantize, "low_ram": True,
        "reference_image": str(reference), "reference_sha256": sha256(reference),
        "reference_images": [{"path": str(path), "sha256": sha256(path)} for path in references],
        "model": {k: record[k] for k in ("repository", "revision", "license")},
        "runtime": {"mflux": importlib.metadata.version("mflux"),
                    "mlx": importlib.metadata.version("mlx")},
    }
    write_json(folder / "request.json", request)
    command = [str(Path(sys.executable).with_name("mflux-generate-flux2-edit")),
               "--model", str(MODEL), "--base-model", "flux2-klein-4b",
               "--quantize", str(args.quantize), "--low-ram",
               "--image-paths", *map(str, references), "--prompt", prompt,
               "--seed", str(args.seed), "--width", str(args.width), "--height", str(args.height),
               "--steps", "4", "--output", str(output)]
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    started = time.perf_counter()
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    seconds = time.perf_counter() - started
    (folder / "mflux.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    metrics = {"status": "failed", "seconds": round(seconds, 3),
               # macOS reports ru_maxrss in bytes; largest finished child (the mflux process).
               "peak_rss_bytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
               "returncode": result.returncode}
    if result.returncode == 0 and output.is_file():
        with Image.open(output) as image:
            image.load()
            metrics.update({"width": image.width, "height": image.height,
                            "output_bytes": output.stat().st_size})
        if (metrics["width"], metrics["height"]) == (args.width, args.height):
            metrics["status"] = "completed"
    write_json(folder / "metrics.json", metrics)
    print(json.dumps({"folder": str(folder), **metrics}, ensure_ascii=False))
    return 0 if metrics["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
