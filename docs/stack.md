# 기술 구성과 실행 경계

현재 앱은 화면에서 API와 PostgreSQL 연결 상태를 확인한다. 아래의 **선택한 구성** 중 로그인·채팅·운영 배포는 아직 구현되지 않았다. 각 기능의 요청·응답과 처리 규칙은 [API](api.md), [DB](database.md), [인증](authentication.md) 기준을 따른다.

| 영역 | 현재 저장소 | 선택한 구현 기준 |
| --- | --- | --- |
| 화면 | React·TypeScript·Vite, npm 잠금 파일과 기본 화면 | React `useState`로 화면 상태 관리 |
| 서버 | Python·FastAPI·Uvicorn, uv 잠금 파일과 `/api/health`·`/api/ready` | FastAPI 라우터·서비스·저장소 계층 |
| 저장 | PostgreSQL 개발·테스트 DB, `psycopg` 연결 확인, SQLAlchemy 2.0.44 공통 동기 세션 | 제품 ORM 모델은 #3·#9에서 추가 |
| 데이터 경계 | 인증·채팅 모델 없음 | API 요청·응답은 Pydantic 모델, DB 행은 별도 ORM 모델 |
| 인증 | 경로·토큰 발급 미구현 | 앱 JWT는 PyJWT, Google OAuth/OIDC는 Authlib, 비밀번호는 `argon2-cffi`의 Argon2id |
| 검사·설치 | npm·uv, 기존 pytest·Ruff·mypy·Vitest·Playwright·Biome | 잠금 파일과 기존 검사 도구 유지 |

`backend/pyproject.toml`에는 현재 FastAPI·Uvicorn·`psycopg` 등이 선언되어 있다. SQLAlchemy 2.0.44는 `pyproject.toml`과 `uv.lock`에 고정되어 Python 3.13.15 및 psycopg 3.2.10과 함께 동기 Session으로 사용한다. PyJWT, Authlib, `argon2-cffi`는 아직 프로젝트 의존성이 아니다.

## 개발과 초기 DB

로컬 개발에서는 Vite와 Uvicorn을 호스트에서 실행하고 Compose의 분리된 개발·테스트 PostgreSQL을 사용한다. 기존 localhost 포트와 테스트 DB 보호 규칙은 [공통 환경](environment.md)과 [실행 체크리스트](onboarding.md)를 따른다.

- **공통 준비:** `app.db`의 Base·동기 Session, `app.models.register_models`, `db-init`·`db-init-test`, 보호된 테스트 DB 명령이 구현되어 있다. PM 완료 게이트는 독립 검토·병합 후 확인한다. 공통 준비 완료 조건은 잠금 의존성·엔진·세션(한 작업의 DB 읽기·쓰기를 묶는 객체)·모델 등록, `local.sh`·`local.ps1` 연동, 빈 DB 초기화, 개발/테스트 DB 보호, 재사용 테스트 세션, 자동 삭제 없이 반복 검증하는 방법을 확인하는 것이다.
- **초기 생성:** 빈 개발·테스트 DB의 테이블은 ORM 모델 메타데이터에 `create_all()`을 적용해 만든다. 요청마다 테이블을 만들지 않는다.
- **기존 DB 변경:** `create_all()`은 기존 스키마를 `ALTER`하지 않는다. 데이터 삭제·재생성은 자동으로 하지 않는다. 이후 변경에는 적용·복구 및 데이터 영향을 검토한 절차가 먼저 필요하며 도구는 아직 선택하지 않았다.
- **참고 SQL:** [DDL 참고안](database-reference.sql)은 제약 설계 자료이며 손으로 작성해야 하는 애플리케이션 DDL은 아니다.

## 선택한 배포 구성

AWS 단일 EC2에서 Docker Compose의 세 서비스를 실행한다. PM이 배포 준비를 별도로 맡으며 지금은 AWS 자원을 만들지 않는다.

| 서비스 | 역할과 연결 |
| --- | --- |
| `nginx` | 유일한 외부 공개 진입점. React 빌드 산출물을 제공하고 `/api/*`를 백엔드로 전달 |
| `backend` | 내부 Docker 네트워크의 FastAPI·Uvicorn |
| `db` | 내부 Docker 네트워크의 PostgreSQL. 데이터는 이름 있는 영속 볼륨에 저장 |

배포 구현에서는 Nginx의 SPA 경로 대체, `/api/*` 전달, 스트리밍 응답의 버퍼링·시간 제한, HTTPS와 인증 쿠키·Google callback의 호환성을 확인해야 한다. 현재 등록된 API 경로는 `/api/health`와 `/api/ready`뿐이다. 호스트 이름·인증서·EC2 사양은 아직 정하지 않았고, 운영 Compose·이미지·배포 실행은 구현되지 않았다. GitHub Actions CI는 있지만 EC2 빌드·배포 자동화는 없다.
