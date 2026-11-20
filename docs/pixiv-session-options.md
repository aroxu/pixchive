# Pixiv 로그인 연결·쿠키 갱신 조사

2026-10-27 조사. 이 문서는 구현 제안이며 제품 인증 방식 변경이나 로그인 호환성 검증 완료를 의미하지 않는다. 실행 DB·등록 쿠키·키·브라우저 로그인 세션은 열람하지 않았다.

후속: 같은 날 사용자가 Playwright·noVNC와 자동 재인증 구현을 승인했다. 선택적 전용 브라우저, 암호화된 상태 보관·복원, 쿠키 교체 및 인증 대기 작업 재개를 구현했다. 아래는 선택 당시 조사 기록이며 실제 실행 범위는 [검증 기록](validation.md), 현재 사용 방법은 [실행 안내](README.md)·[운영 안내](operations.md)를 따른다.

## 확인한 사실

- 익명 HTTP 요청으로 `https://accounts.pixiv.net/login`과 `https://www.pixiv.net/`에서 각각 HTTP 200 및 `X-Frame-Options: SAMEORIGIN`을 확인했다. Pixchive 페이지에서 Pixiv 로그인 페이지를 직접 iframe으로 표시하는 방식은 이 응답 정책에서 차단된다. [X-Frame-Options 의미](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Frame-Options)
- 별도 창으로 여는 것은 가능하지만 Pixchive 웹 JavaScript가 다른 출처의 Pixiv DOM/쿠키를 읽을 수는 없다. 쿠키에 HttpOnly가 설정돼 있으면 해당 출처의 일반 JavaScript에서도 값을 읽을 수 없다. [동일 출처 정책](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy), [쿠키 속성](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Cookies)
- Pixiv는 일정 기간 활동이 없거나 이용 환경에 따라 재로그인을 요구할 수 있다고 안내한다. 요청을 주기적으로 보내면 영구 로그인된다는 보장이나 PHPSESSID 전용 공식 refresh API는 이번 조사에서 확인하지 못했다. [Pixiv 도움말](https://www.pixiv.help/hc/en-us/articles/235583068-Why-am-I-asked-to-log-in-every-time)

## 현재 코드의 보완점

`app/adapters/pixiv/web.py`의 `WebBridge.request()`는 최초 `self.cookie`를 명시적인 Cookie 헤더에 계속 사용한다. 응답의 새 PHPSESSID를 추출해 다음 요청·부모 프로세스·암호화 DB에 반영하는 경로는 없다. 실제 Pixiv가 어떤 조건으로 새 값을 발급하는지는 별도 실계정 PoC가 필요하다.

현재 bridge는 로그인 브라우저가 아닌 JSON-lines 자식 프로세스다. 갱신 값을 전달하려면 `web.py`·`bridge.py`·`live.py`·`accounts.py`·`collection.py`가 중심 변경 범위다. 공개 HTTP API나 로그에 쿠키를 반환하지 않고 비공개 프로세스 통신 안에서 처리해야 한다.

`accounts.store_credential()`은 같은 쿠키도 새로 암호화해 기존 값을 교체한다. `collection.guard()`는 시작 당시 암호문과 현재 DB를 비교하므로, 단순 주기 저장은 진행 중 작업을 `credential_changed`로 중단시킬 수 있다. 동일 값의 재저장 방지, 이전 자격 증명 버전 일치 조건의 교체, 실행 중 작업의 안전한 연결 전환/체크포인트 재개가 함께 필요하다. 기존 계정 일치 검사는 유지한다.

만료 시 `waiting_auth`, 검증 성공 후 `resume_waiting_auth()`와 미완료 다운로드 재개는 이미 있다. 새 연결 경로에서도 이 서비스를 재사용할 수 있다.

## 가능한 구성 비교

| 구성 | 할 수 있는 일 | 제한 |
| --- | --- | --- |
| 서버 응답의 Set-Cookie 반영 | Pixiv가 새로 발급한 PHPSESSID를 요청·DB에 이어서 사용 | 발급되지 않은 새 세션을 만들거나 폐기된 세션을 복구하는 기능은 아님 |
| 사용자 브라우저 확장 | 로그인/갱신으로 쿠키가 바뀌면 Pixchive로 자동 전달 | 확장을 설치한 브라우저가 실행 중이어야 함. cookies 및 Pixiv 호스트 권한 필요 |
| 서버 전용 로그인 브라우저 | 사용자가 공식 Pixiv 화면에서 로그인하고 서버가 해당 브라우저의 쿠키를 자동 반영 | 별도 브라우저 프로필·화면 전달 서비스와 수명/접근 관리 필요. Pixiv 로그인 호환성 PoC 필요 |

확장은 `chrome.cookies.get()`·`onChanged`를 사용할 수 있다. 권한 범위와 실제 전달 대상을 분리해 PHPSESSID 하나와 사용자가 연결한 Pixchive 서버만 처리하도록 구성한다. 브라우저의 삭제/overwrite 이벤트를 바로 계정 해제로 해석하지 않고 새 값의 유효성을 확인해야 한다. [Chrome cookies API](https://developer.chrome.com/docs/extensions/reference/api/cookies)

전용 브라우저는 Playwright의 persistent context와 전용 user-data 디렉터리를 사용한다. 평소 쓰는 Chrome 기본 프로필을 직접 자동화하지 않는다. 브라우저 화면은 noVNC 같은 화면 전달 계층으로 Pixchive 안에 표시할 수 있다. 이때 iframe의 대상은 Pixiv 문서가 아니라 Pixchive의 화면 뷰어다. 아래 흐름은 두 도구의 기능을 조합한 설계 제안이며 Pixiv 실계정에서 이 구성으로 로그인한 검증은 하지 않았다. [Playwright persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context), [브라우저 쿠키 조회](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-cookies), [noVNC](https://github.com/novnc/noVNC)

## 제안하는 구현 방향

상시 실행하는 셀프호스팅 환경과 앱 안에서 로그인하려는 요구에는 **선택적 전용 로그인 브라우저**가 맞는다. 서버 부담을 줄이고 평소 PC 브라우저의 로그인을 재사용하려는 경우에는 확장을 대안으로 둔다. 두 경로 모두 서버 응답의 쿠키 교체 처리와 안전한 저장/재개 서비스를 공유한다.

1. 설정의 로그인 버튼으로 Pixiv 전용 브라우저 화면을 연다. 사용자는 공식 사이트에서 로그인하고 추가 인증을 직접 완료한다. Pixiv 비밀번호 입력 API나 앱 DB 비밀번호 저장은 만들지 않는다.
2. 서버의 브라우저 제어 프로세스가 PHPSESSID를 취득하고 기존과 같은 Pixiv 계정인지 확인한 뒤 기존 키로 암호화한다. 브라우저 프로필도 인증 자료로 취급해 보호하며 공개 정적 파일·일반 내보내기에 포함하지 않는다.
3. 새 쿠키가 관찰되면 같은 값의 불필요한 저장을 피하고 조건부로 반영한다. Pixiv가 더 이상 세션을 인정하지 않으면 연결 필요 상태로 바꾸고 재로그인을 안내한다. 만료된 값을 반복 전송하는 것을 갱신 성공으로 표시하지 않는다.
4. 재로그인·계정 검증이 끝나면 인증 대기 작업이 기존 체크포인트에서 이어지게 한다. 화면 전달과 쿠키 수신은 기존 관리자 인증 경계를 유지한다. 확장 연계에는 별도의 제한된 연결 권한을 설계하고 Origin/CSRF 검사를 넓혀 우회하지 않는다.

이 구성이 없애는 것은 수동 쿠키 복사·붙여넣기다. Pixiv가 추가 인증이나 재로그인을 요구하는 경우까지 영구 무인 처리된다고 약속하지 않는다.

## OAuth와의 관계

Pixiv 앱 API 클라이언트의 refresh token 갱신은 bearer access token을 얻는 별도 흐름이다. PHPSESSID 갱신 API로 대체해서 사용할 수 없다. 앱 API로 전환한다면 북마크·태그 번역·원본·우고이라 계약을 다시 검증해야 하며 현재 쿠키 어댑터 유지 요구와 별도 결정이다. [PixivPy 인증 구현](https://github.com/upbit/pixivpy/blob/master/pixivpy3/api.py)

이번에는 소스 조사·공식 문서 확인·익명 응답 헤더 확인만 수행했다. 제품 코드·설정·API 계약은 바꾸지 않았고 자동 테스트나 새 로그인/쿠키 취득을 실행하지 않았다.
