# Next-Stop-Siren

Next-Stop-Siren은 교통법 질문을 위한 챗봇 프로젝트입니다. 백엔드는 이메일 가입·로그인, 쿠키 세션, Google OIDC, 본인 대화 생성·목록·메시지 저장·재조회를 구현했습니다. 일반 채팅 API는 무료 모의 답변을 반환합니다. 화면은 비회원 소개와 인증 모달까지이며 실제 인증·채팅 API 연결은 후속 작업입니다.

제품 범위와 향후 계획은 [서비스 범위](docs/01-start/service-scope.md), [기술 구성](docs/01-start/stack.md), [API 기준](docs/04-reference/api.md), [DB 설계](docs/04-reference/database.md), [인증 기준](docs/04-reference/authentication.md), [S11 GPT 기준](docs/04-reference/ai.md)을 참고하세요. 팀 작업은 [처음 맡을 작업과 인계](docs/02-team/team-handoff.md), 실행 세부사항은 [온보딩](docs/01-start/onboarding.md)과 [품질·테스트 지침](docs/03-development/quality.md)에 있습니다.

제품 ORM 모델은 `users`, `auth_identities`, `refresh_sessions`, `conversations`, `messages` 5개 테이블입니다. GPT 어댑터는 격리 스모크 전용이며 공개 앱 연결·스트리밍·재시도·회원 한도·배포 완료를 뜻하지 않습니다. 이슈 의존 관계는 [공정진행도](docs/02-team/development-flow.md)를 참고하세요.

## 프런트엔드만 준비하고 실행하기

화면 작업에는 Git과 Node.js/npm만 필요하다. macOS는 저장소 루트에서 다음을 실행한다.

```sh
./local.sh frontend-setup
./local.sh frontend-dev
# 별도 터미널에서
./local.sh frontend-check
./local.sh frontend-test
./local.sh frontend-build
```

Windows PowerShell에서는 같은 명령 이름을 `.\local.ps1 frontend-setup`, `.\local.ps1 frontend-dev`, `.\local.ps1 frontend-check`, `.\local.ps1 frontend-test`, `.\local.ps1 frontend-build`로 실행한다.

`frontend-setup`은 고정 Node.js 24.21.0/npm 11.19.0을 프로젝트 캐시에 설치·검증하고 `npm ci`만 실행한다. Python·uv·백엔드·Docker·`.env`·인증 키 없이 실행하며 Playwright 브라우저는 설치하지 않는다. `B71_NODE`/`B71_NPM`으로 지정한 도구도 실행 버전이 일치해야 한다. `frontend-check`는 Biome 포맷·린트·타입, `frontend-test`는 프런트엔드 단위 검사, `frontend-build`는 정적 빌드를 수행한다.

`frontend-dev`는 http://127.0.0.1:5173 에서 실행하며 `Ctrl+C`로 이 명령이 시작한 화면 프로세스를 종료한다. 포트가 사용 중이면 다른 포트로 넘어가지 않고 실패한다. 기존 프로세스의 소유자를 확인해 수동으로 중지한다. `stop`은 DB용 명령이며 화면 종료 명령이 아니다.

현재 UI의 정적 예시·임시 상태와 단위 검사의 mock은 화면 개발용이다. 이 경로는 API 서버나 실제 로그인·저장·Google 연동을 제공하지 않는다. `/api` 요청은 기존처럼 127.0.0.1:8000으로 전달되므로 API가 없으면 실패한다. 실제 API·DB 통합은 아래 전체 `setup`/`dev` 환경에서 검증한다.

## 문서 안내

[문서 지도](docs/README.md)에서 시작하세요. 실행은 [온보딩](docs/01-start/onboarding.md), 담당별 작업은 [팀 인계](docs/02-team/team-handoff.md), API·DB·인증 정책은 [참조 문서](docs/README.md)를 확인하세요.

## 전체 API·DB 환경 빠른 시작

공개 저장소 복제에는 GitHub 인증이 필요하지 않습니다. 변경 사항을 원격 저장소에 올리려면 별도 쓰기 권한이 필요합니다. Git, 실행 중인 Docker 엔진 및 Compose는 먼저 준비하세요. `setup`은 Node.js·npm·Python·uv를 프로젝트 캐시에 설치하거나 재사용하고, 잠금 파일 기준으로 React/FastAPI 의존성을 설치합니다. PostgreSQL은 Compose로 실행합니다. 화면과 API는 호스트에서 실행합니다.

