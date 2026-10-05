# 데이터베이스 설계

현재 앱에는 아래 테이블과 채팅 기능이 아직 없다. 이 문서는 PostgreSQL 17에서 구현할 **5개 테이블의 설계 기준**이다. ORM(테이블과 Python 클래스를 연결하는 모델)인 SQLAlchemy 2.x로 초기 스키마를 정의하며, [DDL 참고안](database-reference.sql)은 제약의 참고 자료이지 실행된 스키마나 필수 수기 DDL이 아니다. 현재 제품 ORM 모델은 없지만 SQLAlchemy Base·세션·모델 등록 지점과 `db-init`·`db-init-test` 명령은 구현되어 있다. [기술 구성](../01-start/stack.md)에 구현 경계를 정리했다.

```mermaid
erDiagram
  users ||--o{ auth_identities : has
  users ||--o{ refresh_sessions : has
  refresh_sessions o|--o| refresh_sessions : rotated_from
  users ||--o{ conversations : owns
  conversations ||--o{ messages : contains
  messages o|--o{ messages : replies_to
```

각 테이블의 기본키 `id`는 `bigint GENERATED ALWAYS AS IDENTITY`이며 FK 컬럼도 `bigint`다. `users.id`가 인증과 채팅의 공통 사용자 ID다. Google과 이메일·비밀번호 계정은 이메일이 같아도 별도 `users` 행이며 자동 연결하지 않는다. Google 로그인 키는 **검증된** `(issuer, subject)`이고 이메일은 식별 키가 아니다.

DB의 `bigint`는 JavaScript 안전 정수 범위를 넘을 수 있다. [공통 API 형식](api.md)은 ID를 JSON 십진 문자열로 전송한다. 화면에서 숫자로 변환하지 않는다.

## 테이블과 컬럼

`필수`는 `NOT NULL`, `NULL`(nullable)은 비워 둘 수 있음을 뜻한다. `기본값 없음`인 필수 필드는 삽입 시 값을 줘야 한다.

### `users` — 공통 사용자

| 필드 | 타입 | 필수 여부·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `bigint` | 필수 · identity | 기본키 |
| `email` | `text` | 필수 · 기본값 없음 | 연락·표시용 이메일 |
| `password_hash` | `text` | NULL · 기본값 없음 | 로컬 계정 해시; Google 전용 계정은 NULL |
| `created_at` | `timestamptz` | 필수 · `now()` | 생성 시각 |

### `auth_identities` — Google 신원

| 필드 | 타입 | 필수 여부·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `bigint` | 필수 · identity | 기본키 |
| `user_id` | `bigint` | 필수 · 기본값 없음 | `users.id` FK |
| `provider` | `text` | 필수 · `google` | 현재 허용된 제공자 |
| `issuer` | `text` | 필수 · 기본값 없음 | 검증된 발급자 |
| `subject` | `text` | 필수 · 기본값 없음 | 검증된 제공자 사용자 ID |
| `created_at` | `timestamptz` | 필수 · `now()` | 생성 시각 |

### `refresh_sessions` — refresh 세션

| 필드 | 타입 | 필수 여부·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `bigint` | 필수 · identity | 기본키 |
| `user_id` | `bigint` | 필수 · 기본값 없음 | `users.id` FK |
| `token_digest` | `bytea` | 필수 · 기본값 없음 | 32바이트 digest; 원문 토큰 저장 금지 |
| `rotated_from_id` | `bigint` | NULL · 기본값 없음 | 같은 사용자의 전임 세션 FK |
| `issued_at` | `timestamptz` | 필수 · `now()` | 발급 시각 |
| `expires_at` | `timestamptz` | 필수 · 기본값 없음 | 만료 시각 |
| `revoked_at` | `timestamptz` | NULL · 기본값 없음 | 폐기 시각 |

### `conversations` — 대화 소유권

| 필드 | 타입 | 필수 여부·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `bigint` | 필수 · identity | 기본키 |
| `user_id` | `bigint` | 필수 · 기본값 없음 | 소유자 `users.id` FK |
| `created_at` | `timestamptz` | 필수 · `now()` | 생성 시각 |
| `updated_at` | `timestamptz` | 필수 · `now()` | 최근 변경 시각; 서비스가 갱신 |

