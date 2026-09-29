# 팀원 로컬 실행 체크리스트

저장소 루트에서 실행한다. Git, 지정 버전의 Node·npm·Python·uv, 실행 중인 Docker 엔진과 Compose는 팀원이 먼저 설치·준비한다. `setup`은 도구 버전과 Docker daemon을 확인하고, `.env`가 없으면 예시를 복사한 뒤 잠금 파일로 프런트엔드·백엔드 의존성과 프로젝트 Playwright Chromium을 설치한다.

이어 개발·테스트 PostgreSQL을 시작해 각각 `SELECT 1`, API 준비 상태와 기본 검사를 확인한다. Docker가 필요하면 PostgreSQL 17.11 이미지를 가져온다. `setup`은 Node, Python, uv 또는 Docker 자체를 설치하지 않는다.

기존 `.env`와 DB 볼륨을 덮어쓰지 않는다. 이 Mac의 확인은 OS 도구·Docker 이미지·볼륨·다운로드 캐시가 이미 있던 상태에서 수행했으며 Windows 실제 실행은 아직 **NOT RUN**이다.

## macOS: 실제 실행 확인

Git, Node.js 24.21.0(동봉 npm 11.19.0), Python 3.13.15, uv 0.11.19, 실행 중인 Docker Desktop 또는 Colima와 Docker Compose를 준비한다. Python 실행 파일이 `python3.13`이 아니면 `export B71_PYTHON=/절대/경로/python3.13`을 설정한다. 다른 도구도 필요할 때 `B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER`에 실행 파일의 절대 경로를 지정한다.

Node 경로를 지정했다면 그 디렉터리를 `PATH` 앞에 둔다.

```sh
if [ -n "${B71_NODE:-}" ]; then export PATH="$(dirname "$B71_NODE"):$PATH"; fi
"${B71_NODE:-node}" --version      # v24.21.0
"${B71_NPM:-npm}" --version        # 11.19.0
"${B71_PYTHON:-python3.13}" --version  # Python 3.13.15
"${B71_UV:-uv}" --version         # uv 0.11.19
"${B71_DOCKER:-docker}" compose version  # Compose 사용 가능
"${B71_DOCKER:-docker}" info --format '{{.ServerVersion}}'  # daemon 응답
./local.sh setup
```

각 명령이 0으로 끝나야 다음 단계로 간다. `setup` 끝에는 두 DB의 `SELECT 1 passed`, `API readiness passed`, 프런트엔드 타입 검사와 백엔드 단위 검사 성공이 나타난다. 첫 설치의 다운로드 시간은 캐시에 따라 달라진다.

이 Mac의 검증은 프로젝트 `node_modules`와 `.venv`를 따로 보관하고 새로 설치해 통과했지만, OS 도구·Docker 이미지·볼륨·브라우저 다운로드 캐시는 기존 것이었다.

첫 터미널에서 `./local.sh dev`를 실행한다. `db-dev`와 `db-test`의 `SELECT 1 passed`, Vite의 `ready`, API와 UI 주소가 나오면 둘째 터미널에서 확인한다.

```sh
curl -f http://127.0.0.1:5173/
curl -f http://127.0.0.1:5173/api/health   # {"status":"ok"}
curl -f http://127.0.0.1:5173/api/ready    # {"status":"ready"}
curl -f http://127.0.0.1:8000/api/ready    # {"status":"ready"}
```

모두 HTTP 200이어야 한다. 첫 터미널에서 `Ctrl+C`로 `dev`를 끝낸 다음 테스트를 진행한다. 8000·5173 포트에 다른 프로세스가 남아 있으면 원인을 확인하고 그 프로세스의 소유자와 상의한다.

`test-e2e`는 두 포트가 비어 있어야 한다.

테스트 DB URL은 기존 `.env` 설정에서 터미널 변수로만 만든다. 다음 명령은 URL을 화면에 출력하지 않는다. `.env`를 쉘에서 실행하거나 저장소에 넣지 않는다.

```sh
export TEST_DATABASE_URL="$("${B71_PYTHON:-python3.13}" -c 'import sys; from pathlib import Path; from urllib.parse import quote; sys.path.insert(0, "backend/tests"); from db_guard import read_settings; s=read_settings(Path(".env")); print("postgresql://"+quote(s["TEST_DB_USER"],safe="")+":"+quote(s["TEST_DB_PASSWORD"],safe="")+"@127.0.0.1:55433/"+quote(s["TEST_DB_NAME"],safe=""))')"
./local.sh test-unit
./local.sh test-db
./local.sh test-e2e
(cd frontend && "${B71_NPM:-npm}" run format:check && "${B71_NPM:-npm}" run lint && "${B71_NPM:-npm}" run typecheck)
(cd backend && "${B71_UV:-uv}" run --no-sync ruff format --check app tests scripts && "${B71_UV:-uv}" run --no-sync ruff check app tests scripts && "${B71_UV:-uv}" run --no-sync mypy app)
./local.sh build
./local.sh clean
./local.sh stop
```

