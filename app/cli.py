import argparse
import json
import sys
from datetime import datetime

from app.config import settings
from app.doctor import diagnose
from app.jobs.pipeline import execute
from app.process import run, save_json
from app.renderer.ffmpeg import encoder_args, verify
from app.script.models import VideoRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="HEADLINE 60 local production")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("smoke-video")
    make = commands.add_parser("generate")
    make.add_argument("--topic", required=True)
    make.add_argument("--seconds", type=int, choices=[30, 60], default=30)
    make.add_argument("--resolution", type=int, choices=[720, 1080], default=720)
    make.add_argument("--category", default="경제")
    make.add_argument("--style", default="차분한 설명형")
    resume = commands.add_parser("resume")
    resume.add_argument("job_id")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = diagnose(settings)
            settings.data.mkdir(parents=True, exist_ok=True)
            save_json(settings.data / "doctor.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ready"] else 1
        if args.command == "smoke-video":
            folder = settings.outputs / ("smoke-" + datetime.now().strftime("%Y%m%dT%H%M%S%f"))
            folder.mkdir(parents=True, exist_ok=False)
            path = folder / "test.mp4"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n",
                 "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                 "-t", "2", *encoder_args(settings), "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-movflags", "+faststart", str(path)])
            save_json(folder / "ffprobe.json", verify(path, 720, 2))
            print(path)
        elif args.command == "generate":
            request = VideoRequest(topic=args.topic, seconds=args.seconds, resolution=args.resolution,
                                   category=args.category, style=args.style)
            print(execute(settings, request=request) / "output.mp4")
        else:
            print(execute(settings, resume=args.job_id) / "output.mp4")
        return 0
    except KeyboardInterrupt:
        print("작업이 취소되었습니다. resume으로 재개할 수 있습니다.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
