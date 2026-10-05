# 공통 API 형식과 첫 모의 응답

현재 앱에서 동작하는 경로는 `GET /api/health`, `GET /api/ready`뿐이다. 아래 인증·대화 경로는 **후속 구현 기준**이다. 첫 내부 목표는 모의 제공자가 만든 완성 답변을 저장하고, 새로고침 뒤 같은 답변을 조회하는 것이다. 예시 사용자·내용·시각은 모두 가짜다.

## 공통 규칙

이 문서의 **요청·응답 형식**은 앞으로 구현할 API의 기준이다. **테스트 예시(fixture)**는 실제 계정·토큰 없이 만든 가짜 입력과 예상 결과다. **확인된 사용자 정보(principal)**는 서버가 로그인 자격을 검사해 얻은 사용자 ID다.

- 기본 경로는 기존 앱과 같은 `/api`다. JSON 요청에는 `Content-Type: application/json`, 응답에는 `application/json`을 쓴다.
- DB의 `bigint` ID와 이를 참조하는 필드는 JSON **십진 문자열**이다. 화면에서 `Number`로 바꾸지 않는다. 경로와 페이지 기준 ID도 같은 십진 문자열이다.
- 시각은 UTC ISO 8601 문자열(`2026-01-02T03:04:05Z`)이다. 예시에서 `null`은 값이 아직 없거나 해당 상태에 적용되지 않음을 뜻한다.
- 보호 경로는 서버가 검증한 principal의 사용자 ID만 신뢰한다. 요청 본문의 `user_id`는 받지 않는다. 비인증은 401, 없는 자원과 다른 사용자 소유 자원은 똑같이 404다.
- 목록은 `items` 배열과 `next_cursor`(십진 ID 문자열 또는 `null`)를 반환한다. `limit` 기본값은 20, 허용 범위는 1~100이다.
- 대화는 생성 ID 내림차순이고 다음 페이지에 `before_id=next_cursor`를 보낸다. 최근 수정 순서가 아니라 생성 순서를 택해 페이지 이동 중 위치가 바뀌지 않게 한다.
- 메시지는 ID 오름차순이다. 다음 페이지에는 `after_id=next_cursor`를 보낸다. `next_cursor`는 반환한 마지막 항목 ID이며 더 없으면 `null`이다. 새 행이 추가돼도 이미 읽은 행을 중복하지 않는다.
- 커서 페이지는 이미 받은 행의 상태 변경을 알려 주지 않는다. 진행 중인 답변은 같은 요청 키로 다시 조회한다.
- 입력 오류 본문은 아래 공통 형식을 쓴다. 내부 예외, DB 정보, 제공자 응답과 비밀값은 본문에 넣지 않는다.

**오류 본문 예시 — `422 Unprocessable Entity`**

```json
{
  "error": {
    "code": "validation_error",
    "message": "요청을 확인해 주세요.",
    "fields": {
      "content": "공백만 입력할 수 없습니다."
    },
    "trace_id": "trace-demo-001"
  }
}
```

`fields`는 필드별 짧은 문자열의 객체이며 필드 오류가 없으면 `{}`다. `trace_id`는 서버가 오류마다 생성하는 추적 문자열이며 클라이언트의 `request_key`와 무관하다.

경로·본문 검증 실패도 이 형식으로 반환한다. 오류 코드는 아래 표의 안정된 분기 값이다. `message`는 화면에 그대로 표시해도 되는 안전한 문구다.

| 상태 | `error.code` | 뜻 |
| --- | --- | --- |
| 401 | `unauthenticated` | 확인된 사용자가 없음; 로그인 실패·만료·폐기 refresh에도 사용 |
| 403 | `csrf_failed` | Origin 또는 CSRF 검증 실패; 재사용의 증거는 아님 |
| 404 | `not_found` | 자원이 없거나 다른 사용자 소유 |
| 409 | `request_key_conflict` | 같은 대화·키에 다른 질문 본문 |
| 409 | `conversation_busy` | 다른 질문의 답변이 진행 중 |
| 409 | `refresh_race` | 회전된 refresh가 5초 경합 창에 재제출됨 |
| 409 | `local_email_exists` | 로컬 가입 이메일이 이미 존재함 |
| 422 | `validation_error` | 경로·본문·페이지 입력 오류 |
| 429 | `rate_limited` | 로컬 로그인 실패 제한 초과; `Retry-After` 포함 |
| 502 | `provider_failed` | 모의 제공자 실패가 저장됨; 이력 재조회 |
| 504 | `provider_timeout` | 모의 제공자 시간 초과가 중단 상태로 저장됨 |
| 503 | `temporarily_unavailable` | 저장 전 서비스·DB 실패 |

