-- S08 적용: conversations → messages. PostgreSQL 17.
-- 선행: S02의 0001(users)이 먼저 적용되어 있어야 한다. users(id)는 bigint 기본키다.
-- 기준: docs/database.md, docs/database-reference.sql. 되돌림은 0002_conversations_messages.down.sql.
-- 이 파일에는 BEGIN/COMMIT이 없다. 실행하는 쪽이 한 트랜잭션으로 감싼다(psql -1 등).
-- 초기 계정·대화 삭제 기능 없음. 모든 FK는 ON DELETE RESTRICT.

CREATE TABLE conversations (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT conversations_user_fk FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
);
-- 내 대화 목록용.
CREATE INDEX conversations_owner_list_idx ON conversations (user_id, updated_at DESC, id DESC);

CREATE TABLE messages (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id bigint NOT NULL,
  role text NOT NULL,
  status text NOT NULL,
  content text,
  request_key text,
  reply_to_message_id bigint,
  attempt_no integer,
  retry_key text,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT messages_conversation_fk FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE RESTRICT,
  -- 아래 복합 FK가 참조할 대상. 답변이 같은 대화의 메시지만 가리키게 한다.
  CONSTRAINT messages_id_conversation_uq UNIQUE (id, conversation_id),
  CONSTRAINT messages_reply_same_conversation_fk FOREIGN KEY (reply_to_message_id, conversation_id)
    REFERENCES messages(id, conversation_id) ON DELETE RESTRICT,
  CONSTRAINT messages_role_ck CHECK (role IN ('user', 'assistant')),
  CONSTRAINT messages_status_ck CHECK (status IN ('pending', 'completed', 'failed', 'interrupted')),
  -- 질문(user)과 답변(assistant)이 각각 가질 수 있는 필드 조합.
  CONSTRAINT messages_shape_ck CHECK (
    (role = 'user' AND status = 'completed' AND content IS NOT NULL AND length(btrim(content)) > 0
      AND request_key IS NOT NULL AND length(request_key) > 0 AND reply_to_message_id IS NULL
      AND attempt_no IS NULL AND retry_key IS NULL AND error_code IS NULL AND completed_at IS NOT NULL)
    OR
    (role = 'assistant' AND reply_to_message_id IS NOT NULL AND attempt_no IS NOT NULL AND attempt_no >= 1
      AND request_key IS NULL AND ((attempt_no = 1 AND retry_key IS NULL)
                             OR (attempt_no > 1 AND retry_key IS NOT NULL AND length(retry_key) > 0))
      AND ((status = 'completed' AND content IS NOT NULL AND length(content) > 0
            AND completed_at IS NOT NULL AND error_code IS NULL)
        OR (status = 'pending' AND content IS NULL AND completed_at IS NULL AND error_code IS NULL)
        OR (status IN ('failed', 'interrupted') AND content IS NULL AND completed_at IS NULL
            AND error_code IS NOT NULL)))
  )
);
-- 대화 안에서 같은 질문 재전송 키 중복 금지.
CREATE UNIQUE INDEX messages_question_key_uq ON messages (conversation_id, request_key)
  WHERE role = 'user';
-- 질문별 답변 시도 번호 중복 금지.
CREATE UNIQUE INDEX messages_attempt_uq ON messages (reply_to_message_id, attempt_no)
  WHERE role = 'assistant';
-- 질문별 재시도 키 중복 금지.
CREATE UNIQUE INDEX messages_retry_key_uq ON messages (reply_to_message_id, retry_key)
  WHERE role = 'assistant' AND retry_key IS NOT NULL;
-- 대화별 활성 pending 답변은 하나.
CREATE UNIQUE INDEX messages_one_pending_uq ON messages (conversation_id)
  WHERE role = 'assistant' AND status = 'pending';
-- 대화 안의 표시 순서용.
CREATE INDEX messages_order_idx ON messages (conversation_id, id);
-- 답변 이력 조회용.
CREATE INDEX messages_reply_idx ON messages (reply_to_message_id, attempt_no DESC);
