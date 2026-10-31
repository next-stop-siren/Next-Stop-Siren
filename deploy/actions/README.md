# #73 phase 2 — manual Actions preparation (B71-OPS-050)

로컬 준비 파일이며 push·workflow dispatch·GHCR publication·OIDC/SSM·AWS/EC2·실제 배포를
실행하지 않았다. phase 1 B71-OBS-028과 별도 작업이다. 이 workflow는 기본 브랜치에 존재해야
GitHub에서 사용할 수 있으므로, 독립 검토/로컬 커밋 이후 **별도 승인된 main 병합**이 먼저다.
현재 브랜치를 병합하거나 원격에 올리는 단계는 이 범위 밖이다.

## 고정 경계

두 workflow 모두 workflow_dispatch만 허용한다. 저장소·refs/heads/main·원래 actor와
rerun triggering_actor가 모두 Owner xifoxy-ru인지 중복 확인한다. GitHub 환경
`pilot-publication`과 `pilot`을 Owner가 custom branch policy **main 한 개**로 준비해야 한다.
워크플로/코드 ref guard와 환경 정책은 둘 다 필요하다. 환경 조회/branch-policy 조회는
공식 REST 문서상 Actions:read이며 실제 GITHUB_TOKEN 권한/환경 설정은 아직 미검증이다.
API 403/404/알 수 없는 정책은 우회하지 않고 중단한다. PR/fork/merge 자동 게시·배포는 없다.

- 게시 job만 contents:read/actions:read/packages:write. OIDC 권한 없음.
- 배포 job만 contents:read/actions:read/id-token:write. packages:write 없음.
- 배포 동시성1, cancel-in-progress:false. 원격 host lock과 미해결 inflight marker도 별도로 유지한다.
- 기존 CI는 변경하지 않는다. 게시 입력 source_sha가 workflow의 GITHUB_SHA, 현재 main tip 및 checkout HEAD와 같고,
  정확한 CI workflow_id/path, push event, main branch, 원본 repository/id, 같은 head SHA의
  completed/success 결과가 있어야 게시한다. 단순 check 이름으로 대신하지 않는다.

## Owner가 준비할 입력 — 추측 기본값 없음

Repository/environment variables:

| 변수 | 준비 내용 |
| --- | --- |
| PILOT_FRONTEND_REPOSITORY / PILOT_BACKEND_REPOSITORY | 서로 다른 ghcr.io/next-stop-siren/패키지 경로 |
| PILOT_AWS_ACCOUNT_ID / PILOT_AWS_ROLE_ARN / PILOT_AWS_REGION | 실제 계정·OIDC role·명시적 ap-northeast-2 |
| PILOT_INSTANCE_ID | 실제 단일 EC2 ID |
| PILOT_SSM_DOCUMENT_NAME / VERSION / SHA256 | 사용자 관리 Command document, 숫자 version, DescribeDocument Hash/Sha256 |
| PILOT_GATES_SHA256 | 실제 server deploy-gates.json 전체 bytes SHA256 |

공개 GHCR + GITHUB_TOKEN 게시 선택을 유지한다. 첫 package 조회가 **확정404**면 첫 push를
허용하되 생성 직후 private일 수 있다. **두 package 모두 public인지 확인하기 전에는 release
manifest/artifact를 발급하지 않는다.** private이면 Owner에게 package Settings에서 visibility를
Public으로 바꾸도록 안내하고 실패한다. UI 조정 후 현재 main SHA/CI 조건에 맞춰 다시 수동
게시한다. 빈 package를 UI에서 미리 만들 수 있다고 가정하지 않는다. 403은 404로 취급하지 않는다.

AWS OIDC provider/role이 존재한다고 가정하지 않는다. oidc-trust.example.json의 `sub`는
반드시 실제 repository+pilot environment에 적용되는 정확한 관측 subject로 교체한다.
2026-07-15 이후 생성 또는 opt-in 저장소는 immutable owner/repository ID 형식일 수 있고,
environment subject는 ref subject와 다르다. legacy repo:...:ref 형식을 추측하지 않는다.
현재는 token을 획득하지 않았다. Owner가 나중에 공식 GitHub OIDC 설정/claim 형식을 확인하고
실제 승인된 관측값을 안전하게 검증해야 한다. main 제한은 환경 보호와 코드에서도 강제한다.

배포 role은 정확한 document ARN + EC2 ARN에만 SendCommand, 그 document에 Get/DescribeDocument,
GetCommandInvocation 읽기만 가진다. 마지막 API는 resource-level 제한을 지원하지 않아
Resource:*와 region 조건을 사용한다. SendCommand에 wildcard 또는 generic AWS-RunShellScript를
주지 않는다. IAM은 document version을 pin하지 않으므로 코드의 numeric version+HashType/Hash+
실제 GetDocument content 일치가 함께 필요하다. 실제 정책 적용 가능성은 Owner가 별도 검증한다.

