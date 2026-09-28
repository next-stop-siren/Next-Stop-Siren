# B7-1 로컬 개발 시작

이 저장소는 React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL 개발·테스트 DB의 초기 구성을 담습니다. 현재 화면은 API와 DB 연결 상태만 확인합니다. 로그인·채팅 기능은 아직 없습니다. 팀 규칙은 [문서 목록](docs/README.md)을 참고하세요.

## 버전과 준비

Node.js **24.21.0**(포함된 npm **11.19.0**), Python **3.13.15**, uv **0.11.19**, Docker Desktop/Compose가 필요합니다. DB 이미지는 PostgreSQL **17.11**입니다. Node와 Python은 공식 배포본에서 지정 버전을 설치하고, uv도 지정 버전이 설치된 환경에서 진행하세요. 현재 uv 관리형 Python 다운로드 목록에는 3.13.15가 없으므로 `uv python install 3.13.15`를 준비 명령으로 사용하지 않습니다. 설치 후 다음 버전이 정확히 일치하는지 확인합니다.

macOS 터미널:

```sh
node --version
npm --version
python3.13 --version
uv --version
docker compose version
```

Windows PowerShell:

```powershell
node --version
npm --version
py -3.13 --version
uv --version
docker compose version
```

Windows의 Python 명령이 다른 3.13 패치를 가리키면 3.13.15 실행 파일 경로를 확인하고 아래 `uv sync --python`에 그 경로를 지정합니다. Windows 절차는 아직 실제 Windows에서 실행 검증하지 않았습니다.

## macOS 실행

저장소 루트에서 진행합니다. `.env`가 이미 있으면 복사 단계를 건너뛰고 기존 값을 보존하세요. 예시 비밀번호와 설정은 로컬 개발용입니다.

```sh
cp -n .env.example .env
docker compose up -d db-dev
cd backend
uv sync --locked --no-managed-python --python 3.13.15
uv run --no-sync --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port 8000
```

API는 별도 터미널에서 실행해 둡니다. 화면은 저장소 루트의 다른 터미널에서 시작합니다.

```sh
cd frontend
npm ci
npm run dev
```

브라우저에서 `http://127.0.0.1:5173`을 열어 **API 확인**과 **DB 확인**을 누릅니다. 화면의 `/api` 요청은 Vite가 로컬 FastAPI로 전달합니다. `http://127.0.0.1:8000/docs`에는 기본 Swagger UI가 있습니다. `GET /api/health`는 API 생존 응답 `{"status":"ok"}`를, `GET /api/ready`는 DB에서 `SELECT 1`이 성공할 때 `{"status":"ready"}`를 반환합니다. DB가 준비되지 않으면 `/api/ready`는 내부 연결 정보를 숨긴 503을 반환합니다.

## Windows PowerShell 실행

저장소 루트에서 다음 명령을 사용합니다. `.env`가 이미 있으면 복사하지 않습니다. `uv sync`에 전달하는 Python은 반드시 3.13.15여야 합니다.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d db-dev
Set-Location backend
uv sync --locked --no-managed-python --python 3.13.15
uv run --no-sync --env-file ../.env uvicorn app.main:app --host 127.0.0.1 --port 8000
```

다른 PowerShell 창에서 화면을 시작합니다.

```powershell
Set-Location frontend
npm ci
npm run dev
```

## 검사와 정리

화면 검사: `cd frontend`, `npm run typecheck`, `npm run build`. API 스모크 검사: `cd backend`, `uv run --no-sync python -m unittest discover -s tests -v`. DB 접속은 이 스모크 검사에서 모의 처리하므로 실제 DB 확인은 `/api/ready`로 별도 확인해야 합니다. 이번 초기 구성 작업에서는 Docker가 없어 Compose 정적 검사와 DB 실행·접속 검증을 수행하지 않았습니다. 개발 DB와 분리된 테스트 DB가 필요할 때만 `docker compose up -d db-test`를 사용하며 포트는 각각 로컬 전용 `55432`, `55433`입니다.

화면·API 프로세스는 각 터미널에서 Ctrl+C로 중지합니다. DB는 저장소 루트에서 `docker compose stop db-dev`로 중지합니다. `docker compose down -v`는 DB 데이터를 지우므로 일반 정리 명령으로 사용하지 않습니다. 생성된 화면 빌드·Vite 캐시만 정리하려면 `cd frontend`에서 `npm run clean`, 백엔드의 알려진 Python 캐시만 정리하려면 `cd backend`에서 `uv run --no-sync python scripts/clean_cache.py`를 실행합니다. 이 명령은 `.env`, 의존성 설치 폴더와 DB 볼륨을 지우지 않습니다. 의존성 재설치가 필요하면 잠금 파일에 맞춰 `npm ci`, `uv sync --locked`를 다시 실행합니다.
