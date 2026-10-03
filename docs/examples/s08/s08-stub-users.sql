-- S08 로컬 시험 전용 임시 users. 마이그레이션이 아니다.
-- S02의 실제 users 마이그레이션(0001)이 들어오면 이 파일 대신 그것을 적용한다.
-- 반드시 BEGIN ... ROLLBACK 안에서만 실행한다. 테스트 DB에 남기면 S02 적용이 "이미 존재"로 실패한다.
-- S08이 기대하는 계약만 담았다: 테이블 이름 users, 기본키 id bigint.

CREATE TABLE users (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
);
