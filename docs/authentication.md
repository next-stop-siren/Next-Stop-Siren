# 인증 구현 기준

이 문서는 승인된 D1 A·B·C 정책의 팀 구현 기준이다. 경로별 요청·응답은 [공통 API 형식](api.md), 저장 제약은 [DB 설계](database.md)를 따른다. 현재 앱의 인증 경로는 아직 구현되지 않았으며 운영 Origin·키·Google 자격 증명과 공유 로그인 제한 방식은 배포 전에 정한다.

여기서 **확인된 사용자 정보(principal)**는 서버가 로그인 자격을 검사해 얻은 사용자 ID다. **계보(lineage)**는 한 번의 로그인에서 시작해 refresh 토큰을 교체하며 이어진 세션들의 연결이다.

## 빠른 동작표

| 상황 | 브라우저·서버 동작 |
| --- | --- |
| 로컬 가입·로그인 | 공개 사용자와 15분 access JWT를 본문에 반환하고 refresh·CSRF 쿠키를 설정한다. |
| 보호 API | 메모리의 access를 `Authorization: Bearer`로 보낸다. 쿠키는 인증 수단이 아니다. |
| 새로고침 | `/api/auth/csrf`에서 CSRF 값을 받은 뒤 `/api/auth/refresh`를 호출해 새 access를 메모리에 둔다. |
| 로그아웃 | 현재 refresh 계보와 쿠키만 폐기한다. 이미 발급된 access는 최대 15분 유효하다. |
| Google | 서버 시작 OIDC → callback에서 refresh·CSRF 쿠키 설정과 상대 경로 이동 → 화면에서 CSRF·refresh로 access 획득. |
| 탭 경합 | 낡은 refresh와 짝인 CSRF는 5초 안에 409, 창 밖 재사용은 계보 폐기·401. 새 refresh 쿠키와 낡은 CSRF 헤더는 403이다. |

## 화면 흐름

### 로컬 가입·로그인

1. 화면이 정확히 허용된 Origin에서 JSON 이메일·비밀번호를 제출한다. 로컬 이메일은 앞뒤 공백을 제거하고 DB `lower(email)` 판정에 맞춰 비교한다. 형식과 최대 320자를 확인한다.
2. 비밀번호는 원문 그대로 Unicode 코드 포인트 12~128자와 UTF-8 1024바이트 이하를 확인한다. 입력 정규화·절단은 하지 않는다. 위반은 422다.
3. 성공하면 공개 사용자와 access를 화면 메모리에 둔다. refresh와 CSRF는 각각 `HttpOnly` 쿠키로 받는다. 로그인 실패의 없는 계정과 틀린 비밀번호는 같은 401 본문과 비슷한 처리 시간이다.
4. 로컬 이메일별 실패 5회/10분, IP별 실패 30회/10분을 넘으면 429와 `Retry-After`를 받는다. 성공하면 해당 이메일 실패 수를 초기화한다.

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
3. 성공 시 서버는 자체 refresh·CSRF 쿠키를 설정하고 허용 목록의 상대 경로로 이동한다. 화면은 `/csrf`와 `/refresh`를 호출해 메모리 access를 얻는다. redirect URL·쿠키에 access를 넣지 않는다.

트랜잭션 쿠키의 삭제만으로 callback 단일 사용을 보장하지 않는다. 제공자의 code 1회 교환과 state/nonce 검증에 의존한다. 중복 callback 또는 code 교환 실패에는 세션을 발급하지 않는다.

## 보안 경계와 구현 참고

| 항목 | 구현 기준 |
| --- | --- |
| 비밀번호 해시 | Argon2id, 메모리 64 MiB·반복 3·병렬성 1·salt 16바이트·해시 32바이트. 승인된 Python 라이브러리는 `argon2-cffi`; 호환 버전은 구현 때 선택·검증한다. |
| Access JWT | 15분, 메모리 전용. URL·저장소·쿠키 금지. |
| Refresh JWT | 최초 로그인부터 절대 14일. 회전 후임은 루트 `expires_at`을 상속하고 새 `jti`를 가진다. 남은 시간이 없으면 재로그인한다. |
| JWT 검증 | 종류별 서로 다른 256비트 이상 HS256 키, 승인 `kid`만 허용. `iss`, 앱 전용 `aud`, 십진 문자열 `sub`, `iat`·`nbf`·`exp`·`jti`·`token_type`을 검증한다. access/refresh 교차 사용 금지. |
| DB digest | 별도 비밀키의 `HMAC-SHA-256(refresh JWT)` 32바이트만 `refresh_sessions.token_digest`에 저장한다. 원문 토큰 저장 금지. |
| Refresh 쿠키 | `HttpOnly`, 호스트 전용(`Domain` 없음), `SameSite=Lax`, `Path=/api/auth`, 운영 `Secure`. `Expires`는 루트 절대 만료, 회전 `Max-Age`는 남은 초다. |
| CSRF 쿠키 | 별도 `HttpOnly`·호스트 전용·`SameSite=Lax`·`Path=/api/auth`·운영 `Secure`; 서버 MAC 값을 현재 refresh `jti`에 결박한다. 본문으로 받은 값을 화면 메모리에 둔다. |
| Google 트랜잭션 쿠키 | 시작·callback 모두 포함하는 `/api/auth` 경로, 호스트 전용 `HttpOnly`·운영 `Secure`·`SameSite=Lax`; state/nonce/PKCE verifier를 암호화·무결성 보호한다. |
| Origin·CORS | 세션 쿠키를 설정하는 로컬 가입·로그인과 refresh·logout은 정확한 허용 `Origin`을 요구한다. 쿠키 인증 변경 요청에서 Origin 누락·불일치는 거부한다. CORS는 정확한 프론트 Origin에만 credentials를 허용한다. |

