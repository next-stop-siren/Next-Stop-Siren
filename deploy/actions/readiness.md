# Pilot readiness host 검사 (B71-OBS-055)

[verify-pilot-readiness](verify-pilot-readiness)는 기존 `server_deploy.py`가 호출하는
`--source <40hex> --frontend <공개 GHCR digest> --backend <공개 GHCR digest>` 계약을 구현한다.
서버 설치·첫 배포·실사용자 로그인/채팅 검증은 이 로컬 구현과 별도다. 이 파일 추가가
`deploy-gates.json`의 approval이나 #72/#74/#75/#76의 운영 증거를 대신하지 않는다.

## 신뢰와 사전 조건

Ubuntu 24.04의 `/usr/bin/python3`, `/usr/bin/docker`, 로컬 Docker socket
`unix:///var/run/docker.sock`, `/etc/ssl/certs/ca-certificates.crt`를 사용한다.
Python stdlib만 사용하며 `#!/usr/bin/python3 -I`로 user site/PYTHONPATH/작업 디렉터리의
동명 모듈을 신뢰하지 않는다. sibling helper나 애플리케이션 코드를 import하지 않으므로
서버 helper의 기존 root 소유·hash trust를 유지한다. Docker/CA/Python은 신뢰하는 OS 설치여야 한다.

Owner가 별도 승인된 설치 절차에서 이 **정확한 파일**을
`/usr/local/lib/next-stop-siren/verify-pilot-readiness`에 root 소유·0755로 설치하고 SHA-256을
`deploy-gates.json`의 `functional_check_sha256`에 연결해야 한다. 이 작업에서 시스템 경로에
설치하거나 gates를 승인하지 않았다. 기존 server deploy는 파일 소유·쓰기 권한·실행 가능 여부와
hash를 확인한 뒤 이를 실행한다. helper를 바꾸면 Owner가 새 hash와 운영 evidence를 다시 검토한다.

예시 설치 명령은 **실행하지 않은 수동 안내**다:

```sh
sudo install -o root -g root -m 0755 deploy/actions/verify-pilot-readiness \
  /usr/local/lib/next-stop-siren/verify-pilot-readiness
sha256sum /usr/local/lib/next-stop-siren/verify-pilot-readiness
```

## 읽기 전용 확인 범위

1. Docker의 정확한 Compose project `next-stop-siren`, service `nginx`/`backend`,
   `com.docker.compose.oneoff=False`로 컨테이너를 고른다. oneoff 값은 기존 실제 Compose
   inspect 관측과 일치한다. 각 service에 정확히 한 개가 있고 실행 중이어야 한다.
   정지·중복·다른 project/service/oneoff 컨테이너를 성공으로 취급하지 않는다.
   선택된 nginx의 effective `NetworkSettings.Ports["443/tcp"]`만 추가 조회하여 host443이
   `0.0.0.0` 또는 `127.0.0.1`에 실제 publish됐는지 확인한다. 없는 mapping, 다른 host port,
   외부 IP만/IPv6만의 mapping은 IPv4 loopback443의 해당 릴리스로 인정하지 않는다.
2. 전달된 불변 digest의 로컬 image ID와 실제 container image ID가 일치하고,
   두 image의 `org.opencontainers.image.revision`이 `--source`와 같아야 한다.
   이 label은 게시 코드가 설정하므로 없는/다른 label도 실패다. pull/tag/exec/restart나
   Config.Env 전체 조회는 하지 않는다. 검사가 끝난 뒤 같은 ID와 HTTPS binding을 다시 확인해 교체를 거부한다.
3. HTTPS 연결은 DNS/프록시 대신 **숫자 `127.0.0.1:443`**에만 연결한다. SNI·Host와 인증서 검증은
   nginx의 고정 hostname `next-stop-siren.duckdns.org`를 사용한다. CA bundle을 명시하며
   SSL_CERT_FILE/DIR·HTTP(S)/ALL_PROXY·DOCKER_HOST 환경값으로 목적지나 신뢰를 바꾸지 않는다.
   TLS 검증을 끄거나 redirect를 따라가지 않는다. 임의 URL/port/CA/Docker 경로 CLI 옵션은 없다.
4. `GET /api/ready`는 JSON `{"status":"ready"}`와 200이어야 한다. 현재 backend 계약상 이는
   DB read-only SELECT/제품 table·column, 인증 설정, worker poll readiness를 포함한다.
   중복 JSON 키, 잘못된 200, HTML SPA fallback은 거부한다. 503의 상세 body나 원문은 출력하지 않는다.
5. 토큰·쿠키·POST 없이 `GET /api/me`가 401/Bearer와 안정된 `unauthenticated` 오류 shape를
   반환해야 한다. 이것은 **guest 접근 경계 검사**이며 실제 로그인 성공·세션·권한 분리 검증이 아니다.
