# Helper 검증 기록 — 2026-10-27

검증 범위는 `browser_helper/`에 한정한다. 운영 서버·실제 키·쿠키·DB·원본 보관 자료를
읽거나 변경하지 않았다. 앱 API/worker/프런트엔드 통합 및 실제 Pixiv 계정 로그인의
완료를 뜻하지 않는다.

## 자동 검사

`browser_helper/.venv/Scripts/python.exe browser_helper/run_tests.py`:

- helper 테스트 **161개 통과** (수동 로그인 후속 57개 추가; 이전 bounded SSO까지 104개).
- Ruff check 및 format check 통과, 의존성 검사 통과.
- Starlette TestClient가 테스트 전용 HTTPX 사용에 대한 deprecation 경고 1개를 출력한다.
- 새로운 임시 키와 state만 `browser_helper/.tmp`에서 만들고 각 실행 종료 시 정리했다.
- 실제 loopback TCP 서버로 RFB 첫 배너, 양방향 binary, 취소·크기 제한·종료를 확인했다.
  HTTP/WS 무인증 거부, 비공개 HTTP, 마스킹 오류, `viewing`, 연결 삭제도 확인했다.

## 조용한 수동 로그인 후속

Playwright 1.64.1의 storage-state 수집은 현재 열린 페이지가 없는 origin을 확인할 때
`visitOrigins → newPage(forStorageState=true) → close`를 사용한다. 로그인 중 주기적인
암호화 저장도 이 수집을 실행하므로, 화면의 연결 유무만으로 탐색을 막는 것으로는 부족하다.

helper 계약은 다음과 같다.

- 비공개 status에 `login_pending: bool = False`, `login_generation: int = 0`을 추가한다.
  `/open`, `/login`마다 세대 번호를 증가시킨다. 수동 대기와 완료 준비는 메모리 상태이며
  helper 재시작에 보존하지 않는다. 메인 앱은 자신의 대기 상태를 보존한다.
- `/open`, `/login`은 lock을 기다리기 전에 대기를 시작하고 완료 준비를 무효화하며,
  lock을 얻은 뒤에도 이전 완료가 남긴 준비 상태를 다시 무효화한다.
  `/open`은 살아 있는 비어 있지 않은 페이지가 하나라도 있으면 검사·저장·탐색·focus
  이동 없이 그대로 둔다. 처음 빈 화면에서만 LOGIN으로 이동한다. `/login`은 명시적인
  공식 로그인 이동이며 기존 프로필을 지우지 않고 Retry-After 대기도 유지한다.
- 대기 중 `tick`은 쿠키·DOM·storage-state·저장·탐색과 브라우저 재시작을 모두 생략한다.
  WebSocket 종료나 재연결은 대기를 풀지 않는다. `/reauth`는 현재 status만 반환하며
  `/seed`는 브라우저 호출 없이 기존 `409`로 거부한다. 종료 시에도 snapshot하지 않는다.
- `POST /complete`에서만 쿠키와 DOM을 한 번 검사한다. connected이면 IndexedDB를
  포함한 storage-state를 한 번 수집하고 한 번 암호화 저장한다. 완료 준비만 설정하며
  `login_pending`은 계속 true다. 로그인되지 않았으면 needs_login과 pending true를
  반환하고 입력·프로필을 저장하거나 탐색하지 않는다. 검사·저장 오류는 일반적인 503이다.
- 비공개 `GET /credential`은 connected이면 대기 중에도 검사한 쿠키를 반환할 수 있다.
  앱은 `/complete` 응답의 `login_generation`을 보존하고 check-only 계정 검증을 수행한
  뒤, 검증 성공 뒤에만 그 세대 번호로 `/resume`을 호출한다. 검증에서 `/reauth`를 호출하지 않는다.
- `POST /resume`은 JSON `{"generation": 정수}`를 필수로 받는다. 누락·문자열·bool·실수·
  음수·추가 필드는 일반적인 422로 거부한다. 현재 세대 번호와 일치하고 유효한 명시적
  완료 준비가 있을 때만 대기와 완료 준비를 해제한다.
  브라우저 호출·탐색은 없다. 새 `/open`이나 `/login`이 이전 완료를 무효화하면 일반적인
  `503 {"detail":"Browser unavailable"}`을 반환한다. 진행 중 완료와 새 열기의 경합도
  세대 번호로 차단한다. 새 상호작용이 다시 완료됐어도 이전 세대의 재개는 거부하고 현재
  완료 준비를 보존한다. `/disconnect`는 대기·완료 준비를 모두 지운다.

helper `.venv`의 `run_tests.py`에서 **161개 통과**, Ruff check/format check와 의존성
검사 통과를 확인했다. 합성 입력·팝업·빈 탭이 있는 상태에서 자동/managed 모드 각각
200회 due tick과 다섯 번 view 재연결, 실제 TestClient WebSocket 재연결을 검증했다.
새 페이지·snapshot·DOM/쿠키 조회·저장·탐색 호출이 없고 입력·기존 암호문이 유지된다.
성공한 완료의 검사/저장 각 1회, 미인증/오류 때 대기 보존, 완료 전/무효화 후 재개 거부,
대기 시작 전부터 lock을 기다리던 작업과 진행 중 검사 경합, 새 완료 뒤 이전 세대의 재개
거부와 필수 세대 번호 검증, 재개 후 기존 bounded SSO 자동 재인증도 확인했다.
테스트의 키와 상태는 임시 경로에서 새로 만들고 정리했다.

