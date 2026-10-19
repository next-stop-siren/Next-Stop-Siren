# 백엔드 작업 지침

[회원 채팅 정책](../04-reference/chat-policy.md)의 접수·실행·구독 분리와 영속 복구는 미구현 목표다. #52 기반과 #15 예약·정산 인터페이스를 먼저 합의하고 #53에서 API와 실행 시 문맥, 재시도, SSE를 결합한다.

연결 중단으로 생성을 중단하는 현재 스트림과 목표 동작을 구분한다. 목표에서는 결과 불명 호출을 자동 반복하지 않고 늦은 결과의 덮어쓰기를 막는다. 제공자 호출·비용의 exactly-once는 보장하지 않는다.

FastAPI에서는 라우터가 HTTP 입력·출력을, 서비스가 업무 처리를, 저장소 계층이 SQLAlchemy 2.x ORM을 통한 PostgreSQL 접근을 맡는다. 요청·응답의 Pydantic 모델은 ORM 모델과 분리한다. 공통 동기 엔진·세션·모델 등록은 `app.db`와 `app.models`에 있다. 현재의 상태 확인 경로는 기존 `psycopg` 연결을 사용한다. [기술 구성](../01-start/stack.md)에 현재와 선택한 구성을 구분했다. 외부 AI 호출은 별도 모듈에 모아 시간 제한과 실패 처리를 둔다. `app/api/`, `app/services/`, `app/repositories/`, `app/integrations/`처럼 책임이 드러나는 구조를 기본으로 한다.

## 코드와 인증

### ORM 사용 예

새 제품 모델은 `app.db.Base`를 상속하고 `app/models/` 아래에 둔다. 모듈을 추가한 뒤 `app.models.register_models()` 안에서 해당 모듈을 import해야 초기화가 테이블을 발견한다. 예를 들어 아래 `Example`은 설명용이며 저장소에는 구현하지 않는다.

```python
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base

class Example(Base):
    __tablename__ = "examples"
    id: Mapped[int] = mapped_column(primary_key=True)
```

동기 FastAPI 라우터는 `Depends(get_session)`으로 세션을 받고, 변경을 저장할 지점에서 직접 `commit()`을 호출한다. 실패한 작업은 필요하면 명시적으로 `rollback()`한다. 의존성은 요청 종료 시 열린 트랜잭션을 롤백하고 세션을 닫으며 자동 커밋하지 않는다.

```python
from typing import Annotated
from fastapi import Depends
from sqlalchemy.orm import Session
from app.db import get_session

def save_example(session: Annotated[Session, Depends(get_session)]) -> None:
    session.add(Example())
    session.commit()
```

`DATABASE_URL`의 `postgresql://` 또는 `postgres://` 주소는 내부에서 `postgresql+psycopg://`로 정규화한다. 연결은 최대 3초, 풀 대기는 최대 5초, 문장 실행은 최대 5초로 제한한다. `db-init`과 `db-init-test`는 누락 테이블만 생성하고 기존 테이블·행을 바꾸지 않는다. 기존 스키마를 마이그레이션하거나 차이를 조정하지 않는다.

Python 모듈·함수·변수는 `snake_case`, 클래스는 `PascalCase`를 쓴다.

공개 함수의 입력·반환 타입을 명시하고 외부 입력은 경계에서 검증한다. 후속 인증 기능은 이메일·비밀번호 가입과 Google 로그인을 모두 포함한다. 이메일 인증과 비밀번호 찾기는 후속 범위다. Google OIDC 로그인 결과와 외부 식별자는 서버에서 검증한다.

같은 이메일의 Google 계정과 로컬 계정은 별개다. 이메일 일치만으로 계정을 연결하지 않는다. 계정 연결 기능은 이번 범위에 없다.

승인된 비밀번호 해시·로그인 제한, JWT 종류·서명·절대 만료, refresh 교체·재사용·CSRF·logout 규칙은 [인증 구현 기준](../04-reference/authentication.md)을 따른다. 같은 사용자의 로그인·refresh·logout은 사용자 행을 잠가 순서대로 처리한다. 쿠키·토큰은 DB 변경을 저장한 뒤에만 발급한다. logout 뒤 이미 발급한 access는 최대 15분 유효하다.

보호된 요청과 사용자별 대화 조회·변경에서는 서버에서 확인한 사용자 ID와 소유권을 검사한다.

## 채팅과 오류

