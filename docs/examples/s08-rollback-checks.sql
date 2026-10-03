-- S08: 되돌림 검사. 0002_conversations_messages.down.sql 바로 뒤에 실행한다.
-- conversations와 messages는 없어야 하고 users는 그대로 있어야 한다.

\echo ===== 되돌림 뒤: conversations·messages 없음, users 유지 =====
SELECT to_regclass('conversations') IS NULL AS conversations_gone,
       to_regclass('messages') IS NULL AS messages_gone,
       to_regclass('users') IS NOT NULL AS users_kept;

DO $$
BEGIN
  IF to_regclass('conversations') IS NOT NULL OR to_regclass('messages') IS NOT NULL
     OR to_regclass('users') IS NULL THEN
    RAISE EXCEPTION 'FAIL  되돌림 결과가 기대와 다르다';
  END IF;
END;
$$;
