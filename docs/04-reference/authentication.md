# 인증 구현 기준

**Rev 4 · 2026-10-30 (KST)** · 코드 기준: `40035b68217c21913ceb85da704cfc842272f24c`

변경 근거: [PR #47](https://github.com/next-stop-siren/Next-Stop-Siren/pull/47) · [PR #48](https://github.com/next-stop-siren/Next-Stop-Siren/pull/48) · [PR #50](https://github.com/next-stop-siren/Next-Stop-Siren/pull/50) · [PR #62](https://github.com/next-stop-siren/Next-Stop-Siren/pull/62) · [PR #79](https://github.com/next-stop-siren/Next-Stop-Siren/pull/79). [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69)의 서버 구현 `8921f8a`와 검증 `1cbf709`를 반영했다. Rev는 이 문서의 계약·상태·범위 변경을 추적한다. 유지 규칙은 [협업 흐름](../03-development/workflow.md#문서-rev와-변경-이력)을 따른다.

[#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74)의 명시 프록시 신뢰 연결 `4979a9067636b815a66ecc97ba285af6b10faef1`과
실제 Nginx 경유 검사 `40035b68217c21913ceb85da704cfc842272f24c`를 반영했다. 해당 변경의 PR은 아직 생성하지 않았다.
이전 Rev의 코드·PR 근거는 변경 이력에 보존한다.

[회원 채팅 목표](chat-policy.md)의 접수·조회·구독·실행 전 취소·재시도·건너뛰기도 검증된 회원과 대화 소유권으로 제한한다. #19는 이 경계와 비공개 화면·구독 격리를 확인한다.

로그아웃 시 화면의 대화·임시 출력·구독을 제거하되 서버 작업은 취소하지 않는다. 기존 access·refresh 유효성과 잔여 access 규칙은 유지한다.

이 문서는 승인된 D1 A·B·C 정책의 팀 구현 기준이다. 앱 JWT는 PyJWT, Google OAuth/OIDC는 Authlib, 비밀번호 해시는 `argon2-cffi`를 선택했다. 세 라이브러리는 현재 잠금 의존성에 포함된다. [기술 구성](../01-start/stack.md)에 현재 상태를 정리했다. 경로별 요청·응답은 [공통 API 형식](api.md), 저장 제약은 [DB 설계](database.md)를 따른다. 로컬·Google 인증, Bearer 현재 사용자, CSRF·refresh·logout 경로와 관련 검사가 구현되어 있다. 화면 인증 API·세션 복구는 PR #50에서 연결됐다. Google 실패의 서버 복귀 처리는 #69에서 구현·검증했다. 실패 화면 연결, 실제 Google 전체 흐름, 독립 통합·운영 검증은 별도다. 운영 Origin·키·Google 자격 증명과 실제 프록시 신뢰 범위는 배포 전에 확인한다. 단일 프로세스 로그인 제한의 적용 경계와 로컬 검증은 [프록시 뒤 로그인 제한](#프록시-뒤-로그인-제한)을 따른다.

여기서 **확인된 사용자 정보(principal)**는 서버가 로그인 자격을 검사해 얻은 사용자 ID다. **계보(lineage)**는 한 번의 로그인에서 시작해 refresh 토큰을 교체하며 이어진 세션들의 연결이다.

## 빠른 동작표

| 상황 | 브라우저·서버 동작 |
| --- | --- |
| 로컬 가입·로그인 | 공개 사용자와 15분 access JWT를 본문에 반환하고 refresh·CSRF 쿠키를 설정한다. |
| 가입 제한 | 서버 전체 기본 10건/600초·동시 처리 1건. 접수량 초과 429·처리 자리 부족 503과 `Retry-After`를 반환하며 대기열은 없다. |
| 보호 API | 메모리의 access를 `Authorization: Bearer`로 보낸다. 쿠키는 인증 수단이 아니다. |
| 새로고침 | `/api/auth/csrf`에서 CSRF 값을 받은 뒤 `/api/auth/refresh`를 호출해 새 access를 메모리에 둔다. |
| 로그아웃 | 현재 refresh 계보와 쿠키만 폐기한다. 이미 발급된 access는 최대 15분 유효하다. |
| Google | 서버 시작 OIDC → 성공 시 refresh·CSRF 쿠키와 허용 경로 복귀 → 화면에서 access 획득. 실패는 고정 코드로 복귀하며 설정·응답 준비 불가 시 503 JSON. |
| 탭 경합 | 낡은 refresh와 짝인 CSRF는 5초 안에 409, 창 밖 재사용은 계보 폐기·401. 새 refresh 쿠키와 낡은 CSRF 헤더는 403이다. |

## 화면 흐름

### 로컬 가입·로그인

1. 화면이 정확히 허용된 Origin에서 JSON 이메일·비밀번호를 제출한다. 로컬 이메일은 앞뒤 공백을 제거하고 DB `lower(email)` 판정에 맞춰 비교한다. 형식과 최대 320자를 확인한다.
2. 비밀번호는 원문 그대로 Unicode 코드 포인트 12~128자와 UTF-8 1024바이트 이하를 확인한다. 입력 정규화·절단은 하지 않는다. 위반은 422다.
3. 가입은 해시 시작 전에 [가입 접수와 해시 제한](#가입-접수와-해시-제한)을 적용한다. 초과 응답과 재시도 안내는 [API의 가입 제한](api.md#가입-제한)을 따른다.
4. 성공하면 공개 사용자와 access를 화면 메모리에 둔다. refresh와 CSRF는 각각 `HttpOnly` 쿠키로 받는다. 로그인 실패의 없는 계정과 틀린 비밀번호는 같은 401 본문과 비슷한 처리 시간이다.
5. 로그인은 로컬 이메일별 실패 5회/10분, IP별 실패 30회/10분을 넘으면 429와 `Retry-After`를 받는다. 성공하면 해당 이메일 실패 수를 초기화한다. 로그인 실패 제한과 가입 접수 제한은 별도로 관리한다.

미인증 로컬 이메일은 주소 소유의 증거가 아니다. 같은 이메일의 Google 계정은 별도 사용자다. 이메일 인증·비밀번호 찾기·계정 연결은 초기 범위에 없다.

### 새로고침·회전

**회전**은 현재 refresh를 폐기하고 새 refresh 하나로 교체하는 것이다.

1. 화면 시작 시 쿠키를 포함하도록 credentials를 설정해 `GET /api/auth/csrf`를 호출한다. 본문 값을 메모리에 둔다. 이 조회만으로 access가 발급되지는 않는다.
2. 정확히 허용된 `Origin`과 `X-CSRF-Token`을 붙여 `POST /api/auth/refresh`를 보낸다. `Origin`은 요청을 시작한 화면의 출처이고, CSRF 검사는 다른 사이트에서 쿠키를 이용해 요청하는 일을 막는다. 서버는 헤더·CSRF 쿠키 일치 및 현재 refresh `jti`와의 연결을 검사한다.
3. 서버는 같은 사용자 행을 잠근 짧은 DB 트랜잭션에서 유효한 JWT와 DB digest를 확인하고, 이전 refresh 폐기와 새 refresh 하나의 삽입을 함께 커밋한다. 커밋은 트랜잭션의 변경 내용을 확정하는 단계다. 커밋 성공 후에만 새 refresh·CSRF 쿠키와 access를 발급한다. access는 탭마다 자기 메모리에 둔다.
4. 403 또는 409를 받으면 잠시 기다린 뒤 `/csrf` 값을 다시 읽고 refresh를 한 번만 재시도한다. 원 요청을 포함해 POST는 최대 두 번이다. 재실패하면 자동 반복을 멈추고 재로그인을 안내한다.

### 로그아웃

1. 정확한 `Origin`, 현재 CSRF 헤더와 쿠키를 붙여 `POST /api/auth/logout`을 보낸다.
2. 서버는 같은 사용자 잠금 안에서 현재 refresh의 계보만 폐기하고 refresh·CSRF 쿠키를 만료시킨다. 화면은 access·CSRF 메모리를 비운다.
3. 다른 독립 로그인 세션은 유지된다. access 차단 목록이 없으므로 이미 발급한 access는 최대 15분 더 통과할 수 있다.

### Google 로그인

1. 화면은 서버의 `/api/auth/google/start`로 top-level 이동한다. 서버는 요청과 callback을 연결하는 `state`, ID 토큰을 해당 로그인에 묶는 `nonce`, code 교환을 보호하는 PKCE(S256)를 만든다. 서버는 state·nonce·PKCE verifier를 암호화·무결성 보호된 짧은 수명의 앱 호스트 전용 트랜잭션 쿠키에 담아 설정한다. 그런 다음 브라우저를 Google로 이동시킨다.
2. `/api/auth/google/callback`은 일회용 code를 교환하고 state·nonce·verifier, JWKS 서명·허용 알고리즘·`iss`·client `aud`·`exp`·`iat`·`nonce`·`email_verified=true`·필수 이메일을 검증한다. 검증된 `(issuer, subject)`로만 사용자를 찾는다.
3. 성공 시 서버는 refresh·CSRF 쿠키를 발급하고 아래 설정에 따라 복귀한다. callback URL은 API `:8000`이며 host-only 쿠키는 같은 호스트의 포트 사이에서 유지된다.
4. 화면은 `/api/auth/csrf` → `/api/auth/refresh`로 access를 받아 메모리에 둬야 한다. 화면 세션 복구는 PR #50에 구현됐다. 실패의 서버 리다이렉트는 #69에서 추가했으며 실패 안내·재시도 화면 연결과 #19의 인증·소유권 통합 검증은 남아 있다. redirect URL·쿠키에 access를 넣지 않는다.

| `GOOGLE_FRONTEND_ORIGIN` | 성공 및 실패 복귀 |
| --- | --- |
| 검증된 Origin 명시 | 설정 Origin + 성공의 허용 상대 `return_to` 또는 실패의 고정 `/`와 query; 로컬 예시 `http://127.0.0.1:5173/` |
| 설정 항목 없음 | 기존 상대 경로 유지, API `:8000`에 남음 |
| 잘못된 값 명시 | Google 경로만 차단, 로컬 인증 유지 |

Origin은 API와 scheme·hostname이 같고 `AUTH_ALLOWED_ORIGINS`에 정확히 포함되어야 한다. 기존 `.env`는 수동 추가 후 API를 재시작한다. Google 시작은 Swagger Execute 대신 브라우저 전체 이동을 사용한다.

트랜잭션 쿠키의 삭제만으로 callback 단일 사용을 보장하지 않는다. 제공자의 code 1회 교환과 state/nonce 검증에 의존한다. 중복 callback 또는 code 교환 실패에는 세션을 발급하지 않는다.

### Google 실패 복귀

서버는 실패 시 `/`에 고정 query `google_login_error`를 붙여 302로 복귀한다.
유효한 트랜잭션에서 받은 취소는 `cancelled`, 요청·트랜잭션·신원 검증 실패는 `invalid_request`,
제공자·저장 등 일시 장애는 `temporarily_unavailable`로 분류한다.
코드별 상세 조건과 422·503 대체 응답은 [API 계약](api.md#google-실패-복귀-계약)을 따른다.

취소와 제공자 callback 오류도 중복·모순 파라미터, 선택 issuer, 쿠키·state·만료를 검사한 뒤 분류한다.
성공의 state·nonce·PKCE·서명·claim 검증은 유지한다. callback의 return_to와 요청 헤더로 복귀 주소를 정하지 않는다.
실패 복귀의 code는 안내용이며 URL을 조작해도 인증 성공·강제 로그아웃의 근거가 되지 않는다.

실패 응답은 트랜잭션 쿠키만 삭제하고 기존 refresh·CSRF 쿠키와 다른 로그인 세션을 유지한다.
커밋 후 응답 준비가 실패하면 부분 쿠키 응답을 폐기하고 이번 세션만 보상 취소한다.
보상 취소가 실패한 경우에는 안전한 로그를 남기며 세션 잔존 없음이나 취소 성공을 주장하지 않는다.
인증 또는 Google 설정이 없거나 redirect·쿠키 정리 응답을 준비하지 못하면 503 JSON으로 대체한다.

실패 응답은 no-store·no-cache·no-referrer를 적용한다. 화면 복귀 URL·안내·앱 로그에 code·토큰·내부 예외 원문을 넣지 않으며,
서버 Uvicorn·HTTPX 로그에서는 start와 callback의 query를 제거한다. 배포 프록시 로그는 별도 확인한다.
서버 trace ID는 로그와 JSON 대체 응답에만 두고 실패 복귀 query에 넣지 않는다.

화면의 실패 안내·다시 로그인·URL 값 소비와 접근성 처리는 프런트 담당 범위다.
기존 유효 세션이 남을 수 있으므로 화면은 세션 복구 결과와 실패 안내를 구분해야 한다.
[프런트 연동과 수동 확인](../03-development/google-login-failure.md)에 재현 방법과 인계 조건을 정리했다.

## 보안 경계와 구현 참고

| 항목 | 구현 기준 |
| --- | --- |
| 비밀번호 해시 | Argon2id, 메모리 64 MiB·반복 3·병렬성 1·salt 16바이트·해시 32바이트. 승인된 Python 라이브러리는 `argon2-cffi`; 현재 잠금 버전은 25.1.0이다. |
| Access JWT | 15분, 메모리 전용. URL·저장소·쿠키 금지. |
| Refresh JWT | 최초 로그인부터 절대 14일. 회전 후임은 루트 `expires_at`을 상속하고 새 `jti`를 가진다. 남은 시간이 없으면 재로그인한다. |
| JWT 검증 | 종류별 서로 다른 256비트 이상 HS256 키, 승인 `kid`만 허용. `iss`, 앱 전용 `aud`, 십진 문자열 `sub`, `iat`·`nbf`·`exp`·`jti`·`token_type`을 검증한다. access/refresh 교차 사용 금지. |
| DB digest | 별도 비밀키의 `HMAC-SHA-256(refresh JWT)` 32바이트만 `refresh_sessions.token_digest`에 저장한다. 원문 토큰 저장 금지. |
| Refresh 쿠키 | `HttpOnly`, 호스트 전용(`Domain` 없음), `SameSite=Lax`, `Path=/api/auth`, 운영 `Secure`. `Expires`는 루트 절대 만료, 회전 `Max-Age`는 남은 초다. |
| CSRF 쿠키 | 별도 `HttpOnly`·호스트 전용·`SameSite=Lax`·`Path=/api/auth`·운영 `Secure`; 서버 MAC 값을 현재 refresh `jti`에 결박한다. 본문으로 받은 값을 화면 메모리에 둔다. |
| Google 트랜잭션 쿠키 | 시작·callback 모두 포함하는 `/api/auth` 경로, 호스트 전용 `HttpOnly`·운영 `Secure`·`SameSite=Lax`; state/nonce/PKCE verifier를 암호화·무결성 보호한다. |
| Origin·CORS | 세션 쿠키를 설정하는 로컬 가입·로그인과 refresh·logout은 정확한 허용 `Origin`을 요구한다. 쿠키 인증 변경 요청에서 Origin 누락·불일치는 거부한다. CORS는 명시된 정확한 클라이언트 Origin에만 credentials를 허용한다. |

개발 인증은 `127.0.0.1`을 기준으로 API 주소 `http://127.0.0.1:8000`과 허용 Origin `http://127.0.0.1:5173,http://127.0.0.1:8000`을 사용한다. 화면과 Swagger UI의 실제 Origin을 모두 허용하며 `localhost`는 제외한다. 개발용 HTTP `127.0.0.1`에서만 `Secure=false`를 허용한다. CORS credentials도 명시된 Origin과 정확히 일치해야 한다. Google callback은 `http://127.0.0.1:8000/api/auth/google/callback`을 등록한다. 운영 프론트와 API가 서로 다른 사이트라면 `SameSite=Lax`를 다시 결정해야 한다. Bearer만 쓰는 채팅 요청에는 쿠키 CSRF 절차가 적용되지 않는다.

비밀번호 평문, 원문 토큰, 제공자 code 및 비밀은 DB·로그·추적에 남기지 않는다.

### 가입 접수와 해시 제한

[#71 정책 제안](https://github.com/next-stop-siren/Next-Stop-Siren/issues/71#issuecomment-6414521428)과
[Owner 승인](https://github.com/next-stop-siren/Next-Stop-Siren/issues/71#issuecomment-6420826180)에 따라
PR #79에서 가입 해시 전 접수량·동시 처리 제한을 구현했다.
t2.micro 한 대·2~3인 파일럿의 백엔드 단일 프로세스·worker·복제본을 전제로 한다.

| 환경변수 | 생략 시 기본값 | 적용 범위 |
| --- | --- | --- |
| `AUTH_REGISTER_LIMIT` | `10` | 서버 전체 시간창 안의 가입 접수 상한 |
| `AUTH_REGISTER_WINDOW_SECONDS` | `600` | 최근 접수를 집계할 시간창(초) |
| `AUTH_REGISTER_MAX_CONCURRENT` | `1` | 가입 해시·저장·쿠키 설정 또는 실패 보상 종료까지 유지할 처리 자리 수 |
| `AUTH_REGISTER_BUSY_RETRY_SECONDS` | `5` | 처리 자리 부족 응답의 재시도 권고 초 |

네 값은 양의 정수만 허용한다. 항목 생략 시 기본값을 쓰며 빈 값·0·음수·소수·공백이 있는 값은 거부한다.
앱 생성 때 읽어 고정하므로 변경 뒤 백엔드를 재시작한다. 잘못된 설정은 가입을 `503 temporarily_unavailable`로 차단하며,
기존 로그인·상태 확인 경로는 유지한다. 예시 값은 루트 `.env.example`에 있다.

Origin·입력 검사 후 경과 시간 기준의 만료 기록 제거, 접수량 확인, 처리 자리 확보와 접수 기록을 원자적으로 수행한다.
접수 후 시간창 이상 지난 기록은 제외한다. 접수량과 처리 상한에 모두 걸리면 접수량 초과를 우선한다.
대기열은 두지 않으며, 접수 전 거절은 횟수에 포함하지 않는다. 접수 후 중복 이메일·해시·저장·쿠키 실패는 횟수에 유지한다.
HTTP 오류 코드·본문·`Retry-After`는 [API의 가입 제한](api.md#가입-제한)에 정의한다.

처리 자리는 실제 동기 해시부터 저장·쿠키 설정 또는 실패 보상 종료까지 유지한다.
연결이 끊겨도 실행 중인 해시보다 먼저 자리를 반환하지 않으며, 종료 시 정상·예외 경로 모두 자리를 반환한다.
응답을 잃은 가입도 저장될 수 있으므로 같은 이메일 재제출은 기존 중복 판정으로 보호한다.
Argon2 강도와 입력 규칙, 사용자·최초 세션의 저장 원자성 및 커밋 후 토큰·쿠키 반환 순서는 유지한다.

카운터는 프로세스 메모리에 저장하고 재시작 시 초기화한다. 한 사용자가 전체 접수 한도를 소진할 수 있으며 IP별 제한은 이 정책에 포함하지 않는다.
여러 worker·복제본 사이에는 공유되지 않으므로 #73의 실행 구성과 맞추고 확장 전 공유 제한 방식을 재검토한다.
합성 동시 가입과 TCP 연결 종료·저장 실패 검사는 실제 작은 호스트의 처리량 측정과 구분한다.
메모리·CPU 크레딧·지연의 실제 구성 측정은 #76에서 수행한다.

### 프록시 뒤 로그인 제한

#74는 운영 Uvicorn의 `--proxy-headers --workers 1`과 명시적인 `FORWARDED_ALLOW_IPS`를 사용한다.
운영 진입점은 프록시 신뢰 목록이 없거나 잘못되면 시작 전에 거절한다. 허용 형식·운영 env·실행 방법은
[운영 프록시 설정](../../deploy/README.md#프록시로그)과 [비밀값 없는 env 예시](../../deploy/production.env.example)를 따른다.
실제 목록은 Owner가 확인한 Nginx 연결 주소/전용 네트워크로 확정한다. CIDR 문법 검증은 신뢰 범위의 승인이 아니다.

Nginx는 X-Forwarded-For를 실제 연결 상대 주소 하나로 덮어쓰고 Forwarded를 제거한다.
서버는 신뢰한 연결의 전달 주소만 ASGI client에 반영하며 앱은 최종 IP를 검증해 기존 제한 키로 사용한다.
직접 접속·비신뢰 상대의 전달 헤더와 X-Real-IP/Forwarded는 IP 키로 채택하지 않는다.
유효하지 않은 최종 IP는 기존 503으로 거절한다. 신뢰 상대의 빈/누락 XFF는 프록시 IP로 남고,
중복 XFF는 마지막 필드를 사용하는 Uvicorn 기본 동작이므로 Nginx의 덮어쓰기·접근 경계를 함께 검증해야 한다.

이메일별 5회/600초·IP별 30회/600초, 동시 검사 자리 예약, 성공 시 이메일 실패만 초기화,
내부 오류의 실패 횟수 미소모와 기존 429/Retry-After 정책은 유지한다.
로그인 실패·가입 접수 카운터는 프로세스 메모리에 있어 재시작 시 초기화되고 worker/replica 간 공유되지 않는다.
단일 worker/replica·교체 시 동시 서비스 금지·재시작 초기화의 운영 수용은 Owner 확인 대상이다.
외부 명령 override나 scale을 기술적으로 차단했다고 보지 않으며, 확대 전 공유 제한을 검토한다.
새 공유 저장소나 의존성은 추가하지 않았다.

실제 TCP 검사와 Nginx→backend→합성 PostgreSQL의 두 source IP 검사를 수행했다.
로컬 통합 13개 항목·로그인 80건에서 위조/빈/중복 헤더, 동시 실패·정상 로그인·초기화·재시도와 재생성을 확인했다.
세부 결과·재현 방법·검사별 한계는 [운영 로컬 검증](../../deploy/README.md#격리-로컬-검증)에 정리했다.
합성 네트워크 CIDR 신뢰와 IP가 재사용된 재생성 결과를 실제 운영 신뢰 범위·주소 변경 검증으로 간주하지 않는다.
운영 주소/네트워크·접근 경계·EC2 적용·독립 검토와 #76 인계는 남아 있다.

### 세션 직렬화와 실패 경계

**같은 사용자 요청 순서.** 서명·만료·Origin·CSRF를 확인해 사용자 ID를 얻은 뒤, 한 트랜잭션에서 `users` 행을 `FOR UPDATE`로 잠근다. 같은 사용자의 신규 로그인, refresh, 재사용 폐기, logout이 동일한 잠금 순서를 따른다. 이전 refresh 폐기와 새 refresh 삽입이 함께 커밋되기 전에는 쿠키·토큰을 발급하지 않는다.

**계보 길이.** 확인된 루트와 `user_id`에 속한 세션만 따라간다. 루트를 1개로 세며 정상 발급은 최대 4095개, 조회 상한은 4096개다. 다음 새 refresh가 4096번째가 될 때는 발급하지 않는다. 대신 활성 계보를 잠금 안에서 전부 폐기·저장한 후 쿠키 만료와 401을 반환한다.

**이전 refresh 재제출.** 교체 과정에서 폐기된 이전 refresh digest와 그에 맞는 CSRF가 이전 refresh의 `revoked_at`부터 5초 이내에 오면 `409 refresh_race`, `Retry-After: 1`을 반환하고 새 `Set-Cookie`는 보내지 않는다. 이 창에서는 공격자의 재사용도 경합으로 취급한다. 5초 뒤에는 해당 계보의 후임을 폐기하고 401을 반환한다. 지연된 정상 요청도 로그아웃될 수 있다.

**서로 맞지 않는 쿠키와 헤더.** 새 refresh 쿠키에 낡은 메모리 CSRF 헤더가 붙으면 403이다. 이를 재사용의 증거로 삼지 않는다. 순환·4096 초과·사용자 불일치 등 손상된 계보는 토큰 발급 없이 503과 운영 경보로 처리하고, 폐기 성공을 주장하지 않는다.

가입 접수 제한과 프록시 뒤 로그인 제한의 구현·로컬 검증 범위는 위 정책을 따른다. 실제 운영 구성·신뢰 범위와 독립 확인은 별도로 남아 있으며 아래 요구사항을 전체 배포 완료 주장으로 해석하지 않는다.

## 구현·출시 확인

- [ ] 해시 파라미터와 입력 경계, 로컬 이메일 중복, 같은 이메일의 분리된 Google 계정을 검증한다.
- [ ] 가입 접수량·실제 해시 동시 상한, 거절 응답과 재시도 시간, 503 거절의 횟수 미소모, 실패·연결 종료 뒤 자리 반환을 검증한다.
- [ ] JWT 종류·서명·claim·만료 실패, digest 불일치, 절대 만료 상속과 쿠키 속성을 검증한다.
- [ ] 동시 회전 승자 하나, 5초 경합, 창 밖 재사용 폐기, 낡은 CSRF 403, 두 번 제한한 화면 재시도, logout 잔여 access를 검증한다.
- [ ] Google callback의 검증 실패·중복·상대 경로 제한과 callback 뒤 access 획득, 실패 안내·재로그인을 실제 화면과 Google에서 확인한다. 서버 대체 제공자·DB 검증과 구분한다.
- [ ] 배포 전 정확한 프론트/API Origin과 동일 사이트 여부, HTTPS 종료, issuer/audience·키 주입/교체, Google client/callback을 확정한다.
- [ ] 실제 프록시 신뢰 범위·접근 경계, worker/replica 1·재시작 초기화 수용과 독립 검토를 기록한다.
- [ ] 다중 인스턴스 로그인 실패 제한의 공유 원자 카운터 또는 프록시 방식을 결정한다. 그 전에는 다중 인스턴스 실제 로그인을 열지 않는다.

보존·삭제 기간, 백업·운영 로그와 계정 삭제는 별도 개인정보 결정이다. 이 문서는 구현·배포 검증을 대신하지 않는다.

## 변경 이력

| Rev | 일자 (KST) | 변경과 근거 |
| --- | --- | --- |
| 1 | 2026-10-25 | 인증·세션 구현과 화면 연결, Google 실패·운영 검증의 경계를 갱신했다. 당시 기준 코드는 `2d50da5`다. |
| 2 | 2026-10-27 | #71의 승인된 가입 접수·해시 동시 처리 제한, 조정 가능한 설정, 자리 반환과 운영 경계를 PR #79 구현에 맞췄다. 가입 해시 전 제한을 미구현으로 읽게 하는 설명을 수정했다. 코드 기준 `1566e8b`. |
| 3 | 2026-10-27 | #69의 302 실패 복귀·쿠키 정리·기존 세션 보존과 503 대체 응답을 반영했다. 실패 화면·실제 Google·독립 통합 검증은 별도로 유지한다. 구현 `8921f8a`, 검증 `1cbf709`. |
| 4 | 2026-10-30 | #74의 명시 프록시 신뢰 연결과 단일 프로세스 로그인 제한 경계, 실제 Nginx 경유 로컬 검증을 반영했다. 구현 `4979a90`, 검사 `40035b6`. 실제 운영 신뢰 범위·EC2·독립 검토는 미완료다. |