채팅에는 필요한 이전 문맥을 전달하고 질문·응답을 사용자별로 저장·조회한다. S11 실제 연결의 고정 모델·30초 기한·자동 재시도 금지·완성 쌍 문맥은 [GPT 기준](../04-reference/ai.md)을 따른다. 완성 쌍 필터링은 `list_context_messages()`와 관련 테스트에 구현되어 있으며 DB 이력은 유지한다. 화면 이력 연결은 후속 작업이다. 예상 가능한 실패는 명시적인 예외·결과로 처리하며 모든 오류를 잡아 성공 응답으로 바꾸지 않는다. 외부 서비스·DB 오류의 세부 정보나 비밀값은 클라이언트에 노출하지 않고 서버 기록과 사용자 응답을 분리한다.

저장과 응답의 순서를 정해 부분 실패가 성공으로 보이지 않게 한다.

### S11 GPT 어댑터와 격리 스모크 시험

`app/integrations/codyssey.py`의 `CodysseyGptProvider`가 [GPT 기준](../04-reference/ai.md)의 실제 어댑터다. 공개 서비스의 `get_provider()`는 #15·#19와 배포 검증 전까지 모의 제공자를 유지하며, 실제 어댑터는 격리 스모크 시험에서만 연결한다.

- **설정:** 서버 `.env`의 `CODYSSEY_API_KEY`(본인 키)·`CODYSSEY_BASE_URL`·`CODYSSEY_MODEL`을 읽는다. URL·모델이 승인값과 다르면 거부한다.
- **호출:** 표준 라이브러리 `http.client`로 한 번만 요청한다. 자동 재시도·모델 대체가 없고 429·할당 부족을 포함한 비정상 응답은 `ProviderFailed`(기존 502)다.
- **완성 판정:** `finish_reason`이 `stop`인 답변만 완성이다. 길이 제한(`length`)·필터링(`content_filter`)·이유 없음·문자열이 아닌 값(배열·객체 등)은 `ProviderFailed`로 `failed` 저장하며 `completed`로 저장하지 않는다.
- **30초 기한·취소:** 기한이 지나면 연결을 닫아 대기를 끝내고 `ProviderTimeout`(기존 504)을 낸다. 연결 대기 중에 기한이 지나면 연결이 열린 뒤에도 요청을 보내지 않는다. 취소는 전송 완료를 기다리지 않으며, 지연된 전송은 닫힌 소켓에서 실패한다. 늦은 결과는 버린다. 제공자 쪽 생성·차감 중단은 보장하지 않는다.
- **기록:** 어댑터는 HTTP 상태·소요 시간만, 채팅 서비스는 `trace_id`·결과·소요 시간만 남긴다.

모의 검사는 `tests/test_codyssey_provider.py`가 로컬 대체 서버로 수행한다. 실제 스모크는 `live` 표시가 있어 `--run-live` 없이는 CI·`-m db`에서도 건너뛴다. 대시보드에서 남은 할당을 확인한 뒤 합성 질문 1회로 실행하고, 출력된 상태·소요 시간만 증거로 남긴다. 테스트 DB를 쓰므로 실행 전에 `db-test`를 켜고 [온보딩](../01-start/onboarding.md)대로 `TEST_DATABASE_URL`을 지정한다.

```bash
cd backend
uv run --no-sync --env-file ../.env pytest tests/test_codyssey_live.py --run-live -s -q
```

## API·DB 변경과 검사

백엔드 형식 정리와 린트에는 이미 구성된 Ruff 명령을 사용한다. 실행 명령과 검사 범위는 루트 [README](../../README.md)와 `backend/pyproject.toml`을 확인하고 PR 검사 결과에 포함한다.

API 요청·응답 형식에는 메서드·경로, 인증 필요 여부, 요청·성공 응답, 필드 제약, 오류 상태와 예시를 적는다. 잘못된 입력은 4xx, 서버·외부 의존성 실패는 5xx로 구분한다. 첫 모의 응답의 구체적인 형식은 [공통 API 형식](../04-reference/api.md)에 있다. 후속 경로는 실제 구현 전에 화면 담당자와 함께 검토한다.

API 탐색은 FastAPI가 제공하는 Swagger UI를 기본으로 하고 Postman은 선택 도구로 쓴다.

초기 빈 개발·테스트 DB는 `./local.sh db-init`·`./local.sh db-init-test` (Windows는 `.\local.ps1`)에서 ORM 메타데이터의 `create_all()`로 만든다. 현재 등록된 제품 모델은 5개 테이블이며 출력 수는 신규 생성 수가 아니라 등록 수다. 전체 설치 뒤 인증 키 설정 → `db-init` → `dev` → 실제 API 확인 순서는 [온보딩](../01-start/onboarding.md)을 따른다. 상태 확인 경로는 인증 설정·스키마 완성을 검사하지 않는다. `create_all()`은 기존 스키마를 바꾸지 않으므로 이후 변경은 적용·복구와 기존 데이터 영향을 검토한 명시적 절차를 먼저 정한다. 공유 DB를 직접 수동 변경하지 않는다.
