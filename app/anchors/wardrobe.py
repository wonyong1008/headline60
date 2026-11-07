"""A rotating wardrobe for the approved presenter: a different outfit for each video.

Each look is generated once with the local FLUX edit model (face kept from the approved
image, then re-locked with the eye-aligned face transfer) and cached under
data/anchors/wardrobe/<look>/. Production rotates through the looks in order, so
consecutive videos never repeat an outfit until every look has been used.
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from PIL import Image

from app.config import Settings
from app.process import save_json

IDENTITY = (
    "Use the reference image for the EXACT same fictional ADULT Korean female news presenter in her "
    "mid-twenties: keep her face, eyes, eyebrows, nose, lips, skin tone, light makeup, expression and "
    "long dark-brown hair exactly as in the reference. Same camera framing: head-to-knees, standing "
    "upright facing the camera, arms relaxed, space above the head and below the knees. Modern news "
    "studio background with soft blue screens and flattering studio light. Photorealistic editorial "
    "portrait, natural anatomy, sharp focus on the face. No text, no logos. Outfit: "
)

# Tasteful, adult, broadcast-glamour looks: a little skin (shoulders, arms, above-the-knee hems,
# modest necklines), never underwear or nudity.
LOOKS = {
    "sleeveless-wrap-dress": "a fitted sleeveless black wrap dress with a V-neckline and a hem a few inches above the knee, slim belt, bare arms and shoulders, nude heels.",
    "satin-slip-blazer": "a champagne satin slip dress with thin straps ending mid-thigh, worn under an open cream oversized blazer, delicate necklace.",
    "off-shoulder-knit": "a fitted ivory off-the-shoulder ribbed knit dress showing both shoulders and collarbones, hem just above the knee.",
    "halter-midi": "a navy halter-neck dress leaving the shoulders and upper back bare, fitted bodice, soft midi skirt, small hoop earrings.",
    "silk-blouse-mini": "a white silk blouse with the top two buttons open and sleeves rolled, tucked into a black high-waisted mini skirt, sheer tights, heels.",
    "bodycon-red": "a fitted red sleeveless bodycon dress with a square neckline, hem above the knee, bare arms, thin gold bracelet.",
    "sundress-floral": "a light floral sundress with thin straps and a sweetheart neckline, flowing skirt above the knee, bare shoulders.",
    "sheer-sleeve-black": "a fitted black dress with sheer long sleeves and a deep but tasteful V-neckline, hem above the knee.",
    "pencil-skirt-camisole": "a beige silk camisole top with thin straps tucked into a grey pencil skirt above the knee, bare arms and shoulders.",
    "one-shoulder-emerald": "an emerald one-shoulder cocktail dress, fitted, hem above the knee, one bare arm and shoulder, nude heels.",
}

_LOCK = threading.Lock()


def wardrobe_root(config: Settings) -> Path:
    return config.data / "anchors" / "wardrobe"


def next_look(config: Settings) -> str:
    """Rotate through the looks; the counter survives restarts."""
    state_path = wardrobe_root(config) / "state.json"
    names = list(LOOKS)
    with _LOCK:
        state = json.loads(state_path.read_text()) if state_path.is_file() else {"next": 0, "history": []}
        index = int(state.get("next", 0)) % len(names)
        look = names[index]
        state["next"] = (index + 1) % len(names)
        state["history"] = (state.get("history", []) + [{"look": look, "at": time.time()}])[-50:]
        wardrobe_root(config).mkdir(parents=True, exist_ok=True)
        save_json(state_path, state)
    return look


def generate_look(config: Settings, look: str, approved_image: Path) -> Path:
    """Create (once) the outfit image for a look, face re-locked to the approved presenter."""
    folder = wardrobe_root(config) / look
    final = folder / "presenter.png"
    if final.is_file():
        return final
    imagegen = config.root / "runtime/imagegen/.venv/bin/python"
    lipsync = config.root / "runtime/lipsync/.venv/bin/python"
    if not imagegen.is_file() or not lipsync.is_file():
        raise RuntimeError("의상 생성 환경(runtime/imagegen, runtime/lipsync)이 없습니다.")
    if folder.exists():
        # A previous half-finished attempt: keep it for inspection, generate into a fresh folder.
        folder = folder.with_name(f"{look}-{int(time.time())}")
    env_args = [str(imagegen), str(config.root / "runtime/imagegen/outfit_draft.py"),
                "--reference", str(approved_image), "--prompt", IDENTITY + LOOKS[look],
                "--seed", str(11 + list(LOOKS).index(look)), "--output-dir", str(folder)]
    result = subprocess.run(env_args, capture_output=True, text=True, check=False, timeout=900)
    draft = folder / "draft.png"
    if result.returncode != 0 or not draft.is_file():
        raise RuntimeError("의상 이미지 생성 실패: " + (result.stdout + result.stderr)[-300:])
    # Same eye position/size as the approved still, so the fixed lip-sync crop and the
    # on-photo placement look identical from one outfit to the next.
    framed = folder / "reframed.png"
    reframe = subprocess.run([str(lipsync), str(config.root / "runtime/lipsync/reframe_to_reference.py"),
                              "--reference", str(approved_image), "--draft", str(draft), "--output", str(framed)],
                             capture_output=True, text=True, check=False, timeout=300)
    if reframe.returncode != 0 or not framed.is_file():
        framed = draft
        (folder / "reframe-skipped.txt").write_text((reframe.stdout + reframe.stderr)[-500:])
    locked = folder / "presenter.png"
    lock = subprocess.run([str(lipsync), str(config.root / "runtime/lipsync/lock_reference_face.py"),
                           "--reference", str(approved_image), "--draft", str(framed), "--output", str(locked)],
                          capture_output=True, text=True, check=False, timeout=300)
    if lock.returncode != 0 or not locked.is_file():
        # The draft already carries the reference identity; use it rather than failing the video.
        Image.open(framed).save(locked)
        (folder / "face-lock-skipped.txt").write_text((lock.stdout + lock.stderr)[-500:])
    with Image.open(locked) as image:
        if image.size != (720, 1280):
            raise RuntimeError("의상 이미지 크기가 720×1280이 아닙니다.")
    if folder.name != look:
        target = wardrobe_root(config) / look
        if not target.exists():
            folder.rename(target)
            locked = target / "presenter.png"
    return locked


def outfit_for_video(config: Settings, approved_image: Path) -> tuple[str, Path]:
    """Pick the next look and return its image; failures fall back to the approved still."""
    look = next_look(config)
    try:
        return look, generate_look(config, look, approved_image)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        failures = wardrobe_root(config) / "failures.jsonl"
        wardrobe_root(config).mkdir(parents=True, exist_ok=True)
        with failures.open("a") as stream:
            stream.write(json.dumps({"look": look, "error": str(exc)[:300], "at": time.time()}, ensure_ascii=False) + "\n")
        return "approved-default", approved_image
