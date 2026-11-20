# Pixchive login browser helper

별도 내부 FastAPI 서비스(`8765`)와 전용 headed Chromium 로그인 창이다.
앱 코드·원본 보관 볼륨·DB를 import하거나 마운트하지 않는다. noVNC JavaScript와
외부 앱 인증/CSRF/WebSocket 프록시는 메인 앱 담당이다. 이 디렉터리만 Docker 빌드
컨텍스트로 사용한다.

```sh
docker build -t pixchive-browser-helper ./browser_helper
```

## 실행 계약

- 이미지: `mcr.microsoft.com/playwright/python:v1.64.1-noble`, Python 패키지도
  `playwright==1.64.1`. 런타임 및 테스트 의존성은 앱과 별도로 전이 의존성까지 고정한다.
- 사용자: 시작 준비 프로세스만 root로 root-only 원본 키를 읽고 도메인별 파생 키를
  0600 tmpfs 파일로 전달한 뒤 UID/GID `1000:1000`으로 영구 전환한다. 실제 supervisor·
  API·Chromium은 UID/GID 1000이며 원본 키 권한을 바꾸지 않는다. 공식 Noble 이미지의
  `pwuser`는 UID 1001이므로 그 이름의 기본 UID에 의존하지 않는다. sandbox 해제를 거부한다.
- `/run/browser` 전체를 **tmpfs**, UID/GID 1000, mode 0700으로 마운트한다. 예:
  `rw,nosuid,nodev,size=1g,uid=1000,gid=1000,mode=0700`. 프로필은
  `/run/browser/profile`; HOME·캐시·임시 파일도 같은 tmpfs 안에 둔다.
- `/state`에는 helper 전용 영속 볼륨만 마운트하고 UID/GID 1000 쓰기 권한을 준다.
  유일한 영속 인증 자료는 `/state/auth-state.enc`의 Fernet 암호문이다.
  새 named volume의 기본 copy-up은 이미지 디렉터리의 UID/GID 1000을 복사한다.
  기존 볼륨/`volume-nocopy`/bind mount의 소유권은 메인 배포에서 미리 준비해야 한다.
- `PIXCHIVE_KEY_FILE` 기본값은 `/run/pixchive-secrets/credential.key`.
  앱과 같은 키 **파일 하나만 읽기 전용** 마운트한다. 키 생성/교체는 helper가 하지 않는다.
  root-only 0600 키도 지원한다. 원본은 복제하지 않고 HMAC 제어 토큰과 파생 저장 키만
  `/run/browser/keys/derived.json`에 전달한다. 비 root 직접 실행은 읽을 수 있는 별도 키를 쓴다.
- 공식 `vendor/seccomp_profile.json`을 `security_opt: seccomp:...`로 적용하고
  `init: true`, 최소 `shm_size: 256m`를 권장한다. 호스트가 비 root Chromium의
  user namespace sandbox를 허용해야 한다. 실패 시 sandbox를 끄지 않고 오류/재시도로 남긴다.
- 컨테이너에는 `8765`만 expose하며 호스트 `ports`로 공개하지 않는다. Pixiv 접속을
  위한 outbound 연결은 필요하다. Xvfb `:99`의 TCP를 끄고, x11vnc는
  `127.0.0.1:5900`에만 바인딩한다. `5900`/`6080` 외부 공개나 websockify는 필요 없다.
- 단일 Uvicorn worker만 실행한다. 관리되는 프로세스가 죽으면 launcher는 나머지
  자식을 정리하고 종료한다. Compose 측 `restart` 및 45초 이상의 종료 유예를 권장한다.
  API 내부 브라우저/키 초기화 실패는 민감한 예외를 출력하지 않고 30초 후 재시도한다.

Docker Compose 파일 자체와 실제 배포는 메인 담당이 변경한다. helper 빌드/테스트는
운영 `.env`, 키, 쿠키, 앱 데이터나 기존 브라우저 프로필을 사용하지 않는다.

## 내부 API

모든 HTTP/WS에 다음 헤더가 필요하다. 실제 키나 토큰을 인자·로그·문서에 출력하지 않는다.

```python
token = hmac.new(keyfile_bytes, b"pixchive-browser-control-v1", hashlib.sha256).hexdigest()
headers = {"Authorization": f"Bearer {token}"}
```

