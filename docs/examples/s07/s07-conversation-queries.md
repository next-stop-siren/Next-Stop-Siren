# S07 내 대화·메시지 조회 예시

가짜 사용자 둘로 대화 소유권 조회와 다음 AI 문맥에 들어갈 메시지를 확인한 예시다. 테이블 구조와 소유권 기준은 [DB 설계](../../database.md)를 따른다. 가짜 행과 SQL 전문은 [s07-conversation-queries.sql](s07-conversation-queries.sql)에 있다.

이 예시는 마이그레이션이 아니며 실제 AI를 호출하지 않는다. 테이블은 확인할 때만 트랜잭션 안에서 임시로 만들고 되돌린다. 모든 사용자·대화·본문은 가짜다.

## 가짜 행

사용자 `101`은 로컬 계정, `202`는 Google 계정이다. 이메일이 같아도 다른 사용자다.

| 사용자 | 이메일 | 계정 종류 | 대화 |
| --- | --- | --- | --- |
| `101` | `a@example.test` | 로컬 | `501` |
| `202` | `a@example.test` | Google `(https://accounts.google.com, g-202)` | `502` |

대화 `501`(주인 `101`)에는 질문 셋과 답변 셋이 있다. 답변은 완료, 실패, 중단을 하나씩 둔다.

| 메시지 | 역할 | 상태 | 본문 | 참조 질문 | 오류 코드 | 생성 시각(UTC) |
| --- | --- | --- | --- | --- | --- | --- |
| `9001` | `user` | `completed` | 안녕? | | | 03:06:00 |
| `9002` | `assistant` | `completed` | 안녕하세요. | `9001` | | 03:06:00 |
| `9003` | `user` | `completed` | 오늘 할 일을 정리해 줘. | | | 03:10:00 |
| `9004` | `assistant` | `failed` | NULL | `9003` | `provider_failed` | 03:10:00 |
| `9005` | `user` | `completed` | 방금 질문 다시 답해 줘. | | | 03:12:00 |
| `9006` | `assistant` | `interrupted` | NULL | `9005` | `outcome_unknown` | 03:12:00 |

대화 `502`(주인 `202`)에는 질문 `9101`과 완료 답변 `9102`가 있다. 질문 `9101`의 `request_key`는 `9001`과 같은 `q-demo-1`이다. 요청 키는 대화 안에서만 유일하므로 충돌하지 않는다.

날짜는 모두 `2026-01-02`다. 오류 코드는 [공통 API 형식](../../api.md)의 첫 목표 값을 사용했다.

## 대표 SQL

`$1`은 인증 미들웨어가 검증한 사용자 ID, `$2`는 요청한 대화 ID다. 화면이 보낸 `user_id`는 쓰지 않는다.

`messages`에는 사용자 컬럼이 없다. 메시지의 주인은 `conversations`를 거쳐야 알 수 있으므로 모든 조회가 `conversations.user_id` 조건을 함께 건다.

### A. 내 대화 목록

```sql
SELECT c.id, c.user_id AS owner_id, c.created_at, c.updated_at
FROM conversations AS c
WHERE c.user_id = $1
ORDER BY c.updated_at DESC, c.id DESC;
```

### B. 내 대화의 메시지

```sql
SELECT m.id, c.user_id AS owner_id, m.role, m.status, m.content,
       m.reply_to_message_id AS reply_to, m.error_code, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = $2 AND c.user_id = $1
ORDER BY m.id;
```

### C. 다음 AI 문맥

B에 `status = 'completed'` 조건을 더한다.

```sql
SELECT m.id, m.role, m.content
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = $2 AND c.user_id = $1
  AND m.status = 'completed'
ORDER BY m.id;
```

## 사용자별 예상 결과

아래는 로컬 테스트 DB(PostgreSQL 17.11)에서 실제로 실행해 얻은 결과다.

