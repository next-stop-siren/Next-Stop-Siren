# 기술 구성과 실행 경계

[회원 채팅 정책](../04-reference/chat-policy.md)은 미래 구현 기준이다. #52는 영속 작업·실행·복구와 스키마 적용·되돌림을, #15는 예약·정산을, #53은 API 통합을 맡는다. 현재의 다섯 제품 모델과 `create_all()`은 대기열 스키마를 제공하지 않는다. 스트리밍 #44와 재시도 #51은 병합됐지만 대기열과 운영 준비는 별도다.

현재 백엔드는 로컬 가입·로그인, 쿠키 세션, Google OIDC, 본인 대화·메시지 저장·재조회를 제공한다. 화면의 실제 인증·채팅 연결과 운영 배포는 후속 작업이다. 구현 계약은 [API](../04-reference/api.md), [DB](../04-reference/database.md), [인증](../04-reference/authentication.md)을 따른다.

| 영역 | 현재 저장소 | 남은 연결·검증 |
| --- | --- | --- |
| 화면 | React·TypeScript·Vite, 비회원 소개·인증 모달 | 실제 인증·회원 채팅 API 연결 |
| 서버 | FastAPI 라우터·서비스·저장소, health/ready·인증·대화 API | 화면과의 통합·배포 검증 |
| 저장 | PostgreSQL, SQLAlchemy 2.0.44 동기 Session, 제품 5개 테이블 | 기존 스키마 변경 절차·운영 백업 |
| 데이터 경계 | 별도 Pydantic 요청·응답과 ORM 모델 | 구현 변경마다 계약·소유권 검사 유지 |
| 인증 | PyJWT·Authlib·Argon2id, refresh 회전·CSRF·Google callback, 명시한 Google 프런트 Origin으로 복귀 | 화면 세션 완료, 공유 로그인 제한·운영 설정 |
| 외부 AI | Codyssey `gpt-5.5` 어댑터·격리 스모크 경로 | 일반 앱은 모의 제공자 유지; 실제 공개 연결은 후속 승인 |
| 검사·설치 | npm·uv 잠금, pytest·Ruff·mypy·Vitest·Playwright·Biome | 실제 브라우저 가입→채팅 E2E, Windows 실행 |

`backend/pyproject.toml`·`uv.lock`에는 PyJWT 2.15.1, Authlib 1.9.1, argon2-cffi 25.1.0과 SQLAlchemy 2.0.44가 고정되어 있다. Python 3.13.15/psycopg 3.2.10을 사용한다. GPT 어댑터는 표준 라이브러리 `http.client`를 사용하며 일반 모의 채팅에는 실제 AI 키가 필요 없다.

## 개발과 초기 DB

로컬 개발에서는 Vite와 Uvicorn을 호스트에서 실행하고 Compose의 분리된 개발·테스트 PostgreSQL을 사용한다. 인증 개발은 `127.0.0.1`을 기준으로 화면 Origin `http://127.0.0.1:5173`과 Swagger UI Origin `http://127.0.0.1:8000`을 허용하고 `localhost`는 제외한다. 기존 로컬 포트와 테스트 DB 보호 규칙은 [공통 환경](environment.md)과 [실행 체크리스트](onboarding.md)를 따른다.

- **공통 준비:** `app.db`의 Base·동기 Session, `app.models.register_models`, `db-init`·`db-init-test`, 보호된 테스트 DB 명령이 구현되어 있다. 인증·대화 모델 5개와 #15 한도 모델 2개도 등록되어 있다. 엔진·세션(한 작업의 DB 읽기·쓰기를 묶는 객체)·모델 등록과 두 OS용 명령, 개발/테스트 DB 보호 및 테스트 세션의 사용법은 [온보딩](onboarding.md)에 정리했다.
- **초기 생성:** `setup` → 기존 `.env` 인증 설정 → `db-init` → `dev` 순서다. 제품 7개 테이블의 누락분만 `create_all()`로 생성한다. 테스트 DB는 `db-init-test`로 별도 보호한다. 요청마다 테이블을 만들지 않는다. `health`·`ready`는 인증 설정·스키마 준비 상태를 검사하지 않는다.
- **기존 DB 변경:** `create_all()`은 기존 스키마를 `ALTER`하지 않는다. 데이터 삭제·재생성은 자동으로 하지 않는다. 이후 변경에는 적용·복구 및 데이터 영향을 검토한 절차가 먼저 필요하며 도구는 아직 선택하지 않았다.
- **참고 SQL:** [DDL 참고안](../04-reference/database-reference.sql)은 제약 설계 자료이며 손으로 작성해야 하는 애플리케이션 DDL은 아니다.

## 선택한 배포 구성

AWS 단일 EC2에서 Docker Compose의 세 서비스를 실행한다. PM이 배포 준비를 별도로 맡으며 지금은 AWS 자원을 만들지 않는다.

| 서비스 | 역할과 연결 |
| --- | --- |
| `nginx` | 유일한 외부 공개 진입점. React 빌드 산출물을 제공하고 `/api/*`를 백엔드로 전달 |
| `backend` | 내부 Docker 네트워크의 FastAPI·Uvicorn |
| `db` | 내부 Docker 네트워크의 PostgreSQL. 데이터는 이름 있는 영속 볼륨에 저장 |

배포 구현에서는 Nginx의 SPA 경로 대체, `/api/*` 전달, 스트리밍 응답의 버퍼링·시간 제한, HTTPS와 인증 쿠키·Google callback의 호환성을 확인해야 한다. 현재 API 목록과 사용법은 [API 기준](../04-reference/api.md)과 [온보딩](onboarding.md)에 있다. 호스트 이름·인증서·EC2 사양은 아직 정하지 않았고, 운영 Compose·이미지·배포 실행은 구현되지 않았다. GitHub Actions CI는 있지만 EC2 빌드·배포 자동화는 없다.
