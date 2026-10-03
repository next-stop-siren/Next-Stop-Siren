# S08 대화·메시지 마이그레이션 검사 기록

`conversations`와 `messages` 마이그레이션 초안과, 로컬 테스트 DB에서 적용·제약 검사·되돌림을 실행한 기록이다. 테이블 구조는 [DB 설계](../../database.md)와 [DDL 참고안](../../database-reference.sql)을 따른다.

**S02의 실제 `users` 마이그레이션은 아직 없다.** 아래 결과는 시험 전용 임시 `users`로 얻었다. S02 → S08 실제 적용과 S08 → S02 되돌림은 **NOT RUN**이다.

## 파일

| 파일 | 역할 |
| --- | --- |
| [0002_conversations_messages.up.sql](../../../backend/migrations/0002_conversations_messages.up.sql) | 적용: `conversations` → `messages` |
| [0002_conversations_messages.down.sql](../../../backend/migrations/0002_conversations_messages.down.sql) | 되돌림: `messages` → `conversations` |
| [s08-stub-users.sql](s08-stub-users.sql) | 시험 전용 임시 `users`. 마이그레이션이 아니다 |
| [s08-constraint-checks.sql](s08-constraint-checks.sql) | 가짜 행 입력과 거부 검사 |
| [s08-rollback-checks.sql](s08-rollback-checks.sql) | 되돌림 뒤 테이블 상태 검사 |

참고안과 다른 점은 하나다. FK와 CHECK 제약에 이름을 붙였다. 컬럼, 타입, 조건, 인덱스 이름은 참고안과 같다.

## S02와 맞출 약속

S08 마이그레이션은 아래 네 가지를 전제로 한다. 모두 [DB 설계](../../database.md)에 이미 있는 기준이거나 그 기준에서 나온 값이다. 4번의 파일 위치와 번호만 이 작업에서 새로 제안한다.

1. 테이블 이름은 `users`다.
2. `users.id`는 `bigint` 기본키다. `conversations.user_id`도 `bigint`다.
3. `conversations.user_id` FK는 `ON DELETE RESTRICT`다. 대화가 있는 사용자 행은 삭제되지 않는다. 초기 범위에 계정·대화 삭제 기능은 없다.
4. 마이그레이션은 `backend/migrations/`에 `번호_이름.up.sql`과 `번호_이름.down.sql` 한 쌍으로 둔다. S02가 `0001`, S08이 `0002`다. 적용은 번호 오름차순, 되돌림은 내림차순이다. 파일에 `BEGIN`/`COMMIT`을 넣지 않고 실행하는 쪽이 한 트랜잭션으로 감싼다.

4번은 저장소에 마이그레이션 도구가 아직 없어서 새 의존성 없이 `psql`로 실행할 수 있는 형태를 골랐다. S02 담당자나 PM이 다른 도구를 정하면 SQL 본문은 그대로 두고 파일 형식만 옮긴다.

## 검사 결과

로컬 테스트 DB(PostgreSQL 17.11)에서 실행했다. 실행 전후 모두 테스트 DB의 테이블은 0개였다.

| 검사 | 기대 | 결과 |
| --- | --- | --- |
| `users` 없이 S08 적용 | 실패 | 실패: `relation "users" does not exist` |
| 임시 `users` 뒤 S08 적용 | 성공 | 성공 |
| `users.id`와 `conversations.user_id` 타입 | 같음 | `bigint` = `bigint` |
| 정상 가짜 행 입력 | 대화 2개, 메시지 7개 | 2개, 7개 |
| 없는 사용자 `999`의 대화 | 거부 | `23503` `conversations_user_fk` |
| 없는 대화 `999`의 메시지 | 거부 | `23503` `messages_conversation_fk` |
| 다른 대화의 질문을 가리키는 답변 | 거부 | `23503` `messages_reply_same_conversation_fk` |
| 대화가 있는 사용자 삭제 | 거부 | `23503` `conversations_user_fk` |
| 메시지가 있는 대화 삭제 | 거부 | `23503` `messages_conversation_fk` |
| 같은 대화의 중복 `request_key` | 거부 | `23505` `messages_question_key_uq` |
| 같은 질문의 중복 답변 시도 번호 | 거부 | `23505` `messages_attempt_uq` |
| 같은 질문의 중복 `retry_key` | 거부 | `23505` `messages_retry_key_uq` |
| 같은 대화의 두 번째 `pending` 답변 | 거부 | `23505` `messages_one_pending_uq` |
| 허용하지 않는 `role` | 거부 | `23514` `messages_role_ck` |
| 허용하지 않는 `status` | 거부 | `23514` `messages_shape_ck` |
| 공백만인 질문 | 거부 | `23514` `messages_shape_ck` |
| 본문이 있는 실패 답변 | 거부 | `23514` `messages_shape_ck` |
| `retry_key` 없는 두 번째 시도 | 거부 | `23514` `messages_shape_ck` |
| 거부 뒤 행 수 | 그대로 | 대화 2개, 메시지 7개 |
| S08 되돌림 | 두 테이블 없음, `users` 유지 | 일치 |
| 되돌린 뒤 다시 적용·되돌림 | 성공 | 성공 |