| 조회 | 사용자 | 대화 | 결과 |
| --- | --- | --- | --- |
| A | `101` | | 1행: `501` |
| A | `202` | | 1행: `502` |
| B | `101` | `501` | 6행: `9001`~`9006`, `id` 순서 |
| B | `202` | `501` | **0행** |
| B | `202` | `502` | 2행: `9101`, `9102` |
| C | `101` | `501` | 4행: `9001`, `9002`, `9003`, `9005` |

- **대화가 섞이지 않는다.** 사용자 `202`는 대화 번호 `501`을 알아도 행을 받지 못한다. 서버는 이 0행을 [공통 API 형식](../../api.md)의 `404`로 응답해 대화의 존재 여부를 알리지 않는다.
- **순서·시각·소유자를 추적한다.** B의 결과는 `id` 순서로 질문과 답변이 번갈아 나온다. 각 행에 생성 시각과 대화 주인이 있고, 답변의 `reply_to`가 질문을 가리킨다.
- **실패·중단 답변은 문맥에서 빠진다.** C의 결과에 `9004`(`failed`)와 `9006`(`interrupted`)이 없다. 두 답변은 `content`가 NULL이라 문맥에 넣을 본문도 없다.

조회 B의 실행 결과 일부:

```text
  id  | owner_id |   role    |   status    |         content         | reply_to |   error_code    |       created_at
------+----------+-----------+-------------+-------------------------+----------+-----------------+------------------------
 9001 |      101 | user      | completed   | 안녕?                   |          |                 | 2026-01-02 03:06:00+00
 9002 |      101 | assistant | completed   | 안녕하세요.             |     9001 |                 | 2026-01-02 03:06:00+00
 9003 |      101 | user      | completed   | 오늘 할 일을 정리해 줘. |          |                 | 2026-01-02 03:10:00+00
 9004 |      101 | assistant | failed      |                         |     9003 | provider_failed | 2026-01-02 03:10:00+00
 9005 |      101 | user      | completed   | 방금 질문 다시 답해 줘. |          |                 | 2026-01-02 03:12:00+00
 9006 |      101 | assistant | interrupted |                         |     9005 | outcome_unknown | 2026-01-02 03:12:00+00
(6 rows)
```

## 미정 사항: 답변이 실패한 질문

[DB 설계](../../database.md)는 "완료된 답변만 다음 AI 문맥에 넣는다"고 정한다. 답변이 실패·중단된 **질문**을 문맥에 넣을지는 정해져 있지 않다.

조회 C는 질문을 걸러 내지 않으므로 답변 없는 질문 `9003`, `9005`가 문맥에 남는다. SQL 파일의 C-2는 완료 답변이 있는 질문만 남기는 변형이며 결과는 2행(`9001`, `9002`)이다. S08·S10 구현 전에 PM의 결정이 필요하다.

## 직접 실행하기

로컬 테스트 DB 컨테이너가 떠 있어야 한다(`local.sh setup` 또는 `dev` 뒤). 저장소 루트에서 실행한다. macOS 터미널과 Windows PowerShell에서 같은 명령을 쓴다.

```sh
docker cp docs/database-reference.sql b7-1-db-test-1:/tmp/ddl.sql
docker cp docs/examples/s07/s07-conversation-queries.sql b7-1-db-test-1:/tmp/s07.sql
docker exec b7-1-db-test-1 sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB -v ON_ERROR_STOP=1 -q -c BEGIN -f /tmp/ddl.sql -f /tmp/s07.sql -c ROLLBACK'
```

`BEGIN`으로 시작해 테이블 생성, 가짜 행 입력, 조회를 한 뒤 `ROLLBACK`으로 전부 되돌린다. 실행 뒤 테스트 DB에 테이블과 행이 남지 않는다. Windows PowerShell에서 한글이 깨져 보이면 먼저 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`을 실행한다.

확인 범위는 Windows 11의 Docker Desktop과 Compose 프로젝트 `b7-1`의 테스트 DB다. macOS에서는 실행하지 않았다. 컨테이너 이름이 다르면 `docker ps`에서 `db-test` 컨테이너 이름으로 바꾼다.