로컬 HTTP `localhost`에서만 개발 설정으로 `Secure=false`를 허용한다. 운영 프론트와 API가 서로 다른 사이트라면 `SameSite=Lax`를 다시 결정해야 한다. Bearer만 쓰는 채팅 요청에는 쿠키 CSRF 절차가 적용되지 않는다.

비밀번호 평문, 원문 토큰, 제공자 code 및 비밀은 DB·로그·추적에 남기지 않는다.

### 세션 직렬화와 실패 경계

**같은 사용자 요청 순서.** 서명·만료·Origin·CSRF를 확인해 사용자 ID를 얻은 뒤, 한 트랜잭션에서 `users` 행을 `FOR UPDATE`로 잠근다. 같은 사용자의 신규 로그인, refresh, 재사용 폐기, logout이 동일한 잠금 순서를 따른다. 이전 refresh 폐기와 새 refresh 삽입이 함께 커밋되기 전에는 쿠키·토큰을 발급하지 않는다.

**계보 길이.** 확인된 루트와 `user_id`에 속한 세션만 따라간다. 루트를 1개로 세며 정상 발급은 최대 4095개, 조회 상한은 4096개다. 다음 새 refresh가 4096번째가 될 때는 발급하지 않는다. 대신 활성 계보를 잠금 안에서 전부 폐기·저장한 후 쿠키 만료와 401을 반환한다.

**이전 refresh 재제출.** 교체 과정에서 폐기된 이전 refresh digest와 그에 맞는 CSRF가 이전 refresh의 `revoked_at`부터 5초 이내에 오면 `409 refresh_race`, `Retry-After: 1`을 반환하고 새 `Set-Cookie`는 보내지 않는다. 이 창에서는 공격자의 재사용도 경합으로 취급한다. 5초 뒤에는 해당 계보의 후임을 폐기하고 401을 반환한다. 지연된 정상 요청도 로그아웃될 수 있다.

**서로 맞지 않는 쿠키와 헤더.** 새 refresh 쿠키에 낡은 메모리 CSRF 헤더가 붙으면 403이다. 이를 재사용의 증거로 삼지 않는다. 순환·4096 초과·사용자 불일치 등 손상된 계보는 토큰 발급 없이 503과 운영 경보로 처리하고, 폐기 성공을 주장하지 않는다.

## 구현·출시 확인

- [ ] 해시 파라미터와 입력 경계, 로컬 이메일 중복, 같은 이메일의 분리된 Google 계정을 검증한다.
- [ ] JWT 종류·서명·claim·만료 실패, digest 불일치, 절대 만료 상속과 쿠키 속성을 검증한다.
- [ ] 동시 회전 승자 하나, 5초 경합, 창 밖 재사용 폐기, 낡은 CSRF 403, 두 번 제한한 화면 재시도, logout 잔여 access를 검증한다.
- [ ] Google callback의 검증 실패·중복·상대 경로 제한과 callback 뒤 access 획득을 검증한다.
- [ ] 배포 전 정확한 프론트/API Origin과 동일 사이트 여부, HTTPS 종료, issuer/audience·키 주입/교체, Google client/callback을 확정한다.
- [ ] 다중 인스턴스 로그인 실패 제한의 공유 원자 카운터 또는 프록시 방식을 결정한다. 그 전에는 다중 인스턴스 실제 로그인을 열지 않는다.

보존·삭제 기간, 백업·운영 로그와 계정 삭제는 별도 개인정보 결정이다. 이 문서는 구현·배포 검증을 대신하지 않는다.
