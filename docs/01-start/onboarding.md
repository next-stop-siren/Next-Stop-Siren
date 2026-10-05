# 팀원 로컬 실행 체크리스트

저장소 루트에서 실행한다. Git과 Docker 엔진·Compose는 팀원이 먼저 준비한다. `setup`은 Node.js/npm, Python, uv를 프로젝트 안에 설치하고 기존 `.env`와 DB 볼륨을 보존한다.

## macOS: 검증된 경로

### 1. 도구 준비와 버전 확인

| 도구 | 필요한 버전·상태 |
| --- | --- |
| Git | 설치됨 |
| Node.js·npm | 24.21.0 · 11.19.0 |
| Python·uv | 3.13.15 · 0.11.19 |
| Docker | 실행 중인 Colima 또는 Docker Desktop과 Compose |
| PostgreSQL | Compose가 17.11 이미지를 가져옴 |

언어 도구는 `./local.sh setup`이 검증된 배포물에서 `.cache/host-tools/`에 설치한다. 이미 설치한 도구를 쓰려면 `B71_NODE`, `B71_NPM`, `B71_PYTHON`, `B71_UV`를 지정할 수 있다. `B71_DOCKER`는 수동 설치한 Docker CLI 경로다. 이후 `local.sh` 명령은 프로젝트 도구를 자동으로 재사용한다.

```sh
git --version
"${B71_DOCKER:-docker}" compose version  # Compose 사용 가능
"${B71_DOCKER:-docker}" info --format '{{.ServerVersion}}'  # daemon 응답
```

각 명령이 0으로 끝나야 다음 단계로 간다.

### 2. 프로젝트 설치와 DB 확인

```sh
./local.sh setup
```

`setup`은 `.env`가 없을 때만 예시를 복사한다. 잠금 파일로 프런트엔드·백엔드 의존성과 프로젝트 Playwright Chromium을 설치하고, 개발·테스트 PostgreSQL을 시작한다. 각 DB의 `SELECT 1`, API 준비 상태, 기본 검사를 확인한다. 완료 출력에는 `SELECT 1 passed`, `API readiness passed`, 프런트엔드 타입 검사와 백엔드 단위 검사 성공이 나타난다.

### 3. 화면과 API 실행

첫 터미널에서 `./local.sh dev`를 실행한다. 두 DB의 `SELECT 1 passed`, Vite의 `ready`, API와 UI 주소가 나오면 둘째 터미널에서 확인한다.

```sh
curl -f http://127.0.0.1:5173/
curl -f http://127.0.0.1:5173/api/health   # {"status":"ok"}
curl -f http://127.0.0.1:5173/api/ready    # {"status":"ready"}
curl -f http://127.0.0.1:8000/api/ready    # {"status":"ready"}
```

모두 HTTP 200이어야 한다. 첫 터미널에서 `Ctrl+C`로 `dev`를 끝낸다. 이 명령이 시작한 프로세스만 종료한다.

### 4. 테스트와 품질 검사

`test-e2e`는 8000·5173 포트가 비어 있어야 한다. 다른 프로세스가 남아 있다면 원인을 확인하고 소유자와 상의한다.

테스트 DB URL은 기존 `.env`의 `TEST_DB_*`에서 터미널 변수로만 만든다. 아래 명령은 URL을 화면에 출력하지 않는다. `.env`를 쉘에서 실행하거나 Git에 넣지 않는다.

```sh
export TEST_DATABASE_URL="$(backend/.venv/bin/python -c 'import sys; from pathlib import Path; from urllib.parse import quote; sys.path.insert(0, "backend/tests"); from db_guard import read_settings; s=read_settings(Path(".env")); print("postgresql://"+quote(s["TEST_DB_USER"],safe="")+":"+quote(s["TEST_DB_PASSWORD"],safe="")+"@127.0.0.1:55433/"+quote(s["TEST_DB_NAME"],safe=""))')"
./local.sh test-unit
./local.sh test-db
./local.sh db-init-test  # 등록된 앱 모델만; 현재는 0개
./local.sh test-e2e
./local.sh quality
./local.sh build
```

첫 실패에서는 다음 명령으로 넘어가지 않는다. 기대 결과는 백엔드 단위 검사·프런트엔드 2개, 테스트 DB의 `SELECT 1` 및 ORM 롤백 검사, Chromium E2E 1개, 정적 검사 정상 종료와 Vite 빌드 성공이다. 전체 테스트 묶음은 `./local.sh test`로도 실행할 수 있다.

### 5. 정리와 중지

```sh
./local.sh clean
./local.sh stop
```

