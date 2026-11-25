# 단일 EC2 파일럿 운영 파일

## 현재 운영 인계 (2026-11-25 KST)

현재 담당은 Owner `xifoxy-ru` 한 명이며 운영 환경 실행은 PM-2/Owner가 관리한다. 아래 운영 관찰은 Owner·PM-2의 인계 보고이고, 이번 문서 Worker가 EC2·Google·금고·Discord에 다시 접속해 검증한 결과가 아니다. [#73](https://github.com/next-stop-siren/Next-Stop-Siren/issues/73)·[#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74)·[#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75)·[#76](https://github.com/next-stop-siren/Next-Stop-Siren/issues/76)은 열려 있으며 남은 운영 확인을 추적한다.

| 증거 | 확인한 범위 | 아직 대신하지 않는 확인 |
| --- | --- | --- |
| #73 실제 시험 배포 | HTTPS·readiness와 비밀번호 로그인/새로고침, 모의 질문·답변 저장/복구, 한도 1회 차감, 로그아웃 유지 확인 보고 | 실제 AI 생성·미확인 운영 조건 |
| #74 A/B 프록시 관찰 | A/B 요청이 Nginx에서 서로 다른 클라이언트 주소로 관찰됐다는 보고, 별도 격리 검사 34개 통과 보고 | 운영에서 제한 상한까지 소진하는 반복 요청·주소 변경·전체 장애/독립 운영 검사. 34개를 운영 부하 결과로 세지 않음 |
| #75 수동 백업·서버 밖 사본 | 수동 백업 1회와 Owner Mac 암호화 금고 사본의 무결성 일치 보고 | 금고 사본으로 실제 운영 DB 복원, 자동 실행 성공·보존/디스크 수용 검증 |
| #75 timer·알림 | 첫 정기 실행 예정 **2026-11-26 04:00 KST**, Discord 실패 알림 수신 보고 | 11/25 현재 첫 정기 백업은 아직 실행 시각 전. 알림 수신을 정기 백업 성공으로 표시하지 않음 |
| Google 설정 | 동일 backend 이미지에 설정 반영, DB/Nginx 유지, public `/api/ready` 200, start 302→`accounts.google.com`, 정확한 callback 확인 보고 | 실제 신규 Google 가입·재로그인·세션과 비구현자의 독립 확인 ([체크리스트](../docs/03-development/google-login-readiness.md)) |

### 최신 main과 실제 배포 이미지는 다르다

- 이번 문서의 코드 기준은 main **`f3df305ad1b22283fd35b3163ec0df3a17b29dc9`**다. [PR #111](https://github.com/next-stop-siren/Next-Stop-Siren/pull/111) AI opt-in·불완전 스트림 처리, [#112](https://github.com/next-stop-siren/Next-Stop-Siren/pull/112) Google 준비 문서, [#113](https://github.com/next-stop-siren/Next-Stop-Siren/pull/113) 공통 환영·중립 회원 홈, [#114](https://github.com/next-stop-siren/Next-Stop-Siren/pull/114) 오래된 실패 작업 복구가 병합됐고 main push CI 성공이 확인됐다. 각각의 로컬 검사와 실제 운영 적용을 구분한다.
- 확인된 배포 앱의 source는 **`fe2ae6d8993cdf1821588ac0023bca20e74613d8`**다([이미지 게시 run 44274974640](https://github.com/next-stop-siren/Next-Stop-Siren/actions/runs/44274974640), 성공). Google 설정 반영도 이 이미지를 재사용했다는 Owner 보고다.
- [배포 workflow run 45115438120](https://github.com/next-stop-siren/Next-Stop-Siren/actions/runs/45115438120)의 workflow SHA **`9e003adfa5c38e168ce34434e33dace541c28026`**를 앱 이미지 source로 쓰지 않는다. 수동 배포는 이전 publication을 선택할 수 있다. 최신 main 병합·CI 성공은 새 이미지 게시·배포를 뜻하지 않는다.
- 시험 배포에서 저장된 답변은 mock이다. 최신 코드에 Codyssey opt-in이 있어도 현재 서버가 그 코드를 배포·활성화했거나 실제 AI 호출을 확인했다는 증거는 없다.

### 다음 배포 체크리스트 — 이번 작업에서는 실행하지 않음

1. Owner가 대상 source·변경 범위·실행 승인을 확인한다. 최신 main과 현재 이미지 source의 차이, #111–114의 변경과 실제 호출 제외 범위를 검토한다.
2. 같은 source의 신뢰된 main push CI, public GHCR frontend/backend digest 쌍, publication run/attempt·release manifest를 확인한다. workflow SHA와 release source를 따로 기록한다. 기존 helper/contract/readiness hash와 source별 gates도 재대조하며 자동 우회하지 않는다.
3. 현재 데이터와 이전 릴리스·이미지, #75의 변경 전 백업·서버 밖 사본, 스키마 호환·복구/로그 보관과 inflight 상태를 확인한다. [#105](https://github.com/next-stop-siren/Next-Stop-Siren/pull/105)의 교체 후 실패 대응은 병합·독립 재검토 완료이며, 실제 서버 DB 복원 성공으로 확대하지 않는다.
4. 승인된 비밀값·Origin·proxy·Google 설정의 유지와 컨테이너 재생성을 확인한다. Codyssey 사용은 별도 실제 호출 승인·비밀 주입이 필요하다. mock 유지와 실제 제공자 설정 오류의 안전 실패를 혼동하지 않는다.
5. 기존 수동 Actions/OIDC/SSM 경로로 승인된 버전을 배포하고 readiness·실제 image ID/source를 확인한다. 별도 승인 없이 이 문서에서 배포·DB 변경·실제 Google/AI 호출을 실행하지 않는다.
6. 사용자 관점에서 중립 회원 홈·명시적 대화 선택, 저장 이력·100건 밖 실패 작업 동작·SSE/재접속·한도·로그아웃을 확인한다. 실제 Google 가입/재로그인과 AI 확인은 각각 승인된 별도 항목으로 기록한다. 실패 시 inflight를 보존하고 상태를 조정한 뒤 이전 앱/DB 호환성을 검토하며 자동 DB 복원하지 않는다.
7. 첫 timer의 실제 결과(11/26 04:00 이후), 서버 밖 사본·실패 알림·분석 로그, 미수행 #74/#75/#76 항목을 기록한다. 측정·검토는 수행자/환경/시각과 함께 남기며 아래 로컬 리허설을 EC2 성능 증거로 쓰지 않는다.

### #76 로컬 3인 mock 리허설 (2026-11-25)

기존 [`scripts/issue54_test_app.py`](../scripts/issue54_test_app.py)의 `Issue54Provider(MockProvider)`와 [`issue54_e2e.py`](../scripts/issue54_e2e.py)의 격리 engine/probe를 사용했다. 검토용 외부 driver만 추가했고 제품 코드·설정은 바꾸지 않았다. macOS arm64·Python 3.13.15·기존 Colima Engine 29.5.2·캐시 `postgres:17.11`에서 합성 회원 3명, 일회용 DB/임시 schema, 별도 network와 `127.0.0.1` 임시 API/DB 포트를 사용했다. 이미지 pull·실제 키·Google/AI/EC2는 사용하지 않았다.

실행 전에 로컬 이상 징후 기준을 API 응답 경험적 p95 2초 미만, 단일 가입 최대 5초 미만, 접수부터 mock 종결 최대 8초 미만, 시나리오와 다른 HTTP 상태 0건, API RSS 관찰 경계 512MiB 미만으로 고정했다. 이 작은 표본의 기준은 EC2 수용/출시 기준이 아니다.

| 관찰 | 결과 |
| --- | --- |
| 합성 HTTP 요청 | 39건: 200 23건 / 201 6건 / 202 5건 / 예상 interval429 3건 / 타인404 1건 / 시작 대기 중 ready503 1건 |
| API 경험적 p95 (startup ready 제외, 가입 포함) | 0.292초 (< 2초); 작은 표본의 정렬 위치 `floor(.95 × (n−1))` |
| 단일 가입 최대 | 0.292초 (< 5초) |
| 접수부터 mock 종결 최대 | 1.535초 (< 8초) |
| API process 관찰 | 0.25초 간격 19개 표본, RSS 최대 164.61MiB (< 512MiB), `ps %cpu` 최대 72.1. DB/컨테이너 합산이나 t2 CPU credit 값이 아님 |
| 제공자 시작 | 5회: 정상 3회 + 명시적 합성 실패 1회 + 사용자 수동 재시도 1회. 자동 재시도 없음 |
| 정리 | API 종료·해당 임시 schema 부재·자기 container/일회용 DB 저장소·network 부재 확인 |

동시 접수 3건의 mock 답변이 실제 로컬 PostgreSQL에 저장·재조회되고 회원별 SSE completed가 전달됐다. 각 회원의 일일 used=1/reserved=0, 동일 키의 기존 작업 반환, 10초 내 새 키 429, 타인 이력404를 확인했다. 기존 test-only interval probe로 합성 DB의 시각만 조절해 scripted 실패 뒤 예약 해제(used=1/reserved=0)와 명시적 재시도 성공 후 used=2/reserved=0을 확인했다. 이 시간 조절은 실제 10초 대기·기간 경계 부하 측정이 아니다.

최초 두 준비 시도는 포트 매핑 부재와 임시 schema/role 기본 search_path 불일치로 트래픽 시나리오 전에 실패했다. 실패 로그와 매 시도 cleanup을 보존하고 외부 driver의 일회용 DB 설정만 보완한 세 번째 실행에서 통과했다. `/api/ready`의 스키마 조건을 생략하거나 성공으로 대체하지 않았다.

실행 명령은 기존 venv의 Python으로 `rehearse.py <isolated-checkout>`였고, 외부 driver·사전 thresholds·요청별 지연·결과·원본 fixture hash는 이번 작업의 검토 증거에 보존한다. [`deploy/actions/tests`](actions/tests)의 `python3 -m unittest discover -s deploy/actions/tests -v`는 별도로 **60개 통과**했다. scripts stdlib suite는 **43개 실행, 41 통과·1 실패·1 skip**이며 실패는 macOS `os.killpg(..., 0)`의 `PermissionError` 때문에 예상 timeout 문구와 달라진 자식 프로세스 정리 검사다. 기존 Python 3.13 venv의 sandbox 밖 재검사에도 남았다. 같은 테스트 1개를 일반 시스템 Python 3.14로 다시 실행하면 16.631초에 통과했고 소유 fixture 프로세스 잔류 0건을 확인했다. 서로 다른 환경의 결과를 구분하며 전체 scripts 통과로 표시하지 않는다. 제품 코드는 수정하지 않았다.

리허설은 FastAPI·worker·DB·TCP SSE의 제한된 로컬 자체검사다. 브라우저 UI·Nginx/TLS·서로 다른 외부 source IP·장시간 구독·EC2 재시작/자원 압박·알림·복원·실제 AI/Google·운영 독립 확인은 수행하지 않았다. #76 전체 완료나 2~3인 EC2 수용 보장으로 확대하지 않는다.

## phase 1 준비 기록 (2026-10-29 기준)

아래 기준 SHA·미실행 설명·당시 담당/검증 결과는 준비 Phase의 기록이다. 현재 운영 여부는 위 인계를 따른다.

기준: `7b82fefce1c3953df9d92dd40d6904e0d981ccdf`, Work-Item `B71-OBS-028`.
이 디렉터리는 이미지·운영 구성과 격리 로컬 검증을 준비한다. AWS 생성, DNS 변경,
인증서 발급, 이미지 게시, 실제 배포를 실행하거나 #73 전체 완료를 선언하지 않는다.

#74 프록시·로그인 제한의 후속 반영 기준은 `40035b68217c21913ceb85da704cfc842272f24c`
(2026-10-30 KST)이다. 아래 로컬 검사 결과는 실제 운영 신뢰 범위의 승인이나 독립 검토 완료가 아니다.

서울 `ap-northeast-2`의 t2.micro, Ubuntu 24.04 LTS, 1주·2~3명 파일럿을 유지한다.
`next-stop-siren.duckdns.org`의 프런트/API/Google 복귀는 같은 HTTPS Origin이다.
웹은 IP 제한 없이 공개하되 기존 인증·인가·요청 제한을 유지한다. 용량과 가동시간은
#76에서 실제 측정해야 한다. 실제 AI는 활성화하지 않으며 기본 MockProvider를 유지한다.

## 파일과 이미지

- `frontend.Dockerfile`: 잠금 Node/npm 빌드 후 Nginx에서 정적 파일만 제공한다.
- `backend.Dockerfile`: 잠금 Python/uv 의존성과 앱만 복사하고 UID 10001로 실행한다.
- `compose.yaml`: Nginx만 80/443을 공개하고 API/DB는 내부 네트워크에 둔다.
  PostgreSQL 17.11의 이름 있는 볼륨은 백업이 아니다.
- 루트 `.dockerignore`는 기본 거부 후 필요한 소스만 허용한다. Dockerfile도 좁은
  COPY만 사용한다. 실제 env·키·인증서·DB·백업·캐시를 빌드 문맥/레이어에 넣지 않는다.
- `production.env.example`과 `release.env.example`은 비밀값 없는 불완전 예시다.
  그대로 기동하면 필수값 누락으로 실패해야 한다.

CI에서 같은 commit의 프런트/백 이미지를 만들고 기존 공개 GHCR 선택과 GITHUB_TOKEN
게시 방식을 유지한다. 실제 org/package·권한·보존은 미정이다. 이 단계에는 Actions가 없다.
빌드 base tag는 빌드 로그에 실제 digest가 남으며, 배포 릴리스는 **앱 이미지 digest 쌍과
Compose 버전**으로 고정한다. mutable tag만으로 되돌림을 식별하지 않는다. EC2에서 전체
빌드·테스트를 하지 않는다. 이전 릴리스를 보존하며 DB 되돌림은 #75와 별도로 다룬다.

## 서버 설정과 실패 경계

Owner PC의 암호화 금고에 비밀 원본/복구 사본을 보관한다. 서버에는 실행 필수값만
권장 경로 `/etc/next-stop-siren/production.env`에 제한된 권한으로 주입한다. 이 경로의 실제
파일을 여기서 만들거나 읽지 않는다. 제품·계정·금고 경로·서버 권한/교체/복구 절차는
Owner가 정해야 한다. 채워진 파일을 저장소·이미지·배포 명령 출력·로그에 넣지 않는다.
`DATABASE_URL`은 URL 인코딩한 자격을 사용한 **plain `postgresql://...@db:5432/...`**이며
POSTGRES_USER/PASSWORD/DB와 일치해야 한다. `postgresql+psycopg`는 현재 `/api/ready`와
호환되지 않으므로 허용하지 않는다.

`validate-release.py RELEASE_ENV RUNTIME_ENV`는 외부 파일의 제한 권한, 단순 dotenv 문법,
경로, 이미지 digest 형식만 확인한다. 값을 출력하거나 셸로 평가하지 않는다.
그 다음 실제 backend 이미지의 `--validate-only`로 production/Auth/Secure/정확한 Origin/
내부 DB 주소/가입 제한/내부 chat worker 설정과 명시적인 프록시 신뢰 목록을 확인한다. 앱 시작도 이 검사를 먼저 수행한다.
네 개 인증 키는 각기 독립적인 최소 32바이트 Base64 값이어야 한다.

Google 자격은 **비밀번호 로그인·이미지 빌드의 선행 조건이 아니다**. 사용하지 않을 때는
세 자격 설정을 모두 생략한다. Google을 설정하기 시작하면 기존 GoogleSettings 검증과
동일 Origin 프런트 복귀를 요구한다. 실제 client 등록, callback
`https://next-stop-siren.duckdns.org/api/auth/google/callback`, 성공/실패 복귀는 별도 확인한다.

Uvicorn은 한 프로세스, 내부 ChatJobWorker는 기존 `max_members=4`를 유지한다.
Uvicorn의 요청 drain 기한은 10초이며, 앱은 worker의 poll thread join에 35초 인자를 전달한다.
그 뒤 pool.shutdown(wait=True)에는 별도 hard timeout이 없어 **worker.stop 전체가 35초로
끝난다는 보장은 없다**. Compose stop_grace_period 60초는 컨테이너의 최종 stop/kill 경계다.
긴 작업·DB 장애에서는 강제 종료될 수 있으므로 미완료 작업 복구와 종료 실패 조건을 #76에서
추가 검증해야 한다. 이번 mock/idle smoke의 종료 성공을 그 상한 보장으로 세지 않는다.
nginx는 SIGQUIT로 종료한다.

## 프록시·로그

UI 경로에만 SPA fallback을 적용한다. 없는 `/assets/*`는 404이며 해시 asset은 immutable
캐시, index는 no-cache다. `/api` 오류는 upstream의 JSON/상태를 보존한다. SSE buffering과
cache를 끄고 proxy read/send timeout을 75초로 두어 15초 ping에 여유를 준다.
내부 upstream 연결 수립 기한은 5초로 제한해 중단된 backend의 실패 응답을 무한정 기다리지 않는다.

access log는 `$uri`를 쓰며 query·Cookie·Authorization·body를 기록하지 않는다.
Nginx error log는 raw request/query를 포함할 수 있어 **/dev/null로 비활성화**했다.
이는 Nginx 오류 상세 진단을 잃는 절충이다. 안전한 access 상태·지연과 정제된 backend
로그·명시적 probe로 확인한다. 임의 경로 자체에 민감값을 넣는 URL 사용을 허용하거나
모든 가능한 로그 무누출을 보증하는 것은 아니다. 실제 proxy 로그 경로는 운영에서 재확인한다.

Nginx는 외부 `X-Forwarded-For`와 `X-Real-IP`를 `$remote_addr` 하나로 덮어쓰고,
`Forwarded`를 제거한다. Uvicorn은 `--proxy-headers --workers 1`로 실행하며 운영 runtime의
`FORWARDED_ALLOW_IPS`에 명시한 연결 상대 IP/CIDR만 신뢰한다. 앱은 서버가 반영한 ASGI
client IP를 검증해 기존 로그인 제한 키로 사용하며 헤더를 다시 파싱하지 않는다.
직접 접속·비신뢰 연결의 전달 헤더는 사용자 IP로 채택하지 않는다.

`FORWARDED_ALLOW_IPS`는 필수다. 쉼표로 구분한 IPv4/IPv6 주소 또는 엄격한 CIDR을 지정한다.
항목 양끝 공백은 허용하지만 누락·빈 값/항목·`*`·`0.0.0.0/0`·`::/0`, 호스트 이름·URL·포트·
소켓 경로·scope ID가 붙은 IPv6·잘못된 IP/CIDR은 preflight/startup에서 거부한다.
[운영 env 예시](production.env.example)는 아래 전용 네트워크에 고정된 `172.28.74.2` 하나만 신뢰한다.
목록은 backend에서 관찰되는 실제 Nginx 주소 또는 Owner가 승인한 전용 네트워크의 가장 좁은 범위로 정한다.
`nginx` 서비스 이름은 자동 DNS 해석되지 않으며 임의의 Docker 기본 대역을 운영 신뢰 범위로 복사하지 않는다.
형식 검사 통과는 해당 범위의 운영 승인이 아니다. 목록 변경 뒤 backend를 재시작한다.

Compose의 `proxy` 네트워크는 `172.28.74.0/29`(gateway `.1`)이며 Nginx 주소는
`172.28.74.2`로 고정한다. 동적 pool은 `172.28.74.4/30`으로 제한해 Nginx가 없을 때도
다른 컨테이너가 `.2`를 선점하지 못하게 한다. backend만 proxy와 별도 `database` 네트워크에 연결하고 DB는
internal database 네트워크에만 연결한다. Nginx와 DB는 네트워크를 공유하지 않는다.
proxy는 internal이 아니므로 backend의 외부 Google/AI 제공자 연결 경로를 유지한다.
인증 소스 로직은 변경하지 않으며 `/29` 전체를 신뢰하지 않는다.

Owner가 관찰한 EC2 경로 `172.31.32.0/20`, `172.17.0.0/16`과 이 대역은 겹치지 않는다.
실제 적용 직전에 전체 호스트/VPC/컨테이너 경로와 기존 Docker IPAM 충돌을 다시 확인해야 한다.
충돌 시 기존 네트워크를 지우거나 임의 대역으로 바꾸지 말고 중단한다. `/29`에서 gateway와
Nginx를 제외한 동적 pool은 `.4`~`.6` 3개이며 단일 backend 운영을 전제로 한다. smoke는
backend와 두 client에 3개를 사용하고 `.3`은 pool 밖의 수동 여유 주소로 남긴다. 동시 스택·scale·추가 컨테이너 배치 전 주소 용량을 검토한다.
네트워크를 재생성해도 Nginx 주소와 trust는 그대로여야 하며 runtime env를 채운 뒤 backend를 재시작한다.

기존 스택의 DB 컨테이너는 별도 전환이 필요하다. `server_deploy.py`는 DB를 `--no-recreate`로
시작하므로 이전 default 네트워크의 DB를 새 database 네트워크로 자동 이전하지 않는다.
새 disposable 스택의 로컬 검증은 이 전환을 검증하지 않는다. 기존 스택에 적용하기 전
DB 볼륨을 보존하는 컨테이너·네트워크 전환과 백업·복구 계획을 별도로 승인해야 한다.
DB 볼륨을 삭제하거나 단순 재시작만으로 전환된다고 가정하지 않는다. Owner EC2에는 아직
배포된 앱이 없다는 제공 정보에 따르면 첫 신규 배포는 이 기존 스택 전환 대상이 아니다.

Uvicorn CLI가 별도로 읽는 `UVICORN_FORWARDED_ALLOW_IPS`는 생략하는 것을 권장한다.
함께 지정하면 `FORWARDED_ALLOW_IPS`와 문자열이 정확히 같아야 하며, 다른 값(빈 값 포함)이면
preflight/startup에서 거절한다. 공백이나 주소 표기만 달라도 충돌로 처리한다.
이는 검사한 신뢰 목록과 실제 실행에 적용되는 목록이 달라지는 것을 막기 위한 검사다.

신뢰 상대의 XFF 누락·빈 값은 프록시 연결 IP로 남고, 중복 필드는 마지막 값을 쓰는 Uvicorn 기본 동작이다.
앱이 유효하지 않은 최종 IP를 받으면 로그인은 기존 503으로 거절한다.
따라서 Nginx가 항상 단일 실제 주소로 덮어쓰는지, 외부가 backend에 직접 접근할 수 없는지를 운영에서 확인한다.
추가 CDN/LB나 real-IP 재작성 설정을 도입하면 신뢰 경계를 다시 검토한다.

로그인 실패 제한은 이메일별 5회/600초·IP별 30회/600초이며 성공하면 해당 이메일 실패만 초기화한다.
카운터는 프로세스 메모리에 있어 worker/replica 간 공유되지 않고 재시작하면 초기화된다.
기본 CMD의 worker 1만으로 외부 override·scale·동시 릴리스를 차단했다고 보지 않는다.
Owner가 worker 1·backend 1 replica·교체 경로와 재시작 초기화 수용을 확인해야 하며,
확대 전 공유 제한을 재검토한다. Redis나 새 공유 저장소는 추가하지 않았다.
**#74의 로컬 프록시 검사는 통과했지만 실제 운영 신뢰 주소·주소 변화·EC2 적용과 독립 검토는 남아 있다.**

## TLS 발급·자동 갱신 설계 (실행하지 않음)

Let's Encrypt + Certbot HTTP-01을 사용한다. 공개 80은 ACME와 HTTPS redirect, 443은 HTTPS다.
첫 인증서가 없을 때는 `compose.acme-bootstrap.yaml`의 HTTP 전용 설정으로 challenge만 제공한다.
`compose.acme.yaml`의 Certbot은 명시적 작업 때만 쓰는 도구이며 항상 실행하는 앱 서비스가 아니다.
인증서/ACME 볼륨은 Certbot만 쓰고 nginx는 읽기 전용으로 사용한다.

별도 승인된 서버 작업에서 Owner가 외부 release/runtime 파일을 검증한 뒤 다음 순서를 적용한다.
`certificates.sh --approved-server-operation CONTEXT RELEASE_ENV RUNTIME_ENV bootstrap`,
같은 인수의 `issue CONTACT_EMAIL`, 이후 운영 기본 Compose로 전환하고 실제 TLS를 확인한다.
갱신은 같은 스크립트의 `renew`가 Certbot renew → nginx -t → reload를 첫 실패에서 중단한다.
Owner가 서버의 기존 스케줄러에 일일 timer와 실패 알림을 등록해야 자동 갱신이 완성된다.
이 저장소는 timer를 설치하거나 실제 발급/갱신에 성공했다고 주장하지 않는다.
Certbot image도 release 파일에 검토한 digest로 지정한다. DuckDNS 계정·DNS/IP 갱신 방식,
연락 이메일, 발급/갱신 작업 시점과 실제 검증 결과는 남은 입력이다.

## 격리 로컬 검증

기존 개발 compose.yaml/local.sh/DB 보호는 변경하지 않는다. 이미 실행 중인 로컬 Colima 또는
Docker Desktop의 Unix socket을 사용한다. `--context colima` 또는 `--context desktop-linux`를
지정하며 Docker 명령과 smoke의 context를 일치시킨다. 런타임을 시작·재설정하거나 새 도구를 설치하지 않는다.
아래 명령은 Colima 예시이며 Docker Desktop에서는 모든 `colima`를 `desktop-linux`로 바꾼다.

```sh
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python deploy/check.py
docker --context colima build --platform linux/amd64 -f deploy/frontend.Dockerfile -t b71-issue73-fe:local .
docker --context colima build --platform linux/amd64 -f deploy/backend.Dockerfile -t b71-issue73-be:local .
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python deploy/local-smoke.py --context colima \
  --frontend-image b71-issue73-fe:local --backend-image b71-issue73-be:local \
  --evidence-dir /absolute/new/external/evidence-directory
```

smoke는 새로운 무작위 project, loopback 임시 포트, 합성 인증/DB 값과 하루짜리 self-signed
인증서만 쓴다. Docker API는 local Unix socket만 허용하고 이미지 linux/amd64를 확인한다.
새 disposable DB에만 명시적으로 create_all을 호출하며 production startup에서 DDL을 하지 않는다.
해당 project의 소유 label을 확인한 뒤 자기 컨테이너/볼륨만 제거한다. 기존 workload/volume을
사용하거나 전체 prune하지 않는다. 실패 시 external result/commands/failure-stack를 확인한다.
채팅 smoke는 target job의 completed 이벤트와 같은 답변 ID·completed 상태·비어 있지 않은
모의 본문/조각의 일치 및 실제 저장 재조회를 요구한다. 완료 대기는 ping/다른 이벤트로
연장되지 않는 전체 40초 기한이며, 실패·중단·미완료·다른 답변은 거부한다.
smoke는 `example.com`의 공개 DNS 조회와 TCP 443 연결로 outbound 경로를 확인하며
HTTP 요청이나 provider API는 호출하지 않는다. Google/AI/ACME/AWS와 DNS-provider API의
실제 동작은 검증하지 않는다. TLS 검증 생략은 이 loopback self-signed smoke에만 한정한다. 이 결과를 실제 인증서·운영 DNS 설정·가용성·t2 성능 검증으로 세지 않는다.

smoke는 기존 Docker IPAM과 `/29` 충돌을 먼저 거부하고, 새 project proxy 네트워크의 이름·소유 label·정확한 대역을 검사한다.
합성 runtime도 Nginx `172.28.74.2` 하나만 신뢰하고 두 client는 같은 proxy 네트워크의 다른 주소를 쓴다.
서로 다른 실제 source IP의 `client_a`/`client_b`에서 빈·중복·위조 헤더, IP별 분리·이메일별 공유,
동시 실패·성공 후 이메일 초기화/IP 실패 보존, 429·Retry-After와 Nginx/backend 재생성을 확인한다.
컨테이너 재생성 때 IP가 재사용될 수 있으므로 container ID와 실제 주소를 함께 기록한다.
클라이언트 주소는 신뢰 목록에 포함하지 않는다. backend 직접 접근의 전달 헤더 거부 경계는 별도의 [비신뢰 TCP 검사](../backend/tests/test_login_proxy.py)와 실제 운영 구성에서 구분해 확인한다.

`result.json`은 검사·정리 결과, `proxy-layout.json`은 container/IP/image identity,
`login-proxy-results.json`은 응답 상태·오류 코드·재시도 시간과 쿠키/토큰 존재 여부만 담는다.
합성 비밀번호는 stdin으로 전달하고 로그인 응답 요약에 토큰·쿠키 원문을 기록하지 않는다.
`runtime.env`·인증서 private key·실패 명령의 원문 출력은 공유할 요약 자료에 넣지 않는다.

### #74 로컬 확인 결과 (2026-10-30 KST)

코드 `4979a90`의 운영 이미지와 `40035b6`에 포함된 검사 스크립트를 사용했다.
앱/빌드 입력은 두 commit 사이에 변경되지 않았다. Docker Desktop에서 실제 `linux/amd64` 이미지를 빌드하고
Nginx→backend→disposable PostgreSQL의 **13개 통합 항목**을 통과했다.
로그인 15개 시나리오·80건의 결과는 401 68건 / 429 10건 / 200 2건으로 예상과 일치했다.
자기 project의 container/network/volume 정리를 확인했고 기존 개발/테스트 DB는 유지했다.

[오프라인 검사](check.py) 15개, [TCP/기존 로그인 검사](../backend/tests/test_login_proxy.py)와
기존 limiter/API/integration 검사 합계 63개가 통과했다. 전체 비DB 1,738개와 Actions 오프라인 18개는
앞선 인증 구현 단계의 결과이며 수치에 포함 관계가 있어 합산하지 않는다.
일반 CI는 현재 `deploy/check.py`를 실행하지 않으므로 이 15개를 자동 CI 통과로 표시하지 않는다.

결과 원본은 로컬에서 보관했으며 별도 인계가 필요하다. 이번 재생성에서는 IP가 재사용됐고 합성 네트워크 CIDR을 신뢰했다.
실제 운영 신뢰 범위·주소 변경·IPv6 source·EC2·독립 검토와 t2.micro 부하 측정은 미완료다.

## 남은 출시/배포 게이트

- #72: `/api/health`는 프로세스만, `/api/ready`는 DB SELECT 1·인증 설정·제품 테이블/컬럼·
  이 프로세스의 worker poll을 읽기 전용으로 확인한다([상태 확인](../docs/04-reference/api.md#상태-확인-72)).
  rollout은 `ready` 200을 트래픽 투입 조건으로, 503의 `checks`와 `Service not ready` 로그를 원인
  확인에 쓴다. 컬럼 타입·제약 호환성과 실제 로그인·채팅은 확인하지 않으므로 기능 확인은 계속
  필요하다. 실제 서버에서의 확인 순서는 Owner와 #76에서 검증한다.
- #74: 명시 프록시 신뢰와 로컬 검사는 구현했다. Owner의 실제 신뢰 주소/접근 경계·단일 실행·재시작 수용,
  운영 구성 확인과 독립 검토를 마친 뒤 #76 및 trusted_proxy 게이트의 실제 증거에 연결해야 한다.
- #75: 아래 [백업 결정과 인계](#75-백업-결정과-인계)의 주기·보존·RPO는 결정됐다. 빈 DB 초기화/기존
  스키마 적용·백업/검증/복원 절차와 도구, 격리 합성 리허설은 [데이터 보존 배포·복구 절차](data-recovery.md)에 있다.
  RTO·로그 보존·교체 전 DB 보존 기준은 2026-11-09에 정했다. 수동 백업·금고 사본 확인 보고와 첫 timer 예정/실제 운영 복원 미확인은 위 현재 인계를 따른다. 볼륨이나 create_all을
  백업/마이그레이션으로 세지 않는다. 개발 db-init 보호를 우회하지 않는다.
- Owner가 Actions workflow_dispatch + OIDC 단기 AWS 인증 + SSM Run Command를 실행하는
  경로는 [Actions 구현](actions/README.md)을 따른다. 아래 phase 1 선택을 유지한다. 공개 SSH22/self-hosted runner/장기 AWS키/SSH키/
  NAT gateway/유료 private endpoint를 도입하지 않는다. 실제 account/role/instance/branch/
  environment/document/policy와 실행 결과는 PM-2/Owner의 승인된 운영 기록으로 대조한다.
- 수동 rollout concurrency1/cancel-in-progress:false, readiness+기능 확인, 실패 중단·보고,
  Owner의 #75 연계 이전 버전 수동 rollback이 필요하며 DB를 맹목 자동 복원하지 않는다.
- 현재 담당은 Owner xifoxy-ru이며 독립 운영 검증은 해당 변경을 구현하지 않은 검증자가 수행한다. 과거 검토자 이름은 당시 증거의 귀속이며 새 배정이 아니다.

## #75 백업 결정과 인계

[#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75)의 직접 선행은
[#53](https://github.com/next-stop-siren/Next-Stop-Siren/issues/53)의 API·저장 스키마와
[#73](https://github.com/next-stop-siren/Next-Stop-Siren/issues/73)의 운영 구성이다.
이 절은 결정과 다음 인계 기준이며, 선행 충족·운영 준비·복원 성공을 확인한 기록이 아니다.

| 결정 항목 | 파일럿 기준 |
| --- | --- |
| 주기 | 하루 1회 정기 백업하고, 스키마·테이블 구조 변경 또는 기존 데이터 대량 수정 직전에도 백업한다. 일상적인 질문·답변 저장은 변경 전 백업 대상에서 제외한다. |
| 서버 밖 사본 | Owner Mac의 암호화 금고로 복사해 서버 밖에 보관한다. 서버의 DB 볼륨만으로 백업을 대신하지 않는다. |
| 보존 | 1주 파일럿이 끝난 날부터 7일 뒤까지 보관한다. |
| 허용 데이터 손실 (RPO) | 최대 24시간. |
| 복구 목표 시간 (RTO) | 장애 인지 후 4시간 이내 (2026-11-09 운영 기준). |
| 로그 보존 | 기존 크기 제한 회전 유지. 장애 분석에 필요한 로그는 컨테이너 재생성 전에 별도 보관한다. 백업 보존 기간을 로그에 자동 적용하지 않는다 (2026-11-09 운영 기준). |
| 교체 전 DB | 복원 시 디스크 여유를 확인하고, 복원 검증이 끝날 때까지 보존한다 (2026-11-09 운영 기준). |

현재 인계·실행 담당은 Owner `xifoxy-ru`다. 과거 `davekim-dev`/`yejoo0310`의 구현·독립 검토 증거는 [데이터 보존 절차](data-recovery.md)의 날짜별 기록으로 보존하며 새 팀 배정으로 읽지 않는다. Owner는 실제 구성·DB/볼륨·권한과 파일럿 날짜, 변경 전 백업·복원 대상·호환성·실패 중단·로그 보관 조건을 대조한다. 실제 운영 DB 복원과 첫 timer 성공은 미확인이고, 독립 운영 확인자는 해당 변경 구현자와 분리해 결과에 기록한다. 비밀값·실제 DB 내용·금고 경로는 공개 자료에 넣지 않는다.

수동 백업 1회·서버 밖 사본 무결성 일치와 Discord 실패 알림 수신 보고는 위 현재 인계에 구분했다. 정기 백업은 첫 예정 시각 뒤 실제 결과를 확인해야 하며 사본 무결성만으로 복원 가능성을 완료 처리하지 않는다. #105의 교체 후 실패 대응 재검토 완료와 #75 전체 완료도 구분한다. 앱 되돌림·DB 복원은 자동으로 수행하지 않는다.
