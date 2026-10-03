-- S07: 가짜 사용자 둘의 내 대화·메시지 조회와 완료 문맥 예시.
-- 실행 방법과 예상 결과는 s07-conversation-queries.md에 있다.
-- 전제: docs/database-reference.sql의 테이블이 같은 트랜잭션 안에 임시로 만들어져 있다.
-- 이 파일은 마이그레이션이 아니며 실제 AI를 호출하지 않는다. 모든 값은 가짜다.

SET TIME ZONE 'UTC';

-- ---------------------------------------------------------------------------
-- 1. 가짜 행
-- ---------------------------------------------------------------------------
-- id는 GENERATED ALWAYS라서, 예시 번호를 직접 넣으려면 OVERRIDING SYSTEM VALUE가 필요하다.

-- 사용자 101은 로컬 계정, 202는 Google 계정이다. 이메일이 같아도 다른 사용자다.
INSERT INTO users (id, email, password_hash, created_at) OVERRIDING SYSTEM VALUE VALUES
  (101, 'a@example.test', 'fixture-only-not-a-real-hash', '2026-01-02T03:04:05Z'),
  (202, 'a@example.test', NULL,                           '2026-01-02T03:04:30Z');

INSERT INTO auth_identities (id, user_id, issuer, subject, created_at) OVERRIDING SYSTEM VALUE VALUES
  (301, 202, 'https://accounts.google.com', 'g-202', '2026-01-02T03:04:30Z');

-- 대화의 주인은 conversations.user_id 하나로 정해진다.
INSERT INTO conversations (id, user_id, created_at, updated_at) OVERRIDING SYSTEM VALUE VALUES
  (501, 101, '2026-01-02T03:05:00Z', '2026-01-02T03:13:00Z'),
  (502, 202, '2026-01-02T04:00:00Z', '2026-01-02T04:01:00Z');

-- 질문(role = user)은 항상 completed이고 request_key가 있다.
INSERT INTO messages (id, conversation_id, role, status, content, request_key,
                      created_at, updated_at, completed_at) OVERRIDING SYSTEM VALUE VALUES
  (9001, 501, 'user', 'completed', '안녕?',            'q-demo-1',
   '2026-01-02T03:06:00Z', '2026-01-02T03:06:00Z', '2026-01-02T03:06:00Z'),
  (9003, 501, 'user', 'completed', '오늘 할 일을 정리해 줘.', 'q-demo-2',
   '2026-01-02T03:10:00Z', '2026-01-02T03:10:00Z', '2026-01-02T03:10:00Z'),
  (9005, 501, 'user', 'completed', '방금 질문 다시 답해 줘.', 'q-demo-3',
   '2026-01-02T03:12:00Z', '2026-01-02T03:12:00Z', '2026-01-02T03:12:00Z'),
  (9101, 502, 'user', 'completed', '다른 사용자의 질문이야.', 'q-demo-1',
   '2026-01-02T04:00:00Z', '2026-01-02T04:00:00Z', '2026-01-02T04:00:00Z');

-- 답변(role = assistant)은 reply_to_message_id로 같은 대화의 질문을 가리킨다.
-- 완료 답변만 content와 completed_at이 있다. 실패·중단 답변은 content가 NULL이고 error_code가 있다.
INSERT INTO messages (id, conversation_id, role, status, content, reply_to_message_id, attempt_no,
                      error_code, created_at, updated_at, completed_at) OVERRIDING SYSTEM VALUE VALUES
  (9002, 501, 'assistant', 'completed',   '안녕하세요.',          9001, 1, NULL,
   '2026-01-02T03:06:00Z', '2026-01-02T03:07:00Z', '2026-01-02T03:07:00Z'),
  (9004, 501, 'assistant', 'failed',      NULL,                   9003, 1, 'provider_failed',
   '2026-01-02T03:10:00Z', '2026-01-02T03:10:05Z', NULL),
  (9006, 501, 'assistant', 'interrupted', NULL,                   9005, 1, 'outcome_unknown',
   '2026-01-02T03:12:00Z', '2026-01-02T03:13:00Z', NULL),
  (9102, 502, 'assistant', 'completed',   '다른 사용자의 완료 답변입니다.', 9101, 1, NULL,
   '2026-01-02T04:00:00Z', '2026-01-02T04:01:00Z', '2026-01-02T04:01:00Z');

-- ---------------------------------------------------------------------------
-- 2. 조회
-- ---------------------------------------------------------------------------
-- :user_id는 인증 미들웨어가 검증한 사용자 ID, :conversation_id는 요청한 대화 ID다.
-- 서버 코드에서는 각각 $1, $2 자리의 파라미터로 넘긴다. 화면이 보낸 user_id는 쓰지 않는다.

\echo
\echo ===== A. 내 대화 목록: 사용자 101 =====
\set user_id 101
SELECT c.id, c.user_id AS owner_id, c.created_at, c.updated_at
FROM conversations AS c
WHERE c.user_id = :user_id
ORDER BY c.updated_at DESC, c.id DESC;

\echo ===== A. 내 대화 목록: 사용자 202 =====
\set user_id 202
SELECT c.id, c.user_id AS owner_id, c.created_at, c.updated_at
FROM conversations AS c
WHERE c.user_id = :user_id
ORDER BY c.updated_at DESC, c.id DESC;

\echo ===== B. 대화 501의 메시지: 주인 101이 조회 (6행) =====
\set user_id 101
\set conversation_id 501
SELECT m.id, c.user_id AS owner_id, m.role, m.status, m.content,
       m.reply_to_message_id AS reply_to, m.error_code, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
ORDER BY m.id;

\echo ===== B. 대화 501의 메시지: 남인 202가 조회 (0행) =====
\set user_id 202
\set conversation_id 501
SELECT m.id, c.user_id AS owner_id, m.role, m.status, m.content,
       m.reply_to_message_id AS reply_to, m.error_code, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
ORDER BY m.id;

\echo ===== B. 대화 502의 메시지: 주인 202가 조회 (2행) =====
\set user_id 202
\set conversation_id 502
SELECT m.id, c.user_id AS owner_id, m.role, m.status, m.content,
       m.reply_to_message_id AS reply_to, m.error_code, m.created_at
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
ORDER BY m.id;

\echo ===== C. 다음 AI 문맥: 사용자 101, 대화 501 (완료된 메시지만) =====
-- 실패(9004)·중단(9006) 답변은 status 조건에서 빠진다.
\set user_id 101
\set conversation_id 501
SELECT m.id, m.role, m.content
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
  AND m.status = 'completed'
ORDER BY m.id;

\echo ===== C-2. 미정 사항 비교: 완료 답변이 있는 질문만 문맥에 넣는 경우 =====
-- 답변이 실패·중단된 질문(9003, 9005)까지 빼는 변형이다. 어느 쪽을 쓸지는 정해지지 않았다.
SELECT m.id, m.role, m.content
FROM conversations AS c
JOIN messages AS m ON m.conversation_id = c.id
WHERE c.id = :conversation_id AND c.user_id = :user_id
  AND m.status = 'completed'
  AND (m.role = 'assistant'
       OR EXISTS (SELECT 1
                  FROM messages AS a
                  WHERE a.reply_to_message_id = m.id
                    AND a.conversation_id = m.conversation_id
                    AND a.role = 'assistant'
                    AND a.status = 'completed'))
ORDER BY m.id;