`clean`은 `frontend/dist`, Vite 캐시와 알려진 Python 캐시만 제거한다. `.env`, 의존성 디렉터리, DB 볼륨은 남는다. `stop`은 이 프로젝트의 두 DB 컨테이너만 중지한다. `test-db`와 `test-e2e`는 원래 중지된 테스트 DB를 자체적으로 다시 중지한다.

### macOS 검증 범위

이 Mac에서 프로젝트 전용 언어 도구 설치와 재실행, 잠금 설치·DB 준비·기본 검사를 확인했다. Docker와 Git은 이미 준비된 호스트였으므로 새 OS 전체 설치를 검증한 기록은 아니다.

## Windows PowerShell: **NOT RUN**

### 준비

Git과 실행 중인 Docker Desktop(Compose 포함)을 준비한다. `.\windows-check.ps1`과 `.\local.ps1 setup`은 언어 도구를 프로젝트 안에 설치·재사용하도록 구성했다. 필요하면 `B71_NODE`, `B71_NPM`, `B71_PYTHON`, `B71_UV`로 기존 도구를 지정한다. `B71_DOCKER`는 수동 준비한 Docker CLI 경로다. Windows 실제 실행은 아직 확인되지 않았다.

실행 정책이 스크립트를 막으면 조직 정책에 맞는 허용 방법을 관리자에게 확인한다.

### 실행

```powershell
.\windows-check.ps1
```

버전과 Docker daemon을 먼저 확인하고 서비스·포트(55432·55433·8000·5173) 상태를 읽는다. 그다음 잠금 설치, Biome·Ruff·mypy·타입 검사, 단위·전용 DB·브라우저 E2E, 빌드를 순서대로 실행한다. 첫 실패에서 종료 코드가 0이 아니며 다음 단계로 넘어가지 않는다. `dev`를 대기 상태로 실행하지 않는다.

### 결과와 보존

테스트 DB URL은 `.env`의 `TEST_DB_*`를 프로세스 안에서 URL 인코딩해 만든 뒤 DB guard로 검사한다. `.env`를 PowerShell에서 실행하지 않는다. 기존 `.env`·볼륨·데이터와 원래 실행 중인 서비스는 유지한다. 포트가 사용 중이면 E2E가 중단한다.

공유 결과는 `.cache/windows-check/report.json`에 있다. OS·도구 버전·Git 커밋·단계별 `PASS`/`FAIL`/`NOT_RUN`과 종료 코드만 담는다. 환경값, DB URL, 자격 증명, 원시 로그, 개인 경로는 담지 않는다. 상세 실패 출력을 공유할 때는 비밀값을 직접 지운다.

언어 도구 준비가 Python 실행 전에 실패해도 새 결과 파일을 쓰고 `preflight: FAIL`, 나머지 단계 `NOT_RUN`, `failure_phase: host_bootstrap`, 종료 코드 2를 기록한다. 이전 실행 결과를 현재 결과로 읽지 않는다.

### 실패 시 확인

이 명령이 새로 시작한 테스트 DB만 종료한다. Compose가 새 `db-test`의 컨테이너 ID를 확인하지 못하면 추측해 중지하지 않고 실패한다. 안내에 따라 해당 서비스 상태를 직접 확인한 뒤 재시도한다. 다른 서비스나 볼륨을 일괄 중지·삭제하지 않는다.

Windows 팀원의 실제 실행 결과는 아직 **NOT RUN**이다. macOS 모의 검증을 Windows 검증으로 표시하지 않는다.

## 문제 해결

- 버전 오류: 고정 버전의 실행 파일과 `B71_*` 경로를 확인한다.
- Docker 오류: daemon, Compose, 현재 컨텍스트를 확인한다. Colima에서 `docker-credential-osxkeychain`이 없다는 오류가 나오면 Docker CLI의 자격 증명 도우미 설정을 확인한다. 기존 설정이 `osxkeychain`을 지정한다면 공식 Homebrew `docker-credential-helper` 패키지 설치를 검토한다. 이 Mac에서는 설치 후 공개 PostgreSQL 이미지와 `./local.sh setup`을 확인했으며 전역 Docker 설정은 바꾸지 않았다.
- DB 준비 실패: `docker compose ps --all`로 이 프로젝트 서비스 상태를 확인한다. 복구 지시 없이 `down -v`를 실행하지 않는다.
- DB URL 거부: `.env`의 전용 `TEST_DB_*`에서 다시 URL을 만들고 개발 DB 주소를 사용하지 않는다.
- E2E 포트 오류: 8000·5173 포트 사용 여부를 확인한다. 검증 Mac의 첫 시도에서 원인 미상의 포트 오류가 한 번 있었고, 점유 프로세스가 보이지 않은 뒤 재시도에서 통과했다.
