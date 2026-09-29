# B7-1 개발 환경과 실행

React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL 개발·테스트 DB의 초기 구성입니다. 현재 화면은 API와 DB 연결 상태만 확인하며 로그인·채팅·AI 호출은 아직 제공하지 않습니다. [문서 목록](docs/README.md)에서 환경, 역할, 협업과 품질 안내를 찾을 수 있습니다.

처음 참여한다면 [공통 환경](docs/environment.md)을 읽고 [실행 체크리스트](docs/onboarding.md)를 따르세요. Git 작업은 [협업 흐름](docs/workflow.md), 검사 범위는 [품질 지침](docs/quality.md)을 확인하세요.

Git과 실행 중인 Docker 엔진·Compose를 먼저 준비하세요. `setup`이 Node.js 24.21.0/npm 11.19.0, Python 3.13.15, uv 0.11.19를 검증된 배포물에서 저장소의 `.cache/host-tools/`에 설치합니다. Windows PowerShell 실제 실행은 **NOT RUN**입니다.

Windows 팀원은 Docker Desktop daemon을 시작한 뒤 저장소 루트에서 `.\windows-check.ps1`을 실행하세요. 이 명령은 도구 버전부터 빌드까지 순서대로 확인하며 첫 실패에서 종료합니다. 결과 요약은 Git에서 제외되는 `.cache/windows-check/report.json`에 남습니다. 자세한 전제는 [Windows 체크리스트](docs/onboarding.md)에 있습니다. 실제 Windows 실행은 **NOT RUN**입니다.

## 준비

1. Git을 설치하고 저장소를 받으세요. macOS에서는 Colima 또는 Docker Desktop, Windows에서는 Docker Desktop의 Docker daemon과 Compose를 실행하세요.
2. macOS에서 `./local.sh setup`, Windows PowerShell에서 `.\local.ps1 setup`을 실행하세요. 네트워크가 필요합니다. PostgreSQL **17.11** 이미지는 Compose가 가져옵니다.
3. 이미 준비한 도구를 쓰려면 `B71_NODE`, `B71_NPM`, `B71_PYTHON`, `B71_UV`로 경로를 지정하세요. `B71_DOCKER`는 별도로 준비한 Docker CLI의 경로입니다.

도구 다운로드가 실패하면 검증되지 않은 후보는 설치되지 않습니다. 다시 `setup`을 실행하세요. 설치된 도구는 `clean` 명령 후에도 남습니다. 제거하려면 사용 중인 프로세스를 종료한 뒤 `.cache/host-tools/`만 지우세요. `.env`와 DB 볼륨은 보존됩니다.

## 명령

저장소 루트에서 macOS는 `./local.sh <명령>`, Windows PowerShell은 `.\local.ps1 <명령>`을 사용합니다. macOS에서 필요하면 먼저 `chmod +x local.sh`를 실행하세요.

### `setup`

- 프로젝트 전용 도구를 설치·재사용하고 정확한 버전과 Docker daemon을 확인한 뒤 `.env`가 없을 때만 예시를 복사합니다. 이어 `npm ci`와 잠금된 `uv sync`, 프로젝트 캐시의 Playwright Chromium 설치를 실행합니다.
- 개발·테스트 DB를 시작하고 두 DB에서 `SELECT 1`, 실제 API `/api/ready`, 기본 검사를 확인합니다.
- 재실행해도 기존 `.env`와 DB 볼륨을 보존합니다.

### `dev`

- 개발·테스트 DB를 다시 시작하고 준비 상태를 확인한 뒤 API 자동 재시작과 Vite를 로컬에서 실행합니다.
- `Ctrl+C`는 이 명령이 시작한 프로세스만 종료합니다.
- 화면 `http://127.0.0.1:5173`, API 문서 `http://127.0.0.1:8000/docs`.

### `check`

- 프런트엔드 타입 검사, 백엔드 DB 없는 API·보호 검사, Compose 정적 구성을 검사합니다.

