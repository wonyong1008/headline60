# 모델·자산 기록

게시·상업 배포 전에 실제 사용하는 모델, 음성 및 도구 조건을 다시 확인한다.

| 구성 | 사용 항목 | 출처 및 상태 |
|---|---|---|
| LLM | Ollama qwen3:4b (Q4_K_M) | https://ollama.com/library/qwen3:4b — 표시 라이선스 Apache-2.0. 실제 모델 응답은 작업별 저장 |
| 추론 도구 | Ollama | https://github.com/ollama/ollama — MIT |
| 한국어 음성 | macOS say / Yuna | OS 내장 음성. 로컬 합성 검증과 별개로 영상 게시·판매 및 재배포 권리는 Apple 약관 추가 확인 필요 |
| 글꼴 | Apple SD Gothic Neo | macOS 설치본 사용. 글꼴 파일을 프로젝트에 복사하거나 배포하지 않음. 설치형 판매 전 사용 조건 확인 |
| 영상 | Homebrew FFmpeg | 배포 바이너리 및 H.264 관련 라이선스는 설치형 판매 시 별도 검토. 현재는 로컬 실행 |
| 그래픽 | Pillow 코드로 생성한 카드 | 외부 이미지·기사 원문·실제 인물 미사용. 막대는 개념 일러스트이며 실제 통계가 아님 |
| 테스트 콘텐츠 | 일반 개념 설명 | 최신 뉴스·검증된 보도 아님. 출처 목록은 비어 있고 AI 초안 검토 필요 상태로 저장 |

외부 AI API 호출이나 자동 게시 기능은 없다. 모델 다운로드와 패키지 설치 시 인터넷이 필요하며, 설치 이후 영상 제작은 로컬에서 실행한다.

## Phase 1 실제 뉴스 테스트

- 출처: 해양수산부·대한민국 정책브리핑, 2026-10-28, https://www.korea.kr/news/policyNewsView.do?newsId=148974483 .
- 해당 페이지는 텍스트에 대해 공공누리 제1유형(출처표시)을 안내하고 사진·이미지 등은 별도 권리 확인이 필요하다고 명시한다. 이번 테스트는 독자적 요약과 짧은 근거 문구만 저장하고 영상·자료 ZIP에 출처를 기록했다. 원문 사진·도표·영상은 사용하지 않았다.
- Google News RSS는 제목·출처·일시·URL 후보 메타데이터로만 사용한다.
- 위 내용은 Phase 1 당시의 기록이며 Phase 2 TTS 설치는 아래와 같다.

## Phase 2 로컬 TTS 및 그래픽
- MLX Audio 0.6.2: https://github.com/Blaizzy/mlx-audio (MIT). 별도 Python 3.12 `runtime/tts/.venv` 사용, 약 465MiB.
- 모델: https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit . 모델 카드 Apache-2.0, revision `41d3337e8b7f2843a75841595fc14e4b9a7a4b96`, 3,080,141,538 bytes.
- 원본 모델/지원 범위: https://github.com/QwenLM/Qwen3-TTS . 1.7B CustomVoice의 스타일 지시 사용. 0.6B에서 같은 지시 기능을 지원한다고 가정하지 않는다.
- 한국어 고정 프리셋 Sohee 사용. 실제 인물 음성 참조·복제는 수행하지 않았다. 남성 한국어 음성은 미검증으로 제공하지 않는다.
- 새 영상 카드는 Pillow로 직접 그린 관계도·계획 단계도이며 통계 수치를 창작하지 않는다. 외부 기사 사진·영상은 재사용하지 않았다. 글꼴 파일도 배포하지 않는다.
- 진행자 이미지·MuseTalk·립싱크 모델은 설치하지 않았다. 기술적 실행 검증과 사람의 자연스러움·최종 품질 검수는 구분한다.

## Phase 3 5초 기술 시험

