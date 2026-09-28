# B7-1 로컬 개발 시작

React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL 개발·테스트 DB의 초기 구성입니다. 현재 화면은 API와 DB 연결 상태만 확인합니다. 팀 규칙은 [문서 목록](docs/README.md)을 참고하세요.

## 준비

macOS에는 실행 중인 Docker 엔진과 Compose가 필요하며 Colima 또는 Docker Desktop을 사용할 수 있습니다. Windows에는 Docker Desktop(Compose 포함)을 설치하고 **daemon을 실행**하세요. Node.js **24.21.0**(포함된 npm **11.19.0**), Python **3.13.15**, uv **0.11.19**를 설치하세요. PostgreSQL **17.11**은 Compose가 가져옵니다. Python 3.13.15가 일반 명령 경로에 없다면 macOS에서 `B71_PYTHON=/absolute/path/to/python3.13`, Windows에서 `$env:B71_PYTHON='C:\path\to\python.exe'`를 지정하세요. `B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER`도 해당 실행 파일의 경로로 지정할 수 있습니다. Node 경로를 재정의하면 그 디렉터리를 npm 실행 환경의 PATH 앞에 둡니다. uv 관리형 Python 목록에는 현재 3.13.15가 없어 공식 배포본이나 공식 소스 빌드가 필요합니다.

## 명령

저장소 루트에서 macOS는 `./local.sh <명령>`, Windows PowerShell은 `.\local.ps1 <명령>`을 사용합니다. macOS에서 필요하면 먼저 `chmod +x local.sh`를 실행하세요.

| 명령 | 동작 |
| --- | --- |
| `setup` | 정확한 도구 버전과 Docker daemon을 먼저 확인한 뒤 `.env`가 없을 때만 예시를 복사하고, `npm ci`와 잠금된 `uv sync`, 프로젝트 캐시의 Playwright Chromium 설치를 실행합니다. 개발·테스트 DB를 시작하고 두 DB에서 `SELECT 1`, 실제 API `/api/ready`, 기본 검사를 확인합니다. 재실행해도 기존 `.env`와 DB 볼륨을 보존합니다. |
| `dev` | 개발·테스트 DB를 다시 시작하고 준비 상태를 확인한 뒤 API 자동 재시작과 Vite를 로컬에서 실행합니다. `Ctrl+C`는 이 명령이 시작한 프로세스만 종료합니다. 화면 `http://127.0.0.1:5173`, API 문서 `http://127.0.0.1:8000/docs`. |
| `check` | 프런트엔드 타입 검사, 백엔드 DB 없는 API·보호 검사, Compose 정적 구성을 검사합니다. |
| `build` | 프런트엔드 빌드를 생성합니다. |
| `stop` | 이 프로젝트의 `db-dev`, `db-test` Compose 서비스만 중지합니다. DB 데이터와 볼륨은 유지합니다. |
| `clean` | 프런트엔드 `dist`·Vite 캐시와 알려진 Python 캐시만 제거합니다. `.env`, DB·볼륨, `node_modules`, `.venv`, 잠금 파일은 유지합니다. |
| `test-unit` | DB 없이 pytest API·DB 주소 보호 검사와 Vitest 화면 검사를 실행합니다. |
| `test-db` | 명시적 `TEST_DATABASE_URL`을 검증한 뒤 전용 테스트 DB에서 읽기 전용 `SELECT 1`을 실행합니다. |
| `test-e2e` | 전용 테스트 DB·API·화면을 연결한 Chromium 검사를 실행합니다. 8000/5173 포트가 비어 있어야 합니다. |
| `test` | `test-unit`, `test-db`, `test-e2e`를 순서대로 실행합니다. |

`setup`과 `dev`는 Docker daemon을 필요로 합니다. 버전 또는 daemon 사전 검사가 실패하면 설치·파일 복사·컨테이너 시작 전에 종료합니다. 종료 코드 2는 도구/버전 또는 준비 조건, 3은 실행 명령 실패, 4는 준비 시간 초과 또는 개발 프로세스 종료를 뜻합니다. `.env.example`의 자격 증명은 로컬 예시입니다. 기존 `.env`는 자동 명령이 바꾸거나 출력하지 않습니다.

`test-unit`은 DB 없이 실행합니다. `test-db`, `test-e2e`, `test`는 실행 전에 `TEST_DATABASE_URL`을 환경 변수로 명시해야 합니다. URL은 `.env`의 `TEST_DB_USER`, `TEST_DB_PASSWORD`, `TEST_DB_NAME`과 정확히 일치하는 `postgresql://사용자:URL인코딩된암호@127.0.0.1:55433/DB이름` 형식입니다. macOS에서는 `export TEST_DATABASE_URL='...'` 후 `./local.sh test`, PowerShell에서는 `$env:TEST_DATABASE_URL='...'` 후 `.\local.ps1 test`를 실행하세요. 개발 DB(55432), 원격 주소, `.env`의 테스트 설정과 다른 URL은 연결 전에 거부합니다. E2E는 비어 있는 8000/5173 포트에서 소유 서버만 시작·종료하고 원래 중지되어 있던 테스트 DB는 다시 중지합니다. Windows의 실제 실행은 아직 검증하지 않았습니다.

macOS Colima에서 Docker Desktop을 제거한 뒤 `docker-credential-osxkeychain`이 없다는 이미지 가져오기 오류가 나오면 Docker CLI의 자격 증명 도우미 설정을 확인하세요. 기존 설정이 `osxkeychain`을 지정한다면 공식 Homebrew `docker-credential-helper` 패키지로 해당 실행 파일을 설치할 수 있습니다. 이 호스트에서는 설치 후 기본 Colima 컨텍스트와 기존 Docker 설정으로 공개 PostgreSQL 이미지 가져오기와 `./local.sh setup`을 검증했습니다. 전역 Docker 설정은 바꾸지 않았습니다.

`GET /api/health`는 API 생존 응답, `GET /api/ready`는 개발 DB의 실제 `SELECT 1`에 성공할 때 준비 응답을 돌려줍니다. 테스트 DB도 setup에서 직접 `SELECT 1`로 확인합니다. macOS Colima에서 두 DB, API, Vite 프록시, 중지·재시작을 검증했습니다. Windows 실행 검증은 아직 수행하지 않았습니다.
