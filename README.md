# B7-1 로컬 개발 시작

React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL 개발·테스트 DB의 초기 구성입니다. 현재 화면은 API와 DB 연결 상태만 확인합니다. 팀 규칙은 [문서 목록](docs/README.md)을 참고하세요.

## 준비

macOS 또는 Windows에 Docker Desktop(Compose 포함)을 설치하고 **daemon을 실행**하세요. Node.js **24.21.0**(포함된 npm **11.19.0**), Python **3.13.15**, uv **0.11.19**를 설치하세요. PostgreSQL **17.11**은 Compose가 가져옵니다. Python 3.13.15가 일반 명령 경로에 없다면 macOS에서 `B71_PYTHON=/absolute/path/to/python3.13`, Windows에서 `$env:B71_PYTHON='C:\path\to\python.exe'`를 지정하세요. `B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER`도 해당 실행 파일의 경로로 지정할 수 있습니다. Node 경로를 재정의하면 그 디렉터리를 npm 실행 환경의 PATH 앞에 둡니다. uv 관리형 Python 목록에는 현재 3.13.15가 없어 공식 배포본이나 공식 소스 빌드가 필요합니다.

## 명령

저장소 루트에서 macOS는 `./local.sh <명령>`, Windows PowerShell은 `.\local.ps1 <명령>`을 사용합니다. macOS에서 필요하면 먼저 `chmod +x local.sh`를 실행하세요.

| 명령 | 동작 |
| --- | --- |
| `setup` | 정확한 도구 버전과 Docker daemon을 먼저 확인한 뒤 `.env`가 없을 때만 예시를 복사하고, `npm ci`와 잠금된 `uv sync`를 실행합니다. 개발·테스트 DB를 시작하고 두 DB에서 `SELECT 1`, 실제 API `/api/ready`, 기본 검사를 확인합니다. 재실행해도 기존 `.env`와 DB 볼륨을 보존합니다. |
| `dev` | 개발·테스트 DB를 다시 시작하고 준비 상태를 확인한 뒤 API 자동 재시작과 Vite를 로컬에서 실행합니다. `Ctrl+C`는 이 명령이 시작한 프로세스만 종료합니다. 화면 `http://127.0.0.1:5173`, API 문서 `http://127.0.0.1:8000/docs`. |
| `check` | 프런트엔드 타입 검사, 백엔드 스모크 검사, Compose 정적 구성을 검사합니다. |
| `build` | 프런트엔드 빌드를 생성합니다. |
| `stop` | 이 프로젝트의 `db-dev`, `db-test` Compose 서비스만 중지합니다. DB 데이터와 볼륨은 유지합니다. |
| `clean` | 프런트엔드 `dist`·Vite 캐시와 알려진 Python 캐시만 제거합니다. `.env`, DB·볼륨, `node_modules`, `.venv`, 잠금 파일은 유지합니다. |

`setup`과 `dev`는 Docker daemon을 필요로 합니다. 버전 또는 daemon 사전 검사가 실패하면 설치·파일 복사·컨테이너 시작 전에 종료합니다. 종료 코드 2는 도구/버전 또는 준비 조건, 3은 실행 명령 실패, 4는 준비 시간 초과 또는 개발 프로세스 종료를 뜻합니다. `.env.example`의 자격 증명은 로컬 예시입니다. 기존 `.env`는 자동 명령이 바꾸거나 출력하지 않습니다.

`GET /api/health`는 API 생존 응답, `GET /api/ready`는 개발 DB의 실제 `SELECT 1`에 성공할 때 준비 응답을 돌려줍니다. 테스트 DB도 setup에서 직접 `SELECT 1`로 확인합니다. Windows 실행 검증은 아직 수행하지 않았습니다.
