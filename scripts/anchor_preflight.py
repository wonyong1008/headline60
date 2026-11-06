"""Read-only anchor prerequisites. Exit 2 means the experiment is not ready."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.anchors.preflight import diagnose
from app.config import Settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="5초 로컬 립싱크 사전 진단 (설치·다운로드 없음)")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--base-video", type=Path)
    parser.add_argument("--reference-image", type=Path)
    args = parser.parse_args(argv)
    report = diagnose(Settings(root=args.root), base_video=args.base_video,
                      reference_image=args.reference_image, manifest=args.manifest)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("5초 립싱크 사전 진단: " + ("준비됨" if report["ready"] else "준비되지 않음"))
        for missing in report["missing"]:
            print("- " + missing)
        print(report["limitations"])
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