`keyfile_bytes`는 줄바꿈을 포함한 원본 파일 바이트다. 암호화 키는 같은 바이트로
`HMAC-SHA256(keyfile_bytes, b"pixchive-browser-storage-v1")`를 계산하고 URL-safe base64로
인코딩한 별도 Fernet 키다. 원본 키는 최소 32바이트여야 한다.

HTTP는 사설/loopback 주소의 직접 **HTTP** 연결만 받는다. 전달된 IP 헤더를 신뢰하지
않는다. TLS 및 외부 접근 제어는 메인 앱이 맡는다. 모든 HTTP 응답은 `no-store`다.

| 경로 | 응답/동작 |
| --- | --- |
| `GET /status` | `{state,last_checked,cookie_available,viewing,retry_at,login_pending,login_generation}`. state는 `starting`, `connected`, `reauthenticating`, `needs_login`, `error`. 확인 전 시각은 null, 그 외 Unix 초. viewing은 view 연결이 하나라도 있으면 true인 내부 전용 값. |
| `POST /open` | 수동 모드로 전환하고 기존 페이지·포커스·팝업·입력을 그대로 유지. 빈 창일 때만 공식 LOGIN을 연다. 재연결에서도 이동·검사·snapshot을 하지 않는다. status 반환. |
| `POST /login` | 사용자가 명시적으로 공식 로그인 화면을 요청. 기존 앱 쿠키와 프로필은 지우지 않으며 같은 프로필로 계정 로그인 화면을 연다. |
| `POST /reauth` | 기존 context로 `https://www.pixiv.net/` 정상 재방문 후 로그인 증거/쿠키 재확인. 명시적 게스트 홈이면 같은 context의 공식 LOGIN을 한 번 방문해 SSO를 시도하며 폼·challenge·HTTP 오류에는 추가 방문하지 않음. status 반환. 수동 로그인 중에는 이동하지 않고 status만 반환한다. |
| `POST /seed` | JSON `{cookie: string}`. 새 빈 프로필에만 host `www.pixiv.net`, path `/`, Secure/HttpOnly PHPSESSID를 넣고 정상 홈에서 확인. 기존 상태가 있으면 409. |
| `POST /complete` | 사용자가 완료를 눌렀을 때 현재 페이지와 쿠키를 한 번 확인하고 성공 시 암호화 상태를 한 번 저장한다. 수동 모드는 유지하며 앱 계정 검증을 기다린다. 실패 시 입력을 유지하고 이동하지 않는다. |
| `POST /resume` | JSON `{generation: int}` 필수. 완료된 login_generation과 일치하고 검증 대기 준비 상태일 때만 수동 모드를 해제한다. 새 로그인 시도·오래된 완료 요청은 일반 오류로 거부한다. 브라우저 호출은 하지 않는다. |
| `POST /disconnect` | 사용자가 앱 연결을 삭제했을 때만 호출. context의 저장 상태를 비우고 브라우저를 종료해 전용 tmpfs 프로필을 제거하며 암호화 파일도 삭제. 다시 열기/seed 전까지 자동 시작하지 않음. status 반환. |
| `GET /credential` | `{cookie: string or null}`. 로그인 증거와 유효 쿠키를 함께 확인한 `connected`에서만 값 반환. 공개 응답/로그로 전달하지 말 것. |
| `WS /view` | `binary` subprotocol 지원. localhost RFB를 바이너리 그대로 중계. 첫 banner 포함, 프레임 최대 1MiB. 텍스트 1003, 과대 프레임 1009, 미인증 1008. |

미인증 HTTP는 401, 초기화 실패는 민감 정보 없는 503, 잘못된 seed JSON은 422다.
브라우저 방문 자체가 실패하면 성공을 위장하지 않고 `state:error`를 반환한다.
메인 서비스는 `connected` 쿠키도 자신의 CookieAdapter.validate로 계정을 확인한다.

## 인증 유지와 한계

프로필은 helper 전용이다. 기본 Chrome 프로필을 열거나 비밀번호를 받거나 저장하지
않으며 Preferences와 관리 정책에서 비밀번호 관리·자동완성·Chrome 계정 동기화를 끈다.
정상 브라우저 방문에 따른 기존 로그인 상태 복구만 시도한다. CAPTCHA, MFA,
로그인 폼은 `needs_login`이다. 쿠키 존재만으로 연결 성공을 판정하지 않는다.
현재 Pixiv meta `global-data` 또는 Next bootstrap의 로그인 사용자 ID 증거를 확인한다.
인식할 수 없는 화면/메타데이터는 `error`로 남긴다.