### `messages` — 질문과 답변 시도

| 필드 | 타입 | 필수 여부·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `bigint` | 필수 · identity | 기본키·표시 순서 |
| `conversation_id` | `bigint` | 필수 · 기본값 없음 | `conversations.id` FK |
| `role` | `text` | 필수 · 기본값 없음 | `user` 질문 또는 `assistant` 답변 |
| `status` | `text` | 필수 · 기본값 없음 | `pending`, `completed`, `failed`, `interrupted` |
| `content` | `text` | NULL · 기본값 없음 | 완료 본문; 대기·실패·중단 답변은 NULL |
| `request_key` | `text` | NULL · 기본값 없음 | 질문 재전송 식별 키 |
| `reply_to_message_id` | `bigint` | NULL · 기본값 없음 | 답변이 참조하는 같은 대화의 질문 FK |
| `attempt_no` | `integer` | NULL · 기본값 없음 | 답변 시도 번호 |
| `retry_key` | `text` | NULL · 기본값 없음 | 재시도 식별 키 |
| `error_code` | `text` | NULL · 기본값 없음 | 실패·중단 이유 코드 |
| `created_at` | `timestamptz` | 필수 · `now()` | 생성 시각 |
| `updated_at` | `timestamptz` | 필수 · `now()` | 최근 변경 시각; 서비스가 갱신 |
| `completed_at` | `timestamptz` | NULL · 기본값 없음 | 질문·완료 답변의 완료 시각 |

모든 FK는 `ON DELETE RESTRICT`다. **초기 범위에 계정·대화 삭제 기능은 없다.** FK는 참조하는 자식 행이 있을 때 부모 행의 삭제를 막는다. 보존 기간, 일괄 삭제, 백업·운영 로그 처리는 별도 결정이다.

## 제약과 조회 경로

- `auth_identities(issuer, subject)`는 Google 신원 중복을 막는다. `users`의 로컬 이메일 부분 유일 인덱스는 `password_hash IS NOT NULL`에만 적용한다. 서비스는 계정 종류와 신원 일치, 이메일 입력을 확인한다.
- `refresh_sessions.token_digest`는 유일하고 32바이트다. 전임 ID의 유일성은 후임을 하나로 제한하며 `(rotated_from_id, user_id) → (id, user_id)`는 같은 사용자 연결을 강제한다. `(user_id, expires_at DESC)`는 사용자 세션 조회용이다.
- `conversations(user_id, updated_at DESC, id DESC)`는 내 대화 목록용이다. `messages(conversation_id, id)`는 대화 안의 표시 순서용이다.
- `messages`의 부분 유일 인덱스는 대화의 `request_key`, 질문별 `attempt_no`·`retry_key`, 대화별 활성 `pending` 답변의 중복을 막는다. `messages(reply_to_message_id, attempt_no DESC)`는 답변 이력 조회용이다.
- `reply_to_message_id` 복합 FK는 같은 대화의 메시지만 참조하게 한다. 참조 대상이 실제 `user` 질문인지, 모든 읽기·쓰기 요청자가 대화 소유자인지는 서비스가 확인한다.

승인된 D1 기준은 [인증 구현 기준](authentication.md)에 있다. `token_digest`는 별도 비밀키의 HMAC-SHA-256(refresh JWT) 32바이트다. 최초 로그인 시각 +14일의 절대 `expires_at`을 새 refresh에도 그대로 적용한다.

같은 사용자의 `users` 행을 잠가 신규 로그인·refresh·재사용 폐기·logout을 순서대로 처리한다. 이전 refresh 폐기와 새 refresh 하나의 삽입, 5초 경합 판정, 그 이후의 세션 연결 전체 폐기도 이 잠금 아래 수행한다. 한 로그인에서 이어진 세션 연결은 최초 세션을 포함해 정상 발급 최대 4095개다. 다음 발급 전에 활성 연결을 폐기한다. 조회 상한은 4096개이며 연결이 손상되면 503과 운영 경보다. DDL만으로 이 동작이 구현되지는 않는다.

