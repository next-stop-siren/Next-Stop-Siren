# 팀원 로컬 실행 체크리스트

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

## 전체 환경(API·DB 연동)

저장소 루트에서 실행한다. Git과 Docker 엔진·Compose는 팀원이 먼저 준비한다. `setup`은 Node.js/npm, Python, uv를 프로젝트 안에 설치하고 기존 `.env`와 DB 볼륨을 보존한다.

## macOS 전체 환경: 설치와 API 확인

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

### 2-1. 인증 키 생성과 .env 설정

가입 API를 사용하려면 `setup` 완료 후 저장소 루트의 `.env`에 인증 키 4개를 설정한다. `.env.example`의 키는 의도적으로 비어 있으므로 Origin 주소만 설정해서는 가입 API를 사용할 수 없다. `.env`가 없으면 먼저 `setup`을 실행한다. 기존 `.env`를 예시 파일로 덮어쓰지 않는다.

키는 외부 서비스에서 발급받지 않고 각 팀원이 자신의 개발 환경에서 생성한다. 각각 독립적인 무작위 32바이트를 Base64 문자열로 변환하며, 네 값은 서로 달라야 한다.

| 설정 | 용도 |
| --- | --- |
| `AUTH_ACCESS_KEY` | access JWT 서명 |
| `AUTH_REFRESH_KEY` | refresh JWT 서명 |
| `AUTH_REFRESH_DIGEST_KEY` | DB에 저장할 refresh digest 생성 |
| `AUTH_CSRF_KEY` | refresh에 연결된 CSRF 값 생성 |

macOS 터미널에서 다음 명령을 한 번 실행한다.

```sh
backend/.venv/bin/python - <<'PY'
import base64
import secrets

for name in (
    "AUTH_ACCESS_KEY",
    "AUTH_REFRESH_KEY",
    "AUTH_REFRESH_DIGEST_KEY",
    "AUTH_CSRF_KEY",
):
    key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    print(f"{name}={key}")
PY
```