5초마다 쿠키/현재 DOM을 읽고 변경 시 암호문을 갱신한다. IndexedDB/localStorage도
30초마다 변경 여부를 확인하고 종료 때 저장한다. 원자적 교체 전에 암호문을 fsync하며
복원은 `context.set_storage_state(storage_state)`를 사용한다. Pixiv 도메인의 쿠키와
HTTPS localStorage/IndexedDB만 보존하며 다른 사이트 상태·비밀번호·가상 passkey·OPFS는
내보내지 않는다. 암호문이 손상되었거나 다른 키라면 덮어쓰지 않는다.

수동 모드에서는 viewer 수와 무관하게 DOM·쿠키·storage_state 검사·저장·이동을 하지 않는다. 연결 끊김이나 창 닫기로 모드를 해제하지 않으며 앱이 명시적인 완료·계정 검증 뒤 /resume을 호출한다. 완료된 상태의 독립 실행에서는 기존 프로필로 약 10분마다 정상 홈을 재방문한다. Compose의
`PIXCHIVE_BROWSER_MANAGED_NAVIGATION=1`에서는 재방문을 앱 worker가 담당해 계정 공용
429 대기 시간을 지킨다. 정상 페이지의 429 Retry-After도 계정 worker에 전달한다.
view 연결 중에는 helper의 자동
navigation을 피하고 최소 쿠키/DOM 확인·저장만 한다. 메인이 자동 `/reauth`를 별도로
호출한다면 로그인 입력 중 그 요청도 미루어야 한다. 수동 `/reauth`는 즉시 수행한다.
최초 빈 프로필은 자동 방문하지 않아 **처음 `/open`이나 `/reauth`를 호출하기 전에**
`/seed`를 할 수 있다. 이미 로그인 화면을 열었거나 snapshot을 복원했다면 seed를 거부한다.

PHPSESSID 하나만 `/seed`한 프로필에는 계정 SSO 쿠키/저장 상태가 없다. 그 PHPSESSID가
만료되면 홈 재방문만으로 새로운 세션을 발급받을 근거가 없어 `needs_login`이 될 수 있다.
SSO 상태에 따른 자동 복구를 사용하려면 **전용 noVNC 로그인 창에서 정상 로그인을
완료**하여 계정 도메인의 쿠키·localStorage·IndexedDB도 보관해야 한다. 그렇게 보관한
SSO 자체가 만료되거나 추가 인증을 요구하는 경우에도 사용자의 로그인이 필요하다.

쿠키 만료나 추가 인증을 영구히 피할 수 없다. UI 구조·브라우저/호스트 sandbox 정책
변경에 따른 실제 Pixiv 로그인 호환성은 배포 환경에서 별도 확인해야 한다.
연결 삭제는 논리적 삭제이며 저장 장치의 과거 블록을 보안 삭제한다고 보장하지 않는다.

## 격리 검증

Python 3.12 이상에서 이 폴더 전용 가상환경을 만들고 의존성을 설치한 뒤 실행한다.

```sh
python -m venv browser_helper/.venv
# Linux: browser_helper/.venv/bin/python
# Windows: browser_helper/.venv/Scripts/python.exe
browser_helper/.venv/bin/python -m pip install --no-cache-dir -r browser_helper/requirements-test.txt
browser_helper/.venv/bin/python browser_helper/run_tests.py
```

검증 스크립트는 임시 키와 암호문을 `browser_helper/.tmp`에만 만들고 정리한다.
가짜 BrowserContext와 임시 loopback RFB 서버를 사용해 암호화/원자 저장, 로그인 증거,
홈 방문으로 쿠키 교체, snapshot/IndexedDB 복원, 인증 거부, 연결 삭제, 입력 중 자동 방문
억제, 최초 RFB banner, 양방향 binary 전달, 취소/크기 제한/종료를 확인한다.
실제 서버·Pixiv 계정·실제 인증 파일에 접속하는 테스트는 없다.

공식 참고: [Docker/nonroot/seccomp](https://playwright.dev/python/docs/docker),
[storage-state restore](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-set-storage-state),
[persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).
