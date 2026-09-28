# 팀원 로컬 실행 체크리스트

저장소 루트에서 실행한다. 도구 설치는 팀원이 먼저 한다. `setup`은 Node·npm·Python·uv 버전과 Docker daemon을 확인하고, `.env`가 없으면 예시를 복사한 뒤 잠금 파일로 프런트엔드·백엔드 의존성과 프로젝트 Playwright Chromium을 설치한다. 이어 개발·테스트 PostgreSQL을 시작해 각각 `SELECT 1`, API 준비 상태와 기본 검사를 확인한다. Docker가 필요하면 PostgreSQL 17.11 이미지를 가져온다. `setup`은 Node, Python, uv 또는 Docker 자체를 설치하지 않는다. 기존 `.env`와 DB 볼륨을 덮어쓰지 않는다.

## macOS: 실제 실행 확인

Git, Node.js 24.21.0(동봉 npm 11.19.0), Python 3.13.15, uv 0.11.19, 실행 중인 Docker Desktop 또는 Colima와 Docker Compose를 준비한다. Python 실행 파일이 `python3.13`이 아니면 `export B71_PYTHON=/절대/경로/python3.13`을 설정한다. 다른 도구도 필요할 때 `B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER`에 실행 파일의 절대 경로를 지정한다. Node 경로를 지정했다면 그 디렉터리를 `PATH` 앞에 둔다.

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

각 명령이 0으로 끝나야 다음 단계로 간다. `setup` 끝에는 두 DB의 `SELECT 1 passed`, `API readiness passed`, 프런트엔드 타입 검사와 백엔드 단위 검사 성공이 나타난다. 첫 설치의 다운로드 시간은 캐시에 따라 달라진다. 이 Mac의 검증은 프로젝트 `node_modules`와 `.venv`를 따로 보관하고 새로 설치해 통과했지만, OS 도구·Docker 이미지·볼륨·브라우저 다운로드 캐시는 기존 것이었다.

첫 터미널에서 `./local.sh dev`를 실행한다. `db-dev`와 `db-test`의 `SELECT 1 passed`, Vite의 `ready`, API와 UI 주소가 나오면 둘째 터미널에서 확인한다.

```sh
curl -f http://127.0.0.1:5173/
curl -f http://127.0.0.1:5173/api/health   # {"status":"ok"}
curl -f http://127.0.0.1:5173/api/ready    # {"status":"ready"}
curl -f http://127.0.0.1:8000/api/ready    # {"status":"ready"}
```

모두 HTTP 200이어야 한다. 첫 터미널에서 `Ctrl+C`로 `dev`를 끝낸 다음 테스트를 진행한다. 8000·5173 포트에 다른 프로세스가 남아 있으면 원인을 확인하고 그 프로세스의 소유자와 상의한다. `test-e2e`는 두 포트가 비어 있어야 한다.

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

하나라도 실패하면 다음 명령으로 넘어가지 않고 오류를 확인한다. 기대 결과는 단위 검사 15개 백엔드·2개 프런트엔드, 테스트 DB 검사 1개, Chromium E2E 1개, 정적 검사 정상 종료, Vite 빌드 성공이다. `clean` 뒤 `frontend/dist`와 Vite·알려진 Python 캐시가 사라지지만 `.env`, 의존성 디렉터리, DB 볼륨은 남는다. `stop` 뒤 이 프로젝트의 두 DB 컨테이너가 중지된다. `test-db`와 `test-e2e`는 기존에 중지된 테스트 DB를 자체적으로 다시 중지한다. 전체 검사 묶음은 `./local.sh test`로도 실행할 수 있다.

## Windows PowerShell: **NOT RUN**

Windows 팀원이 Git, Node.js 24.21.0/npm 11.19.0, Python 3.13.15, uv 0.11.19, 실행 중인 Docker Desktop(Compose 포함)을 설치한 후 저장소 루트의 PowerShell에서 실행한다. Python 실행 파일을 직접 지정해야 하면 `$env:B71_PYTHON='C:\절대\경로\python.exe'`를 설정한다. 다른 실행 파일은 `B71_NODE`, `B71_NPM`, `B71_UV`, `B71_DOCKER`로 지정할 수 있다. Node 경로를 지정하면 npm 스크립트가 그 Node를 찾도록 해당 디렉터리를 `PATH` 앞에 둔다. PowerShell 실행 정책 때문에 로컬 스크립트가 막히면 조직 정책에 맞는 허용 방법을 관리자에게 확인한다.