## 인증 요청·응답 — 후속 구현 기준

정책과 브라우저 절차는 [인증 구현 기준](authentication.md)을 따른다. 현재 이 경로들은 동작하지 않는다. 예시 계정·토큰·시각은 가짜 테스트 데이터다.

모든 인증 오류도 위의 `error`/`fields`/`trace_id` 형식을 쓴다. 없는 계정과 틀린 비밀번호는 같은 `401 unauthenticated` 문구다. 로컬 가입·로그인 및 refresh·logout `POST`에는 정확히 허용된 `Origin`(요청을 시작한 화면의 출처)이 필요하다. Google의 top-level 시작·callback `GET`은 state·nonce·PKCE로 검증한다.

**로컬 계정과 현재 사용자**

- `POST /api/auth/register`: JSON `email`, `password`와 `Origin`을 보낸다. 성공은 `201`, 공개 `user`, 메모리용 `access_token`, `expires_in: 900`, 본문의 `csrf_token`, refresh·CSRF `Set-Cookie`다. 실패는 입력 422, 로컬 이메일 중복 409, Origin 403이다.
- `POST /api/auth/login`: 가입과 같은 JSON·`Origin`을 보낸다. 성공은 `200`과 같은 인증 본문·쿠키다. 실패는 동일한 로그인 실패 401, 실패 제한 429, Origin 403이다.
- `GET /api/me`: `Authorization: Bearer <access>`를 보낸다. 성공은 `200`과 공개 `user`, access 검증 실패는 401이다.

**쿠키 세션**

- `GET /api/auth/csrf`: refresh 쿠키를 포함하도록 credentials를 설정한다. 성공은 `200`과 `csrf_token`, refresh 실패는 401이다.
- `POST /api/auth/refresh`: refresh·CSRF 쿠키, `Origin`, `X-CSRF-Token`을 보내며 본문은 없다. 성공은 `200`, 새 access·CSRF 본문과 교체된 쿠키다. 실패는 만료·재사용·계보 한도 401, Origin/CSRF 403, 경합 409, 손상 계보 503이다.
- `POST /api/auth/logout`: refresh·CSRF 쿠키, `Origin`, `X-CSRF-Token`을 보내며 본문은 없다. 성공은 `204`와 두 쿠키 만료다. 실패는 refresh 401, Origin/CSRF 403, 손상 계보 503이다.

**Google 로그인**

- `GET /api/auth/google/start`: top-level 이동으로 시작한다. 선택 `return_to`는 허용 목록의 상대 경로다. 성공은 Google authorization endpoint로 `302`와 트랜잭션 쿠키다. 복귀 경로 오류는 422, 설정·제공자 실패는 503이다.
- `GET /api/auth/google/callback`: 제공자의 `code`, `state`와 트랜잭션 쿠키를 받는다. 성공은 허용된 상대 경로로 `302`와 refresh·CSRF 쿠키다. 검증 실패는 401, 제공자·설정 실패는 503이며 실패 시 세션은 없다.

로컬 이메일은 앞뒤 공백 제거, 유효한 형식과 최대 320자이며 로컬 계정에서만 `lower(email)`로 중복 판정한다. 비밀번호는 정규화 없이 Unicode 코드 포인트 12~128자와 UTF-8 1024바이트 이하를 모두 만족해야 한다. 두 값 모두 필수 문자열이다. 가입 중복의 `409` 코드는 `local_email_exists`이며 Google과 이메일이 같아도 중복이 아니다. 미인증 이메일은 소유 증명이 아니다.

`POST /api/auth/register` 요청 예시 (`/login`도 같은 입력):

```json
{
  "email": "a@example.test",
  "password": "fixture-only-password"
}
```

가입·로그인 성공 본문 예시 (가입 201, 로그인 200):

```json
{
  "user": {
    "id": "101",
    "email": "a@example.test",
    "created_at": "2026-01-02T03:04:05Z"
  },
  "access_token": "fixture.access.jwt",
  "token_type": "Bearer",
  "expires_in": 900,
  "csrf_token": "fixture-csrf-value"
}
```

`access_token`은 화면 메모리 전용이며 URL·쿠키·브라우저 저장소에 넣지 않는다.

