# 단일 EC2 파일럿 운영 파일 — #73 phase 1

기준: `7b82fefce1c3953df9d92dd40d6904e0d981ccdf`, Work-Item `B71-OBS-028`.
이 디렉터리는 이미지·운영 구성과 격리 로컬 검증을 준비한다. AWS 생성, DNS 변경,
인증서 발급, 이미지 게시, 실제 배포를 실행하거나 #73 전체 완료를 선언하지 않는다.

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
내부 DB 주소/가입 제한/worker 설정을 확인한다. 앱 시작도 이 검사를 먼저 수행한다.
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

전달 헤더는 클라이언트 입력을 그대로 연결하지 않지만 Uvicorn `--no-proxy-headers`로
신뢰하지 않는다. 따라서 현재 로그인 제한의 client IP는 proxy 주소일 수 있다.
**#74의 실제 사용자 IP/위조 헤더/제한 검증이 완료된 설정으로 오해하지 않는다.**

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

기존 개발 compose.yaml/local.sh/DB 보호는 변경하지 않는다. 이미 실행 중인 로컬 Colima만
사용한다. 런타임을 시작·재설정하거나 새 도구를 설치하지 않는다.

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
실제 Google/AI/ACME/DNS/AWS를 호출하지 않는다. TLS 검증 생략은 이 loopback self-signed
smoke에만 한정한다. 이 결과를 실제 인증서·DNS·가용성·t2 성능 검증으로 세지 않는다.

## 남은 출시/배포 게이트

- #72: `/api/health`는 프로세스, `/api/ready`는 SELECT 1뿐이다. preflight도 설정 검증일 뿐
  제품 스키마·worker 준비를 완전히 증명하지 않는다. 실제 기능 확인까지 별도 필요하다.
- #74: 신뢰 proxy/IP와 프로세스별 로그인 제한의 운영 검증이 필요하다.
- #75: 운영 초기화/기존 스키마 적용·백업/복원·보존/RPO/RTO는 미해결이다. 볼륨이나
  create_all을 백업/마이그레이션으로 세지 않는다. 개발 db-init 보호를 우회하지 않는다.
- Owner가 Actions workflow_dispatch + OIDC 단기 AWS 인증 + SSM Run Command를 실행하는
  다음 범위는 여기서 구현하지 않는다. 공개 SSH22/self-hosted runner/장기 AWS키/SSH키/
  NAT gateway/유료 private endpoint를 도입하지 않는다. 실제 account/role/instance/branch/
  environment/document/policy는 미정이다.
- 수동 rollout concurrency1/cancel-in-progress:false, readiness+기능 확인, 실패 중단·보고,
  Owner의 #75 연계 이전 버전 수동 rollback이 필요하며 DB를 맹목 자동 복원하지 않는다.
- Owner와 yejoo0310 검토에서 각 부분의 nonimplementer가 확인한다. yejoo 작성 인증 부분의
  다른 검토자는 미정이다. 최종 파일과 검증 증거를 독립 검토한 뒤에만 로컬 커밋한다.
