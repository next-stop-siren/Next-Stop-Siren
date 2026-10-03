-- S08: conversations·messages 제약 검사.
-- 실행 방법과 결과 기록은 s08-migration-checks.md에 있다.
-- 전제: 같은 트랜잭션 안에 users와 0002_conversations_messages.up.sql이 적용되어 있다.
-- 이 파일은 마이그레이션이 아니다. 모든 값은 가짜이며 S07 예시와 같은 번호를 쓴다.

SET TIME ZONE 'UTC';

-- 거부되어야 하는 SQL을 실행하고 오류 코드와 제약 이름을 확인한다.
-- 거부되지 않거나 다른 이유로 실패하면 예외를 던져 전체 실행을 멈춘다.
CREATE FUNCTION pg_temp.expect_reject(label text, stmt text, want_states text[], want_constraint text)
RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  got_state text;
  got_constraint text;
BEGIN
  BEGIN
    EXECUTE stmt;
  EXCEPTION WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS got_state = RETURNED_SQLSTATE, got_constraint = CONSTRAINT_NAME;
    IF got_state = ANY (want_states) AND got_constraint = want_constraint THEN
      RETURN format('PASS  %s  [%s %s]', label, got_state, got_constraint);
    END IF;
    RAISE EXCEPTION 'FAIL  %: 기대 % %, 실제 % %', label, want_states, want_constraint, got_state, got_constraint;
  END;
  RAISE EXCEPTION 'FAIL  %: 거부되지 않았다', label;
END;
$$;

-- ---------------------------------------------------------------------------
-- 1. users.id와 FK 컬럼의 타입
-- ---------------------------------------------------------------------------
\echo ===== 1. 타입: users.id와 conversations.user_id가 같아야 한다 =====
SELECT format_type(u.atttypid, u.atttypmod) AS users_id,
       format_type(c.atttypid, c.atttypmod) AS conversations_user_id,
       CASE WHEN u.atttypid = c.atttypid THEN 'PASS' ELSE 'FAIL' END AS result
FROM pg_attribute AS u, pg_attribute AS c
WHERE u.attrelid = 'users'::regclass AND u.attname = 'id'
  AND c.attrelid = 'conversations'::regclass AND c.attname = 'user_id';

DO $$
BEGIN
  IF (SELECT atttypid FROM pg_attribute WHERE attrelid = 'users'::regclass AND attname = 'id')
     IS DISTINCT FROM
     (SELECT atttypid FROM pg_attribute WHERE attrelid = 'conversations'::regclass AND attname = 'user_id')
  THEN
    RAISE EXCEPTION 'FAIL  users.id와 conversations.user_id의 타입이 다르다';
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. 정상 가짜 행: 모두 들어가야 한다
-- ---------------------------------------------------------------------------
-- id는 GENERATED ALWAYS라서, 예시 번호를 직접 넣으려면 OVERRIDING SYSTEM VALUE가 필요하다.
INSERT INTO users (id) OVERRIDING SYSTEM VALUE VALUES (101), (202);

INSERT INTO conversations (id, user_id, created_at, updated_at) OVERRIDING SYSTEM VALUE VALUES
  (501, 101, '2026-01-02T03:05:00Z', '2026-01-02T03:13:00Z'),
  (502, 202, '2026-01-02T04:00:00Z', '2026-01-02T04:01:00Z');

-- 질문. 9101의 request_key는 9001과 같지만 대화가 달라서 허용된다.
INSERT INTO messages (id, conversation_id, role, status, content, request_key,
                      created_at, updated_at, completed_at) OVERRIDING SYSTEM VALUE VALUES
  (9001, 501, 'user', 'completed', '안녕?',                 'q-demo-1',
   '2026-01-02T03:06:00Z', '2026-01-02T03:06:00Z', '2026-01-02T03:06:00Z'),
  (9003, 501, 'user', 'completed', '오늘 할 일을 정리해 줘.', 'q-demo-2',
   '2026-01-02T03:10:00Z', '2026-01-02T03:10:00Z', '2026-01-02T03:10:00Z'),
  (9101, 502, 'user', 'completed', '다른 사용자의 질문이야.', 'q-demo-1',
   '2026-01-02T04:00:00Z', '2026-01-02T04:00:00Z', '2026-01-02T04:00:00Z');

-- 답변. 질문 9003은 실패(시도 1) → 재시도 실패(시도 2) → 재시도 대기(시도 3) 순서다.
INSERT INTO messages (id, conversation_id, role, status, content, reply_to_message_id, attempt_no,
                      retry_key, error_code, created_at, updated_at, completed_at)
                      OVERRIDING SYSTEM VALUE VALUES
  (9002, 501, 'assistant', 'completed', '안녕하세요.', 9001, 1, NULL,       NULL,
   '2026-01-02T03:06:00Z', '2026-01-02T03:07:00Z', '2026-01-02T03:07:00Z'),
  (9004, 501, 'assistant', 'failed',    NULL,         9003, 1, NULL,       'provider_failed',
   '2026-01-02T03:10:00Z', '2026-01-02T03:10:05Z', NULL),
  (9005, 501, 'assistant', 'failed',    NULL,         9003, 2, 'r-demo-1', 'provider_failed',
   '2026-01-02T03:11:00Z', '2026-01-02T03:11:05Z', NULL),
  (9006, 501, 'assistant', 'pending',   NULL,         9003, 3, 'r-demo-2', NULL,
   '2026-01-02T03:12:00Z', '2026-01-02T03:12:00Z', NULL);

\echo ===== 2. 정상 가짜 행: 대화 2개, 메시지 7개 =====
SELECT (SELECT count(*) FROM conversations) AS conversations,
       (SELECT count(*) FROM messages) AS messages;

