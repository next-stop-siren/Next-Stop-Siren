# 백엔드 작업 지침

FastAPI에서는 라우터가 HTTP 입력·출력을, 서비스가 업무 처리를, 저장소 계층이 SQLAlchemy 2.x ORM을 통한 PostgreSQL 접근을 맡는다. 요청·응답의 Pydantic 모델은 ORM 모델과 분리한다. 공통 동기 엔진·세션·모델 등록은 `app.db`와 `app.models`에 있다. 현재의 상태 확인 경로는 기존 `psycopg` 연결을 사용한다. [기술 구성](stack.md)에 현재와 선택한 구성을 구분했다. 외부 AI 호출은 별도 모듈에 모아 시간 제한과 실패 처리를 둔다. `app/api/`, `app/services/`, `app/repositories/`, `app/integrations/`처럼 책임이 드러나는 구조를 기본으로 한다.

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

승인된 비밀번호 해시·로그인 제한, JWT 종류·서명·절대 만료, refresh 교체·재사용·CSRF·logout 규칙은 [인증 구현 기준](authentication.md)을 따른다. 같은 사용자의 로그인·refresh·logout은 사용자 행을 잠가 순서대로 처리한다. 쿠키·토큰은 DB 변경을 저장한 뒤에만 발급한다. logout 뒤 이미 발급한 access는 최대 15분 유효하다.

보호된 요청과 사용자별 대화 조회·변경에서는 서버에서 확인한 사용자 ID와 소유권을 검사한다.

## 채팅과 오류

채팅에는 필요한 이전 문맥을 전달하고 질문·응답을 사용자별로 저장·조회한다. 예상 가능한 실패는 명시적인 예외·결과로 처리하며 모든 오류를 잡아 성공 응답으로 바꾸지 않는다. 외부 서비스·DB 오류의 세부 정보나 비밀값은 클라이언트에 노출하지 않고 서버 기록과 사용자 응답을 분리한다.

저장과 응답의 순서를 정해 부분 실패가 성공으로 보이지 않게 한다.

## API·DB 변경과 검사

백엔드 형식 정리와 린트에는 이미 구성된 Ruff 명령을 사용한다. 실행 명령과 검사 범위는 루트 [README](../README.md)와 `backend/pyproject.toml`을 확인하고 PR 검사 결과에 포함한다.

API 요청·응답 형식에는 메서드·경로, 인증 필요 여부, 요청·성공 응답, 필드 제약, 오류 상태와 예시를 적는다. 잘못된 입력은 4xx, 서버·외부 의존성 실패는 5xx로 구분한다. 첫 모의 응답의 구체적인 형식은 [공통 API 형식](api.md)에 있다. 후속 경로는 실제 구현 전에 화면 담당자와 함께 검토한다.

API 탐색은 FastAPI가 제공하는 Swagger UI를 기본으로 하고 Postman은 선택 도구로 쓴다.

초기 빈 개발·테스트 DB는 `./local.sh db-init`·`./local.sh db-init-test` (Windows는 `.\local.ps1`)에서 ORM 메타데이터의 `create_all()`로 만든다. 현재 앱 모델 등록은 비어 있으므로 명령은 0개 테이블을 보고하고 아무 테이블도 만들지 않는다. `create_all()`은 기존 스키마를 바꾸지 않으므로 이후 변경은 적용·복구와 기존 데이터 영향을 검토한 명시적 절차를 먼저 정한다. 공유 DB를 직접 수동 변경하지 않는다.