`smoke_browser.py`에는 실제 Chromium용 오프라인 수동 로그인 단계를 추가했다. 합성
origin에 localStorage를 쓴 뒤 해당 페이지를 닫고, 합성 입력을 작성한 주 화면만 남긴다.
context의 page 이벤트·주 화면 navigation 이벤트와 storage-state/쿠키/DOM/저장 호출
수를 관찰해 200회 due tick·view 재연결 동안 모두 0인지 검사한다. 명시적 완료 한 번은
검사/저장 각 1회와 pending 유지를 확인하고, 합성 계정의 check-only 성공을 대신하는
확인 뒤 완료 응답의 세대 번호를 사용한 브라우저 호출 없는 resume 및 정상 자동 tick 재개를 검사한다.
**이번 후속에서는 이 Chromium 단계를 실행하거나 Docker를 빌드·실행·배포하지 않았다.**
아래 Chromium/이미지 통과 기록은 이 새 단계 추가 전의 결과다. 운영 환경·실제 계정·
환경 파일·키·쿠키는 읽거나 변경하지 않았으며 실제 입력 장면의 추가 재검증은 남아 있다.

## 이미지와 실제 Chromium

Playwright Python `v1.64.1-noble` 기반으로 고정한 모든 의존성의 Linux/Python 3.12 설치와
`pixchive-browser:local` 빌드를 통과했다. 기본 이미지의 HTTP Azure Ubuntu 미러에서
해시 불일치가 있어, 검증을 유지하며 공식 HTTPS Ubuntu 미러를 사용하도록 변경했다.

`smoke_browser.py`를 **network none**, 새로운 `/state` named volume, `/run/browser`
512MiB tmpfs, `/tmp` 256MiB tmpfs, `/dev/shm` 512MiB, UID/GID 1000으로 실행했다.
합성 페이지는 Playwright route로만 제공했으며 원격 사이트에 연결하지 않았다.

- 새 named volume의 이미지 디렉터리 소유권 copy-up과 UID 1000 쓰기 성공.
- 호스트 기본 Docker seccomp에서는 `No usable sandbox`로 Chromium 실행이 거부됨.
- 보관한 **공식 Playwright seccomp 프로필**을 적용하면 `chromium_sandbox=True`,
  nonroot headed Chromium 실행 성공. unconfined, SYS_ADMIN, no-sandbox는 사용하지 않음.
- 실제 Chromium에서 일반 홈 탐색·쿠키 교체·로그인 사용자 meta/Next bootstrap 확인.
- 로그인 폼·MFA one-time-code·CAPTCHA DOM 및 게스트 계정이 성공으로 판정되지 않음.
- 암호문 저장, 0600 권한, 세션/저장 상태의 평문 미포함 확인.
- live profile을 제거한 뒤 새 context의 `set_storage_state`로 localStorage/IndexedDB 복원.
- **PHPSESSID만 실제 만료**시킨 뒤 계정 SSO 쿠키와 localStorage를 유지. due 10분
  자동 tick이 정상 홈 → 계정 도메인의 SSO 쿠키 및 localStorage 확인 → 정상 홈으로
  브라우저 이동해 **새 PHPSESSID를 발급**받은 후 로그인 계정 증거와 함께 connected 확인.
  모든 이동은 오프라인 합성 페이지의 정상 브라우저 navigation이다. HTTP 클라이언트로
  재인증하지 않았다. Playwright가 HTTP redirect chain의 첫 요청만 가로채는 제약 때문에
  합성 SSO의 이동에는 일반 페이지의 `location.replace`를 사용했다.
- disconnect 뒤 전용 프로필과 암호화 파일 삭제 확인.

위 기록 당시 source/image를 다시 빌드한 뒤 동일 smoke 전체를 다시 통과했다.
검증 컨테이너는 `--rm`으로 종료했으며 사용한 격리 named volume은 별도로 제거했다.

## 비리다이렉트 게스트 HOME의 bounded SSO 후속

명시적 `reauth`, 독립 실행의 자동 tick, 복원 후 최초 자동 방문이 같은 경로를 사용한다.
정상 HOME 방문이 신뢰하는 HTTPS `www.pixiv.net`의 명시적 게스트 DOM으로 끝나고
로그인 폼·challenge·탐색 오류가 없을 때만 공식 `https://accounts.pixiv.net/login`을
같은 context에서 한 번 방문한다. 계정 로그인/MFA 페이지·알 수 없는 DOM·HTTP 오류·
429에는 후속 방문하지 않는다. LOGIN에서 폼이나 추가 인증이 나오면 `needs_login`이며
게스트 HOME으로 돌아와도 재귀적으로 다시 방문하지 않는다. 폼 제출이나 쿠키 삭제는
추가하지 않았고 view·managed navigation·공용 Retry-After 대기를 유지한다.

