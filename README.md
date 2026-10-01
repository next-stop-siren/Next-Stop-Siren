# Next-Stop-Siren

Next-Stop-Siren은 사용자 질문에 답하는 챗봇을 만들기 위한 프로젝트입니다. 현재 구현은 화면에서 API와 PostgreSQL 연결 상태를 확인하는 준비 단계입니다. 로그인, 채팅, AI 호출은 아직 구현되지 않았습니다.

제품 범위와 향후 계획은 [서비스 범위](docs/service-scope.md), [API 기준](docs/api.md), [DB 설계](docs/database.md), [인증 기준](docs/authentication.md)를 참고하세요. 팀 작업은 [처음 맡을 작업과 인계](docs/team-handoff.md), 실행 세부사항은 [온보딩](docs/onboarding.md)과 [품질·테스트 지침](docs/quality.md)에 있습니다.

## 빠른 시작

저장소는 비공개이므로 GitHub 저장소 접근 권한과 인증이 필요합니다. Git, 실행 중인 Docker 엔진 및 Compose는 먼저 준비하세요. `setup`은 Node.js·npm·Python·uv를 프로젝트 캐시에 설치하거나 재사용하고, 잠금 파일 기준으로 React/FastAPI 의존성을 설치합니다. PostgreSQL은 Compose로 실행합니다. 화면과 API는 호스트에서 실행합니다.

### macOS 터미널

```sh
git clone https://github.com/next-stop-siren/Next-Stop-Siren.git
cd Next-Stop-Siren
./local.sh setup
./local.sh dev
```

### Windows PowerShell

```powershell
git clone https://github.com/next-stop-siren/Next-Stop-Siren.git
Set-Location Next-Stop-Siren
.\local.ps1 setup
.\local.ps1 dev
```

`setup`은 환경과 의존성을 준비하고 DB 및 기본 검사를 확인한 뒤 끝납니다. `dev`는 실행 상태로 계속 대기합니다. 첫 터미널에서 `Ctrl+C`를 누르면 `dev`가 시작한 API와 화면 프로세스가 종료됩니다. DB 컨테이너는 별도로 실행 중이므로 필요하면 `stop` 명령으로 중지하세요.

화면은 [http://127.0.0.1:5173](http://127.0.0.1:5173), API 문서는 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)입니다. 화면에는 현재 API·DB 준비 상태가 표시됩니다. API의 `/api/health`는 정상일 때 `{"status":"ok"}`, `/api/ready`는 DB 연결 성공 시 `{"status":"ready"}`를 반환합니다.

## Swagger로 API 확인

`dev`가 실행 중일 때 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)를 여세요. 현재 애플리케이션은 `GET /api/health`와 `GET /api/ready` 두 경로만 등록합니다. 경로를 펼치고 **Try it out**을 누른 뒤 **Execute**를 선택하면 요청을 보낼 수 있습니다. 정상 응답은 각각 HTTP 200과 `{"status":"ok"}`, `{"status":"ready"}`입니다. DB를 사용할 수 없으면 `/api/ready`는 HTTP 503을 반환합니다. OpenAPI 스키마는 [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)에서 확인할 수 있습니다.

Swagger 화면은 FastAPI에 등록된 경로와 요청·응답 모델을 보여 줍니다. 개발자가 구현을 추가해 FastAPI에 등록하면 문서에도 반영됩니다. 현재 가입, 인증, 채팅 경로는 없으며 Swagger만으로 사용자 소유권이나 인증을 검증할 수 없습니다. 향후 Bearer 인증 설정도 아직 없습니다.

## 저장소 구조

- `frontend/`: React·TypeScript·Vite 화면과 브라우저 검사
- `backend/app/`: FastAPI 애플리케이션
- `backend/tests/`: API 및 DB 안전성 검사
- `scripts/`, `local.sh`, `local.ps1`: macOS·Windows 개발 명령
- `compose.yaml`: 개발·테스트 PostgreSQL 서비스
- `docs/`: 제품 범위, API·DB·인증 기준, 온보딩 및 팀 지침

현재 Compose에는 DB만 있습니다. 프런트엔드와 API는 위 명령으로 호스트에서 실행합니다. AWS 배포는 목표 환경이지만 구체적인 구성은 미정이고 배포는 구현되지 않았습니다. `build`는 프런트엔드 산출물만 만듭니다.

## 명령어

저장소 루트에서 macOS는 `./local.sh <명령>`, Windows PowerShell은 `.\local.ps1 <명령>`을 사용합니다. `setup` 전에는 Git과 Docker/Compose가 준비되어 있어야 합니다.

| 명령 | 동작 |
| --- | --- |
| `setup` | 프로젝트 도구·잠금 의존성과 Playwright Chromium을 준비하고 개발·테스트 DB 및 기본 검사를 확인 |
| `dev` | DB를 시작하고 API와 화면을 호스트에서 실행, `Ctrl+C`로 자식 프로세스 종료 |
| `check` | 프런트엔드 타입 검사, DB 없는 API·보호 검사, Compose 설정 검사 |
| `quality` | 프런트엔드 포맷·린트·타입 검사와 백엔드 Ruff·mypy 검사 |
| `build` | 프런트엔드 빌드 |
| `test-unit` | DB 없이 백엔드 API·주소 보호와 프런트엔드 단위 검사 |
| `test-db` | 전용 테스트 DB에서 읽기 전용 `SELECT 1` |
| `test-e2e` | 전용 테스트 DB·API·화면을 연결한 Chromium 검사 |
| `test` | `test-unit`, `test-db`, `test-e2e` 순서로 실행 |
| `stop` | 이 프로젝트의 개발·테스트 DB 컨테이너만 중지, 데이터 보존 |
| `clean` | 프런트엔드 빌드/Vite 캐시와 알려진 Python 캐시 정리 |

`test-db`, `test-e2e`, `test`에는 실제 테스트 DB 주소를 `TEST_DATABASE_URL`에 지정해야 합니다. macOS에서 URL을 만드는 명령과 자세한 테스트 절차는 [온보딩 §4 테스트와 품질 검사](docs/onboarding.md#4-테스트와-품질-검사)를 따르세요. Windows에서는 `windows-check.ps1`이 테스트 URL을 자체 구성·검증합니다.

Windows 전체 환경 검사는 저장소 루트에서 `.\windows-check.ps1`을 실행합니다. 첫 실패에서 멈추며 결과 요약은 `.cache/windows-check/report.json`에 기록됩니다. Windows 실제 실행은 아직 **NOT RUN**입니다.

`test-e2e`는 8000과 5173 포트를 사용하므로 `dev`를 먼저 중지해야 합니다. `.env`, DB 볼륨·데이터, 프로젝트 도구 캐시는 유지됩니다. `setup`은 기존 `.env`를 덮어쓰지 않습니다. 자격 증명이나 `.env`를 Git에 추가하지 마세요.

## 확인 범위

macOS Colima 환경에서 프로젝트 도구 설치·재사용, 잠금 의존성 설치, DB 준비와 기본 검사를 확인했습니다. Git과 Docker는 이미 준비된 호스트였습니다. Windows PowerShell의 실제 실행은 **NOT RUN**입니다. [온보딩 문서](docs/onboarding.md)에서 확인된 조건과 Windows 체크 결과를 확인하세요.
