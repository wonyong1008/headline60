> 이 문서는 Phase 0 당시의 기록입니다. 최신 구현과 검증은 [Phase 1 검증 기록](PHASE1_VALIDATION.md)을 참고하세요.

# 실제 실행 기록 — 2026-10-28 (Asia/Seoul)

## 환경 확인

- macOS 26.6.2 (25G83), arm64, Mac15,12 / Apple M3.
- Unified Memory: `sysctl`로 17,179,869,184 bytes (16GiB) 확인.
- 초기 `df -h`: 여유 약 65GiB. 작업 후 Python 디스크 조회 약 73GiB.
  Homebrew 정리 및 macOS 공간 계산 시점 차이가 있어 수치는 달라질 수 있다.
- 기존 Homebrew 6.0.19, Homebrew Git 2.53.0, Homebrew Python 3.14.7 확인.
- 실행 PATH에 따라 Apple Git 2.50.1 / 시스템 Python 3.9.6도 보인다.
  프로젝트는 `uv run`과 `.venv/bin/python`으로 Python 3.12.16을 사용한다.
- 한국어 `Yuna` 및 한국어 변형 시스템 음성이 이미 설치되어 있었다.

## 설치 및 기존 환경 처리

- uv 0.13.6, FFmpeg/ffprobe 9.0.2, Ollama 0.40.11 설치.
- uv managed Python 3.12.16 및 프로젝트 `.venv`, `uv.lock` 생성.
- qwen3:4b Q4_K_M 약 2.5GB, 모델 ID `359d7dd4bcda` 다운로드.
- 주요 Python 패키지: FastAPI 0.162.0, Uvicorn 0.54.0, Pillow 12.3.0,
  httpx 0.28.1, Pydantic 2.14.1. 정확한 전체 버전은 `uv.lock`에 저장.
- Homebrew가 의존성으로 Python 3.15.2, OpenSSL 4.1.0, mlx 등을 추가하고
  readline 8.3.6, SQLite 3.54.1, xz 5.8.4, CA 인증서 등을 갱신했다.
  기본 Homebrew Python 및 OpenSSL 링크는 기존 Python 3.14.7 / OpenSSL 3.6.3으로 복원했다.
  추가 설치된 의존성은 삭제하지 않았다.
- 기존 별도 Wineskin tap의 신뢰 오류가 `brew list` 전체 조회에서 발생했다.
  해당 tap의 신뢰 설정은 변경하지 않았고 필요한 패키지 설치는 정상 완료했다.
- 프로젝트 Git 저장소를 `main`으로 초기화했다. 커밋·원격 저장소·외부 게시 작업은 하지 않았다.

## 실행 결과

| 확인 | 실제 결과 |
|---|---|
| `./scripts/dev.sh` | 127.0.0.1:8060 개발 서버 시작 |
| `GET /health` | HTTP 200, `status=ok` |
| 관리 화면 | 브라우저에서 열림, 환경 진단 `제작 준비 완료`, 최근 작업 표시 |
| Ollama `/api/tags` | qwen3:4b 설치 확인 |
| Ollama `/api/generate` | 한국어 JSON 대본 생성 및 Pydantic 검증 |
| Ollama `/api/ps` | 작업 종료 후 `models=[]`, 모델 언로드 확인 |
| VideoToolbox | 인코더 목록 및 실제 하드웨어 인코딩 성공 (`-allow_sw 0`) |
| smoke-video | 720×1280, 2초, H.264/AAC 파일 생성·전체 디코딩 성공 |
| 30초 CLI | 6개 장면, 실제 Yuna 음성, 카드·확대·페이드·장면별 자막 생성 |
| 60초 CLI | 10개 장면, 1080×1920 최종 규격 생성 |
| ffprobe / 전체 디코딩 | 두 길이 모두 30fps, H.264, AAC, 정확한 30.0 / 60.0초 확인 |
| 30초 음성 신호 | 1,440,000 samples, mean -16.6dB, max -4.0dB (무음 아님) |
| 브라우저 재생 | 30초 MP4 `paused=false`, `currentTime=5.516`, `readyState=4`, 720×1280 |
| 실패·재개 | 대본 검사에서 실패한 `20261028T214601-d520228c`를 resume으로 재개해 완료 |
| 정적 검사 / 테스트 | ruff 통과, pytest 16개 통과 |

테스트는 프레임 타임라인/SRT 종료 시간, 잘못된 음성 길이, 로컬 URL 제한, 입력 검증,
health와 파일 접근 제한, Host 검증, 실패 단계 재개·완료 결과 보존,
취소 상태, 동시 실행 잠금, 경로 이탈 차단, 한자 혼입·일부 무출처 주장 차단을 확인한다.

## 전달할 결과

- 30초: `outputs/20261028T214143-9d4e507a/output.mp4` (720×1280).
- 최종 규격 60초: `outputs/20261028T214601-d520228c/output.mp4` (1080×1920).
- 각 폴더에 `script.json`, `metadata.json`, `sources.json`, `subtitles.srt`,
  `job.json`, `job.jsonl`, `timing.json`, `ollama-response.json`, `ffprobe.json`과 장면 자료 저장.
- 초기 60초 실험 `20261028T214235-bc173550`은 기술 검증은 통과했지만
  한자 혼입 및 출처 없는 통계 표현이 발견된 초안이다. 게시에 사용하지 않는다.
  대본 검사를 보완한 뒤 위 최종 60초 작업을 다시 만들었다.

## 남은 제약

- 설명형 테스트이며 최신 보도나 사실 검증이 완료된 게시물은 아니다.
  출처 목록은 비어 있고 메타데이터의 `review_required=true`, `verified_news=false`다.
- 대본 검사는 일부 표현을 찾는 휴리스틱이다. 사실 검증·금융/의약품 검토를 대신하지 않는다.
  최종 60초 대본 역시 표현 정확성·중복·음성 속도 등을 게시 전에 편집해야 한다.
- 자막은 장면 단위이며 단어별 강제 정렬은 없다. 길이에 맞춰 음성이 늘어나거나 빨라질 수 있다.
- 음성 합성과 신호 존재를 검증했으나 사람이 전체를 듣고 자연스러움을 평가한 것은 아니다.
- UI는 진단·내역·미리보기·다운로드를 제공한다. 생성·취소·재시도는 CLI로 실행한다.
- 취소는 Ctrl+C로 지원한다. SIGKILL·재부팅 시 실행 중으로 남을 수 있으며 같은 ID로 재개한다.
- 뉴스 수집, AI 이미지, 립싱크, 자동 업로드, 설치형 패키징은 구현하지 않았다.
- Apple 음성·글꼴의 상업 게시·재배포 권리는 별도 확인이 필요하다 (`ASSETS.md`).
