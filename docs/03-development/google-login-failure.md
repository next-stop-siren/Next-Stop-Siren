# Google 로그인 실패 복귀 연동 안내

#69의 백엔드는 실패를 고정 코드와 안전한 Location으로 전달한다.
yejoo0310은 서버 규약·검증 결과를 인계하고, gittul-123은 실패 안내·재시도·이메일 로그인 재진입을 연결한다.
코드 기준은 `1cbf709`이며 응답과 분류의 단일 기준은 [API 계약 Rev 3](../04-reference/api.md#google-실패-복귀-계약)이다.
성공 검증과 세션 정책은 [인증 구현 기준 Rev 3](../04-reference/authentication.md#google-실패-복귀)을 따른다.

## 서버에서 전달하는 응답

다음은 프런트 Origin을 `http://127.0.0.1:5173`으로 설정한 개발 환경의 예시다.
실패 302의 본문은 비어 있고 트랜잭션 쿠키를 삭제한다.
기존 refresh·CSRF 쿠키는 유지하며 새 인증 쿠키는 발급하지 않는다.

```http
HTTP/1.1 302 Found
Location: http://127.0.0.1:5173/?google_login_error=cancelled
Cache-Control: no-store
Pragma: no-cache
Referrer-Policy: no-referrer
Set-Cookie: google_transaction=""; HttpOnly; Max-Age=0; Path=/api/auth; SameSite=lax
```

예시에서 삭제 쿠키의 Expires는 생략했다. 운영 HTTPS에는 Secure도 적용된다.
Location의 고정 값은 cancelled·invalid_request·temporarily_unavailable 세 가지이며 조건은 API 계약을 따른다.
프런트 Origin을 생략하면 Location은 `/?google_login_error=cancelled`처럼 API Origin 기준 상대 경로다.

설정 또는 실패 응답 준비가 불가능하면 Location 없이 503 JSON으로 대체한다.
트랜잭션 쿠키 삭제와 위 캐시·referrer 헤더는 대체 응답에도 적용한다.
아래 trace ID는 가짜 예시이며 실제 값은 서버가 생성한다.

```json
{
  "error": {
    "code": "temporarily_unavailable",
    "message": "일시적으로 서비스를 이용할 수 없습니다.",
    "fields": {},
    "trace_id": "00000000-0000-4000-8000-000000000069"
  }
}
```

## 프런트 인계 조건

실패 query는 안내용이다. URL을 수동으로 바꿀 수 있으므로 인증 성공·강제 로그아웃·계정 전환의 근거로 사용하지 않는다.
기존 유효 세션이 남을 수 있으며 실제 인증 상태는 기존 csrf→refresh→me 복구 결과로 판단한다.

| 프런트 담당 작업 | 연동 조건 |
| --- | --- |
| 실패 안내 | 고정 코드만 해석하고 서버·제공자 오류 원문을 출력하지 않는다. |
| 알 수 없는·빈·중복 값 | 원문을 표시하거나 인증 상태를 바꾸지 않고 안전한 일반 안내 또는 무시로 처리한다. |
| Google 다시 시도 | `/api/auth/google/start`로 전체 페이지 이동한다. 이전 callback의 code·state는 재사용하지 않는다. |
| 이메일 로그인 | 기존 이메일 로그인 폼으로 진입한다. 실패만으로 이메일·Google 계정을 연결하지 않는다. |
| URL 값 소비 | 처리한 실패 파라미터를 제거해 새로고침 때 이전 안내가 반복되지 않게 한다. |
| 기존 세션과 접근성 | 기존 회원 화면·세션을 보존하고 모달·배너 형태와 초점·키보드 동작을 구현·검사한다. |
| 개발용 미리보기 | 실제 callback 복귀와 구분하고 미리보기만으로 연동 완료를 표시하지 않는다. |

문구는 다음 제안을 함께 검토한다. 백엔드는 문구를 URL로 보내지 않는다.

| 코드 | 제목 | 안내 문구 제안 |
| --- | --- | --- |
| `cancelled` | Google 로그인을 취소했어요 | 다시 시도하거나 이메일로 로그인해 주세요. |
| `invalid_request` | Google 로그인에 실패했어요 | 로그인 요청을 확인할 수 없어요. 다시 시작하거나 이메일로 로그인해 주세요. |
| `temporarily_unavailable` | Google 로그인에 실패했어요 | 지금은 Google 로그인을 완료할 수 없어요. 잠시 후 다시 시도하거나 이메일로 로그인해 주세요. |

## 로컬 브라우저에서 확인

API와 프런트를 실행하고, 유효한 Google 설정과 허용된 `GOOGLE_FRONTEND_ORIGIN`을 준비한다.
서로 다른 포트를 쓰는 개발 환경에서는 `http://127.0.0.1:5173`이 설정되어야 프런트로 복귀한다.
설정값은 앱 시작 때 고정하므로 변경하면 백엔드를 재시작한다.
DevTools Network의 Preserve log를 켜고 주소창에 아래 전체 HTTP 주소를 입력한다.
Swagger Execute는 redirect를 따라가므로 원래 302의 확인에는 Network의 응답 헤더를 사용한다.

### 잘못된 callback과 외부 복귀 거부

```text
http://127.0.0.1:8000/api/auth/google/callback?state=wrong
```

callback 요청의 상태는 302, Location의 고정 값은 invalid_request여야 한다.
삭제 쿠키의 Max-Age=0·Path=/api/auth와 캐시·referrer 응답 헤더를 확인한다.
새 refresh·CSRF 쿠키가 없어야 한다. 화면에 실패 모달이 뜨는지는 프런트 연결 후 별도로 확인한다.

```text
http://127.0.0.1:8000/api/auth/google/start?return_to=https://example.com
```

상태는 422이며 JSON의 error.code는 validation_error, fields에는 return_to가 포함되어야 한다.
외부 사이트로 이동하거나 트랜잭션 쿠키를 설정·삭제하면 안 된다.
주소창이 file://가 되지 않도록 http://127.0.0.1:8000을 포함한다.

### 유효한 트랜잭션의 취소와 제공자 오류 분류

1. 같은 브라우저에서 `http://127.0.0.1:8000/api/auth/google/start`를 열어 Google 로그인 화면으로 이동한다.
2. Network의 start 응답 Location에서 방금 발급한 state를 로컬에서만 확인한다.
   트랜잭션 쿠키가 있는 동일 브라우저·호스트에서 5분 이내에 아래 요청을 사용한다.
3. 아래 state 자리표시자를 실제 값으로 바꾸고, code 파라미터 없이 접속한다.

```text
http://127.0.0.1:8000/api/auth/google/callback?state=<방금 발급한 state>&error=access_denied
```

이 합성 callback은 cancelled로 302 복귀해야 한다.
다시 start로 새 트랜잭션을 만든 뒤 error를 server_error로 바꾸면 temporarily_unavailable로 302 복귀해야 한다.
이 검사는 실제 Google에서 받은 취소·장애가 아니라 서버의 분류와 복귀를 직접 확인하는 방법이다.
state나 쿠키를 캡처·메시지·로그로 공유하지 않는다.
틀린 state, 만료·누락 쿠키로 요청하면 두 경우 모두 invalid_request로 처리된다.

### 설정 부재의 503 대체 응답

Google 화면의 이메일 형식 오류는 우리 서버의 503이 아니다.
정상 설정에서 제공자 장애를 처리하는 302와, 안전한 복귀 설정이 없는 503을 구분한다.

1. 로컬 `.env`의 기존 GOOGLE_CLIENT_ID 값을 보관한다.
2. `GOOGLE_CLIENT_ID=invalid-for-local-test`로 임시 변경하고 백엔드를 재시작한다.
3. `http://127.0.0.1:8000/api/auth/google/start`에 접속한다.
4. 상태 503, error.code=temporarily_unavailable, Location 없음, 트랜잭션 삭제 쿠키를 확인한다.
5. 기존 값을 복원하고 백엔드를 다시 시작한다.

### 실제 Google 로그인

start에서 이동한 Google 화면에서 실제 로그인을 완료하고 callback의 성공 302와 인증 쿠키,
복귀 후 csrf→refresh→me를 확인한다. Google 취소가 실제 callback으로 전달되는 경우 그 실패도 별도로 기록한다.
브라우저 뒤로 가기나 탭 닫기만으로는 Google이 취소 callback을 보냈다고 판단하지 않는다.
DB 저장 실패·부분 쿠키·보상 실패는 테스트용 오류 주입으로 검증하며 수동 확인용 장애 endpoint는 제공하지 않는다.

## 자동 검사와 인계 상태

`1cbf709` 기준 Google 비DB 585개와 callback DB 37개를 확인했고,
전체 DB 실행 및 수정 항목 재검사를 합쳐 고유 DB 검사 346개 통과, 실제 GPT 검사 1개 skip을 확인했다.
DB 검사에는 중복 callback, rollback·보상, 기존 세션의 refresh·me, 새 로그인 재진입과 복귀 경로 공격이 포함된다.

일반 재실행은 저장소의 안전 검사 절차를 따른다.

```sh
# 전용 TEST_DATABASE_URL을 로컬 설정과 일치하게 준비한 환경에서 실행한다.
./local.sh test-db
```

TEST_DATABASE_URL에 실제 계정 정보를 문서나 메시지로 복사하지 않는다.
테스트 대상은 전용 로컬 db-test이며 개발·운영 DB로 바꾸지 않는다.

화면 미리보기, 대체 제공자 서버 검사, 실제 Google 실행은 별도 증거로 기록한다.
실패 안내·재시도 버튼·URL 소비와 브라우저 통합은 프런트 연결 후 공동 확인하고,
인증 변경을 구현하지 않은 davekim-dev의 독립 검토를 거쳐 #19에 인계한다.
이 문서는 #69·#19 완료나 담당자 간 합의·검토 완료를 대신하지 않는다.