### `quality`

- 저장소에 설치된 도구로 프런트엔드 형식·린트·타입 검사와 백엔드 Ruff·mypy 검사를 실행합니다.

### `build`

- 프런트엔드 빌드를 생성합니다.

### `stop`

- 이 프로젝트의 `db-dev`, `db-test` Compose 서비스만 중지합니다.
- DB 데이터와 볼륨은 유지합니다.

### `clean`

- 프런트엔드 `dist`·Vite 캐시와 알려진 Python 캐시만 제거합니다.
- `.env`, DB·볼륨, `node_modules`, `.venv`, 잠금 파일은 유지합니다.

### `test-unit`

- DB 없이 pytest API·DB 주소 보호 검사와 Vitest 화면 검사를 실행합니다.

### `test-db`

- 명시적 `TEST_DATABASE_URL`을 검증한 뒤 전용 테스트 DB에서 읽기 전용 `SELECT 1`을 실행합니다.

### `test-e2e`

- 전용 테스트 DB·API·화면을 연결한 Chromium 검사를 실행합니다.
- 8000/5173 포트가 비어 있어야 합니다.

### `test`

- `test-unit`, `test-db`, `test-e2e`를 순서대로 실행합니다.

CI와 동일한 정적 검사는 `./local.sh quality` 또는 `.\local.ps1 quality`로 실행합니다. 잠금 파일 설치 후 실행하세요. CI 설정과 수동 리뷰 준비 단계는 [품질 지침](docs/quality.md)에 있습니다.

`setup`은 프로젝트 전용 언어 도구를 먼저 준비한 뒤 버전과 Docker daemon을 검사합니다. 사전 검사가 실패하면 프로젝트 의존성 설치, `.env` 복사와 컨테이너 시작 전에 종료합니다. `dev`도 Docker daemon이 필요합니다. 종료 코드 2는 도구/버전 또는 준비 조건, 3은 실행 명령 실패, 4는 준비 시간 초과 또는 개발 프로세스 종료를 뜻합니다.

`.env.example`의 자격 증명은 로컬 예시입니다. 기존 `.env`는 자동 명령이 바꾸거나 출력하지 않습니다.

`test-unit`은 DB 없이 실행합니다. `test-db`, `test-e2e`, `test`는 실행 전에 `TEST_DATABASE_URL`을 환경 변수로 명시해야 합니다. URL은 `.env`의 `TEST_DB_USER`, `TEST_DB_PASSWORD`, `TEST_DB_NAME`과 정확히 일치하는 `postgresql://사용자:URL인코딩된암호@127.0.0.1:55433/DB이름` 형식입니다.

macOS에서는 `export TEST_DATABASE_URL='...'` 후 `./local.sh test`, PowerShell에서는 `$env:TEST_DATABASE_URL='...'` 후 `.\local.ps1 test`를 실행하세요. 개발 DB(55432), 원격 주소, `.env`의 테스트 설정과 다른 URL은 연결 전에 거부합니다. E2E는 비어 있는 8000/5173 포트에서 소유 서버만 시작·종료하고 원래 중지되어 있던 테스트 DB는 다시 중지합니다. Windows의 실제 실행은 아직 검증하지 않았습니다.

## 확인된 범위

`GET /api/health`는 API 생존 상태를, `GET /api/ready`는 개발 DB의 `SELECT 1` 성공을 확인합니다. `setup`은 테스트 DB도 `SELECT 1`로 확인합니다.

macOS Colima에서 프로젝트 전용 언어 도구 설치·재사용과 잠금 설치, 두 DB, API 및 기본 검사를 확인했습니다. Git과 Docker는 이미 준비된 호스트였습니다. 새 OS 전체 설치나 Windows 실제 실행은 검증하지 않았습니다.

Colima의 이미지 가져오기에서 `docker-credential-osxkeychain` 부재 오류가 나오면 [온보딩 문제 해결](docs/onboarding.md#문제-해결)을 확인하세요.
