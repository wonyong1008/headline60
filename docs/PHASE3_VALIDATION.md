# Phase 3 5초 립싱크 실험 — 2026-10-30

## 사용자 결정과 범위

사용자는 속도보다 억양·말끝이 문제라고 평가했으며, 말투 튜닝을 보류하고 다음 과제로 진행하도록 요청했다. Phase 2 품질 검수 완료로 처리하지 않는다. MuseTalk 약 4GB 모델 묶음과 별도 환경 설치 후 **5초 실험**을 승인받았다. 추가 이미지·영상 모델, 유료 API, 외부 서버는 사용하지 않는다.

## 실제 설치 및 소스 보완

- 앱과 분리한 Python 3.12.16 `runtime/lipsync/.venv`, 설치 용량 약 1.6GiB.
- torch 2.15.0, torchvision 0.29.2, MediaPipe 0.10.14, Diffusers 0.30.2, Transformers 4.39.2. 정확한 전체 버전은 전용 uv.lock에 기록.
- macOS arm64 / Apple M3에서 torch MPS built/available과 필수 모듈 import 확인. 이 확인만으로 추론 성공이라고 보고하지 않는다.
- MuseTalk 포트: https://github.com/barnent1/musetalk-mac , revision `7fd019315127d7f31e2e7f9853547ddceabbeb6e`, MIT.
- 포트에 `upstream/musetalk/models/vae.py`, `unet.py`가 누락되어 Tencent 원본 revision `0a89dec45a0192b824e3cf4daf96c239440c5ed8`에서 보완. URL·SHA256은 vendor/headline60-source.json에 기록.
- 모델은 CPU에서 역직렬화한 뒤 worker가 명시적으로 MPS에 올린다. 원본 CLI의 예외 삼킴/셸 실행 경로 및 외부 유료 API 서버는 사용하지 않는다.
- 필요한 가중치 **3,986,182,419 bytes**를 고정 revision/크기/SHA256으로 확인. 모델 목록은 `data/models/musetalk-v15-required.json`.
- UNet + SD-VAE + Whisper tiny + BiSeNet + ResNet18 한 추론 묶음이다. TTS/LLM과 동시에 추론하지 않는다.

## 입력과 판정 범위

