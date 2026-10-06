# S07 내 대화·메시지 조회 예시

가짜 사용자 둘로 대화 소유권 조회, 페이지 처리, 다음 AI 문맥에 들어갈 메시지를 확인한 예시다. 테이블 구조와 소유권 기준은 [DB 설계](../../04-reference/database.md), 정렬과 커서는 [공통 API 형식](../../04-reference/api.md)을 따른다. 가짜 행과 SQL 전문은 [s07-conversation-queries.sql](s07-conversation-queries.sql)에 있다.

이 예시는 [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8)의 ORM 조회 함수와 pytest가 따르는 기대 결과를 설명하는 참고 자료이며, 그 함수와 검사를 대신하지 않는다. 함수와 검사의 위치는 [ORM 조회 함수와 pytest](#orm-조회-함수와-pytest)에 있다.

이 예시는 마이그레이션이 아니며 실제 AI를 호출하지 않는다. 테이블은 확인할 때만 트랜잭션 안에서 임시로 만들고 되돌린다. 모든 사용자·대화·본문은 가짜다.

## 가짜 행

사용자 `101`은 로컬 계정, `202`는 Google 계정이다. 이메일이 같아도 다른 사용자다.

| 사용자 | 이메일 | 계정 종류 | 대화 |
| --- | --- | --- | --- |
| `101` | `a@example.test` | 로컬 | `501`, `503`, `505` |
| `202` | `a@example.test` | Google `(https://accounts.google.com, g-202)` | `502` |

사용자 `101`의 대화 사이에 사용자 `202`의 대화 `502`가 끼어 있다.

| 대화 | 주인 | 생성 시각(UTC) | 최근 변경(UTC) | 메시지 |
| --- | --- | --- | --- | --- |
| `501` | `101` | 03:05:00 | 06:00:00 | `9001`~`9006` |
| `502` | `202` | 04:00:00 | 04:01:00 | `9101`, `9102` |
| `503` | `101` | 05:00:00 | 05:00:00 | 없음 (빈 대화) |
| `505` | `101` | 05:30:00 | 05:31:00 | `9201`, `9202` |

대화 `501`은 가장 먼저 만들었지만 가장 최근에 변경했다. 목록이 변경 시각이 아니라 `id` 순서를 따르는지 확인하려고 이렇게 두었다.

대화 `501`에는 질문 셋과 답변 셋이 있다. 답변은 완료, 실패, 중단을 하나씩 둔다.

| 메시지 | 역할 | 상태 | 본문 | 참조 질문 | 오류 코드 | 생성 시각(UTC) |
| --- | --- | --- | --- | --- | --- | --- |
| `9001` | `user` | `completed` | 안녕? | | | 03:06:00 |
| `9002` | `assistant` | `completed` | 안녕하세요. | `9001` | | 03:06:00 |
| `9003` | `user` | `completed` | 오늘 할 일을 정리해 줘. | | | 03:10:00 |
| `9004` | `assistant` | `failed` | NULL | `9003` | `provider_failed` | 03:10:00 |
| `9005` | `user` | `completed` | 방금 질문 다시 답해 줘. | | | 03:12:00 |
| `9006` | `assistant` | `interrupted` | NULL | `9005` | `outcome_unknown` | 03:12:00 |

대화 `502`와 `505`에는 질문 하나와 완료 답변 하나씩이 있다. 질문 `9101`, `9201`의 `request_key`는 `9001`과 같은 `q-demo-1`이다. 요청 키는 대화 안에서만 유일하므로 충돌하지 않는다.

날짜는 모두 `2026-01-02`다. 오류 코드는 [공통 API 형식](../../04-reference/api.md)의 첫 목표 값을 사용했다.

## 대표 SQL

`:user_id`는 인증 미들웨어가 검증한 사용자 ID, `:conversation_id`는 요청한 대화 ID다. 화면이 보낸 `user_id`는 쓰지 않는다. `:before_id`와 `:after_id`는 이전 응답의 `next_cursor`이며 첫 페이지에서는 NULL이다.

`messages`에는 사용자 컬럼이 없다. 메시지의 주인은 `conversations`를 거쳐야 알 수 있으므로 모든 조회가 `conversations.user_id` 조건을 함께 건다.

### A. 내 대화 목록

`id` 내림차순이다. 변경 시각으로 정렬하면 페이지를 넘기는 동안 대화의 위치가 바뀔 수 있어 생성 순서를 쓴다.

```sql
SELECT c.id, c.user_id AS owner_id, c.created_at, c.updated_at
FROM conversations AS c
WHERE c.user_id = :user_id
  AND (:before_id::bigint IS NULL OR c.id < :before_id)
ORDER BY c.id DESC
LIMIT :limit;
```

### O. 대화의 존재와 소유권 확인

메시지를 읽기 전에 먼저 실행한다.

```sql
SELECT c.id
FROM conversations AS c
WHERE c.id = :conversation_id AND c.user_id = :user_id;
```

- **0행**: 찾을 수 없음이다. 없는 대화와 남의 대화는 결과가 같아 구분되지 않는다.
- **1행**: 내 대화다. 이어지는 B가 0행이어도 정상적인 빈 목록이다.

B의 0행만 보고 찾을 수 없음으로 처리하면 내 빈 대화까지 `404`가 되므로 두 단계로 나눈다.

### B. 내 대화의 메시지

`id` 오름차순이다. O를 통과한 뒤에 실행하지만 소유자 조건은 그대로 둔다.

```sql
SELECT m.id, c.user_id AS owner_id, m.role, m.status, m.content,
       m.reply_to_message_id AS reply_to, m.error_code, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
  AND (:after_id::bigint IS NULL OR m.id > :after_id)
ORDER BY m.id
LIMIT :limit;
```

### C. 다음 AI 문맥

B의 소유자 조건에 `status = 'completed'`를 더한다. 문맥은 페이지로 나누지 않는다.

```sql
SELECT m.id, m.role, m.content
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
  AND m.status = 'completed'
ORDER BY m.id;
```

## 사용자별 예상 결과

아래는 로컬 테스트 DB(PostgreSQL 17)에서 실제로 실행해 얻은 결과다. 페이지가 나뉘는 모습을 보이려고 `limit`를 작게 잡았다. API 기본값은 20이다.

| 조회 | 사용자 | 대화 | 조건 | 결과 |
| --- | --- | --- | --- | --- |
| A | `101` | | `limit` 2 | 2행: `505`, `503` |
| A | `101` | | `limit` 2, `before_id` 503 | 1행: `501` |
| A | `202` | | `limit` 2 | 1행: `502` |
| O | `101` | `501` | | 1행 |
| O | `202` | `501` | | **0행** (남의 대화) |
| O | `101` | `999` | | **0행** (없는 대화) |
| O | `101` | `503` | | 1행 |
| B | `101` | `501` | `limit` 4 | 4행: `9001`~`9004` |
| B | `101` | `501` | `limit` 4, `after_id` 9004 | 2행: `9005`, `9006` |
| B | `101` | `503` | `limit` 4 | 0행 (정상적인 빈 목록) |
| B | `202` | `502` | `limit` 4 | 2행: `9101`, `9102` |
| C | `101` | `501` | | 4행: `9001`, `9002`, `9003`, `9005` |

- **대화가 섞이지 않는다.** 사용자 `101`의 목록에 `502`가 없고 사용자 `202`의 목록에는 `502`만 있다.
- **없는 대화와 남의 대화가 같다.** 사용자 `202`가 대화 `501`을 확인한 결과와 사용자 `101`이 없는 대화 `999`를 확인한 결과가 모두 0행이다. 서버는 둘 다 [공통 API 형식](../../04-reference/api.md)의 `404`로 응답해 대화의 존재 여부를 알리지 않는다.
- **빈 대화는 찾을 수 없음이 아니다.** 대화 `503`은 O가 1행이고 B가 0행이다. 응답은 `items: []`다.
- **목록은 생성 순서다.** 대화 `501`은 가장 최근에 변경했지만 목록의 마지막에 나온다.
- **페이지에 누락·중복이 없다.** A의 두 페이지를 이으면 `505`, `503`, `501`이고 B의 두 페이지를 이으면 `9001`~`9006`이다.
- **실패·중단 답변은 문맥에서 빠진다.** C의 결과에 `9004`(`failed`)와 `9006`(`interrupted`)이 없다. 두 답변은 `content`가 NULL이라 문맥에 넣을 본문도 없다.

`next_cursor`는 반환한 마지막 항목의 ID이고 더 없으면 `null`이다. 예시 SQL은 다음 페이지가 있는지 판단하지 않는다. A의 두 번째 페이지와 B의 두 번째 페이지 뒤에는 행이 없으므로 `next_cursor`는 `null`이어야 한다.

조회 B 첫 페이지의 실행 결과:

```text
  id  | owner_id |   role    |  status   |         content         | reply_to |   error_code    |       created_at
------+----------+-----------+-----------+-------------------------+----------+-----------------+------------------------
 9001 |      101 | user      | completed | 안녕?                   |          |                 | 2026-01-02 03:06:00+00
 9002 |      101 | assistant | completed | 안녕하세요.             |     9001 |                 | 2026-01-02 03:06:00+00
 9003 |      101 | user      | completed | 오늘 할 일을 정리해 줘. |          |                 | 2026-01-02 03:10:00+00
 9004 |      101 | assistant | failed    |                         |     9003 | provider_failed | 2026-01-02 03:10:00+00
(4 rows)
```

## ORM 조회 함수와 pytest

위 SQL은 `backend/app/repositories/conversations.py`의 ORM 조회 함수로 구현했다. 함수는 [#10](https://github.com/next-stop-siren/Next-Stop-Siren/issues/10)의 API가 그대로 호출할 수 있게 세션과 서버가 확인한 사용자 ID를 받는다.

| 조회 | 함수 | 결과 |
| --- | --- | --- |
| A | `list_conversations` | 대화 목록 |
| O | `owns_conversation` | 내 대화면 `True` |
| O 뒤 B | `list_messages` | 찾을 수 없으면 `None`, 내 빈 대화면 빈 목록 |
| O 뒤 C | `list_context_messages` | 찾을 수 없으면 `None`, 아니면 완료된 메시지 |

예상 결과는 main의 공통 테스트 fixture(`orm_test_connection`)를 쓰는 `backend/tests/test_conversation_queries_db.py`가 검사한다. ID는 DB가 생성하므로 검사는 위 표의 고정 ID 대신 같은 구조의 행을 넣고 돌려받은 ID와 비교한다.

| 검증 | 근거가 되는 예시 | 테스트 |
| --- | --- | --- |
| 사용자별 데이터 격리 | A의 `101`·`202` 목록, B의 `502` | `test_each_user_sees_only_their_own_conversations_and_messages` |
| 없는 대화와 남의 대화가 같은 결과 | O의 `202`→`501`, `101`→`999` | `test_missing_and_another_users_conversation_are_the_same_not_found` |
| 본인 소유의 빈 대화는 빈 목록 | O와 B의 `503` | `test_own_conversation_without_messages_is_an_empty_list` |
| 여러 대화의 정렬·페이지 처리 | A의 두 페이지, B의 두 페이지 | `test_conversations_are_paged_by_descending_id`, `test_messages_are_paged_by_ascending_id` |
| 실패·중단 답변의 AI 문맥 제외 | C | `test_failed_and_interrupted_answers_are_left_out_of_the_context` |

검사는 저장소 루트에서 `./local.sh test-db`(Windows는 `.\local.ps1 test-db`)로 실행한다.

## PM 결정 대기: 답변이 실패한 질문

[DB 설계](../../04-reference/database.md)는 "완료된 답변만 다음 AI 문맥에 넣는다"고 정한다. 답변이 실패·중단된 **질문**을 문맥에 넣을지는 정해져 있지 않으며 PM이 결정한다.

조회 C는 질문을 걸러 내지 않으므로 답변 없는 질문 `9003`, `9005`가 문맥에 남는다. SQL 파일의 C-2는 완료 답변이 있는 질문만 남기는 변형이며 결과는 2행(`9001`, `9002`)이다. 결정 전까지 pytest는 실패·중단 **답변**이 빠지는 것만 확인한다.

## 직접 실행하기

로컬 테스트 DB 컨테이너가 떠 있어야 한다(`local.sh setup` 또는 `dev` 뒤). 저장소 루트에서 실행한다. macOS 터미널과 Windows PowerShell에서 같은 명령을 쓴다.

```sh
docker cp docs/04-reference/database-reference.sql b7-1-db-test-1:/tmp/ddl.sql
docker cp docs/examples/s07/s07-conversation-queries.sql b7-1-db-test-1:/tmp/s07.sql
docker exec b7-1-db-test-1 sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -q -c BEGIN -f /tmp/ddl.sql -f /tmp/s07.sql -c ROLLBACK'
```

`BEGIN`으로 시작해 테이블 생성, 가짜 행 입력, 조회를 한 뒤 `ROLLBACK`으로 전부 되돌린다. 실행 뒤 테스트 DB에 테이블과 행이 남지 않는다. Windows PowerShell에서 한글이 깨져 보이면 먼저 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`을 실행한다.

확인 범위는 Windows 11의 Docker Desktop과 Compose 프로젝트 `b7-1`의 테스트 DB다. macOS에서는 실행하지 않았다. 컨테이너 이름이 다르면 `docker ps`에서 `db-test` 컨테이너 이름으로 바꾼다.