## artifact와 서버 전달 — S3/새 스택 없음

게시 workflow는 동일 source의 linux/amd64 두 이미지를 만들고 digest로 기록한다. OCI revision/source
label도 같은 SHA다. public release artifact에는 closed release.json, 정확한 Compose, host helper,
contract, controlled SSM document만 있다. 환경 파일·DB·백업·비밀값은 artifact/image에 없다.
upload-artifact v4의 overwrite:false, run/attempt별 이름을 사용한다.

배포 실행 코드는 workflow의 github.sha로 checkout하고 prepare가 실제 git HEAD=GITHUB_SHA를
artifact 다운로드/OIDC 전에 대조한다. 이전 release를 선택할 수 있으므로 release source와
배포 workflow SHA를 같게 강제하지 않는다.

배포는 성공한 manual publication run을 선택한다. 원본 workflow_id/path/event/main/repository/
actor+rerun·current-main ancestry를 확인하고 유일한 해당 artifact ID를 고른다. GitHub가 제공한
archive SHA256과 실제 zip bytes를 비교한 다음 고정 경로·파일 hash·source/run/attempt/ID를
대조한다. signed download redirect에는 GitHub token을 전달하지 않고 URL/본문을 출력하지 않는다.

검증된 **작은 공개 manifest+gates hash만 Base64 SSM parameter**로 보낸다. shell command나
앱 secret을 전달하지 않는다. ENV_VAR parameter interpolation을 지원하는 SSM Agent가 필요하다.
실제 instance role/SSM Agent 및 AWS endpoint/GHCR/raw.githubusercontent.com outbound는 Owner가 준비한다.
공개 SSH22/self-hosted runner/NAT gateway/유료 private endpoint/장기 AWS key/SSH private key는 없다.

Owner가 사전 검토해 root 소유·비쓰기 권한으로 server_deploy.py와 contract.py를
`/usr/local/lib/next-stop-siren/`에 설치하고, 정확한 document version/content/hash를 준비한다.
서버 helper 자신의 hash와 contract hash가 release와 다르면 실행하지 않는다. Compose만 공개
GitHub의 exact SHA URL에서 받아 manifest hash와 대조하므로 rebuild나 token/zip 전달이 없다.
새 source에서 helper가 바뀌면 Owner의 별도 설치·검증이 필요하며 자동 self-update하지 않는다.

## 호스트 순서와 외부 운영 게이트

실제 서버에 다음이 없거나 불일치하면 active 서비스 변경 전에 실패한다:

- `/etc/next-stop-siren/production.env`: Owner PC 암호화 vault에서 필요한 값만 주입한 제한 파일.
- `/etc/next-stop-siren/deploy-gates.json`: source에 맞는 #72 readiness/#74 trusted proxy/#75 data recovery의
  Owner 승인과 실제 evidence/hash. 예시는 NOT_APPROVED여서 통과하지 않는다.
- `/usr/local/lib/next-stop-siren/verify-pilot-readiness`: root-owned executable과 승인된 정확한 hash.
  입력 --source/--frontend/--backend에 대해 **실제 auth·schema·worker·기능·이미지 identity**를 검증해야 한다.
  SELECT1/프로세스 응답만 반환하는 stub은 승인할 수 없다. 이 단계는 그 기능을 구현하지 않는다.
- 검토한 Certbot digest, 실제 DNS/TLS와 영속 데이터/복구 준비. 이 phase에서 준비됐다고 선언하지 않는다.

호스트는 flock을 전체 작업 중 유지하고 자식 Docker 명령도 FD를 상속한다. Actions가 poll을
중단해도 실행 중 remote process의 lock을 지우지 않는다. config/pull/backend --validate-only 뒤
inflight marker를 기록하고 DB는 --no-recreate, backend, **nginx --force-recreate** 순서로 적용한다.
nginx를 다시 만들어 backend 교체 후 이전 IP를 계속 쓰는 일을 막는다. 실제 container/image ID와
Owner 기능검사가 일치해야 current-release를 기록한다. 같은 marker라도 실제 image ID+기능검사를
다시 통과해야 idempotent 성공이다. active 변경 중 실패·timeout·강제종료면 inflight를 남겨 후속
실행을 차단한다. Owner가 실제 상태를 확인/조정한 뒤 수동 이전 release를 선택해야 하며,
앱 rollback·DB 호환/복원은 #75와 조율한다. 자동 rollback/DB restore는 하지 않는다.

호스트 Docker/기능 명령 예산900초/명령당최대300초이며, Compose fetch30초와 유한한 image ID 조회
(각15초)도 별도로 bounded다. 전체 host 강제종료 경계는 SSM document1200초이며,
runner poll1200초/job25분이다.
SSM send는 AWS CLI retry도1로 제한해 한 번만 시도한다. 응답 유실/실패는 uncertain으로 보고 **재전송하지
않는다**. InvocationDoesNotExist는 eventual consistency로 bounded retry, Pending/InProgress/Delayed는
poll, failure/timeout/nonzero는 실패다. poll timeout에서 remote가 계속 실행될 수 있어 Owner가 SSM
기록/host inflight를 조정해야 한다. 전체 command stdout/stderr와 앱 secret은 Actions에 내보내지 않는다.