-- ---------------------------------------------------------------------------
-- 3. 잘못된 FK
-- ---------------------------------------------------------------------------
\echo ===== 3. 잘못된 FK =====
SELECT pg_temp.expect_reject('없는 사용자 999의 대화',
  $q$INSERT INTO conversations (user_id) VALUES (999)$q$,
  ARRAY['23503'], 'conversations_user_fk');

SELECT pg_temp.expect_reject('없는 대화 999의 메시지',
  $q$INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
     VALUES (999, 'user', 'completed', '질문', 'q-x', now())$q$,
  ARRAY['23503'], 'messages_conversation_fk');

SELECT pg_temp.expect_reject('다른 대화(501)의 질문 9001을 가리키는 대화 502의 답변',
  $q$INSERT INTO messages (conversation_id, role, status, content, reply_to_message_id, attempt_no,
                           retry_key, completed_at)
     VALUES (502, 'assistant', 'completed', '답변', 9001, 2, 'r-x', now())$q$,
  ARRAY['23503'], 'messages_reply_same_conversation_fk');

-- PostgreSQL 17은 RESTRICT 위반을 23503으로 알린다. 23001은 이후 버전 대비다.
SELECT pg_temp.expect_reject('대화가 있는 사용자 101 삭제',
  $q$DELETE FROM users WHERE id = 101$q$,
  ARRAY['23503', '23001'], 'conversations_user_fk');

SELECT pg_temp.expect_reject('메시지가 있는 대화 501 삭제',
  $q$DELETE FROM conversations WHERE id = 501$q$,
  ARRAY['23503', '23001'], 'messages_conversation_fk');

-- ---------------------------------------------------------------------------
-- 4. 중복
-- ---------------------------------------------------------------------------
\echo ===== 4. 중복 request_key·답변 시도·retry_key·pending =====
SELECT pg_temp.expect_reject('같은 대화 501의 중복 request_key q-demo-1',
  $q$INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
     VALUES (501, 'user', 'completed', '안녕?', 'q-demo-1', now())$q$,
  ARRAY['23505'], 'messages_question_key_uq');

SELECT pg_temp.expect_reject('질문 9001의 중복 답변 시도 1',
  $q$INSERT INTO messages (conversation_id, role, status, content, reply_to_message_id, attempt_no, completed_at)
     VALUES (501, 'assistant', 'completed', '또 답변', 9001, 1, now())$q$,
  ARRAY['23505'], 'messages_attempt_uq');

SELECT pg_temp.expect_reject('질문 9003의 중복 retry_key r-demo-1',
  $q$INSERT INTO messages (conversation_id, role, status, reply_to_message_id, attempt_no, retry_key, error_code)
     VALUES (501, 'assistant', 'failed', 9003, 9, 'r-demo-1', 'provider_failed')$q$,
  ARRAY['23505'], 'messages_retry_key_uq');

SELECT pg_temp.expect_reject('대화 501의 두 번째 pending 답변',
  $q$INSERT INTO messages (conversation_id, role, status, reply_to_message_id, attempt_no, retry_key)
     VALUES (501, 'assistant', 'pending', 9001, 2, 'r-demo-9')$q$,
  ARRAY['23505'], 'messages_one_pending_uq');

-- ---------------------------------------------------------------------------
-- 5. 값과 필드 조합
-- ---------------------------------------------------------------------------
\echo ===== 5. 허용하지 않는 값과 필드 조합 =====
SELECT pg_temp.expect_reject('허용하지 않는 role',
  $q$INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
     VALUES (501, 'system', 'completed', '질문', 'q-x', now())$q$,
  ARRAY['23514'], 'messages_role_ck');

-- PostgreSQL은 CHECK를 이름 순서로 검사한다. 잘못된 status는 messages_status_ck보다
-- 이름이 앞선 messages_shape_ck에도 걸리므로 그쪽 이름으로 보고된다.
SELECT pg_temp.expect_reject('허용하지 않는 status',
  $q$INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
     VALUES (501, 'user', 'done', '질문', 'q-x', now())$q$,
  ARRAY['23514'], 'messages_shape_ck');

SELECT pg_temp.expect_reject('공백만인 질문',
  $q$INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
     VALUES (501, 'user', 'completed', '   ', 'q-x', now())$q$,
  ARRAY['23514'], 'messages_shape_ck');

SELECT pg_temp.expect_reject('본문이 있는 실패 답변',
  $q$INSERT INTO messages (conversation_id, role, status, content, reply_to_message_id, attempt_no,
                           retry_key, error_code)
     VALUES (501, 'assistant', 'failed', '반쯤 쓴 답', 9001, 2, 'r-x', 'provider_failed')$q$,
  ARRAY['23514'], 'messages_shape_ck');

SELECT pg_temp.expect_reject('retry_key 없는 두 번째 시도',
  $q$INSERT INTO messages (conversation_id, role, status, reply_to_message_id, attempt_no, error_code)
     VALUES (501, 'assistant', 'failed', 9001, 2, 'provider_failed')$q$,
  ARRAY['23514'], 'messages_shape_ck');

-- 거부된 삽입이 행을 남기지 않았는지 확인한다.
\echo ===== 6. 거부 뒤 행 수는 그대로: 대화 2개, 메시지 7개 =====
SELECT (SELECT count(*) FROM conversations) AS conversations,
       (SELECT count(*) FROM messages) AS messages;

DO $$
BEGIN
  IF (SELECT count(*) FROM conversations) <> 2 OR (SELECT count(*) FROM messages) <> 7 THEN
    RAISE EXCEPTION 'FAIL  거부된 삽입이 행을 남겼다';
  END IF;
END;
$$;