로그인·가입의 refresh 쿠키는 `HttpOnly; SameSite=Lax; Path=/api/auth`, CSRF도 별도의 같은 속성 쿠키다. 둘 다 호스트 전용(`Domain` 없음)이며 운영에서는 `Secure`다. refresh는 최초 로그인부터 절대 14일로 만료된다. refresh 쿠키의 `Expires`는 그 절대 시각, 교체할 때의 `Max-Age`는 남은 초다.

로컬 HTTP `localhost` 개발 설정에 한해 `Secure=false`를 허용한다. CORS credentials는 정확한 프론트 Origin에만 허용한다.

### 현재 사용자 — `GET /api/me`

본문과 쿼리는 없다. 검증된 access principal의 공개 사용자만 반환한다. `200 OK`:

```json
{
  "user": {
    "id": "101",
    "email": "a@example.test",
    "created_at": "2026-01-02T03:04:05Z"
  }
}
```

인증 실패는 `401 unauthenticated`다. 채팅 보호 경로도 같은 Bearer access를 요구한다.

### 브라우저 복구·로그아웃

1. 가입·로그인 뒤 `csrf_token`과 access를 메모리에 둔다. 화면 새로고침 뒤에는 refresh 쿠키를 직접 읽지 않고 credentials를 포함해 `GET /api/auth/csrf`를 호출한다. 성공 `200`:

```json
{
  "csrf_token": "fixture-csrf-value"
}
```

2. 받은 값을 `X-CSRF-Token`에 싣고 정확한 `Origin`과 credentials로 `POST /api/auth/refresh`를 보낸다. 성공 `200`에는 새 refresh·CSRF 쿠키도 포함된다:

```json
{
  "access_token": "fixture.new.access.jwt",
  "token_type": "Bearer",
  "expires_in": 900,
  "csrf_token": "fixture-new-csrf-value"
}
```

3. 낡은 refresh와 짝 CSRF가 5초 안에 다시 오면 `409 refresh_race`, `Retry-After: 1`, `Set-Cookie` 없음이다. 새 refresh 쿠키에 낡은 CSRF 헤더가 붙으면 `403 csrf_failed`이며 재사용 폐기로 취급하지 않는다. 화면은 잠시 기다려 `/csrf`를 다시 읽고 refresh를 한 번만 재시도한다. 실패하면 재로그인을 안내한다.
4. `POST /api/auth/logout` 성공은 `204 No Content`와 두 쿠키 만료다. 화면 메모리의 access·CSRF를 지운다. 다른 로그인 계보는 유지되고 이미 발급된 access는 최대 15분 유효할 수 있다.

일반 인증 오류 예시 (`401`; 403·409·429도 같은 JSON 형식에 각각 위 코드를 쓴다):

```json
{
  "error": {
    "code": "unauthenticated",
    "message": "로그인이 필요합니다.",
    "fields": {},
    "trace_id": "trace-demo-auth-001"
  }
}
```

손상된 계보는 `503 temporarily_unavailable`로 운영 경보를 남긴다. 4095개 정상 발급 상한에 도달하면 활성 계보를 폐기하고 쿠키 만료·401을 반환한다. 폐기 커밋 실패 시 성공 또는 새 토큰을 반환하지 않는다.

### Google에서 돌아온 화면

`/api/auth/google/start`에서 서버는 요청과 callback을 연결하는 `state`, ID 토큰을 해당 로그인에 묶는 `nonce`, code 교환을 보호하는 PKCE(S256)를 시작한다. callback은 Google의 서명과 필수 claim, 검증된 `(issuer, subject)`를 확인한다. 같은 이메일의 로컬 사용자와 합치지 않는다.

callback 성공은 허용 목록의 상대 경로로만 redirect한다. 외부 URL과 `//`로 시작하는 프로토콜 상대 URL은 `return_to`로 허용하지 않는다. 돌아온 화면이 `/api/auth/csrf` → `/api/auth/refresh`를 호출해 access를 메모리에 넣는다. access는 redirect URL이나 쿠키에 실리지 않는다.

Google 트랜잭션 쿠키는 시작과 callback을 모두 포함하는 `Path=/api/auth`이며 짧은 수명·호스트 전용·`HttpOnly; SameSite=Lax`, 운영 `Secure`다.

## 대화

모든 경로는 인증이 필요하다. `id`, `user_id`, 시각은 클라이언트가 생성하지 않는다.

### 생성 — `POST /api/conversations`

요청 본문은 빈 객체 `{}`다. `201 Created`:

```json
{
  "conversation": {
    "id": "501",
    "created_at": "2026-01-02T03:05:00Z",
    "updated_at": "2026-01-02T03:05:00Z"
  }
}
```

