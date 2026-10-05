# S08 대화·메시지 ORM 모델 검사 기록

`conversations`와 `messages`의 SQLAlchemy ORM 모델과, 전용 테스트 DB에서 실행하는 pytest 제약 검사의 기록이다. 테이블 구조는 [DB 설계](../../04-reference/database.md)를 따르고, 제약 이름과 조건은 [DDL 참고안](../../04-reference/database-reference.sql)과 대조했다. 초기화와 테스트 환경은 [백엔드 지침](../../03-development/backend.md)과 [품질·테스트 지침](../../03-development/quality.md)의 공통 구성을 쓴다.

**선행 #3의 사용자 ORM 모델은 아직 `main`에 없다.** `conversations.user_id`는 `users.id`를 참조하므로 #3이 병합되기 전에는 `db-init`·`db-init-test`·`test-db`가 `users` 테이블을 찾지 못해 실패한다. 아래 결과는 저장소에 넣지 않은 로컬 임시 `users` 모델로 얻은 사전 확인이다. 실제 사용자 모델과의 연동은 **NOT RUN**이다.

## 파일

| 파일 | 역할 |
| --- | --- |
| [conversation.py](../../../backend/app/models/conversation.py) | `Conversation`, `Message` ORM 모델과 인덱스 |
| [models/\_\_init\_\_.py](../../../backend/app/models/__init__.py) | `register_models()`에 모델 모듈 등록 |
| [test_conversation_models_db.py](../../../backend/tests/test_conversation_models_db.py) | 공통 `orm_test_connection` fixture를 쓰는 제약 검사 |

수기 적용·되돌림 SQL(`backend/migrations/`)과 `psql` 검사 SQL은 삭제했다. 테이블은 ORM 메타데이터의 `create_all()`로만 만든다. DDL 참고안과 다른 점은 FK에도 이름을 붙인 것 하나다. 컬럼, 타입, 조건, 인덱스 이름은 참고안과 같다.

## DB 제약과 서비스가 확인할 범위

| 항목 | DB 제약 | 서비스 |
| --- | --- | --- |
| 대화의 사용자, 메시지의 대화가 실제로 있음 | FK `conversations_user_fk`, `messages_conversation_fk` | — |
| 답변의 참조 대상이 **같은 대화의 메시지** | 복합 FK `messages_reply_same_conversation_fk` | — |
| 참조 대상이 실제 질문(`role=user`) | 보장하지 않음 | 확인한다 |
| 요청자가 대화 소유자 | 보장하지 않음 | 모든 읽기·쓰기에서 확인한다 |
| `request_key`·답변 시도 번호·`retry_key`·대화별 `pending` 중복 | 부분 유일 인덱스 | 충돌을 기존 결과 조회나 오류 응답으로 바꾼다 |
| `role`·`status`별 본문·완료 시각·오류 코드 조합 | CHECK `messages_shape_ck` | 상태 전이와 `updated_at` 갱신 |
| 참조 중인 사용자·대화·질문 삭제 | `ON DELETE RESTRICT`로 거부 | 초기 범위에 삭제 기능 없음 |

이 ORM 모델만으로 사용자 접근 제한이 완성되지는 않는다. 소유권 조건이 들어간 조회 함수는 #8, API는 #10에서 다룬다. `test_database_alone_does_not_require_a_reply_target_to_be_a_question`은 답변을 가리키는 답변을 DB가 받아들인다는 것을 보여 주어 이 경계를 고정한다.

## 검사 결과

로컬 전용 테스트 DB(PostgreSQL 17)에서 `test-db`로 실행했다. 각 테스트는 바깥 트랜잭션 안에서 테이블을 만들고 끝나면 롤백하므로 테스트 DB에 테이블이나 행이 남지 않는다.

| 검사 | 기대 | 테스트 |
| --- | --- | --- |
| 타입·FK·CHECK·인덱스 생성 | `bigint`, `timestamptz`, 이름 있는 제약과 `RESTRICT` | `test_schema_matches_database_design` |
| 정상 데이터 저장 | 대화 2개, 메시지 7개 | `test_valid_rows_are_stored` |
| 없는 사용자·대화 참조 | FK 거부 | `test_missing_user_and_conversation_are_rejected` |
| 다른 대화의 메시지를 참조하는 답변 | FK 거부 | `test_reply_to_a_message_in_another_conversation_is_rejected` |
| 질문 키·답변 시도 번호·재시도 키·동시 `pending` 중복 | 유일 인덱스 거부 | `test_duplicate_keys_attempts_and_pending_answers_are_rejected` |
| 질문의 `role`·`status`·본문·요청 키·완료 시각 조건 | CHECK 거부 | `test_invalid_questions_are_rejected` |
| 답변의 `status`별 본문·완료 시각·오류 코드·재시도 키 조건 | CHECK 거부 | `test_invalid_answers_are_rejected`, `test_invalid_pending_answers_are_rejected` |
| 참조 중인 사용자·대화·**질문** 삭제 | FK 거부 | `test_referenced_user_conversation_and_question_cannot_be_deleted` |
| 반복 초기화 | 기존 행 유지 | `test_repeated_initialization_keeps_existing_rows` |

```text
36 passed, 22 deselected
```

이 중 34개가 이 작업의 검사이고 2개는 기존 공통 DB·ORM 검사다. 임시 `users` 없이 현재 브랜치만으로 실행하면 `users` 테이블이 없어 DB 검사가 실패한다. 같은 대화 안에서만 유일한 `request_key`는 다른 대화에서 다시 쓸 수 있고, 정상 데이터가 이를 포함한다. 잘못된 `status`는 필드 조합 검사에도 걸리므로 검사는 CHECK 이름을 하나로 고정하지 않는다. 서비스는 CHECK 제약 이름으로 원인을 구분하지 않는다.

확인 범위는 Windows 11의 Docker Desktop과 Compose 프로젝트 `b7-1`의 테스트 DB다. macOS에서는 실행하지 않았다.

## 직접 실행하기

저장소 루트에서 실행한다. Windows PowerShell에서는 `./local.sh` 대신 `.\local.ps1`을 쓴다. `TEST_DATABASE_URL` 설정은 [온보딩 §4](../../01-start/onboarding.md#4-테스트와-품질-검사)를 따른다.

```sh
./local.sh db-init-test   # 전용 테스트 DB에 누락 테이블만 생성
./local.sh test-db        # 제약 검사를 포함한 DB 통합 테스트
./local.sh db-init        # 개발 DB에 누락 테이블만 생성
```

`create_all()`은 없는 테이블만 만들고 기존 테이블과 행을 바꾸거나 지우지 않는다. 이미 만든 테이블의 구조 변경과 되돌림은 이 초기화의 범위가 아니며, [DB 설계](../../04-reference/database.md#소유자와-초기-적용)에 따라 명시적 절차를 먼저 정한다.

## #3 병합 뒤에 남은 일

- [ ] 최신 `main`을 작업 브랜치에 반영하고 #3의 모델이 `register_models()`에 함께 등록되는지 확인한다.
- [ ] 테스트의 사용자 생성(`add_user`)을 실제 `users` 필수 컬럼에 맞춘다. 지금은 `email`만 넣는다.
- [ ] `db-init-test`와 `test-db`를 임시 `users` 없이 실행하고 결과를 이 문서와 PR 본문에 반영한다.