```powershell
$ErrorActionPreference = 'Stop'
if (-not $env:B71_PYTHON) {
    $env:B71_PYTHON = (& py -3.13 -c 'import sys; print(sys.executable)')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.13 launcher failed' }
}
$py = $env:B71_PYTHON
if ($env:B71_NODE) { $env:PATH = (Split-Path -Parent $env:B71_NODE) + [IO.Path]::PathSeparator + $env:PATH }
$node = if ($env:B71_NODE) { $env:B71_NODE } else { 'node' }
$npm = if ($env:B71_NPM) { $env:B71_NPM } else { 'npm.cmd' }
$uv = if ($env:B71_UV) { $env:B71_UV } else { 'uv' }
$docker = if ($env:B71_DOCKER) { $env:B71_DOCKER } else { 'docker' }
function Check-Tool([string]$Name, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed: exit $LASTEXITCODE" }
}
Check-Tool node $node @('--version')                # v24.21.0
Check-Tool npm $npm @('--version')                  # 11.19.0
Check-Tool python $py @('--version')               # Python 3.13.15
Check-Tool uv $uv @('--version')                   # uv 0.11.19
Check-Tool docker $docker @('compose', 'version')  # Compose 사용 가능
Check-Tool docker $docker @('info', '--format', '{{.ServerVersion}}')  # daemon 응답
function Run-Step([string]$Name) {
    & .\local.ps1 $Name
    if ($LASTEXITCODE -ne 0) { throw "$Name failed: exit $LASTEXITCODE" }
}
Run-Step setup
```

버전이 맞지 않거나 daemon이 응답하지 않거나 `setup`이 실패하면 멈추고 해결한다. 기대 결과는 macOS와 같은 두 DB `SELECT 1 passed`, API 준비 상태와 기본 검사 성공이다. 같은 PowerShell에서 `.\local.ps1 dev`를 실행하고, 다른 창에서 아래 네 요청이 HTTP 200인지 확인한 뒤 `dev` 창에서 `Ctrl+C`를 누른다. 그 창에서 아래 테스트 명령을 이어 실행한다.

```powershell
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/).StatusCode
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/api/health).StatusCode
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/api/ready).StatusCode
(Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/api/ready).StatusCode
```

처음 창에서 테스트 URL을 화면에 출력하지 않고 `.env`로부터 만든 다음 검사한다.

```powershell
$env:TEST_DATABASE_URL = (& $py -c 'import sys; from pathlib import Path; from urllib.parse import quote; sys.path.insert(0, "backend/tests"); from db_guard import read_settings; s=read_settings(Path(".env")); print("postgresql://"+quote(s["TEST_DB_USER"],safe="")+":"+quote(s["TEST_DB_PASSWORD"],safe="")+"@127.0.0.1:55433/"+quote(s["TEST_DB_NAME"],safe=""))')
if ($LASTEXITCODE -ne 0) { throw 'Could not derive test URL' }
Run-Step test-unit
Run-Step test-db
Run-Step test-e2e
Push-Location frontend
& $npm run format:check; if ($LASTEXITCODE -ne 0) { throw 'format:check failed' }
& $npm run lint; if ($LASTEXITCODE -ne 0) { throw 'lint failed' }
& $npm run typecheck; if ($LASTEXITCODE -ne 0) { throw 'typecheck failed' }
Pop-Location
Push-Location backend
& $uv run --no-sync ruff format --check app tests scripts; if ($LASTEXITCODE -ne 0) { throw 'ruff format failed' }
& $uv run --no-sync ruff check app tests scripts; if ($LASTEXITCODE -ne 0) { throw 'ruff check failed' }
& $uv run --no-sync mypy app; if ($LASTEXITCODE -ne 0) { throw 'mypy failed' }
Pop-Location
Run-Step build
Run-Step clean
Run-Step stop
```

한 단계라도 실패하면 뒤 단계를 실행하지 말고 명령, 종료 코드, 비밀값을 지운 오류 메시지를 공유한다. 완료 시 OS/도구 버전, `setup`·`dev` HTTP 상태·각 검사·`build`·`clean`·`stop`의 PASS/FAIL, 시작·종료 시 DB 컨테이너 상태를 팀에 전달한다. Windows에서 실행하기 전까지 이 절차의 결과는 **NOT RUN**이다.

## 문제 해결

- 버전 오류는 표시된 고정 버전의 실행 파일과 `B71_*` 경로를 확인한다. `setup`이 도구를 자동 설치하지 않는다.
- Docker 오류는 daemon과 Compose, 현재 컨텍스트를 확인한다. macOS에서 `docker-credential-osxkeychain` 부재 오류가 나면 [README의 Colima 안내](../README.md)를 따른다. 전역 Docker 설정이나 기존 볼륨을 임의로 지우지 않는다.
- DB 준비 실패는 `docker compose ps --all`로 이 프로젝트 서비스 상태를 보고 오류를 기록한다. 데이터 복구 지시 없이 `down -v`를 실행하지 않는다.
- `test-db`·`test-e2e`가 URL을 거부하면 `.env`의 전용 `TEST_DB_*`에서 다시 URL을 만들고 개발 DB 주소를 사용하지 않는다.
- `test-e2e`가 포트 사전 검사에서 멈추면 8000·5173의 사용 여부를 확인한다. 검증 Mac에서는 첫 시도에 포트 오류가 한 번 발생했고 점유 프로세스가 보이지 않은 뒤 재시도에서 통과했다. 원인은 확인되지 않았다.