비인증은 위 `401` 형식. 요청 필드가 있거나 JSON 형식이 틀리면 `422 validation_error`다.

### 내 목록 — `GET /api/conversations?limit=20`

선택 쿼리 `limit`, `before_id`를 받는다. `before_id`는 이전 응답의 `next_cursor`다. 소유자의 대화만 반환한다. `200 OK`:

```json
{
  "items": [
    {
      "id": "501",
      "created_at": "2026-01-02T03:05:00Z",
      "updated_at": "2026-01-02T03:07:00Z"
    }
  ],
  "next_cursor": null
}
```

비인증은 `401`, 잘못된 쿼리는 `422`다. 항목이 없으면 `items: []`, `next_cursor: null`이다.

## 메시지

메시지 표시는 같은 대화에서 `id` 오름차순이다. 공개 메시지에는 다음 필드가 **항상** 있다.

| 필드 | JSON 형식 | 규칙 |
| --- | --- | --- |
| `id` | 문자열 | 메시지 ID |
| `role` | `user` 또는 `assistant` | 질문/답변 |
| `status` | `pending`, `completed`, `failed`, `interrupted` | 질문은 `completed`만 |
| `content` | 문자열 또는 `null` | 질문·완료 답변은 비어 있지 않은 문자열; 그 외 답변은 `null` |
| `request_key` | 문자열 또는 `null` | 질문에만 원래 요청 키 |
| `reply_to_message_id` | 문자열 또는 `null` | 답변에만 질문 ID |
| `attempt_no` | 정수 또는 `null` | 답변에만 1 이상; 첫 답변은 1 |
| `error_code` | 문자열 또는 `null` | 실패·중단 답변에만 안전한 이유 코드 |
| `created_at` | UTC 시각 | 항상 있음 |
| `updated_at` | UTC 시각 | 항상 있음 |
| `completed_at` | UTC 시각 또는 `null` | 질문·완료 답변에만 시각 |

첫 목표의 답변은 `attempt_no: 1`이다. `retry_key`는 이후 정할 수동 재시도 규칙에 속하므로 첫 화면 형식에 넣지 않는다. 첫 목표의 저장된 `error_code`는 `provider_failed`(실패) 또는 `outcome_unknown`(시간 초과·중단)이다.

### 이력 — `GET /api/conversations/501/messages?limit=20`

선택 쿼리 `limit`, `after_id`를 받는다. 소유권을 확인한 뒤 메시지를 조회한다. `200 OK`:

```json
{
  "items": [
    {
      "id": "9001",
      "role": "user",
      "status": "completed",
      "content": "안녕?",
      "request_key": "q-demo-1",
      "reply_to_message_id": null,
      "attempt_no": null,
      "error_code": null,
      "created_at": "2026-01-02T03:06:00Z",
      "updated_at": "2026-01-02T03:06:00Z",
      "completed_at": "2026-01-02T03:06:00Z"
    },
    {
      "id": "9002",
      "role": "assistant",
      "status": "completed",
      "content": "안녕하세요.",
      "request_key": null,
      "reply_to_message_id": "9001",
      "attempt_no": 1,
      "error_code": null,
      "created_at": "2026-01-02T03:06:00Z",
      "updated_at": "2026-01-02T03:07:00Z",
      "completed_at": "2026-01-02T03:07:00Z"
    }
  ],
  "next_cursor": null
}
```

비인증 `401`, 없는/타인 대화 `404`, 잘못된 ID·쿼리 `422`다. 다른 사용자 `202`에게 대화 `501`의 존재 여부를 알리지 않는다.

### 질문 전송 — `POST /api/conversations/501/messages`

요청은 `content`(공백만이 아닌 문자열)와 `request_key`(비어 있지 않은 문자열)만 받는다. `request_key`는 한 대화에서 한 질문을 식별하며 응답을 잃고 재전송할 때 **같은 값과 같은 본문**을 쓴다. 질문 길이 상한은 실제 저장 구현 전에 D2에서 확정한다.

```json
{
  "content": "안녕?",
  "request_key": "q-demo-1"
}
```

모의 제공자 완성 답변을 저장한 신규 요청은 `201 Created`와 다음 본문을 돌려준다. `question`과 `answer`는 위 공개 메시지 형식이다.