- 가상 얼굴: `data/anchors/benchmark-input/reference.png`, 1024×1024 원본 보존, 추론용 임시 프레임만 512×512.
- 출처: [Syn-Vis-v0](https://huggingface.co/datasets/retowyss/Syn-Vis-v0), revision `100262732989e77f38cd831d70a376a93735006a`, images/headshot/001-0030.png. 개별 이미지는 CC0-1.0, 데이터셋/문서는 CC-BY-SA-4.0. provenance.json에 URL·해시 기록.
- 공개 가상 성인 테스트 자산이며 채널용 진행자/의상 조합으로 확정하지 않았다. 새 이미지 모델은 설치하지 않았다.
- 음성: 기존 Sohee 첫 장면 4.4539초 + 끝 무음 패딩으로 만든 5초 48kHz 모노 PCM16 WAV. 표시 문구는 “바다 산업의 인공지능 전환, 핵심은 뭘까요?”이며 추가 뉴스 사실을 생성하지 않았다.
- 정지 이미지의 입 주변 합성만 검증한다. 몸짓·깜빡임·자연스러운 고개 움직임·의상/얼굴 일관성·움직이는 기본영상 품질은 별도 검증 대상이다.
- worker는 batch 1, MPS 필수, UNet float32/VAE float16, offline/no telemetry, 25fps로 실행. neural CPU fallback을 자동 활성화하지 않는다. MediaPipe/영상 전처리의 CPU 사용과 구분한다.
- 완성 파일 H.264/AAC·프레임 수·길이·전체 디코딩을 확인한 후에만 성공 기록. 사람의 시각적 립싱크 검수와 파일 검증을 구분한다.

## 실행

```sh
# 설치된 소스/환경 재사용. 가중치 자동 다운로드 없음
runtime/lipsync/.venv/bin/python runtime/lipsync/worker.py --request data/benchmarks/lipsync-5s-20261030T010123/request.json
# 읽기 전용 준비 상태 진단. 정지 이미지 시험과 움직이는 기본영상 준비 여부를 분리해 읽는다.
uv run python scripts/anchor_preflight.py --json --reference-image data/anchors/benchmark-input/reference.png
```

위 worker는 기존 결과를 덮어쓰지 않는다. 재실험은 새 benchmark 폴더와 output_path를 사용한다. 사전 진단은 패키지 import·실제 추론·얼굴 품질 검사를 대체하지 않는다.

## 삭제 가능 범위

실험 후 `runtime/lipsync/.venv/`, `runtime/lipsync/vendor/`를 지우면 해당 환경과 가중치를 제거할 수 있다. 기존 앱 `.venv`, `runtime/tts`, TTS 모델, 뉴스 DB, 완성 뉴스 영상은 별개다. 실험 영상·메트릭·출처 기록과 소스 준비 스크립트는 보존할 수 있다. 공유 uv 캐시는 다른 프로젝트에서도 사용할 수 있으므로 일괄 삭제하지 않는다. 현재 삭제는 실행하지 않았다.

## 측정 결과

정지 이미지 5초 기술 시험은 완료했다. 실제 측정은 아래와 같으며 Phase 3 전체 완료가 아니다.

### 5초 실측 완료

`data/benchmarks/lipsync-5s-20261030T010123/`의 request.json, metrics.json, worker.log 및 세 provenance/manifest 파일에 기록했다.

| 측정 | 실제 값 |
|---|---:|
| 모델 로딩 | 5.840초 |
| 한국어 음성 특징 추출 | 31.127초 |
| 얼굴 준비 | 2.691초 |
| UNet/VAE 추론 | 46.015초 |
| 추론+블렌딩+인코딩 | 47.216초 |
| mux+출력 검증 | 0.247초 |
| worker 전체 | 89.867초 |
| worker peak RSS | 7,062,257,664 bytes (약 6.58GiB) |
| MPS allocator 관측 최대 | 3,832,935,680 bytes |
| MPS driver 관측 최대 | 4,647,518,208 bytes |

출력은 **5.000초, 512×512, 25fps, 125프레임, H.264/AAC 48kHz 모노, 145,360bytes**. ffprobe 및 전체 디코딩 통과. 정지 이미지의 입 모양이 프레임마다 달라지는 것까지 확인했다. 자연스러운 몸짓·깜빡임·미세한 한국어 음소 싱크·의상별 얼굴 일관성 승인은 별개다.

RSS는 worker 프로세스만 포함하며 자식 FFmpeg는 제외한다. MPS 값은 100ms 및 프레임 경계 샘플의 관측 최대이며 RSS와 겹칠 수 있으므로 합산하지 않는다. 시스템 전체 물리 통합 메모리나 지속 부하 성능 수치가 아니다. 첫 실험의 컴파일/초기화 비용을 포함하며 반복 성능은 새로 측정하지 않았다.

### 10초·15초 실측 (기존 결과 확인, 재실행 없음)

같은 얼굴·같은 worker(`musetalk-smoke-v1`)로 이미 completed된 결과다. 2026-10-30 세션에서 metrics.json을 읽고 ffprobe·전체 디코딩만 다시 확인했다.

| 측정 | 10초 `lipsync-10s-20261030T011837` | 15초 `lipsync-15s-20261030T012018` |
|---|---:|---:|
| 모델 로딩 | 5.422초 | 5.186초 |
| 음성 특징 추출 | 1.635초 | 1.137초 |
| UNet/VAE 추론 | 87.708초 (250프레임) | 131.883초 (375프레임) |
| worker 전체 | 100.403초 | 144.482초 |
| worker peak RSS | 7,025,655,808 bytes | 7,596,998,656 bytes |
| 출력 | 512×512, 25fps, H.264/AAC 48kHz 모노, 291,878 bytes | 동일 형식, 456,415 bytes |

프레임당 추론은 약 0.35초로 영상 1초당 약 8.8초다. 5초 실험의 음성 특징 31.127초는 첫 실행 초기화가 포함된 값이며 10·15초에서는 1~2초다. 두 결과 모두 정지 이미지 입 주변만 합성한 것이고 몸짓·깜빡임은 없다.

### 의상 시안 요청

사용자는 하의까지 보이는 구도와 더 높은 패션 강도를 요청했다. 별도의 채팅 imagegen 도구로 성인 크롭톱/로라이즈 하의 시안을 두 번 요청했으나 출력 단계에서 차단되어 결과물이 없다.

2026-10-30 사용자 지시로 로컬 이미지 편집 모델 한 종류를 설치하고 시안 1장을 만들었다. 출처·라이선스는 docs/ASSETS.md, 실행 방법은 runtime/imagegen/README.md를 따른다.

| 항목 | 실제 값 |
|---|---|
| 모델·도구 | FLUX.2 [klein] 4B 8bit 양자화, mflux 0.23.0, 4 steps, guidance 1.0, seed 7, `--low-ram` |
| 입력 | 기존 Syn-Vis 얼굴 1장 + 프롬프트(얼굴~무릎, 오프숄더 크롭 니트, 배꼽 노출, 로라이즈 와이드 팬츠, 뉴스 스튜디오) |
| 출력 | `data/anchors/outfit-drafts/draft-20261030T020000/draft.png` 720×1280, 1,178,067 bytes |
| 시간·메모리 | 전체 145.293초(step당 약 35초), mflux 프로세스 peak RSS 2,129,526,784 bytes |
| 비교 | `compare.png`: MediaPipe로 얼굴을 잘라 원본(얼굴 폭 301px)과 시안(167px)을 나란히 표시 |

육안 판정: 크롭톱·배꼽·하의가 한 화면에 보인다. 다만 프레임 하단이 허벅지 중간이라 무릎은 보이지 않고, 허리선이 배꼽 바로 아래라 로라이즈 느낌이 약하다. 손은 자연스럽다. 머리 모양·눈썹·눈매는 원본과 비슷하지만 얼굴형이 넓어지고 웃는 표정으로 바뀌어 동일 인물로 단정하기 어렵다. 이 판정은 생체 인증이 아니다. 사용자 피드백 전이며 제작용 진행자·의상 조합으로 노출하지 않는다.

### 별도 시각 검수

선택 프레임에서 입 벌림·치아 노출 변화와 눈/머리/몸 고정을 확인했다. 뚜렷한 직사각 피부색 패치는 발견하지 못했으나, 원본보다 진해진 입술·일부 흐린 입술 경계·2~3초 구간의 불균일한 치아 표현이 남는다. 선택 스틸 검수이며 전체 시간축의 미세 떨림·사람이 느끼는 자연스러움·한국어 음소 싱크를 승인한 것은 아니다. production 진행자 조합으로 노출하지 않는다.

### 관리 화면 및 최종 검사

`/anchors` 진행자 실험 메뉴를 추가했다. 저장된 실험 영상 미리보기·다운로드와 실제 시간/RSS/MPS 관측값만 표시한다. 임시 얼굴·기존 의상·정지 이미지 입 모양 실험임을 명시하며 제작용 진행자 선택에는 연결하지 않는다. 실험 ID·경로·심볼릭 링크 검증을 적용했다.

전체 Ruff 통과, pytest **212개 통과**. 이 중 새 worker 보호 테스트 46개는 입력·원본/출력/metrics 경로 충돌·잘못된 형식·누락 파일·MPS 미지원 오류를 Mock/임시 fixture로 검사하며 위 실제 추론 결과와 구분한다. 관리 화면 API 8개, 사전 진단 17개, 발음/캐시 27개, 자막 프레임 19개와 기존 95개를 유지한다. 자막 프레임 테스트에는 실제 짧은 FFmpeg 합성 fixture가 포함된다.

브라우저 `/anchors`에서 5초 영상 재생 상태 `readyState=4`, 512×512, duration=5, paused=false, 오류 없음 확인. 서버는 HEADLINE 60 관리 API와 기존 외부 Ollama를 유지하며 실험 worker는 종료해 모델 메모리를 해제했다.

### 의상 시안 2 — 사용자 얼굴 승인 후 의상 변경

사용자가 시안1의 얼굴을 승인하고 의상 노출을 더 늘리도록 요청했다. 같은 로컬 모델/8bit/4steps/seed7에서 시안1을 직접 참조했다. 새 preset `halter-lowrise-shorts`, `--reference`를 추가했으며 별도 모델 설치는 없다.

- 결과: `data/anchors/outfit-drafts/draft-20261030T090604/draft.png`, 720×1280, 1,142,142bytes.
- 실제 생성133.481초, mflux child peakRSS2,193,424,384bytes. 시스템 전체 통합메모리 수치가 아니다.
- V넥 홀터 크롭톱과 쇼츠로 변경했고 두 무릎이 보인다. 얼굴은 이전 시안을 참조했으며 동일성에 대한 생체 인증은 수행하지 않았다.
- 허리선이 배꼽 부근이라 요청한 낮은 로라이즈 실루엣은 아직 미달이다. 사용자 피드백 전 재생성·영상화를 진행하지 않는다.

### 의상 시안 3 — 성인25세 인상/낮은 허리선 요청

시안2와 원래 가상 얼굴을 함께 참조했다. adult25-extra-lowrise preset 및 --face-reference 옵션, 기존8bit/4steps/seed7로1장 생성. 결과 `data/anchors/outfit-drafts/draft-20261030T091257/draft.png`, 720×1280, 1,121,951bytes, 230.417초, child peakRSS2,721,087,488bytes. 얼굴 변화는 작고 허리선이 조금 내려갔으나 배꼽 약8cm 아래의 확실한 로라이즈에는 미달한다. 사용자 피드백 전 추가 생성이나 영상화를 하지 않는다.