Windows 사용자는 아래 [PowerShell 인증 키 설정](#powershell-인증-키-설정) 절차를 따른다. Windows 실제 실행은 미확인이다.

출력된 `AUTH_…_KEY=…` 네 줄을 자신의 `.env`에서 같은 이름의 빈 항목 대신 붙여넣고 저장한다. 다른 DB·Origin 설정은 유지한다. 같은 키를 네 항목에 복사하거나 실행할 때마다 다시 생성하지 않는다. 이미 유효한 키가 설정돼 있다면 이 단계를 반복하지 않는다.

실제 키와 명령 출력은 `.env`에만 보관하며 `.env.example`, Git, PR, 채팅, 공유 화면에 넣지 않는다. 키를 교체하면 기존 토큰·세션·CSRF 값이 더 이상 유효하지 않을 수 있으므로 다시 인증해야 한다.

macOS에서는 다음 명령으로 키 값을 출력하지 않고 인증 설정 전체를 확인할 수 있다.

```sh
backend/.venv/bin/python - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, "backend/tests")
from app.security.settings import AuthSettings
from db_guard import read_settings

AuthSettings.from_env(read_settings(Path(".env")))
print("인증 설정 검증 통과")
PY
```

오류가 발생하면 표시된 설정 항목을 확인한다. 키는 빈 값이 아니고 Base64로 해석한 결과가 최소 32바이트여야 하며, 네 키가 서로 달라야 한다. 로컬 API·허용 Origin·쿠키 설정도 아래 실행 안내와 일치해야 한다.

설정 검증을 마치면 아래 `db-init` → `dev` → API 확인 순서로 진행한다. 앱은 생성 시 인증 설정을 읽으므로 기존 `dev`가 실행 중이면 해당 터미널에서 `Ctrl+C`로 종료한다. `.env` 저장만으로 실행 중인 서버 설정은 갱신되지 않는다.

### 2-2. Google 로그인용 .env 설정

**선택 절차다.** 로컬 가입·로그인과 모의 채팅만 확인하려면 이 절을 건너뛰고 [개발 DB 생성](#2-3-개발-db의-누락-테이블-생성)으로 이동한다.

Google 로그인까지 사용할 팀원은 기존 인증 키 4개와 별도로 아래 Google 항목을 자신의 `.env`에 준비한다. Google 설정이 없어도 로컬 가입·비밀번호 로그인은 사용할 수 있다.

| 설정 | 준비 방법 |
| --- | --- |
| `GOOGLE_CLIENT_ID` | 개발용 Google OAuth client에서 발급받은 ID 입력 |
| `GOOGLE_CLIENT_SECRET` | 같은 client의 secret 입력 |
| `GOOGLE_REDIRECT_URI` | 기본 개발 주소 `http://127.0.0.1:8000/api/auth/google/callback` 유지 |
| `GOOGLE_TRANSACTION_KEY` | 각 팀원이 자신의 컴퓨터에서 독립적인 암호화 키 생성 |
| `GOOGLE_FRONTEND_ORIGIN` | 선택 복귀 목적지 `http://127.0.0.1:5173`; callback URL과 구분 |

**새 값을 직접 준비하는 것은 ID·secret·트랜잭션 키 세 개다.** redirect URI는 `.env.example`에 기본값이 있다. 다만 이미 존재하는 `.env`에는 새 항목이 자동 추가되지 않으므로, 아래 항목 중 빠진 것만 직접 추가한다. `GOOGLE_FRONTEND_ORIGIN`은 비밀값이 아니며 프런트로 돌아오려면 이 값도 추가해야 한다. 기존 DB·인증 설정은 유지하고 `.env` 전체를 덮어쓰지 않는다.

```dotenv
GOOGLE_CLIENT_ID=발급받은_client_ID
GOOGLE_CLIENT_SECRET=같은_client의_secret
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/auth/google/callback
GOOGLE_FRONTEND_ORIGIN=http://127.0.0.1:5173
GOOGLE_TRANSACTION_KEY=직접_생성한_Base64_키
```

`GOOGLE_FRONTEND_ORIGIN`은 경로·끝 `/`·쿼리·fragment 없는 정확한 Origin이어야 한다. `AUTH_ALLOWED_ORIGINS`에 포함되고 API와 scheme·hostname이 같아야 하며 포트만 달라도 된다. 로컬에서는 `http://127.0.0.1:5173`을 쓴다. 설정 항목 자체를 생략하면 기존 상대 redirect를 유지하지만, 빈 값이나 잘못된 값을 명시하면 Google 경로만 503으로 차단된다. 로컬 비밀번호 인증 설정은 유지된다.

#### Google client ID·secret 준비

팀에서 사용하는 개발용 OAuth client가 있으면 담당자에게 ID·secret을 안전한 경로로 전달받고 등록된 callback을 확인한다. 없다면 [Google Cloud Console](https://console.cloud.google.com/)에서 개인 개발용 client를 만든다.

1. 개발용 프로젝트를 선택하거나 생성한다.
2. Google Auth Platform에서 앱 이름·연락처 등 동의 화면을 설정한다. 테스트 사용자 제한을 사용하는 경우 검사할 팀원 계정을 등록한다.
3. Clients에서 OAuth client를 만들고 유형은 **Web application**을 선택한다.
4. **Authorized redirect URIs**에 아래 주소를 등록한다. Authorized JavaScript origins 입력란과 구분한다.

   ```text
   http://127.0.0.1:8000/api/auth/google/callback
   ```

5. 발급된 client ID·secret을 `.env`의 같은 이름 항목에 입력한다. ID와 secret은 반드시 같은 client의 값이어야 한다.

등록된 URI·`.env`의 `GOOGLE_REDIRECT_URI`·`AUTH_API_ORIGIN`에 callback 경로를 붙인 값은 정확히 일치해야 한다. 개발에서는 `localhost`, 다른 포트, 끝의 `/`를 섞지 않는다. Google 자격 증명 발급과 redirect 일치 기준은 [공식 OIDC 안내](https://developers.google.com/identity/openid-connect/openid-connect)를 따른다.

#### 트랜잭션 암호화 키 생성

이 키는 Google에서 발급받지 않는다. 아래 명령으로 독립 난수 32바이트를 Base64로 변환하고, 출력된 한 줄을 `.env`의 `GOOGLE_TRANSACTION_KEY` 항목에 넣는다. 기존 `AUTH_*_KEY`를 복사해서 사용하지 않는다. 이미 유효한 키가 있으면 다시 생성하지 않는다.

macOS:

```sh
backend/.venv/bin/python -c 'import base64,secrets; print("GOOGLE_TRANSACTION_KEY=" + base64.b64encode(secrets.token_bytes(32)).decode("ascii"))'
```

Windows PowerShell:

```powershell
& .\backend\.venv\Scripts\python.exe -c 'import base64,secrets; print("GOOGLE_TRANSACTION_KEY=" + base64.b64encode(secrets.token_bytes(32)).decode("ascii"))'
```

실제 secret·암호화 키는 `.env`에만 보관한다. 명령 출력이나 값이 담긴 파일을 Git·PR·채팅에 올리지 않는다.

#### 값 노출 없이 검증하고 실행

저장소 루트에서 아래 명령을 실행한다. 기존 인증 설정과 Google 설정의 누락·키 형식·callback 일치를 검사하며, Google에 요청하거나 실제 설정값을 출력하지 않는다.

macOS:

```sh
backend/.venv/bin/python - <<'PY'
from pathlib import Path
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, "backend/tests")
from app.security.settings import GoogleSettings
from db_guard import read_settings

GoogleSettings.from_env(read_settings(Path(".env")))
print("Google settings validated")
PY
```

Windows PowerShell:

```powershell
@'
from pathlib import Path
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, "backend/tests")
from app.security.settings import GoogleSettings
from db_guard import read_settings

GoogleSettings.from_env(read_settings(Path(".env")))
print("Google settings validated")
'@ | & .\backend\.venv\Scripts\python.exe -
```

PowerShell 명령은 Windows에서 실제 실행하지 않았다.

`Google settings validated`가 출력되면 설정 형식 검사는 통과한 것이다. 실제 ID·secret의 유효성은 Google 로그인에서 확인한다. `.env`를 저장한 뒤 [개발 DB 생성](#2-3-개발-db의-누락-테이블-생성)과 `dev`까지 마친 상태에서 아래 선택 로그인을 확인한다. 이미 실행 중인 서버는 해당 터미널의 `Ctrl+C`로 종료한 뒤 다시 실행한다.

기존 `.env`에는 새 항목이 자동 반영되지 않는다. 프런트 복귀를 사용할 때 비밀값이 아닌 `GOOGLE_FRONTEND_ORIGIN=http://127.0.0.1:5173`을 직접 추가·저장하고 자신이 실행한 API를 재시작한다. callback 등록 주소는 계속 `http://127.0.0.1:8000/api/auth/google/callback`이다.

| 확인 순서 | 기대 결과 |
| --- | --- |
| `dev`로 화면 `:5173`과 API `:8000` 실행 후 브라우저 주소창에서 `http://127.0.0.1:8000/api/auth/google/start`로 이동 | Google 로그인 화면으로 이동; Swagger Execute로 대신하지 않음 |
| 로그인 후 Network의 `:8000/api/auth/google/callback` 응답 확인 | `302`, `Location: http://127.0.0.1:5173/`, refresh·CSRF 쿠키 설정과 트랜잭션 쿠키 제거 |
| 프런트 복귀 화면 확인 | 화면은 여전히 비회원 UI일 수 있음; 이것만으로 로그인 상태 완료를 판단하지 않음 |

목적지는 서버 설정과 허용된 `return_to`(현재 `/`만)로 결정하며 요청 헤더·쿼리의 임의 URL은 사용하지 않는다. 설정 항목이 없으면 예전처럼 `Location: /`가 되어 API `:8000`에 남는다. 그 최종 페이지의 404와 callback 성공을 구분한다.

쿠키는 host-only이므로 같은 `127.0.0.1`의 포트가 달라도 API 요청에 사용할 수 있다. `Path=/api/auth`, HttpOnly·SameSite·Secure 정책은 그대로이며 JavaScript가 쿠키를 직접 읽지 않는다. 화면에서 credentials를 포함한 `/api/auth/csrf` → `/api/auth/refresh`로 access를 받아 메모리에 두는 연결은 아직 후속 작업이다([#17](https://github.com/next-stop-siren/Next-Stop-Siren/issues/17), [#19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)). URL이나 브라우저 저장소에 토큰을 넣지 않는다.

### 2-3. 개발 DB의 누락 테이블 생성

`setup`이 시작한 개발 DB를 대상으로 저장소 루트에서 실행한다. 인증 키 검증을 마친 뒤 진행하며 Google·AI 설정은 일반 모의 대화에 필요하지 않다.

```sh
./local.sh db-init
```

Windows는 `.\local.ps1 db-init`이다. 현재 등록된 `users`, `auth_identities`, `refresh_sessions`, `conversations`, `messages` 5개 테이블을 확인하고 없는 테이블만 만든다. 출력의 개수는 이번에 새로 만든 수가 아니라 등록 모델 수다. `setup`은 이 작업을 대신하지 않는다.

`create_all()`은 기존 테이블의 컬럼·제약을 바꾸지 않으며 데이터도 삭제하지 않는다. 오래된 스키마가 있으면 `db-init` 성공만으로 호환성을 보장할 수 없다. 삭제·재생성으로 해결하지 말고 백업·적용·복구 계획을 먼저 검토한다. 마이그레이션과 운영 준비 상태 검사는 별도 후속 작업이다.

### 3. 화면과 API 실행

첫 터미널에서 `./local.sh dev`를 실행한다. 두 DB의 `SELECT 1 passed`, Vite의 `ready`, API와 UI 주소가 나오면 둘째 터미널에서 확인한다.

```sh
curl -f http://127.0.0.1:5173/
curl -f http://127.0.0.1:5173/api/health   # {"status":"ok"}
curl -f http://127.0.0.1:5173/api/ready    # {"status":"ready"}
curl -f http://127.0.0.1:8000/api/ready    # {"status":"ready"}
```

모두 HTTP 200이어야 한다. `health`는 API 프로세스 응답, `ready`는 DB의 `SELECT 1` 성공만 뜻한다. 인증 키·Google 설정·5개 테이블의 존재와 스키마 호환성을 확인하지 않으므로 아래 실제 API 절차도 필요하다. 현재 화면은 비회원 소개와 인증 모달이며 API 인증·회원 채팅 연결은 아직 없다. 아래 실제 API 확인까지 마친 뒤 첫 터미널에서 `Ctrl+C`로 `dev`가 시작한 프로세스를 종료한다.

인증 개발은 `http://127.0.0.1`을 기준으로 한다. `.env`의 `AUTH_API_ORIGIN`은 `http://127.0.0.1:8000`, `AUTH_ALLOWED_ORIGINS`는 `http://127.0.0.1:5173,http://127.0.0.1:8000`으로 설정한다. 화면은 5173, Swagger UI는 8000의 `/docs`에서 제공하므로 두 실제 Origin을 모두 허용하고 `localhost`는 제외한다. Origin에는 `/docs` 경로를 포함하지 않는다. `.env` 변경은 실행 중인 앱 설정에 적용되지 않으므로 직접 관리하는 API 개발 프로세스를 재시작한다. Google 로그인 callback은 `http://127.0.0.1:8000/api/auth/google/callback`을 등록한다.

### 3-1. Swagger로 가입·로그인과 모의 대화 확인

개발용 계정·대화가 DB에 저장되는 수동 확인 절차다. 본인 로컬 개발 DB에서 가짜 이메일·합성 질문만 사용한다. 자동 테스트 DB와 구분하고, 응답의 토큰·쿠키·비밀번호를 공유하지 않는다. 이번 문서 검토에서 이 절차를 실제 실행한 것은 아니다.

`dev`를 켜 둔 채 [Swagger UI](http://127.0.0.1:8000/docs)를 연다. `localhost`나 저장한 HTML 파일을 사용하지 않는다. 가입·로그인 POST의 브라우저 Origin은 `http://127.0.0.1:8000`이며 `.env` 허용 목록과 일치해야 한다. 403이면 Network의 실제 Origin과 설정을 확인한다.

| 순서 | Swagger에서 실행 | 기대 결과 |
| --- | --- | --- |
| 1 가입 | `POST /api/auth/register` → Try it out → 아래 가입 본문 | 새 로컬 이메일 `201`; 같은 이메일 재가입 `409 local_email_exists` |
| 2 로그인 | `POST /api/auth/login`, 같은 이메일·비밀번호 | `200`, `user`·`access_token`·`csrf_token`; 브라우저에 refresh·CSRF 쿠키 설정 |
| 3 인증 | Authorize → AccessBearer에 access 문자열만 입력 → `GET /api/me` | Bearer 접두사 자동 추가, 같은 사용자 정보와 `200` |
| 4 대화 생성 | `POST /api/conversations`, 본문 `{}` | `201`, `conversation.id` 기록 |
| 5 대화 확인 | `GET /api/conversations` | 자신의 대화가 목록에 표시 |
| 6 질문 | `POST /api/conversations/{conversation_id}/messages`, 아래 질문 본문 | `201`, 질문과 `completed` 답변 `모의 답변입니다.` |
| 7 저장 확인 | `GET /api/conversations/{conversation_id}/messages` | 같은 질문·답변 ID와 내용 재조회 |

가입·로그인 본문 예시다. 아직 쓰지 않은 가짜 이메일로 바꾼다. 비밀번호는 12~128자·UTF-8 1024바이트 이하다.

```json
{"email":"local-demo@example.test","password":"local-demo-password-2026"}
```

질문 본문 예시다. 새 질문에는 새 `request_key`를 사용한다.

```json
{"content":"교통법 안내 예시를 보여 주세요.","request_key":"local-demo-question-1"}
```

ID는 숫자로 변환하지 않고 문자열로 유지한다. Swagger 페이지를 새로고침한 뒤 필요하면 로그인·Authorize를 다시 하고 **같은 대화 ID**로 조회한다. 같은 본문·키 재전송은 기존 쌍을 `200`으로 반환하며 새 답변을 만들지 않는다. 없는 대화·타인 대화는 동일한 `404`다.

일반 API는 `MockProvider`를 사용하므로 Google·Codyssey 키나 실제 AI 호출이 필요 없다. 실제 2인 소유권·브라우저 통합 검사는 [품질 기준](../03-development/quality.md)에 따라 별도 수행한다.

#### 쿠키 세션 확인

같은 Swagger 브라우저·API 주소를 유지한다. HttpOnly 쿠키는 브라우저가 관리하며 값을 복사하거나 JavaScript로 읽지 않는다. Bearer만으로 refresh·logout을 수행할 수 없다.

| 순서 | 요청 | 입력·기대 결과 |
| --- | --- | --- |
| 1 | `GET /api/auth/csrf` | 현재 쿠키에 맞는 `csrf_token` 반환 |
| 2 | `POST /api/auth/refresh` | X-CSRF-Token에 위 값을 입력, 본문 없음; 브라우저가 쿠키·Origin 전송, 새 access·CSRF와 교체 쿠키 반환 |
| 3 | Authorize 갱신 | 새 access 사용; 다음 세션 변경에는 새 CSRF 사용 |
| 4 | `POST /api/auth/logout` | 최신 X-CSRF-Token과 쿠키·Origin, 본문 없음; 성공 `204` |

access는 15분 유효하고 보호 API는 쿠키만으로 인증되지 않는다. logout도 이미 발급된 access를 즉시 무효화하지 않으므로 남은 유효 시간 안에는 통과할 수 있다.

프런트엔드 `:5173`의 로그인 모달·Google 버튼·채팅 화면에 위 절차가 연결됐다는 뜻은 아니다. Google은 [선택 설정](#2-2-google-로그인용-env-설정)을 준비한 뒤 브라우저 주소창 이동으로 시작한다. `GOOGLE_FRONTEND_ORIGIN`을 설정하면 callback 성공 후 `:5173`으로 이동하며, 없으면 기존 `:8000` 상대 경로를 유지한다. 화면의 CSRF·refresh 완료와 로그인 상태 연결은 아직 미구현이다([#17](https://github.com/next-stop-siren/Next-Stop-Siren/issues/17), [#19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)).

### 4. 테스트와 품질 검사

`test-e2e`는 8000·5173 포트가 비어 있어야 한다. 다른 프로세스가 남아 있다면 원인을 확인하고 소유자와 상의한다.

테스트 DB URL은 기존 `.env`의 `TEST_DB_*`에서 터미널 변수로만 만든다. 아래 명령은 URL을 화면에 출력하지 않는다. `.env`를 쉘에서 실행하거나 Git에 넣지 않는다.

```sh
export TEST_DATABASE_URL="$(backend/.venv/bin/python -c 'import sys; from pathlib import Path; from urllib.parse import quote; sys.path.insert(0, "backend/tests"); from db_guard import read_settings; s=read_settings(Path(".env")); print("postgresql://"+quote(s["TEST_DB_USER"],safe="")+":"+quote(s["TEST_DB_PASSWORD"],safe="")+"@127.0.0.1:55433/"+quote(s["TEST_DB_NAME"],safe=""))')"
./local.sh test-unit
./local.sh test-db
./local.sh db-init-test  # 보호된 테스트 DB에 등록된 5개 제품 테이블 확인·누락 생성
./local.sh test-e2e
./local.sh quality
./local.sh build
```

첫 실패에서는 다음 명령으로 넘어가지 않는다. 현재 백엔드에는 실제 HTTP 가입·로그인 access로 모의 답변 저장·새 클라이언트 재조회·2인 격리를 잇는 `test_auth_chat_db.py`와 인증·세션·대화·메시지·GPT 대체 서버 검사가 있고, 프런트엔드에는 비회원·모달 단위 검사와 브라우저 키보드 검사가 있다. Chromium readiness 검사는 페이지 안에서 `/api/ready`를 fetch하고 503을 모의하며 실제 로그인→채팅 E2E를 대신하지 않는다. 각 도구가 보고한 실제 실행·skip·실패 수를 기록한다. 전체 테스트 묶음은 `./local.sh test`로도 실행할 수 있다.

### 5. 정리와 중지

```sh
./local.sh clean
./local.sh stop
```

`clean`은 `frontend/dist`, Vite 캐시와 알려진 Python 캐시만 제거한다. `.env`, 의존성 디렉터리, DB 볼륨은 남는다. `stop`은 이 프로젝트의 두 DB 컨테이너만 중지한다. `test-db`와 `test-e2e`는 원래 중지된 테스트 DB를 자체적으로 다시 중지한다.

### macOS 검증 범위

과거 이 Mac에서 프로젝트 전용 언어 도구 설치와 재실행, 잠금 설치·DB 준비·기본 검사를 확인한 기록이 있다. 이번 문서 검토에서는 소스의 라우트·스키마·모델 선언을 대조했으며 실제 개발 DB 초기화·가입·Google·AI 호출을 재실행하지 않았다. Docker와 Git은 이미 준비된 호스트였으므로 새 OS 전체 설치를 검증한 기록은 아니다.

## Windows PowerShell: **NOT RUN**

### 준비

Git과 실행 중인 Docker Desktop(Compose 포함)을 준비한다. `.\windows-check.ps1`과 `.\local.ps1 setup`은 언어 도구를 프로젝트 안에 설치·재사용하도록 구성했다. 필요하면 `B71_NODE`, `B71_NPM`, `B71_PYTHON`, `B71_UV`로 기존 도구를 지정한다. `B71_DOCKER`는 수동 준비한 Docker CLI 경로다. Windows 실제 실행은 아직 확인되지 않았다.

실행 정책이 스크립트를 막으면 조직 정책에 맞는 허용 방법을 관리자에게 확인한다.

### PowerShell 인증 키 설정

아래 명령은 모두 저장소 루트의 PowerShell에서 실행한다. 키의 용도와 보관 기준은 [인증 키 생성과 .env 설정](#2-1-인증-키-생성과-env-설정)을 따른다. 이미 유효한 키가 설정됐다면 생성·교체를 반복하지 않는다.

1. 프로젝트 설치를 완료한다. `.env`가 없을 때만 예시를 복사하며 기존 파일은 보존한다.

```powershell
.\local.ps1 setup
```

2. 다음 블록 전체를 복사해 실행한다. 네 키를 각각 독립적으로 생성하며 실제 키가 터미널에 출력되므로 결과를 공유하지 않는다.

```powershell
@'
import base64
import secrets

for name in (
    "AUTH_ACCESS_KEY",
    "AUTH_REFRESH_KEY",
    "AUTH_REFRESH_DIGEST_KEY",
    "AUTH_CSRF_KEY",
):
    key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    print(f"{name}={key}")
'@ | & .\backend\.venv\Scripts\python.exe -
```

3. 메모장으로 `.env`를 열고, 출력된 네 줄을 같은 이름의 빈 항목 대신 붙여넣는다. 다른 설정은 유지하고 `.env` 이름 그대로 저장한다. `.env.txt`나 `.env.example`에 저장하지 않는다.

```powershell
notepad .env
```

4. 키를 출력하지 않고 설정을 검증한다. `Auth settings validated`가 출력돼야 한다. 오류가 나면 해당 설정 항목을 수정한 뒤 다시 확인한다.

```powershell
@'
from pathlib import Path
import sys

sys.path.insert(0, "backend")
sys.path.insert(0, "backend/tests")
from app.security.settings import AuthSettings
from db_guard import read_settings

AuthSettings.from_env(read_settings(Path(".env")))
print("Auth settings validated")
'@ | & .\backend\.venv\Scripts\python.exe -
```

5. 개발 DB의 누락 테이블을 만든 뒤 서버를 실행한다. 기존 테이블을 마이그레이션하는 명령은 아니다. 이미 실행 중인 `dev`는 해당 터미널에서 `Ctrl+C`로 종료한다. 이어서 [Swagger API 확인](#3-1-swagger로-가입로그인과-모의-대화-확인)을 따른다.

```powershell
.\local.ps1 db-init
.\local.ps1 dev
```

이 절차는 Windows에서 실제 실행하지 않았다. Python 실행 파일이 없으면 `setup` 완료 여부와 현재 디렉터리를 먼저 확인한다.

Google 로그인도 사용할 경우 [Google 로그인용 .env 설정](#2-2-google-로그인용-env-설정)의 client 발급 안내와 PowerShell 키 생성·검증 명령을 이어서 실행한다.

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
- DB 준비 실패: [Compose 상태 확인 명령](#compose-프로젝트-식별과-기존-볼륨)로 이 프로젝트 서비스 상태를 확인한다. 복구 지시 없이 `down -v`를 실행하지 않는다.
- DB URL 거부: `.env`의 전용 `TEST_DB_*`에서 다시 URL을 만들고 개발 DB 주소를 사용하지 않는다.
- E2E 포트 오류: 8000·5173 포트 사용 여부를 확인한다. 검증 Mac의 첫 시도에서 원인 미상의 포트 오류가 한 번 있었고, 점유 프로세스가 보이지 않은 뒤 재시도에서 통과했다.

## Compose 프로젝트 식별과 기존 볼륨

모든 로컬 Compose 호출은 프로젝트 `b7-1`, 현재 저장소의 정확한 루트, 루트 `.env`, 루트 `compose.yaml`을 명시한다. 개발·테스트 볼륨은 각각 `b7-1_db-dev-data`, `b7-1_db-test-data`다. `setup`/`dev`/`stop`은 기존 같은 프로젝트 컨테이너의 checkout 경로와 Compose 파일 label을 확인하고 다른 checkout 소유이면 변경 전에 거부한다. 테스트 DB·초기화의 기존 대상·소유권 보호도 유지한다.

이전 기본 프로젝트 이름으로 만든 컨테이너·볼륨은 자동 이동·삭제하지 않는다. 새 이름의 빈 볼륨은 이전 데이터가 사라졌다는 뜻이 아니다. 이전 환경이 55432/55433 포트를 사용 중이면 새 시작이 실패할 수 있다. 기존 컨테이너 label, 프로젝트 이름, 포트, 볼륨과 필요한 데이터를 먼저 확인하고 소유자와 백업·전환 계획을 정한다. 다른 환경을 임의로 중지하거나 `down -v`, 볼륨 삭제, 이름 변경으로 해결하지 않는다. 기존 데이터 이전은 이 준비 절차의 범위 밖이다.

수동 상태 확인도 저장소 루트에서 정확한 대상을 지정한다.

```sh
docker compose --project-directory "$PWD" --env-file "$PWD/.env" -f "$PWD/compose.yaml" -p b7-1 ps --all
```

PowerShell에서는 `$PWD` 대신 `$PSScriptRoot`가 아닌 현재 저장소 절대 경로를 변수로 지정한다.

```powershell
$repo = (Get-Location).Path
docker compose --project-directory $repo --env-file "$repo/.env" -f "$repo/compose.yaml" -p b7-1 ps --all
```