```json
{
  "question": {
    "id": "9001",
    "role": "user",
    "status": "completed",
    "content": "안녕?",
    "request_key": "q-demo-1",
    "reply_to_message_id": null,
    "attempt_no": null,
    "error_code": null,
    "created_at": "2026-01-02T03:06:00Z",
    "updated_at": "2026-01-02T03:06:00Z",
    "completed_at": "2026-01-02T03:06:00Z"
  },
  "answer": {
    "id": "9002",
    "role": "assistant",
    "status": "completed",
    "content": "안녕하세요.",
    "request_key": null,
    "reply_to_message_id": "9001",
    "attempt_no": 1,
    "error_code": null,
    "created_at": "2026-01-02T03:06:00Z",
    "updated_at": "2026-01-02T03:07:00Z",
    "completed_at": "2026-01-02T03:07:00Z"
  }
}
```

같은 키·같은 본문의 재전송은 새 행이나 제공자 호출 없이 기존 질문·답변 쌍을 반환한다.

- 완료 상태: `200 OK`와 같은 쌍을 반환한다.
- 진행 상태: `202 Accepted`와 같은 쌍을 반환하되 `answer.status`는 `pending`, `answer.content`와 `answer.completed_at`은 `null`이다. 이는 기존 작업의 조회이며 새 제공자 호출을 시작하지 않는다.
- 실패·중단 상태: `200 OK`와 기존 쌍을 반환한다. `answer.status`, `answer.error_code`, `answer.content: null`로 실패를 구분한다.

화면은 전송 전에 `request_key`와 본문을 보관하고, 연결이 끊기거나 `pending`이면 **같은 값으로 이 경로를 다시 호출**해 결과를 확인한다. 이미 받은 메시지 ID 뒤의 `after_id` 페이지만 조회하면 같은 답변 행의 상태 변경을 놓친다.

신규 요청의 실패 상태는 다음과 같다.

- 같은 키에 다른 본문: `409 request_key_conflict`.
- 다른 질문이 진행 중: `409 conversation_busy`.
- 비인증: `401`. 없는 대화 또는 타인 대화: `404`. 빈 질문·키 또는 잘못된 본문: `422`.
- 모의 제공자 실패를 `failed`로 저장: `502 provider_failed`.
- 시간 초과·결과 불명을 `interrupted`로 저장: `504 provider_timeout`.

실패·중단 답변의 `content`는 `null`이고 완료 답변으로 표시하거나 다음 AI 문맥에 넣지 않는다. 연결만 끊긴 경우 새 키로 제출하지 않는다. 같은 키 재전송은 실패·중단 상태도 추가 호출 없이 기존 쌍으로 보여 준다. 수동 재시도는 별도 규칙을 정하기 전까지 제공하지 않는다.

**제공자 실패 예시 — `502 Bad Gateway`**

```json
{
  "error": {
    "code": "provider_failed",
    "message": "답변을 만들지 못했습니다. 대화 기록을 확인해 주세요.",
    "fields": {},
    "trace_id": "trace-demo-003"
  }
}
```

## 구현 순서와 인수 확인

1. PM이 이 형식과 미정 항목을 공유한다. #2 인증 담당은 재사용 가능한 pytest 인증 fixture와 401 검사를 만든다. #9 뒤 #8 채팅 담당은 사용자 `101`·`202`의 분리된 이력을 조회 함수와 pytest로 검증한다.
2. #10 채팅 담당은 #8의 소유권 조회 함수를 API에 연결하고, #11에서 짧은 트랜잭션에 질문+`pending` 답변을 만든 뒤 트랜잭션 밖에서 모의 제공자를 호출한다. 완료 결과만 저장한다.
3. #18 화면 담당은 이 형식으로 목록·전송·새로고침 재조회를 연결한다. 실제 인증 통합은 위 인증 기준에 따라 별도로 검증한다.

- [ ] 두 사용자에게 대화·메시지가 섞이지 않고, 비인증 401과 타인 대화 404가 구분된다.
- [ ] `bigint` ID를 문자열로 유지하고 목록 페이지에 누락·중복이 없다.
- [ ] 공백 질문은 422, 같은 키·다른 본문은 409, 같은 키·같은 본문은 기존 결과다.
- [ ] 성공 답변은 저장 후 재조회되며 실패·중단 답변은 완료나 다음 문맥으로 취급하지 않는다.
- [ ] 모의 제공자 호출 중 DB 트랜잭션을 열어 두지 않는다. 오래된 `pending`의 유한 회복값은 #11 시작 전에 PM이 D4에서 정한다.

이 문서는 후속 라우트·ORM 테이블·인증 보안 동작의 구현 완료를 뜻하지 않는다. 스트리밍, 수동 재시도, 실제 제공자와 비용·회원 한도는 후속 결정이다.