안전한 실패 코드는 원문 API 응답·환경값·stdout을 출력하지 않는다. `AWS_ACCOUNT_REQUIRED` 등
configuration 코드는 Owner 변수 준비, `SAME_SHA_TRUSTED_CI_SUCCESS_REQUIRED`는 같은 main-push CI,
`ENVIRONMENT_*`는 main 전용 환경 정책, `GITHUB_API_ACCESS_OR_INPUT`는 권한/입력 확인을 뜻한다.
`PROVENANCE_OR_CONTRACT_REJECTED`는 artifact/source/hash 계약을 재검토한다.
`PINNED_DOCUMENT_*`는 실제 document version/content/hash 확인, `REMOTE_FAILED`/`REMOTE_NONZERO`는
원격 실패 확인이다. `SSM_COMMAND_ACCEPTED`의 검증된 command ID만 기록하며 raw 응답은 숨긴다.
`DISPATCH_UNKNOWN_DO_NOT_RESEND`는 응답 유실이므로 자동 재전송 금지,
`TIMEOUT_RECONCILE_COMMAND`는 마지막 command ID/SSM 기록/host 상태를 Owner가 확인한다.
원인 불명 `LOCAL_OR_REMOTE_OPERATION_FAILED`도 재시도 전에 해당 단계의 설정·상태를 확인한다.

action pin은40hex 모양뿐 아니라 공식 git/commits의 성공 응답으로 확인한다. annotated tag는
공식 git/tags chain을 실제 commit까지 peel하며 tag object나404를 commit 증거로 쓰지 않는다.
오프라인 준비 검증은 저장된 ref/tag/commit 응답 및 명령 exit와 workflow pin을 대조하며,
workflow 실행 시 ref를 다시 resolve하거나 새 런타임 의존성을 추가하지 않는다.

## SSM ReleaseManifest 제약

`ReleaseManifest`는 `allowedPattern: ^[A-Za-z0-9+/=]+$`로 비어 있지 않은 base64 문자만
허용하고 숫자 `maxChars: 4096`으로 길이를 제한한다. `{1,4096}`처럼 큰 반복 수를 패턴에
넣지 않는다. [Go/RE2 반복 구문](https://pkg.go.dev/regexp/syntax)은 bounded repeat의
최소·최대 수가 1000을 넘으면 거부하므로 Python `re` 컴파일 성공만으로 SSM 호환성을 판단할 수 없다.
오프라인 테스트는 이 제한에 대한 정적 회귀 검사와 값 경계 검사를 분리한다. 실제 SSM 엔진 호출은 아니다.

Owner는 이전 패턴의 생성 실패와 수정된 문서 `NextStopSiren-PilotDeploy` version `1` 생성 성공,
`HashType: Sha256`, hash `6c4c103fd32436fb95495fe5c97c2579256821d1a7c9da21adb0dcef0d962f97`을
보고했다. 이는 사용자 제공 증거이며 이 변경에서 AWS에 직접 조회하지 않았다. 배포는 설정한
AWS 보고 hash를 DescribeDocument와 대조하고, GetDocument JSON 내용은 로컬 소스와 의미적으로
비교한다. 공백·키 순서는 무관하며 내용 변경은 거부한다. 로컬 JSON 파일의 raw-byte hash를
AWS 보고 hash와 동일하다고 가정하지 않는다. 이 소스 제약 변경에는 host helper 재설치가 필요 없다.

## 로컬 검증과 미실행

`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s deploy/actions/tests -v`
는 GitHub/AWS/docker를 fake로 대체한다. 실제 CLI publish/prepare/dispatch/server --execute는
이 작업에서 호출하지 않는다. YAML은 기존 Ruby/Psych로 parse하며 actionlint는 설치된 경우만 쓴다.
새 의존성을 설치하거나 act/실제 credentials로 테스트하지 않는다.

공식 근거: [GitHub environments](https://docs.github.com/en/rest/deployments/environments),
[branch policies](https://docs.github.com/en/rest/deployments/branch-policies),
[AWS DescribeDocument](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_DescribeDocument.html),
[GetDocument](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetDocument.html),
[AWS DLM role resource examples](https://docs.aws.amazon.com/ebs/latest/userguide/service-role.html),
[GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

실제 publication/push/OIDC/SSM/EC2/Google/DNS/ACME/AI는 미실행. #72/#74/#75/#76과 phase1의 mockAI,
shutdown pool wait 한계는 그대로이며 배포/production readiness나 #73 전체 완료를 주장하지 않는다.