## 저장과 소유권

가짜 예시: 사용자 `101`(`a@example.test`, 로컬)과 `202`(`a@example.test`, Google `(https://accounts.google.com, g-202)`)는 다른 행이다. 사용자 `101`의 대화 `501`에는 질문 `9001`(`request_key=q-demo-1`, `안녕?`)과 완료 답변 `9002`(`reply_to_message_id=9001`, `attempt_no=1`, `안녕하세요.`)가 있다. 사용자 `202`가 대화 `501`을 조회하거나 쓰면 서버는 찾을 수 없음으로 응답한다.

```sql
-- $1: 인증 미들웨어가 검증한 사용자 ID, $2: 요청한 대화 ID
SELECT m.id, m.role, m.status, m.content, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = $2 AND c.user_id = $1
ORDER BY m.id;
```

1. 인증 미들웨어가 확인한 사용자 정보의 `user_id`로 `conversations.id`와 소유자를 함께 조회한다. 모든 대화 읽기·쓰기에 이 조건을 적용한다. `messages` FK만으로 사용자 격리를 보장하지 않는다.
2. 공백만인 질문은 거부한다. 질문과 첫 `pending` 답변을 짧은 트랜잭션에서 생성한다. 같은 요청 키의 다른 본문은 충돌로 처리하고, 동일 재전송은 기존 결과를 조회한다.
3. 완료된 답변만 다음 AI 문맥에 넣는다. 실패·중단 답변의 `content`는 NULL이다. 상태 전이와 `updated_at` 갱신은 서비스가 담당한다.

첫 내부 목표는 모의 제공자의 완성 답변 저장·재조회다. 수동 재시도·스트리밍의 상세 규칙과 회원 한도는 후속 단계에서 정한다. 멈춘 답변의 생성·회복 기한은 #11 시작 전에 PM이 정한다.

## 소유자와 초기 적용

1. PM 공통 엔진·세션·모델 등록, 개발·테스트 DB 분리, 테스트 세션과 초기화 명령은 현재 `main`에 구현되어 있다. 제품 모델 등록 지점은 비어 있어 현재 초기화할 제품 테이블은 없다. 초기 빈 개발·테스트 DB에서 ORM 메타데이터의 `create_all()`로 테이블을 만든다. 테이블 간 FK 순서는 모델 등록과 메타데이터가 다룬다.
2. 인증 담당은 `users`, `auth_identities`, `refresh_sessions` ORM 모델과 DB 검사를 맡는다. 채팅 담당은 인증 모델을 인계받아 `conversations`, `messages` ORM 모델과 DB 검사를 맡는다. 직접 선행 관계는 [공정진행도](../02-team/development-flow.md)의 #3 → #9 → #8 → #10을 따른다.
3. 조회 함수는 사용자 소유권 조건을 포함하고, API 요청·응답의 Pydantic 모델과 ORM 모델을 분리한다. 실제 JWT/Google 보호와 사용자 간 격리는 통합 때 확인한다.

`create_all()`은 기존 테이블을 `ALTER`하지 않는다. 기존 데이터나 스키마를 자동 삭제·재생성하지 않고 요청마다 테이블을 만들지 않는다. 이후 기존 스키마 변경은 적용·복구와 데이터 영향을 검토한 명시적 절차를 먼저 정한다. 도구는 아직 선택하지 않았다.

최소 완료 확인:

- [ ] 빈 전용 테스트 DB에 다섯 ORM 모델의 테이블·인덱스·제약이 생성된다.
- [ ] 잘못된 FK와 중복 `request_key`·답변 시도·`retry_key`가 거부된다.
- [ ] 같은 이메일의 로컬·Google 계정이 분리되고 Google은 검증된 `(issuer, subject)`로 식별된다.
- [ ] 타인 대화의 읽기·쓰기가 거부된다.
- [ ] 완료 답변만 재조회·문맥에 포함되고 실패·중단 답변은 문맥에서 빠진다.

D1 인증 기준과 D4 재시도/회복값의 동작을 각각 구현 뒤 시험한다. 이 문서 작업에서는 **SQL을 실행하지 않았다**.
