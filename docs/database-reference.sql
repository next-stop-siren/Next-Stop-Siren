-- PostgreSQL 17 DB 설계 참고안. 실행/적용되지 않았음.
-- 초기 빈 개발·테스트 DB 테이블은 SQLAlchemy ORM metadata.create_all()로 만든다.
-- 이 SQL은 bigint·유일성·복합 FK·RESTRICT·CHECK 제약의 비교 자료이며 앱 초기화 명령은 아니다.
-- digest 산출법과 토큰 수명·회전·재사용 정책은 승인된 docs/authentication.md를 따른다.
-- 초기 계정/대화 삭제 기능 없음. 모든 FK는 ON DELETE RESTRICT.

CREATE TABLE users (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email text NOT NULL,
  password_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (email = btrim(email) AND length(email) BETWEEN 3 AND 320),
  CHECK (password_hash IS NULL OR length(password_hash) > 0)
);
-- 로컬 비밀번호 계정의 로그인 이름만 중복 금지. Google과 같은 이메일이어도 자동 연결하지 않는다.
CREATE UNIQUE INDEX users_local_email_uq ON users (lower(email)) WHERE password_hash IS NOT NULL;

CREATE TABLE auth_identities (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  provider text NOT NULL DEFAULT 'google' CHECK (provider = 'google'),
  issuer text NOT NULL,
  subject text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (length(issuer) > 0 AND length(subject) > 0),
  UNIQUE (issuer, subject)
);
CREATE INDEX auth_identities_user_idx ON auth_identities (user_id);

CREATE TABLE refresh_sessions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  token_digest bytea NOT NULL UNIQUE,
  rotated_from_id bigint UNIQUE,
  issued_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  UNIQUE (id, user_id),
  FOREIGN KEY (rotated_from_id, user_id) REFERENCES refresh_sessions(id, user_id) ON DELETE RESTRICT,
  CHECK (expires_at > issued_at),
  CHECK (octet_length(token_digest) = 32)
);
CREATE INDEX refresh_sessions_user_idx ON refresh_sessions (user_id, expires_at DESC);

CREATE TABLE conversations (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX conversations_owner_list_idx ON conversations (user_id, updated_at DESC, id DESC);

CREATE TABLE messages (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id bigint NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  status text NOT NULL CHECK (status IN ('pending', 'completed', 'failed', 'interrupted')),
  content text,
  request_key text,
  reply_to_message_id bigint,
  attempt_no integer,
  retry_key text,
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (id, conversation_id),
  FOREIGN KEY (reply_to_message_id, conversation_id) REFERENCES messages(id, conversation_id) ON DELETE RESTRICT,
  CHECK (
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
CREATE UNIQUE INDEX messages_question_key_uq ON messages (conversation_id, request_key)
  WHERE role = 'user';
CREATE UNIQUE INDEX messages_attempt_uq ON messages (reply_to_message_id, attempt_no)
  WHERE role = 'assistant';
CREATE UNIQUE INDEX messages_retry_key_uq ON messages (reply_to_message_id, retry_key)
  WHERE role = 'assistant' AND retry_key IS NOT NULL;
CREATE UNIQUE INDEX messages_one_pending_uq ON messages (conversation_id)
  WHERE role = 'assistant' AND status = 'pending';
CREATE INDEX messages_order_idx ON messages (conversation_id, id);
CREATE INDEX messages_reply_idx ON messages (reply_to_message_id, attempt_no DESC);