### macOS 터미널

```sh
git clone https://github.com/next-stop-siren/Next-Stop-Siren.git
cd Next-Stop-Siren
./local.sh setup
# 기존 .env를 보존하고 인증 키 4개 설정: 아래 온보딩 절차 참고
./local.sh db-init
./local.sh dev
```

### Windows PowerShell

```powershell
git clone https://github.com/next-stop-siren/Next-Stop-Siren.git
Set-Location Next-Stop-Siren
.\local.ps1 setup
# 기존 .env를 보존하고 인증 키 4개 설정: 아래 온보딩 절차 참고
.\local.ps1 db-init
.\local.ps1 dev
```

`setup`은 환경과 의존성을 준비하고 DB 연결 및 기본 검사를 확인한 뒤 끝납니다. 제품 테이블은 만들지 않습니다. 이어서 [인증 키 설정](docs/01-start/onboarding.md#2-1-인증-키-생성과-env-설정) → `db-init` → `dev` 순서로 진행하세요. `db-init`은 누락 테이블만 만들며 기존 스키마를 마이그레이션하지 않습니다. `dev`는 실행 상태로 계속 대기합니다. 첫 터미널에서 `Ctrl+C`를 누르면 `dev`가 시작한 API와 화면 프로세스가 종료됩니다. DB 컨테이너는 별도로 실행 중이므로 필요하면 `stop` 명령으로 중지하세요.

화면은 [http://127.0.0.1:5173](http://127.0.0.1:5173), API 문서는 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)입니다. 현재 화면은 비회원 소개입니다. API의 `/api/health`는 정상일 때 `{"status":"ok"}`, `/api/ready`는 DB 연결 성공 시 `{"status":"ready"}`를 반환합니다.

인증 개발은 `http://127.0.0.1`을 기준으로 합니다. `.env`의 `AUTH_API_ORIGIN`은 `http://127.0.0.1:8000`, `AUTH_ALLOWED_ORIGINS`는 `http://127.0.0.1:5173,http://127.0.0.1:8000`으로 설정합니다. 화면(5173)과 Swagger UI(8000)의 실제 Origin을 모두 허용하며 `localhost`는 허용 목록에서 제외합니다. Origin에는 `/docs` 경로를 넣지 않습니다. Google 로그인 callback은 `http://127.0.0.1:8000/api/auth/google/callback`을 등록합니다.

## 실제 API 확인

[온보딩의 API 확인 순서](docs/01-start/onboarding.md#3-1-swagger로-가입로그인과-모의-대화-확인)를 따르세요. Swagger에서 가입 `201`, 로그인 `200`, **Authorize → AccessBearer**에 access 토큰 입력, `/api/me`, 대화 생성·메시지 전송·재조회를 확인합니다. 일반 mock 채팅에는 Google·실제 AI 키가 필요하지 않습니다. refresh·logout은 브라우저의 HttpOnly 쿠키와 최신 `X-CSRF-Token`을 함께 사용합니다.

`/api/health`는 프로세스 응답, `/api/ready`는 DB의 `SELECT 1`만 확인합니다. 둘이 성공해도 인증 설정이나 제품 스키마·로그인·채팅이 정상이라는 뜻은 아닙니다. Google callback은 `:8000`을 유지합니다. 성공 후 `:5173`로 이동하려면 기존 `.env`에 `GOOGLE_FRONTEND_ORIGIN=http://127.0.0.1:5173`을 직접 추가하고 API를 재시작하세요([설정 절차](docs/01-start/onboarding.md#2-2-google-로그인용-env-설정)). 설정이 없으면 기존 상대 경로로 돌아옵니다. 화면의 CSRF·refresh·로그인 상태 연결은 [후속 작업](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)이며 Swagger Execute는 Google 브라우저 이동을 대신하지 않습니다.

## 저장소 구조

- `frontend/`: React·TypeScript·Vite 화면과 브라우저 검사
- `backend/app/`: FastAPI 애플리케이션
- `backend/tests/`: API 및 DB 안전성 검사
- `scripts/`, `local.sh`, `local.ps1`: macOS·Windows 개발 명령
- `compose.yaml`: 개발·테스트 PostgreSQL 서비스
- `docs/`: 제품 범위, API·DB·인증 기준, 온보딩 및 팀 지침

현재 Compose에는 개발·테스트 DB만 있습니다. 프런트엔드와 API는 위 명령으로 호스트에서 실행합니다. 운영 대상은 [단일 AWS EC2의 Nginx·백엔드·PostgreSQL Compose 구성](docs/01-start/stack.md#선택한-배포-구성)으로 정했으며, 배포는 아직 구현되지 않았습니다. `build`는 프런트엔드 산출물만 만듭니다.

## 명령어

저장소 루트에서 macOS는 `./local.sh <명령>`, Windows PowerShell은 `.\local.ps1 <명령>`을 사용합니다. `setup` 전에는 Git과 Docker/Compose가 준비되어 있어야 합니다.

| 명령 | 동작 |
| --- | --- |
| `setup` | 프로젝트 도구·잠금 의존성과 Playwright Chromium을 준비하고 개발·테스트 DB 및 기본 검사를 확인 |
| `dev` | DB를 시작하고 API와 화면을 호스트에서 실행, `Ctrl+C`로 자식 프로세스 종료 |
| `check` | 프런트엔드 타입 검사, DB 없는 API·보호 검사, Compose 설정 검사 |
| `quality` | 프런트엔드 포맷·린트·타입 검사와 백엔드 Ruff·mypy 검사 |
| `build` | 프런트엔드 빌드 |
| `test-unit` | DB 없는 백엔드 인증·채팅·보호 검사와 프런트엔드 단위 검사 |
| `test-db` | 전용 테스트 DB의 ORM·인증·대화·메시지 통합 검사; 가짜 데이터·격리 트랜잭션 사용 |
| `db-init` | 등록된 앱 모델의 누락 테이블만 개발 DB에 생성 |
| `db-init-test` | 전용 테스트 DB 보호 검사 후 누락 테이블만 생성 |
| `test-e2e` | Chromium의 API·DB 준비 상태 fetch와 인증 모달 키보드 검사; 실제 가입→채팅 E2E는 미구현 |
| `test` | `test-unit`, `test-db`, `test-e2e` 순서로 실행 |
| `stop` | 이 프로젝트의 개발·테스트 DB 컨테이너만 중지, 데이터 보존 |
| `clean` | 프런트엔드 빌드/Vite 캐시와 알려진 Python 캐시 정리 |

`test-db`, `test-e2e`, `test`, `db-init-test`에는 실제 테스트 DB 주소를 `TEST_DATABASE_URL`에 지정해야 합니다. macOS에서 URL을 만드는 명령과 자세한 테스트 절차는 [온보딩 §4 테스트와 품질 검사](docs/01-start/onboarding.md#4-테스트와-품질-검사)를 따르세요. Windows에서는 `windows-check.ps1`이 테스트 URL을 자체 구성·검증합니다.

Windows 전체 환경 검사는 저장소 루트에서 `.\windows-check.ps1`을 실행합니다. 첫 실패에서 멈추며 결과 요약은 `.cache/windows-check/report.json`에 기록됩니다. Windows 실제 실행은 아직 **NOT RUN**입니다.

`test-e2e`는 8000과 5173 포트를 사용하므로 `dev`를 먼저 중지해야 합니다. `.env`, DB 볼륨·데이터, 프로젝트 도구 캐시는 유지됩니다. `setup`은 기존 `.env`를 덮어쓰지 않습니다. 자격 증명이나 `.env`를 Git에 추가하지 마세요.

## 확인 범위

과거 macOS Colima 환경에서 프로젝트 도구 설치·재사용, 잠금 의존성 설치, DB 준비와 기본 검사를 확인한 기록이 있습니다. 이번 문서 검토는 현재 소스·명령 계약을 대조했으며 실제 가입·DB 초기화·Google·AI 호출을 새로 실행하지 않았습니다. Git과 Docker는 이미 준비된 호스트였습니다. Windows PowerShell의 실제 실행은 **NOT RUN**입니다. [온보딩 문서](docs/01-start/onboarding.md)에서 확인된 조건과 Windows 체크 결과를 확인하세요.

Compose는 항상 `b7-1`과 현재 checkout의 루트·환경·파일을 명시합니다. 기존 다른 이름의 볼륨·컨테이너는 자동 이전·삭제하지 않습니다. 포트 충돌이나 다른 checkout 소유권 오류는 [기존 볼륨 전환 안내](docs/01-start/onboarding.md#compose-프로젝트-식별과-기존-볼륨)에 따라 확인하세요.