- MuseTalk Mac MIT 포트 및 Tencent 원본 보완 소스의 고정 revision은 docs/PHASE3_VALIDATION.md와 vendor/headline60-source.json에 기록한다.
- 모델 배포 파일은 data/models/musetalk-v15-required.json의 URL·revision·크기·SHA256을 따른다. MuseTalk/SD-VAE/Whisper/face-parsing의 구성별 조건을 보존한다.
- 시험용 가상 성인 얼굴은 retowyss/Syn-Vis-v0의 개별 CC0 이미지 1장. 데이터셋 문서는 CC-BY-SA-4.0이며 원본 출처를 provenance.json에 기록한다. 채널의 고정 진행자로 확정하거나 실제 인물로 표시하지 않는다.
- 포트의 실인물 테스트 영상은 이번 실험 입력으로 사용하지 않았다. 유료 Kling/Replicate/ElevenLabs 예제도 실행하지 않았다.

## Phase 3 의상 시안 (로컬 이미지 편집)

- 2026-10-30 사용자 지시(시스템에 영향 없는 설치는 묻지 않고 진행, 모델은 한 종류만)에 따라 설치했다.
- 모델: https://huggingface.co/black-forest-labs/FLUX.2-klein-4B , revision `e7b7dc27f91deacad38e78976d1f2b499d76a294`, 모델 카드/LICENSE.md 표시 **Apache-2.0** (같은 계열 9B는 비상업 라이선스라 사용하지 않음). 비게이트 저장소.
- diffusers 구성(transformer·text_encoder·vae·tokenizer·scheduler)만 받았다. 15,980,151,320 bytes. 중복 단일 파일 체크포인트와 예시 이미지는 제외. 경로 `data/models/flux2-klein-4b/`, 기록 `headline60-model.json`.
- 실행 도구: mflux 0.23.0 (MIT, https://github.com/filipstrand/mflux), 별도 Python 3.12 `runtime/imagegen/.venv`. Pillow 13은 Python 3.12에서 import 오류가 나서 12.x로 고정했다.
- 입력 얼굴은 기존 Syn-Vis CC0 합성 이미지 1장. 생성 시안은 실험 결과이며 채널 진행자·의상 조합으로 확정하지 않았다. 실제 인물 사진은 입력하지 않는다.


## 2026-10-30 실제 아파트 사진 릴스 시안
- Wikimedia Commons `Ttukseom_apartment_buildings_seoul.jpg`(Sgroey, 2024-08-21), `Dangsan_apartments.jpg`(Brit in Seoul, 2020-11-07). 모두 CC BY-SA 4.0.
- 출처 URL·촬영일·SHA256·이용 조건: `data/reels-assets/sources.json`. 결과 폴더 `outputs/20261030T102758-1e50025b/CREDITS.md`에 원본 링크와 변경 내용, 영상 편집물 CC BY-SA 4.0 조건을 기록했다.
- 자료사진에 크롭·줌/팬·텍스트·음성을 추가했다. 특정 단지의 현재 시세를 주장하지 않는다.
- 새 모델·폰트 설치 없음. 기존 AppleSDGothicNeo, 기존 Sohee 음성을 재사용했다.


## 2026-10-30 금융 실제 자료화면
- Pexels License(무료 상업 이용/편집 허용, 원본 단독 판매·배포 및 인물/브랜드 후원 암시 금지): https://www.pexels.com/license/
- Uriel Mont, ATM 사용 영상: https://www.pexels.com/video/woman-using-a-atm-machine-6276983/
- Kampus Production, 금융 상담 사진: https://www.pexels.com/photo/an-agent-showing-documents-to-an-elderly-man-8441811/
- Thang Nguyen, 은행 창구 사진: https://www.pexels.com/photo/smiling-bank-teller-assisting-customers-indoors-38815862/
- 파일별 원본/해시/라이선스 확인일/키워드는 data/finance-stock/sources.json, 새 시안 media-sources.json 및 CREDITS.md. 해외 일반 자료화면으로 표시하고 특정 한국 은행의 실제 사례나 지원을 암시하지 않는다.