`23503`은 FK 위반, `23505`는 중복 위반, `23514`는 CHECK 위반이다.

- **다른 대화의 같은 `request_key`는 허용된다.** 대화 `501`의 질문 `9001`과 대화 `502`의 질문 `9101`은 둘 다 `q-demo-1`이다. 요청 키는 대화 안에서만 유일하다.
- **잘못된 `status`는 `messages_shape_ck`로 보고된다.** PostgreSQL은 CHECK를 이름 순서로 검사하고, 잘못된 `status`는 필드 조합 검사에도 걸린다. 서비스는 CHECK 제약 이름으로 원인을 구분하지 않는다.
- **행을 지우면 되돌릴 수 없다.** 되돌림은 두 테이블의 행을 함께 지운다. 데이터가 있는 DB에서는 되돌리기 전에 보존 여부를 확인한다.

거부 검사의 실행 결과 일부:

```text
===== 3. 잘못된 FK =====
 PASS  없는 사용자 999의 대화  [23503 conversations_user_fk]
 PASS  없는 대화 999의 메시지  [23503 messages_conversation_fk]
 PASS  다른 대화(501)의 질문 9001을 가리키는 대화 502의 답변  [23503 messages_reply_same_conversation_fk]
===== 4. 중복 request_key·답변 시도·retry_key·pending =====
 PASS  같은 대화 501의 중복 request_key q-demo-1  [23505 messages_question_key_uq]
 PASS  질문 9001의 중복 답변 시도 1  [23505 messages_attempt_uq]
 PASS  질문 9003의 중복 retry_key r-demo-1  [23505 messages_retry_key_uq]
 PASS  대화 501의 두 번째 pending 답변  [23505 messages_one_pending_uq]
===== 되돌림 뒤: conversations·messages 없음, users 유지 =====
 conversations_gone | messages_gone | users_kept
--------------------+---------------+------------
 t                  | t             | t
```

## 직접 실행하기

로컬 테스트 DB 컨테이너가 떠 있어야 한다(`local.sh setup` 또는 `dev` 뒤). 저장소 루트에서 실행한다. macOS 터미널과 Windows PowerShell에서 같은 명령을 쓴다.

```sh
docker exec b7-1-db-test-1 mkdir -p /tmp/s08
docker cp backend/migrations/0002_conversations_messages.up.sql b7-1-db-test-1:/tmp/s08/up.sql
docker cp backend/migrations/0002_conversations_messages.down.sql b7-1-db-test-1:/tmp/s08/down.sql
docker cp docs/examples/s08/s08-stub-users.sql b7-1-db-test-1:/tmp/s08/stub.sql
docker cp docs/examples/s08/s08-constraint-checks.sql b7-1-db-test-1:/tmp/s08/checks.sql
docker cp docs/examples/s08/s08-rollback-checks.sql b7-1-db-test-1:/tmp/s08/rollback.sql
docker exec b7-1-db-test-1 sh -c 'cd /tmp/s08 && psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -q -c BEGIN -f stub.sql -f up.sql -f checks.sql -f down.sql -f rollback.sql -f up.sql -f down.sql -f rollback.sql -c ROLLBACK'
```

`BEGIN`으로 시작해 임시 `users` 생성, S08 적용, 검사, 되돌림, 재적용, 재되돌림을 한 뒤 `ROLLBACK`으로 전부 되돌린다. 실행 뒤 테스트 DB에 임시 `users`를 포함해 아무 테이블도 남지 않는다. 검사가 하나라도 기대와 다르면 `FAIL`로 시작하는 오류와 함께 멈춘다.

Windows PowerShell에서 한글이 깨져 보이면 먼저 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`을 실행한다. 컨테이너 이름이 다르면 `docker ps`에서 `db-test` 컨테이너 이름으로 바꾼다.

확인 범위는 Windows 11의 Docker Desktop과 Compose 프로젝트 `b7-1`의 테스트 DB다. macOS에서는 실행하지 않았다.

## S02 뒤에 남은 일

- [ ] S02의 실제 `users.id` 타입이 `bigint`인지 확인한다. 검사 1번이 타입이 다르면 멈춘다.
- [ ] 위 명령의 `stub.sql` 자리에 S02의 `0001` 적용 파일을, 마지막 되돌림 뒤에 S02의 되돌림 파일을 넣어 깨끗한 테스트 DB에서 S02 → S08 적용과 S08 → S02 되돌림을 실행한다.
- [ ] 가짜 사용자 입력을 S02의 실제 `users` 컬럼에 맞춘다. 지금은 `id`만 넣는다.
- [ ] 결과를 이 문서에 반영하고 `s08-stub-users.sql`을 지운다.