하나라도 실패하면 다음 명령으로 넘어가지 않고 오류를 확인한다. 기대 결과는 단위 검사 15개 백엔드·2개 프런트엔드, 테스트 DB 검사 1개, Chromium E2E 1개, 정적 검사 정상 종료, Vite 빌드 성공이다. `clean` 뒤 `frontend/dist`와 Vite·알려진 Python 캐시가 사라지지만 `.env`, 의존성 디렉터리, DB 볼륨은 남는다.

`stop` 뒤 이 프로젝트의 두 DB 컨테이너가 중지된다. `test-db`와 `test-e2e`는 기존에 중지된 테스트 DB를 자체적으로 다시 중지한다. 전체 검사 묶음은 `./local.sh test`로도 실행할 수 있다.

## Windows PowerShell 한 명령: **NOT RUN**

Windows 팀원이 Git, Node.js 24.21.0/npm 11.19.0, Python 3.13.15, uv 0.11.19, 실행 중인 Docker Desktop(Compose 포함)을 설치한 후 저장소 루트의 PowerShell에서 실행한다. 이 명령은 도구 자체를 설치하지 않는다. Python 실행 파일을 직접 지정해야 하면 `$env:B71_PYTHON='C:\절대\경로\python.exe'`를 설정한다.

`B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER` 실행 파일 경로도 지정할 수 있다. npm은 Windows의 `npm.cmd`를 사용할 수 있고, Node 경로를 지정하면 그 디렉터리가 npm 실행 환경의 `PATH` 앞에 놓인다. 실행 정책이 스크립트를 막으면 조직 정책에 맞는 허용 방법을 관리자에게 확인한다.

```powershell
.\windows-check.ps1
```

명령은 정확한 버전과 Docker daemon을 먼저 검사하고, 개발·테스트 서비스 및 55432·55433·8000·5173 포트 상태를 읽는다. 이후 `.env`가 없을 때만 예시를 복사하고 `npm ci`, `uv sync --locked`, 프로젝트 캐시의 Chromium 설치를 실행한다. 프런트엔드 Biome 포맷·린트·타입, 백엔드 Ruff 포맷·린트·mypy, 단위 테스트, 전용 테스트 DB, 실제 API/UI를 띄운 유한 시간 Playwright E2E, 빌드 순서다.

첫 실패에서 종료 코드가 0이 아니며 다음 단계는 실행되지 않는다. `dev`를 대기 상태로 실행하지 않는다.

테스트 DB URL은 `.env`의 `TEST_DB_*`에서 URL 인코딩해 프로세스 안에서 만들고 기존 DB guard로 검사한다. `.env`를 PowerShell에서 읽어 실행하지 않는다. 전용 DB 테스트는 기존 로컬 보호 경로를 사용해 이 명령이 시작한 테스트 DB만 종료한다.

원래 실행 중인 서비스는 유지하며 개발 DB는 이 명령이 시작하지 않는다. 포트가 이미 사용 중이면 E2E가 자체 검사에서 중단한다. 기존 `.env`·볼륨·데이터는 보존한다.

드물게 Compose가 새로 시작한 `db-test`의 컨테이너 ID를 확정할 수 없으면 보호 로직은 추측해 중지하지 않고 실패한다. 이때 화면에 수동 확인 안내가 나온다. 재시도 전에 이 프로젝트의 `db-test` 상태를 직접 확인하고, 다른 서비스나 볼륨을 일괄 중지·삭제하지 않는다.

공유할 결과는 `.cache/windows-check/report.json`이다. OS, 도구 버전, Git 커밋, 각 단계의 `PASS`/`FAIL`/`NOT_RUN`, 종료 코드만 담고 환경값·DB URL·자격 증명·원시 로그·개인 경로는 담지 않는다. 실패 화면의 상세 출력은 공유 전에 비밀값을 직접 지워야 한다.

Windows 팀원 실행 전 현재 결과는 **NOT RUN**이며 macOS 모의 검증이 Windows 동작을 증명하지 않는다.

## 문제 해결

- 버전 오류는 표시된 고정 버전의 실행 파일과 `B71_*` 경로를 확인한다. `setup`이 도구를 자동 설치하지 않는다.
- Docker 오류는 daemon과 Compose, 현재 컨텍스트를 확인한다. macOS에서 `docker-credential-osxkeychain` 부재 오류가 나면 [README의 Colima 안내](../README.md)를 따른다. 전역 Docker 설정이나 기존 볼륨을 임의로 지우지 않는다.
- DB 준비 실패는 `docker compose ps --all`로 이 프로젝트 서비스 상태를 보고 오류를 기록한다. 데이터 복구 지시 없이 `down -v`를 실행하지 않는다.
- `test-db`·`test-e2e`가 URL을 거부하면 `.env`의 전용 `TEST_DB_*`에서 다시 URL을 만들고 개발 DB 주소를 사용하지 않는다.
- `test-e2e`가 포트 사전 검사에서 멈추면 8000·5173의 사용 여부를 확인한다. 검증 Mac에서는 첫 시도에 포트 오류가 한 번 발생했고 점유 프로세스가 보이지 않은 뒤 재시도에서 통과했다. 원인은 확인되지 않았다.
