# 배포 환경의 Google 가입·로그인 준비 (#107)

B71-OBS-057 · 2026-11-25 · 코드 대조 기준 main `9e003adfa5c38e168ce34434e33dace541c28026`.
기존 OAuth 성공 경로와 [#69 실패 복귀](google-login-failure.md)를 사용한다. 이 작업에서는 실제 환경 파일·자격 증명·Google Console·서버를 읽거나 변경하지 않았다. 아래 운영 절차는 Owner가 승인된 환경에서 수행할 인계이며, 문서 작성이나 합성 테스트 통과를 실제 Google 가입·로그인 성공으로 표시하지 않는다.

## 현재 운영 보고 (2026-11-25 KST)

Owner가 Google Web OAuth 클라이언트를 생성하고 Mac 암호화 금고에 설정을 보관한 뒤, PM-2/Owner가 EC2 backend에 설정을 반영했다고 인계했다. 기존 backend 이미지를 재사용했고 DB/Nginx는 변경하지 않았다는 보고다. public `/api/ready` 200, Google start 302의 목적지 `accounts.google.com`, 정확한 callback `https://next-stop-siren.duckdns.org/api/auth/google/callback`을 확인했다. 아래 Console·주입 절차는 새 환경/설정 변경 때의 참조이며 현재 미생성 상태를 뜻하지 않는다.

이 보고는 문서 Worker가 자격 증명·서버·Google에 접속해 독립 재검증한 결과가 아니다. **실제 신규 Google 가입·기존 계정 재로그인·세션 복구는 아직 미검증**이다. start 302는 code 교환·DB 저장·사용자 로그인 성공의 증거가 아니다. 현재 운영 담당은 Owner `xifoxy-ru`이며 실제 실행은 PM-2/Owner가 관리한다.

## 확인된 구현과 아직 확인하지 않은 것

| 구분 | 현재 근거 | 남은 확인 |
| --- | --- | --- |
| 설정 형식 | `app.security.settings.GoogleSettings.from_env()`가 필수값·callback·독립 키·복귀 Origin을 검증 | 실제 OAuth client 존재, 같은 client의 secret, Console 등록·사용자 권한 |
| 시작·callback | `app.api.google_auth`와 `app.services.google_login`이 state·nonce·PKCE, 서명/claim을 검증한 뒤 저장·쿠키 발급 | 배포 환경에서 Google과 실제 교환·DB 저장·브라우저 세션 복구 |
| 실패·취소 | 고정 cancelled/invalid_request/temporarily_unavailable 복귀와 안전한 503, 기존 세션 보존 | 실제 Google이 보낸 취소/오류와 합성 callback의 결과 구분 |
| 상태 확인 | `/api/ready`는 DB·앱 인증/가입 설정·제품 스키마·worker를 확인 | 선택 기능인 Google 자격 증명·제공자 연결·계정 성공은 검사하지 않음 |
| 배포 설정 | `deploy/backend-entrypoint.py`와 `deploy/production.env.example`의 고정 HTTPS Origin | 설정 반영·start 302는 위 Owner 보고, 실제 값과 서버는 이 문서 Worker가 미조회 |

형식에 맞는 합성 `fixture.apps.googleusercontent.com`과 가짜 secret도 설정 파서를 통과할 수 있다. 이는 외부 자격의 진위를 조회하지 않는 기존 계약이다. `--validate-only`도 외부 서비스를 검사하지 않는다고 출력한다. 이를 Google 운영 준비 완료로 부르거나 실제 값을 추측하지 않는다. 검토한 경로에서 이 구분 자체를 실행 코드 결함으로 판단하지 않았으며 새 OAuth 구현이나 자격 검증 API를 추가하지 않았다.

## 배포 대상의 정확한 URL·설정 조건

아래는 기존 배포 코드가 요구하는 URL이다. 실제 서버·DNS·인증서·Console 설정을 관찰한 결과가 아니다.

| 항목 | 배포 구성 기준 |
| --- | --- |
| 프런트/API Origin | `https://next-stop-siren.duckdns.org` |
| 시작 URL | `https://next-stop-siren.duckdns.org/api/auth/google/start` |
| **Authorized redirect URI / GOOGLE_REDIRECT_URI** | **`https://next-stop-siren.duckdns.org/api/auth/google/callback`** |
| GOOGLE_FRONTEND_ORIGIN | `https://next-stop-siren.duckdns.org` (경로·끝 `/` 없음) |
| AUTH_API_ORIGIN / AUTH_ALLOWED_ORIGINS | 둘 다 위의 동일 HTTPS Origin; 운영 entrypoint는 하나의 정확한 Origin만 허용 |
| AUTH_ENV / AUTH_COOKIE_SECURE | `production` / `true` |

`GOOGLE_CLIENT_ID`는 Web application client ID, `GOOGLE_CLIENT_SECRET`은 그 **동일 client**의 secret이어야 한다. `GOOGLE_TRANSACTION_KEY`는 독립 난수 32바이트의 Base64이며 네 `AUTH_*_KEY` 중 어느 것도 재사용하지 않는다. 실제 값은 저장소·이슈·PR·명령 인자·로그에 넣지 않는다. 이 설정은 프런트 번들에 전달하지 않는다.

callback은 `AUTH_API_ORIGIN + /api/auth/google/callback`과 문자열이 정확히 같아야 한다. `http`, 다른 host/port, `localhost`, 끝 `/`, query·fragment를 섞지 않는다. Console의 **Authorized JavaScript origins** 입력만 채우는 것은 redirect URI 등록을 대신하지 않는다. 현재 서버가 OAuth를 시작하고 callback을 처리하므로 브라우저 Google SDK나 새 client 구현은 필요 없다.

Google을 쓰지 않을 때 자격을 생략해도 비밀번호 인증은 유지된다. Google을 쓰려면 ID·secret·transaction key·redirect URI를 모두 준비하고 배포 entrypoint가 요구하는 frontend Origin도 명시한다. 일반 `/api/ready` 200만으로 이 항목들을 확인했다고 기록하지 않는다.

## Operator: Google Console 설정 절차 (참조)

1. Owner가 승인한 Google Cloud 프로젝트를 선택하고 **Google Auth Platform**을 연다. 새 프로젝트·청구 계정·유료 서비스를 임의로 만들지 않는다.
2. **Branding**에서 앱 이름·지원 이메일·개발자 연락처와 필요한 동의 화면 항목을 확인한다. **Audience**에서 Internal/External과 Publishing status를 확인한다. External의 Testing 상태라면 실제 파일럿 Google 계정을 **Test users**에 등록해야 한다. 조직 내부 전용이면 허용 조직 계정인지 확인한다. 임의로 Production 게시나 scope 확대를 하지 않는다.
3. **Data Access**에서 기존 코드의 `openid`, `email` 범위를 대조한다. 새 권한은 요구하지 않는다. 정책상 추가 검증이 필요하면 해당 Console 안내와 Owner 판단을 따른다.
4. 새 환경에서 웹 클라이언트가 없다면 Owner가 **Clients → Create client → Application type: Web application**으로 파일럿용 OAuth client를 만든다. client 이름은 비밀이 없는 식별 이름으로 정한다. 다른 유형의 client나 다른 프로젝트의 secret을 혼합하지 않는다. 실제 생성은 Owner의 승인된 운영 범위에서 수행하며 이 문서 작업에서는 실행하지 않는다.
5. **Authorized redirect URIs → Add URI**에 위 HTTPS callback 전체 문자열을 등록·저장한다. 개발용 callback을 함께 쓸 때는 별도 허용 항목으로 정확히 등록하며 배포 값을 개발 주소로 바꾸지 않는다. 변경 전후 확인은 URI·client 유형·프로젝트 식별에 필요한 최소 비밀 없는 기록으로 남긴다.
6. 동일 client의 ID·secret을 승인된 비밀 보관 경로로 전달한다. JSON 다운로드·secret 화면·네트워크 캡처를 공개 증거에 붙이지 않는다. Console 저장/전파가 끝난 뒤 다음 서버 설정 단계로 넘긴다.

Google Console UI 이름은 계정·언어에 따라 다를 수 있다. callback의 최종 값과 테스트 사용자 허용 여부를 기준으로 확인한다. 이 문서는 Console 실제 변경을 수행하지 않았다.

## Operator: 서버 주입·형식 확인·재생성 (참조)

Owner/PM-2가 승인된 운영 절차로 기존 `/etc/next-stop-siren/production.env`에 필요한 Google 값만 주입한다. 다른 DB·인증 키를 교체하거나 파일을 예시로 덮어쓰지 않는다. 파일 소유·권한과 secret 보관은 기존 [배포 지침](../../deploy/README.md#서버-설정과-실패-경계)을 따른다. 입력 파일을 shell에서 source하거나 `cat`, 전체 `docker inspect`, `compose config` 출력으로 공유하지 않는다.

기존 backend 이미지의 `--validate-only`는 production/Auth/Origin/Google 형식을 검사한다. Google 값이 모두 생략된 비밀번호 전용 구성도 통과하므로, #107을 확인할 때는 아래처럼 **GoogleSettings도 명시적으로** 검사한다. 다음 코드는 승인된 backend 컨테이너 내부의 Python에서 현재 주입된 환경을 검사할 때 사용할 본문이다. 이 문서 작업에서 실행한 서버 명령이 아니다.

```python
import os
from app.security.settings import AuthSettings, AuthSettingsError, GoogleSettings

try:
    auth = AuthSettings.from_env(os.environ)
    google = GoogleSettings.from_env(os.environ, auth_settings=auth)
    origin = "https://next-stop-siren.duckdns.org"
    if (
        auth.environment != "production"
        or not auth.cookie_secure
        or auth.api_origin != origin
        or auth.allowed_origins != (origin,)
        or google.redirect_uri != origin + "/api/auth/google/callback"
        or google.frontend_origin != origin
    ):
        raise ValueError
except (AuthSettingsError, ValueError):
    raise SystemExit("Google configuration format rejected (values hidden).") from None
print("Google configuration format valid; real Google login NOT VERIFIED.")
```

환경 파일 수정만으로 실행 중인 컨테이너 환경이나 앱 생성 때 읽은 설정은 바뀌지 않는다. 승인된 배포 경로에서 backend를 **재생성**해 env_file 변경을 반영하고, backend 교체에 맞춰 Nginx upstream도 기존 배포 절차대로 갱신한다. 단순 `docker compose restart`는 바뀐 env_file을 다시 주입하지 않는다. 임의 Compose 명령·이미지·DB 변경을 이 문서로 승인하지 않는다. 정확한 release/config/gate를 검토한 뒤 PM-2가 실행하며, 새 컨테이너·이미지 revision과 형식 검사 결과만 비밀 없이 기록한다.

## 실제 Google 가입·로그인과 독립 확인 (미실행)

1. 승인된 파일럿 계정과 HTTPS 화면에서 **Google로 로그인**을 눌러 전체 페이지 이동을 시작한다. Swagger Execute·callback URL 직접 열기는 실제 성공 확인을 대신하지 않는다.
2. 새 Google 신원으로 성공하면 사용자·Google identity·refresh session을 기존 코드가 저장하고, callback은 인증 쿠키를 설정한 뒤 위 frontend Origin의 `/`로 302 복귀해야 한다. 동일 이메일의 로컬 계정과 자동 연결하지 않는다.
3. 화면의 csrf → refresh → me 복구 뒤 회원 화면, 새로고침 세션 유지, 로그아웃을 확인한다. 새 회원 홈의 중립 진입은 [#108](https://github.com/next-stop-siren/Next-Stop-Siren/issues/108)/[#109](https://github.com/next-stop-siren/Next-Stop-Siren/issues/109) 결과와 대조한다. 이 단계에서 대화를 자동 생성해야 한다고 요구하지 않는다.
4. 같은 Google 계정으로 다시 로그인해 기존 사용자로 복구되고 중복 Google 신원이 생기지 않는지 승인된 최소 DB/앱 확인으로 검증한다. 다른 사용자 기록이 보이지 않아야 한다. 실제 DB 조회·계정 생성은 별도 운영 범위에서만 수행한다.
5. 기존 실패·취소 복귀, 실패 시 기존 세션 유지, 트랜잭션 쿠키 정리와 비밀값 비노출을 [실패 안내](google-login-failure.md)에 따라 확인한다. 합성 `access_denied`와 실제 Google이 보낸 취소를 같은 증거로 세지 않는다. 실제 Google에서 취소 callback을 관찰하지 못했으면 미확인으로 남긴다.
6. 구현자가 아닌 확인자가 대상 revision·환경·시각·성공/실패·미실행을 기록한다. token/code/state/cookie/secret·실제 이메일·대화 원문·원시 Network 캡처는 첨부하지 않는다. 상태 코드·복귀 host/path·세션/쿠키 존재 여부와 검증자만으로 요약한다.

## 로컬 회귀와 완료 인계

기존 `backend/tests/test_google_settings.py`, `test_google_start_api.py`, `test_google_callback_api.py`, `test_google_cookies.py`, `test_google_login_verification.py`는 합성 설정·서명 토큰·대체 HTTP·가짜 저장 경계로 성공·실패를 확인한다. DB 테스트와 실제 계정·Google 네트워크는 별도다. 기존 의존성 환경에서 backend 디렉터리를 기준으로 다음을 실행한다.

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/existing/venv/bin/python -m pytest -p no:cacheprovider tests/test_google_settings.py tests/test_google_start_api.py tests/test_google_callback_api.py tests/test_google_cookies.py tests/test_google_login_verification.py -q
```

[공정진행도](../02-team/development-flow.md#현재-수정-작업과-인계-2026-11-25)와 #107 인계에서는 문서·형식/합성 검사 결과, Console·서버 실제 설정 여부, 실제 신규/기존 Google 로그인 결과, 실패 보존·독립 확인을 각각 기록한다. 문서·테스트 완료로 미실행 운영 단계를 완료 처리하지 않는다.