6. `/`의 한국어 app root와 하나의 동일 origin `/assets/index-<hash>.js` module을 확인한다.
   asset은 JS MIME의 비어 있지 않은 non-HTML 응답이어야 한다. 외부/상대 경로·query·traversal·
   redirect·SPA asset fallback을 거부한다. minified 함수 이름이나 번들 크기에 성공을 결합하지 않는다.
   브라우저에서 JS를 실행하거나 chat/AI/user-data를 요청하지 않는다.

검사는 GET과 Docker ps/선택한 inspect projection만 사용한다. DB/auth/chat 쓰기, 로그인,
쿠키 저장·전송, schema 변경, 이미지 pull/push, 서비스 재시작이나 release/approval state 쓰기는 없다.

## 시간·출력·실패

전체 60초 hard deadline과 작업별 최대 3초를 둔다. 시작 대기에는 transient HTTPS/ready 502·503·504에
1·2·최대 3초 backoff를 사용하고, 마지막 여섯 Docker identity 조회를 위한 시간을 남긴다.
초기 조회가 빠르면 약 40초의 startup grace를 쓸 수 있다. 잘못된 이미지·source·TLS·응답 계약은
우회하지 않는다. 느린 header/body도 전체/phase timer로 중단한다. Docker 출력 16KiB,
JSON/HTML body 128KiB, JS body 2MB를 넘으면 거부한다. 하위 query는 spawn/handle 인계부터
SIGALRM을 자기 cleanup 완료까지 미루되 select/wait의 남은 deadline은 유지한다.
멈추거나 deadline이 오면 자기 process group만 종료하고, pending alarm은 그 후 전달한다.
cleanup은 만료된 check 예산과 별개로 최대 1초 reaping을 시도하고 pipe를 닫는다.
자식이 poll 직후 종료하는 race를 처리하고, cleanup 오류로 원래 실패를 덮거나 성공시키지 않는다.

exit 0의 `PILOT_READINESS_OK`만 성공이다. 실패는 exit 1과 고정 `PILOT_READINESS_FAILED: CODE`를 출력한다.
원문 argv/환경값/URL/응답 body/Docker stderr/예외 문자열은 출력하지 않는다.
`ARGUMENTS`는 source/digest 인자, `CONTAINER_*`/`IMAGE_IDENTITY`는 실제 선택/이미지/revision,
`HTTPS_LISTENER_IDENTITY`는 선택한 nginx의 IPv4 host443 연결, `TLS_IDENTITY`는 hostname/인증서, `READINESS_CONTRACT`/`JSON_CONTRACT`는 ready JSON,
`GUEST_AUTH_CONTRACT`는 guest 경계, `FRONTEND_*`는 app/asset, `DOCKER_*`/`HTTPS_TRANSPORT`/
`DEADLINE`/`STARTUP_DEADLINE`은 로컬 daemon·전송·시작 예산을 확인한다.
`LOCAL_OR_CONTRACT_FAILURE`는 분류하지 못한 로컬/계약 실패다. 로그를 그대로 공개하거나 보호를 끄지 않는다.

기존 deploy의 inflight 중단·수동 reconciliation/rollback 정책을 유지한다. readiness 실패가
자동 배포 재시도·DB 복원·운영 gate 우회를 허용하지 않는다.

## 로컬 검증과 한계

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/actions/tests -v`는 기존
오프라인 검사와 합성 TLS/CLI 검사를 실행한다. OpenSSL은 이미 설치된 로컬 도구를 사용하며
테스트 인증서·키는 fixture이고 실제 인증서/비밀값이 아니다. 외부 네트워크나 live stack에 접속하지 않는다.
새 TLS fixture는 real loopback HTTPS와 subprocess main을 사용하되 host에서는 CA/port/Docker 경로와
시간 상수만 in-memory test wrapper로 바꾼다. 이 wrapper를 실제 설치 파일 실행과 혼동하지 않는다.
별도 승인된 cached-container 검사는 network none에서 **정확한 실행 파일/shebang과 원래 경로·시간 상수**를
사용하며 loopback443·합성 CA·합성 Docker CLI를 제공한다. helper에는 test override 옵션이 없다.
Docker 상태와 backend/API body는 합성이다. 실제 EC2·DNS/TLS·앱·DB·worker 전체가 검증됐다는 뜻이 아니다.

실제 운영 evidence는 Owner가 #75의 초기화/복원 증거와 gates/hash를 준비하고, 별도 승인된 첫 배포 뒤
실제 HTTPS 로그인·채팅, #74 사용자별 제한, #76 부하·알림·복구를 확인해야 한다.
