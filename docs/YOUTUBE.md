# YouTube 업로드 연결

HEADLINE 60은 완성·검증된 영상(`ffprobe.json`이 있는 `output.mp4`)만 YouTube Data API v3로 업로드한다. **공개 범위는 항상 `private`** 이며 코드에서 바꿀 수 없다. 공개 전환은 사람이 YouTube Studio에서 내용을 확인한 뒤 직접 한다.

## 최초 연결

1. Google Cloud 프로젝트에서 YouTube Data API v3를 사용 설정한다.
2. OAuth 동의 화면을 만들고, 사용자 인증 정보에서 **데스크톱 앱** OAuth 클라이언트를 만든 뒤 JSON을 받는다.
3. 관리 화면 → YouTube 설정에서 JSON을 등록하고 “연결”을 누른다. 브라우저에서 업로드할 채널의 계정으로 승인한다(로컬 `127.0.0.1` 콜백 + PKCE).
4. 연결 후 표시되는 채널 이름·ID가 맞는지 확인한다.

인증 정보는 `data/integrations/youtube/`(권한 0700/0600)에만 저장된다. `client.json`, `token.json`, 업로드 기록 `youtube-upload.json`은 `.gitignore`로 제외되며 화면·API 응답에 키나 토큰을 표시하지 않는다.

## 잘못된 채널 업로드 방지

`.env`에 업로드 허용 채널 ID를 고정한다(채널 ID는 공개 값이며 비밀이 아니다).

```sh
HEADLINE_YOUTUBE_CHANNEL_ID=UC...
```

업로드할 때마다 현재 토큰으로 `channels?mine=true`를 다시 조회해, 실제 채널이 연결 시점 채널과 고정 채널 ID 모두와 정확히 같을 때만 업로드한다. 다르면 `needs_auth` 상태로 멈추고 영상은 전송하지 않는다.

## 업로드 동작

- 재개 가능한(resumable) 업로드. 끊기면 같은 세션 URL로 서버가 받은 위치를 확인한 뒤 이어서 보낸다.
- 영상 ID를 받으면 작업 폴더의 `youtube-upload.json`에 기록하고, 재시도해도 새 영상을 만들지 않고 기존 ID를 돌려준다.
- 설명에는 전체 내레이션, 원문 출처, 자료사진·영상 크레딧(`CREDITS.md`)이 들어간다. `containsSyntheticMedia=true`.
- 썸네일은 영상 첫 화면(0.8초)에서 추출한다. 맞춤 썸네일 권한이 없는 채널이면 썸네일만 실패로 기록하고 영상 업로드는 유지한다.

## 테스트 모드와 장기 운영

현재 OAuth 앱은 **Testing** 상태이며 등록된 테스트 사용자만 승인할 수 있다.

- Testing 상태에서 발급된 갱신 토큰은 **7일 후 만료**된다. 만료되면 큐 항목이 `needs_auth`(재연결 필요)로 멈추며, 관리 화면에서 다시 “연결”하면 대기 중인 업로드가 이어서 진행된다.
- 장기 무인 운영을 하려면 Google Cloud 콘솔에서 OAuth 앱을 **In production**으로 게시한다. `youtube.upload` 범위는 민감한 범위라 Google 검증이 요구될 수 있으며, 검증 전에는 “확인되지 않은 앱” 경고가 표시되고 사용자 수 제한이 있다. 개인 채널 하나에 업로드하는 용도라면 경고를 확인하고 진행할 수 있다.
- 새 Google Cloud 프로젝트의 업로드 API는 감사 전까지 비공개로만 업로드되도록 제한될 수 있다. 이 앱은 원래 private만 사용하므로 동작에는 영향이 없다.
- API에는 일일 할당량이 있고 영상 업로드는 단위 소모가 큰 요청이다. 실제 사용량·한도는 Google Cloud 콘솔의 할당량 화면에서 확인한다. 한도를 넘기면 해당 항목은 실패로 표시되며 다음 날 재시도한다.