helper 전용 `.venv`의 `run_tests.py`로 **104개 통과**, Ruff check/format check 및
의존성 검사 통과를 확인했다. 기존 80개에 24개 회귀 사례를 추가했다. HOME이 리다이렉트하지
않아도 기존 SSO 쿠키/localStorage/IndexedDB를 유지한 LOGIN 전환에서 새 PHPSESSID를
얻는 세 진입 경로, 후속 방문 금지 조건, 한 번만 방문하는 경계와 공용 대기를 검증한다.

`smoke_browser.py`도 HOME의 자동 이동을 제거하고 실제 Chromium의 정상 LOGIN SSO 이동을
필수로 하도록 변경했다. challenge/429에서 LOGIN을 방문하지 않고 쿠키와 대기를 유지하는
검사도 추가했다. **이번 후속의 이미지 재빌드와 갱신 smoke 전체가 통합 검증에서도 통과**했다. network none·read-only·UID 1000·새 익명 상태 볼륨으로 실행했고 HOME이 리다이렉트하지 않는 정상 게스트 페이지여도 공식 LOGIN의 기존 SSO로 새 PHPSESSID를 발급받았다. challenge/429 추가 이동 금지·암호화 복원·연결 삭제까지 통과했으며 컨테이너와 익명 볼륨은 자동 제거됐다.
앞 절의 Chromium 통과 기록은 HOME이 자동 이동하던 이전 합성 페이지의 결과다.

다음 PowerShell 명령은 새 helper 이미지를 빌드한 뒤 작업 디렉터리에서 실행한다.
Docker가 매번 새 익명 `/state` 볼륨을 만들고 `--rm` 종료 시 제거한다. 읽기 전용 smoke
파일 하나만 연결하며 실제 키·환경 파일·사용자 자료·기존 볼륨을 연결하지 않는다.
합성 키는 tmpfs에서 생성하고 모든 페이지 요청은 Playwright route로 제공하거나 차단한다.

```powershell
$helperRoot = (Resolve-Path .\browser_helper).Path
docker run --rm --init --network none --read-only --user 1000:1000 `
  --security-opt "seccomp=$helperRoot\vendor\seccomp_profile.json" `
  --shm-size 512m `
  --tmpfs "/run/browser:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=0700" `
  --tmpfs "/tmp:rw,nosuid,nodev,size=256m,mode=1777" `
  --mount "type=volume,destination=/state" `
  --mount "type=bind,source=$helperRoot\smoke_browser.py,target=/opt/pixchive-browser/smoke_browser.py,readonly" `
  --entrypoint python pixchive-browser:local /opt/pixchive-browser/smoke_browser.py
```

## 남은 확인

통합 후속: root-only 0600 원본 키의 권한을 유지하기 위해 시작 시 도메인별 파생 키만
tmpfs에 전달한 뒤 UID/GID 1000으로 영구 전환하는 launcher를 추가했다. 파생 키 검사,
worker가 관리하는 navigation, Retry-After 대기, 기존 세션을 지우지 않는 공식 로그인
화면 이동을 포함해 당시 helper 테스트는 **80개 통과**했으며 위 bounded SSO 후속까지 **104개 통과**했다. 수동 로그인 후속의 현재 결과는 위 161개 기록을 따른다. 앱에서 실제 로그인 화면 연결과
암호화된 상태의 재시작 복원도 확인했으며 그 범위는 상위 docs/validation.md를 따른다.

실제 Pixiv의 SSO 유효 기간·쿠키 정책·현재 로그인 UI·CAPTCHA/MFA 공급자 호환성은
실계정 검증 대상이다. PHPSESSID만 seed한 상태에는 SSO 증거가 없을 수 있으며 정상
전용 로그인 창에서 만든 프로필이 자동 복구에 필요하다. SSO 자체 만료나 추가 인증을
자동으로 우회할 수 없다. 메인 앱 프록시·noVNC UI·운영 마운트는 통합 담당이 검증한다.

통합 재검증: 최종 helper 이미지와 network none·UID 1000·read-only·새 익명 상태 볼륨의 Chromium smoke 전체가 통과했다. 수동 입력과 다른 origin의 저장 상태를 준비하고 200회의 만료 주기·5회의 화면 재연결 동안 새 페이지·snapshot·DOM/쿠키 검사·저장·이동이 모두 0회이며 입력과 암호문이 유지됐다. 완료 시 한 번 검사/저장 후 대기, 세대가 일치하는 resume 후 자동 tick·암호화 복원·SSO 재인증·challenge/429·연결 삭제도 통과했다. 초기 smoke의 내장 메서드 이벤트 핸들러 호환 오류는 일반 함수 핸들러로 고쳐 전체를 다시 실행했다. 운영 자료·키·계정은 이 격리 smoke에 연결하지 않았다.
