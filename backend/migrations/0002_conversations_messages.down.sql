-- S08 되돌림: messages → conversations. 적용의 역순이다.
-- S02의 0001(users) 되돌림보다 먼저 실행한다. users는 건드리지 않는다.
-- 두 테이블의 행과 인덱스가 함께 사라진다. CASCADE와 IF EXISTS는 쓰지 않는다.
-- 다른 테이블이 참조하고 있거나 이미 없으면 실패해서 순서가 틀렸음을 알린다.

DROP TABLE messages;
DROP TABLE conversations;
