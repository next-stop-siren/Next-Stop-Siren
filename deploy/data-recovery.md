# 파일럿 데이터 보존 배포·복구 절차 — #75

기준: [DB 설계](../docs/04-reference/database.md) Rev 4, [회원 채팅 정책](../docs/04-reference/chat-policy.md) Rev 3,
[#75 백업 결정](README.md#75-백업-결정과-인계). 도구는 [`db-recovery.py`](db-recovery.py),
격리 리허설은 [`db-recovery-rehearsal.py`](db-recovery-rehearsal.py), 오프라인 검사는 [`check.py`](check.py)의
`DataRecoveryChecks`다.

이 문서는 절차와 격리 리허설 결과다. **실제 서버 DB 작업, 서버 밖 복사, 배포를 실행하거나 승인하지 않는다.**
실제 실행은 Owner `xifoxy-ru`가 별도로 승인한 작업에서만 한다. 외부 유료 저장소는 쓰지 않는다.

## 책임

| 범위 | 담당 |
| --- | --- |
| 현재 사전 점검·백업·검증·스키마 적용·되돌림·복원 절차와 도구 관리 | Owner `xifoxy-ru` |
| 도구 설치, 실제 백업 실행·일일 timer, Owner Mac 암호화 금고 복사·검증, 로그 보존 실행 조건, 실제 작업 승인 | Owner `xifoxy-ru` |
| 절차·복원 결과의 독립 운영 검토 | 해당 변경을 구현하지 않은 검증자. 과거 검토자·결과는 날짜별 기록으로 보존 |

## 원칙

- `create_all()`은 **빈 테이블만 만들고 기존 테이블을 바꾸지 않는다.** 기존 데이터가 있는 DB에 삭제·재초기화를
  적용하지 않는다. 앱 시작은 DDL을 실행하지 않는다.
- **DB 볼륨은 백업이 아니다.** 백업은 `pg_dump` custom archive와 SHA-256 manifest 쌍이며, 서버 밖 사본과 복원
  검증을 마쳐야 쓸 수 있는 백업으로 센다.
- 도구는 `docker exec`로 DB 컨테이너 안에서만 `psql`·`pg_dump`·`pg_restore`를 실행한다. 접속 정보는 컨테이너
  환경변수에 남고 명령 인수·출력에 나오지 않는다.
- 보고서는 표별 행 수·행 내용 SHA-256·상태별 개수·제약·인덱스 이름만 담는다. 대화 원문·토큰·설정값을 담지 않는다.
  실패 명령의 서버 메시지는 키 값을 인용할 수 있어 작업 디렉터리의 `diagnostics-*.txt`(0600)에만 남기고 공유하지 않는다.
- 운영 DB를 지우는 명령은 없다. 복원은 새 DB에 먼저 풀어 확인한 뒤 이름을 바꾸고, 교체된 DB는 Owner가 지울 때까지 남긴다.

## 1. 사전 점검과 경로 판정

`precheck`는 읽기 전용 트랜잭션에서 상태를 읽고 분류한다. 결과 파일은 이후 비교 기준이다.

| 분류 | 뜻 | 다음 단계 |
| --- | --- | --- |
| `empty_use_db_init` | 제품 테이블이 없다. 첫 파일럿 배포가 여기에 해당한다. | 빈 DB 초기화(아래) |
| `needs_reservation_fk` | 10개 테이블이 있지만 #53의 `chat_jobs_reservation_fk`가 없다(#52 시점 DB). | [4. 스키마 적용](#4-기존-스키마-적용--chat_jobs_reservation_fk) |
| `current` | 현재 모델의 테이블과 FK가 있다. | 적용할 것 없음 |
| `partial_stop` | 일부 테이블만 있다. | **중단**하고 Owner가 원인을 확인한다. `create_all()`로 메우지 않는다. |

빈 DB 초기화는 Owner가 승인한 첫 배포에서 한 번만, 격리 smoke와 같은 명시 명령으로 한다. 앱 시작이나 배포
스크립트가 자동으로 하지 않는다.

```sh
docker compose --project-name next-stop-siren --env-file /etc/next-stop-siren/production.env \
  --env-file "$REL/release.env" --file "$REL/compose.yaml" run --rm --no-deps backend python -c \
  "import os; from app.db_init import initialize_schema; initialize_schema(os.environ['DATABASE_URL'])"
```

초기화 뒤 `precheck`가 `current`여야 한다. 그 뒤로는 이 명령을 다시 쓰지 않는다.

## 2. 명령 형식

Owner가 검토된 commit의 `deploy/db-recovery.py`를 서버의 root 소유 경로(예: `/usr/local/lib/next-stop-siren/`)에
설치하고 SHA-256을 기록한다. `$REL`은 현재 릴리스 디렉터리(`/opt/next-stop-siren/releases/<source>-<id>`)다.
작업 디렉터리는 저장소 밖의 root 전용 경로이며 도구가 0700으로 만든다.

```sh
DBR="python3 /usr/local/lib/next-stop-siren/db-recovery.py --compose-project next-stop-siren \
  --env-file /etc/next-stop-siren/production.env --env-file $REL/release.env --compose-file $REL/compose.yaml \
  --work-dir /var/backups/next-stop-siren"
$DBR precheck
$DBR backup --purpose daily            # 또는 pre-change
$DBR verify <manifest> [--restore-check]
$DBR apply-reservation-fk <pre-change manifest>
$DBR revert-reservation-fk <pre-change manifest>
$DBR restore <manifest> [--verify-report <verify report>]
```

성공하면 한 줄 JSON(분류·결과·보고서 경로)을, 실패하면 `DB_RECOVERY_FAILED: <코드>`만 출력하고 종료 코드 1을 낸다.
Owner Mac이나 리허설에서는 `--compose-project` 대신 일회용 컨테이너 `--db-container <이름>`을 쓴다.

## 3. 백업

| 종류 | 시점 | 조건 |
| --- | --- | --- |
| `daily` | 하루 1회(파일럿 결정) | backend 실행 중 가능. 덤프 전후 스냅샷이 같으면 `consistent: true`. 다르면 기대 상태가 없으므로 `verify --restore-check`의 결과를 기대 상태로 쓴다. |
| `pre-change` | 스키마 변경·기존 데이터 대량 수정 직전 | backend가 멈춰 있어야 하고 전후 스냅샷이 같아야 한다. 아니면 `stop_backend_first`·`changed_during_backup`. |

일상적인 질문·답변 저장은 변경 전 백업 대상이 아니다. 작업 디렉터리에는 `backup-<UTC>-<종류>.dump`와
`.manifest.json`(크기·SHA-256·생성 시각·백업 당시 DB 크기·스냅샷)이 생긴다. 남은 공간이 DB 크기의 2배 + 64 MiB보다
작으면 시작하지 않는다.

**검증(`verify`)**: manifest와 같은 디렉터리의 archive 크기·SHA-256, `pg_restore --list`의 표 목록을 확인한다.
`--restore-check`는 같은 PostgreSQL 안의 별도 DB `nss_restore_check`에 풀어 스냅샷을 비교한 뒤 그 DB를 지운다.
시작 전 DB 볼륨 여유가 [복원 공간 기준](#복원-공간-기준)보다 작으면 실행하지 않는다.
운영 DB에는 쓰지 않지만 디스크를 일시 사용하므로, 서버에서는 변경 전 백업에만 쓰고 일일 백업은 Owner Mac에서
일회용 컨테이너로 확인하는 것을 권장한다.

**서버 밖 사본(Owner)**: `.dump`와 `.manifest.json`을 Owner Mac의 암호화 금고로 복사한다. 복사 경로는 공개 SSH 22를
열지 않는 Owner 승인 방식으로 정한다. 금고 쪽에서 `shasum -a 256` 값이 manifest와 같은지 확인하고, 일회용
`postgres:17.11` 컨테이너에 `verify --restore-check`를 실행해 복원 가능성을 확인한 뒤 결과를 남긴다.
백업 파일은 대화 원문을 포함한 운영 데이터다. 저장소·이슈·PR·로그 수집에 넣지 않는다.

**보존**: 금고 사본은 파일럿 종료 후 7일까지 보존한다(결정). 서버에는 디스크 보호를 위해 최근 일일 백업 3개와
진행 중인 변경의 변경 전 백업만 두고, 금고 복사·검증이 끝난 오래된 파일을 Owner가 지우는 것을 제안한다.
도구는 백업을 자동으로 지우지 않는다.

## 4. 기존 스키마 적용 — `chat_jobs_reservation_fk`

`needs_reservation_fk` DB(#52 때 `chat_jobs`를 만든 개발·테스트·공유 DB)에만 해당한다. 첫 파일럿 배포는 빈 DB라 해당 없다.

1. `precheck`: 분류 `needs_reservation_fk`, `orphan_job_reservations: 0`, 실행 중 작업 수를 확인한다.
2. backend를 멈춘다(`docker compose ... stop backend`). Nginx는 남아 API에 502를 낸다. 대기 작업은 DB에 남아 재시작 뒤 이어서 실행된다.
3. `backup --purpose pre-change`
4. `verify <manifest> --restore-check`
5. `apply-reservation-fk <manifest>`
6. backend를 시작하고 `/api/ready` 200과 기능 확인(`verify-pilot-readiness`)을 한다.
7. `precheck`가 `current`인지 확인하고 보고서들을 증거로 남긴다.

도구가 강제하는 조건: backend 정지, 1시간 이내의 `pre-change` 백업, 그 백업의 스냅샷이 현재와 같음(백업 뒤 변경 없음),
고아 예약 0개. FK는 `lock_timeout 5s` 트랜잭션 하나에서 추가하며, PostgreSQL이 기존 행을 검사해 하나라도 어긋나면
전체를 되돌린다. 적용 뒤 표별 행 수·행 해시·상태별 개수·인덱스가 그대로이고 제약은 FK 하나만 늘었는지 비교한다.
이미 적용됐으면 아무것도 바꾸지 않고 `already_applied`로 끝난다.

## 5. 실패 시 판단

| 출력 코드 | 뜻 | 조치 |
| --- | --- | --- |
| `stop_backend_first` | backend 실행 중 | backend를 멈추고 다시 시작한다. |
| `fresh_pre_change_backup_required`, `backup_does_not_match_current` | 백업이 오래됐거나, `daily`이거나, 백업 뒤 데이터가 바뀌었다. | 새 `pre-change` 백업부터 다시 한다. |
| `orphan_reservations` | 없는 예약을 가리키는 작업이 있다. FK를 추가할 수 없다. | **중단.** 데이터를 임의로 고치거나 지우지 않는다. 행 ID는 진단 파일로만 확인하고 정정 방법은 Owner가 별도 결정한다. 앱은 FK 없이 그대로 동작한다. |
| `apply_failed` | 트랜잭션 실패(잠금 5초 초과 포함). 변경은 반영되지 않았다. | `precheck`로 FK 없음·데이터 동일을 확인하고 backend를 다시 시작한다. 원인 확인 후 재시도한다. |
| `unexpected_change_after_apply` | 적용 뒤 FK 외 차이 | backend를 멈춘 채 두고 보고서의 차이 이름을 확인한다. 데이터 손상이면 [7. 복원](#7-복원)을 결정한다. |
| `backup_sha256_mismatch`, `backup_size_mismatch`, `manifest_invalid` | archive가 바뀌었거나 짝이 맞지 않는다. | 그 백업을 쓰지 않는다. 금고 사본과 대조한다. |
| `insufficient_*` | 디스크 부족 | 오래된 서버 백업 정리 등 공간을 확보한다. 볼륨을 지우지 않는다. |
| `restore` 중 `restored_snapshot_mismatch`, `schema_read_failed`, `snapshot_failed` | 교체 전 staging 비교 실패일 수도, 교체 뒤 재비교 실패일 수도 있다. | backend를 멈춘 채 [교체 후 검증 실패 대응](#교체-후-검증-실패-대응) 1단계로 교체 여부부터 확인한다. |

## 6. 앱 되돌림과 스키마 호환

되돌림은 **앱 → 스키마 → 데이터** 순으로, 데이터 손실이 없는 단계에서 멈춘다.

1. **앱 되돌림**: Owner가 이전 릴리스(앱 이미지 digest 쌍 + Compose)를 수동으로 다시 배포한다. DB는 건드리지 않는다.
   되돌릴 릴리스가 요구하는 테이블·컬럼이 현재 스키마에 있어야 한다. 이는 그 릴리스의 `/api/ready` `schema=ok`로 확인한다.
   현재 파일럿 후보 릴리스는 모두 같은 10개 모델이고 FK 추가는 추가형 변경이라, FK가 있든 없든 열 요구가 충족된다(리허설 확인).
   #53 이전처럼 실제 예약 행 없이 `reservation_id`를 쓰던 앱은 파일럿 대상이 아니며, 되돌린다면 FK를 먼저 되돌려야 한다.
2. **스키마 되돌림(데이터 보존)**: `revert-reservation-fk`가 FK 제약 하나만 지운다. 되돌림도 스키마 변경이라 적용과
   같은 조건(backend 정지, 1시간 이내이며 **현재 상태와 같은** 변경 전 백업)을 요구한다. FK 적용 전에 만든 백업은 그 뒤
   쌓인 데이터·FK 때문에 현재와 달라 `backup_does_not_match_current`로 거부되므로, 되돌릴 때 새 백업을 만든다.
   1. `precheck`로 분류가 `current`인지 확인한다.
   2. backend를 멈춘다(`docker compose ... stop backend`).
   3. `backup --purpose pre-change`로 지금 상태의 새 백업을 만든다.
   4. `verify <새 manifest> --restore-check`
   5. `revert-reservation-fk <새 manifest>`: 표별 행 수·행 해시·상태별 개수·인덱스가 그대로이고 제약은 FK 하나만 줄었는지 비교한다.
   6. 되돌린 앱 릴리스로 backend를 시작하고 `/api/ready` 200과 기능 확인을 한다.
   7. `precheck`가 `needs_reservation_fk`인지 확인하고 보고서들을 증거로 남긴다.

   이전 문서의 `DROP TABLE` 세 개 되돌림은 대기열·실행권·정지 기록을 지우므로 **쓰지 않는다**.
3. **데이터 복원**: 데이터가 손상·유실됐을 때만 마지막 백업 이후 변경 손실(최대 24시간)을 받아들이고 복원한다.

향후 새 컬럼·테이블 변경은 같은 형식(사전 점검 → 변경 전 백업 → 검증 → 단일 트랜잭션 적용 → 비교 → 데이터 보존 되돌림)으로
도구에 명시 단계를 추가해 검토한다. 이번 작업은 마이그레이션 도구를 도입하지 않았다.

## 7. 복원

1. backend를 멈춘다. 복원할 manifest를 고른다: 손상 직전의 가장 최근 **검증된** 백업, 서버 사본이 없거나 의심되면 금고 사본.
   복원 직전에 `precheck`를 한 번 실행해 그 보고서를 남긴다. 교체 후 검증이 실패했을 때 이전 DB를 대조하는 기준이다.
2. 일일 백업처럼 manifest 스냅샷이 없으면 그 백업의 `verify --restore-check` 보고서를 `--verify-report`로 함께 준다.
3. `restore <manifest>`: 크기·SHA-256 확인 → [복원 공간](#복원-공간-기준) 확인 → 새 DB `nss_restore_staging`에 단일 트랜잭션 복원 → 기대 스냅샷 비교 →
   한 트랜잭션에서 운영 DB 이름을 `nss_replaced_<UTC>`로, staging을 운영 이름으로 바꾼다 → 다시 비교한다.
   **이름 교체 전**(공간 확인부터 staging 비교, 이름 교체 트랜잭션까지)에 실패하면 staging을 지우고 운영 DB는 그대로 둔다.
   **이름 교체 뒤** 재비교에서 실패하면 운영 이름은 이미 복원한 DB를 가리킨다. 이때는 [교체 후 검증 실패 대응](#교체-후-검증-실패-대응)을 따른다.
4. backend를 시작하고 `/api/ready`·기능 확인을 한다. identity sequence도 함께 복원되어 새 행 ID가 겹치지 않는다.
   backend가 시작되는 순간부터 사용자 요청과 worker(복원된 대기 작업의 실행·만료)가 복원한 DB에 새로 쓸 수 있다.
   이 확인이 실패하면 [교체 후 검증 실패 대응](#교체-후-검증-실패-대응)을 따르며, 그 절의 4단계(새 쓰기 확인)를 건너뛰지 않는다.
5. 보고서(`restore-*.json`: 이전·이후 스냅샷, 소요 시간, 보존 DB 이름)를 증거로 남긴다.
6. **교체 전 DB는 복원 검증이 끝날 때까지 보존한다.** 복원 보고서 비교, `/api/ready`, 기능 확인이 모두 끝난 뒤에만
   Owner가 디스크 여유를 확인하고 `DROP DATABASE nss_replaced_<UTC>;`로 지운다. 그 전까지 DB 디스크를 두 배 쓰므로,
   보존 중에는 `df`로 볼륨 여유를 확인하고 다른 복원·복원 검사를 시작하지 않는다.

### 교체 후 검증 실패 대응

이름 교체 **뒤에** 실패하면 운영 이름은 이미 복원한 DB를 가리키고, 원래 DB는 `nss_replaced_<UTC>`로 남아 있다.
해당하는 경우는 두 가지다.

- 도구가 교체 뒤 재비교에서 실패했다(`restored_snapshot_mismatch`, `schema_read_failed`, `snapshot_failed`).
- 도구는 성공했지만 backend 시작 뒤 `/api/ready`나 기능 확인이 실패했다.

이 세 코드는 교체 전 staging 비교에서도 같은 코드로 나온다. 교체 뒤 상태 읽기는 표 목록부터 읽으므로, 복원한 DB에 접속하지
못하면 `snapshot_failed`가 아니라 `schema_read_failed`가 먼저 나올 수 있다. 그래서 출력만으로는 교체 여부를 판단하지 않고
아래 1번으로 확인한다. **어느 경우든 먼저 backend를 멈추고, 판단이 끝날 때까지 다시 시작하지 않는다.**
아래 명령의 `$DBX`는 운영 Compose의 DB 컨테이너 실행 접두어다. SQL은 stdin으로 보내 접속 정보가 명령에 나오지 않는다.

```sh
DBX="docker compose --project-name next-stop-siren --env-file /etc/next-stop-siren/production.env \
  --env-file $REL/release.env --file $REL/compose.yaml exec -T db"
```

1단계에서 이번 복원 시각을 확인하면 `STAMP=20261119t010203z`처럼 호스트 변수로 정한다. 형식은 `nss_replaced_` 뒤의
소문자 UTC 값이다. DB 이름은 `sh -c`의 위치 인수(`"$1"`)로 넘겨 컨테이너 셸이 다시 해석하지 않게 한다.

**1. 운영 이름을 쓰는 DB가 무엇인지 확인한다**

```sh
$DBX sh -c 'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -v target="$POSTGRES_DB"' <<'SQL'
SELECT datname, datname = :'target' AS live FROM pg_database WHERE datname = :'target' OR datname LIKE 'nss\_%' ORDER BY datname;
SQL
```

| 확인 결과 | 뜻 |
| --- | --- |
| 이번 복원 시각(UTC)의 `nss_replaced_<UTC>`가 있고, 작업 디렉터리에 시각이 초까지 같은 `restore-*.json`이 있다. DB 이름은 `20261118t222132z`(소문자, 초까지), 보고서는 `restore-20261118T222132186540Z.json`(대문자, 마이크로초까지)이라 앞 15자리를 비교한다. | 교체가 끝났다. 운영 이름 = 복원한 DB, `nss_replaced_<UTC>` = 원래 DB |
| 이번 시각의 `nss_replaced_<UTC>`가 없다 | 교체되지 않았다. 운영 DB는 그대로다. 이 절이 아니라 [5.](#5-실패-시-판단)를 따른다. |
| `nss_restore_staging`이 남아 있다 | 도구가 중간에 강제 종료됐다. 운영 이름은 위 두 줄로 판단하고, staging은 원인 확인 뒤 지운다. |

교체 뒤 상태 읽기가 `schema_read_failed`나 `snapshot_failed`로 실패하면 `restore-*.json`이 만들어지지 않는다. 이때는 `nss_replaced_<UTC>`의
시각이 이번 실행 시각과 같은지로 판단한다.

**2. 이전 DB가 보존됐는지 확인한다**

`nss_replaced_$STAMP`의 표별 행 수를 읽는다. 이 값이 복원 직전에 남긴 `precheck` 보고서의 `snapshot.tables.*.rows`와
같아야 한다. 같은 값은 restore 보고서의 `before`에도 있다.

```sh
$DBX sh -c 'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "nss_replaced_$STAMP" <<'SQL'
SELECT 'users', count(*) FROM users UNION ALL SELECT 'auth_identities', count(*) FROM auth_identities
UNION ALL SELECT 'refresh_sessions', count(*) FROM refresh_sessions UNION ALL SELECT 'conversations', count(*) FROM conversations
UNION ALL SELECT 'messages', count(*) FROM messages UNION ALL SELECT 'chat_quotas', count(*) FROM chat_quotas
UNION ALL SELECT 'chat_quota_reservations', count(*) FROM chat_quota_reservations
UNION ALL SELECT 'chat_queue_members', count(*) FROM chat_queue_members
UNION ALL SELECT 'chat_jobs', count(*) FROM chat_jobs UNION ALL SELECT 'chat_conversation_holds', count(*) FROM chat_conversation_holds;
SQL
```

볼륨 손실 뒤 복원이었다면 원래 DB는 빈 DB다. 이 경우 돌아갈 대상이 아니다.

**3. 어떤 검증이 실패했는지 확인한다**

| 실패한 검증 | 보는 곳 | 원인 위치 |
| --- | --- | --- |
| 도구 재비교 `restored_snapshot_mismatch` | `restore-*.json`의 `unexpected_changes`(다른 표·제약 이름) | 복원한 DB |
| 도구 재비교 `schema_read_failed`, `snapshot_failed` | 작업 디렉터리의 `diagnostics-*.txt`, `docker compose ... ps db` | DB 컨테이너 또는 복원한 DB |
| `/api/ready` `schema` 가 `ok`가 아님 | 응답 `checks`와 backend의 `Service not ready` 로그(누락 표·열 이름) | 복원한 DB |
| `/api/ready` `database` 가 `ok`가 아님 | DB 컨테이너 상태·healthcheck | DB 컨테이너 |
| `/api/ready` `auth`·`worker` 가 `ok`가 아님 | 응답 `checks`, runtime env 검증 | 설정·앱 (DB 아님) |
| 기능 확인만 실패 | 기능 확인 출력 | 앱 버전 또는 데이터 |

**4. 교체 뒤 새 쓰기가 있었는지 확인한다**

이전 DB로 돌아가면 운영 이름이 원래 DB로 바뀐다. 교체 뒤 복원한 DB에 새로 저장된 대화·질문·작업·예약 변경은 운영 DB에서
빠지고 `nss_failed_restore_<UTC>`에만 남는다. 실패한 복원본을 보존해도 새 데이터가 자동으로 합쳐지지 않는다.
backend 정지를 유지한 채(사용자 요청과 worker가 함께 멈춘다) 지금 운영 이름의 상태를 교체 직후 상태와 비교한다.
`$DBR`은 [2. 명령 형식](#2-명령-형식)의 도구 명령이다.

```sh
$DBR precheck   # 지금 운영 이름(복원한 DB)의 상태
python3 - /usr/local/lib/next-stop-siren/db-recovery.py <restore 보고서> <방금 만든 precheck 보고서> <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1]); r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
after = json.load(open(sys.argv[2], encoding="utf-8"))["after"]
now = json.load(open(sys.argv[3], encoding="utf-8"))["snapshot"]
print(r.differences(after, now) or "no_new_writes")
PY
```

| 결과 | 뜻 |
| --- | --- |
| `no_new_writes` | 교체 뒤 새 쓰기가 없다. 돌아가도 추가 손실이 없다. |
| 표 이름 목록(`tables.messages` 등) | 교체 뒤 새 쓰기가 있다. 돌아가면 그 표의 새 변경을 잃는다. 출력된 표 이름과 행 수 차이를 기록한다. |
| `restore-*.json`이 없다(교체 뒤 상태 읽기 실패) | 도구 실패 뒤 backend를 시작하지 않았다면 앱의 새 쓰기는 없다. backend를 시작했거나 시작 여부가 불분명하면 "새 쓰기 있음"으로 본다. |
| `precheck`가 실패한다 | 새 쓰기 여부를 확인할 수 없다. "새 쓰기 있음"으로 본다. |

새 쓰기가 있으면 Owner가 다음 중 하나를 결정하고 결정 근거를 #75에 남긴다. 새 쓰기를 다시 옮겨 넣는 작업은 이 절차에 없으며 별도로 결정한다.

- 새 쓰기 손실을 받아들이고 이전 DB로 돌아간다(5단계 조건도 만족해야 한다).
- 돌아가지 않고 복원한 DB에서 원인을 고친다(설정 수정, [앱 되돌림](#6-앱-되돌림과-스키마-호환) 등).

**5. 이전 DB로 돌아갈지 판단한다**

이전 DB로 돌아가는 경우는 아래 조건을 **모두** 만족할 때다. 4단계에서 `no_new_writes`였다면 돌아가도 복원 직전 상태가
되므로 추가 데이터 손실이 없다. 새 쓰기가 있었다면 Owner가 그 손실을 받아들이기로 결정한 경우에만 돌아간다.

- 원인이 복원한 DB 자체에 있다. 다음 중 하나에 해당하는 경우다.
  - `unexpected_changes`에 표·제약이 있다.
  - `schema`가 `ok`가 아니다.
  - 복원한 DB는 읽히지 않는데 다른 DB는 읽힌다.
- 2단계에서 `nss_replaced_<UTC>`가 보존돼 있고, 행 수가 복원 전 `precheck`와 같다.
- 원래 DB가 빈 DB(볼륨 손실)가 아니다.

다음 경우에는 돌아가지 않는다.

- 복원한 DB가 백업과 일치하는데 `auth`·`worker`·앱 버전 때문에 실패했다. DB는 그대로 두고 설정을 고치거나 [앱 되돌림](#6-앱-되돌림과-스키마-호환)을 한다.
- DB 컨테이너 자체가 응답하지 않는다. 먼저 컨테이너를 복구하고 1단계부터 다시 한다.
- 이전 DB의 보존이나 행 수를 확인할 수 없다. 이름을 바꾸지 않고 Owner가 함께 판단한다.

**6. 이전 DB로 돌아가는 절차** (backend 정지 유지, Owner 승인)

한 트랜잭션에서 운영 이름의 DB(실패한 복원본)는 `nss_failed_restore_<UTC>`로, `nss_replaced_<UTC>`는 운영 이름으로 바꾼다.
트랜잭션이므로 둘 다 바뀌거나 둘 다 그대로다. 실패한 복원본은 원인을 확인할 때까지 지우지 않는다.
```sh
$DBX sh -c 'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -v target="$POSTGRES_DB" \
  -v retained="$1" -v failed="$2"' sh "nss_replaced_$STAMP" "nss_failed_restore_$STAMP" <<'SQL'
SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
  WHERE datname IN (:'target', :'retained') AND pid <> pg_backend_pid();
BEGIN;
ALTER DATABASE :"target" RENAME TO :"failed";
ALTER DATABASE :"retained" RENAME TO :"target";
COMMIT;
SQL
```

`database ... is being accessed by other users`로 실패하면 트랜잭션 전체가 취소된 것이다. backend가 멈춰 있는지
다시 확인하고 같은 명령을 다시 실행한다.

**7. 복귀 후 확인 순서**

1. 1단계 조회로 운영 이름 = 원래 DB, `nss_failed_restore_<UTC>` 존재, `nss_replaced_<UTC>` 없음을 확인한다.
2. `precheck`를 실행해 표별 행 수·행 해시(`snapshot.tables`)와 제약이 복원 전 `precheck` 보고서와 같은지 비교한다.
3. backend를 시작하고 `/api/ready` 200을 확인한다.
4. 기능 확인(`verify-pilot-readiness`)을 한다.
5. 다음을 #75에 남긴다. 복원·복귀 시각, 실패한 검증과 원인 위치, 1·2단계 결과, 4단계의 새 쓰기 여부와 Owner 결정, 복귀 후 `precheck` 비교 결과, 남긴 DB 이름.
6. 다른 백업(금고 사본 등)으로 다시 복원할지 Owner가 결정한다. `nss_failed_restore_<UTC>`는 원인을
   확인하고 디스크 여유를 본 뒤 Owner가 `DROP DATABASE`로 지운다.

2026-11-19 `davekim-dev` 자체 확인: 일회용 `postgres:17.11` 컨테이너에서 실제 `restore`로 교체 후 상태를 만들고
1·2·6·7단계(당시 번호 1·2·5·6) 명령을 `$DBX`만 `docker exec -i <컨테이너>`로 바꿔 실행했다. 복귀 뒤 `precheck`가 복원 직전과 행 수·행 해시·제약·
인덱스까지 같았다. 운영 Compose 경로, backend `/api/ready`·기능 확인, 이름 변경 중 재접속 경합은 시험하지 않았다.
자동 격리 리허설 항목이 아니며, 독립 검토자가 격리 환경에서 절차대로 다시 확인한다.
2026-11-20 독립 검토(`yejoo0310`, `ae0ce6f` 기준)에서 앱 재시작 뒤 새 쓰기가 복귀 후 운영 DB에서 빠지는 것을 재현해
4단계(새 쓰기 확인)와 교체 전·후 실패 구분, `schema_read_failed` 안내를 추가했다. 4단계 명령은 아직 실행해 보지 않았다.

### 복원 공간 기준

`restore`와 `verify --restore-check`는 시작 전 DB 볼륨 여유가 **백업 당시 DB 크기 × 2 + 1 GiB**보다 작으면
`insufficient_volume_space`로 멈춘다. DB 크기는 백업 때 `pg_database_size`로 재서 manifest의 `database_bytes`에 기록한다.
압축된 archive 크기는 복원 크기를 알려 주지 않기 때문이다. ×2는 복원되는 DB 사본과 복원 중 생기는 WAL, 1 GiB는
PostgreSQL 기본 `max_wal_size`만큼의 여유다. 복원 중 볼륨이 차면 같은 볼륨의 운영 DB도 멈추므로 기준을 낮추지 않는다.
기준을 넘지 못하면 오래된 서버 백업·보존 DB를 정리하거나 볼륨을 늘린 뒤 다시 실행한다.

볼륨이 통째로 사라진 경우에도 같다. 새 볼륨으로 DB를 띄우면 빈 DB(`empty_use_db_init`)가 되고, 여기에 `create_all()`을
하지 않고 바로 `restore`한다.

## 8. 손실·복구 목표

| 항목 | 값 |
| --- | --- |
| 허용 데이터 손실(RPO) | 최대 24시간 — 2026-11-05 파일럿 결정 |
| 복구 시간 목표(RTO) | 장애 인지 후 4시간 이내에 서비스 재개(기능 확인 포함) — 2026-11-09 운영 기준. 리허설 복원 자체는 [10.](#10-격리-리허설)의 측정값처럼 짧고, 대부분은 인지·승인·금고 사본 이동 시간이다. |
| 교체 전 DB | 디스크 여유를 확인하고 복원 검증이 끝날 때까지 보존 — 2026-11-09 운영 기준 |

## 9. 로그 보존·회전·디스크 상한

- 운영 Compose의 세 서비스는 `json-file` 드라이버로 파일당 5 MiB, 2개씩 회전한다. 컨테이너 로그는 호스트에서
  최대 약 30 MiB다(`check.py`가 설정을 검사). 컨테이너를 다시 만들면(배포·재생성) 이전 로그 파일도 사라지므로
  보존 기간은 "크기 상한 안에서 다음 재생성까지"다.
- Nginx access log는 `$uri`만 남기고 query·Cookie·Authorization·본문을 남기지 않는다. error log는 꺼져 있다.
  backend 로그는 #70의 `SafeError` 분류만 남기며 예외 원문·키·연결 문자열·SQL을 남기지 않는다.
- **외부 로그 수집은 #70의 민감 로그 차단 단계 확인 뒤에만 한다.** 수집하더라도 대화 원문·토큰을 모으지 않는다.
  백업 파일과 도구의 `diagnostics-*.txt`는 로그로 수집하지 않는다.
- 로그 보존(2026-11-09 운영 기준): **기존 크기 제한 회전을 유지한다.** 장애 분석에 필요한 로그는 **컨테이너를 다시
  만들기 전에**(배포·재생성·복원 뒤 재시작 전) `docker compose ... logs --no-color <서비스>`로 꺼내 별도로 보관한다.
  보관본은 Owner가 관리하는 비공개 위치에 두고 저장소·이슈·PR에 넣지 않는다. 백업 보존 기간을 로그에 자동으로
  적용하지 않는다.
- 디스크: 백업 전 작업 디렉터리 여유가 DB 크기×2 + 64 MiB, 복원·복원 검사 전 DB 볼륨 여유가
  [복원 공간 기준](#복원-공간-기준)보다 작으면 도구가 시작하지 않는다. 복원 뒤 교체 전 DB가 남는 동안 사용량이 늘어난다.

## 10. 격리 리허설

```sh
backend/.venv/bin/python deploy/db-recovery-rehearsal.py --context desktop-linux \
  --evidence-dir /absolute/new/directory/outside/the/repository
```

Windows는 `backend\.venv\Scripts\python.exe`. 리허설은 네트워크 없는(`--network none`) 일회용 `postgres:17.11`
컨테이너와 라벨 붙은 볼륨 하나만 만들고 끝에 지운다. 개발·테스트 DB, Compose project, 다른 컨테이너를 쓰지 않는다.
스키마는 앱 모델의 `create_all()` DDL에서 FK만 뺀 #52 시점 상태로 만들고, 사용자·Google 신원·refresh 연결·대화·
질문/답변(완료·실패)·예약(정산·예약 해제·예약)·작업(완료·실패·정지·대기)·정지 기록을 합성 데이터로 넣는다.
모든 DB 조작은 운영과 같은 `db-recovery.py`로 하며, 고아 행 주입과 직접 FK 실행만 의도된 장애 주입이다.

| 확인 | 내용 |
| --- | --- |
| 격리 | 네트워크 없음·포트 없음·라벨 소유 확인 |
| 사전 점검 | 읽기 전용, 반복해도 같은 결과 |
| 백업·검증 | manifest 스냅샷 = 현재 상태, 별도 DB 복원 비교 후 그 DB 제거, 운영 DB 무변경 |
| 손상 백업 | archive 1바이트 변경 → `backup_sha256_mismatch` |
| 적용 실패 | 오래된 백업 → `backup_does_not_match_current`, 고아 예약 → `orphan_reservations`, 데이터 무변경 |
| DB 원자성 | 고아 예약이 있는 상태에서 직접 실행한 FK를 PostgreSQL이 거부하고 데이터 무변경 |
| 실패 후 복원 | 이전 백업 복원 → 장애 전 상태와 행 해시까지 일치, 교체된 DB 보존 |
| 적용 성공 | 행 수·행 해시·상태별 개수·인덱스 동일, FK만 추가, 재실행은 `already_applied` |
| FK 효과 | 정상 쓰기 허용, 새 고아 쓰기 거부(둘 다 롤백) |
| 앱 호환 | FK 있음·없음 모두 앱 readiness의 테이블·컬럼 비교 `ok`, FK 되돌림 후 데이터 동일 |
| 볼륨 손실 | 서버 밖 사본으로 복사 → 컨테이너·볼륨 삭제 → 새 빈 DB에 복원 → 상태 일치, sequence 복원 |
| 보고서 | 메시지 본문 canary가 보고서·도구 출력에 없음 |

### 리허설 결과 (2026-11-15 KST, `davekim-dev`)

PR #99 병합 뒤 main `ef24f75`로 Windows Docker Desktop(`desktop-linux`, Engine 29.8.0)에서 `postgres:17.11`
(17.11 Debian) 리허설을 다시 실행했다. [복원 공간 기준](#복원-공간-기준)과 FK 되돌림 순서를 반영한 마지막 수정이
포함된 코드다. 위 표의 **13개 항목이 모두 통과**했고, 모든 manifest가 `schema_version` 2로 `database_bytes`를 기록했다.
리허설 소유 컨테이너·볼륨은 제거됐고 같은 라벨의 리소스가 남지 않았다. 같은 commit의 오프라인 검사
`check.py DataRecoveryChecks` 13개도 통과했다.

| 측정 | 값 |
| --- | --- |
| 합성 행 | users 2, auth_identities 1, refresh_sessions 2, conversations 3, messages 6, chat_quotas 1, chat_quota_reservations 4, chat_queue_members 1, chat_jobs 4, chat_conversation_holds 1 |
| 별도 DB 복원 검사 | 0.99초 |
| 적용 실패 후 복원 | 1.16초 (교체된 DB에 고아 작업 포함 chat_jobs 5행 보존) |
| 볼륨 손실 후 복원 | 1.24초 |

2026-11-07 KST의 이전 리허설(`98e1ec1`, 13개 통과)은 복원 공간 기준 변경(manifest `schema_version` 2) 이전 코드의
결과라 이 결과로 대체한다. 시간은 극소량 합성 데이터의 절차 자체 소요이며 실제 데이터·t2.micro 성능이나 RTO 근거가
아니다. 리허설은 공간 부족(`insufficient_volume_space`) 경로를 실제로 일으키지 않으며 그 판정은 오프라인 검사가 맡는다.
macOS(Colima) 실행과 독립 검토자 재현은 아직 없다. 증거 디렉터리(`result.json`, `commands.json`, 보고서)는 합성
데이터만 담지만 저장소에 넣지 않고 `davekim-dev`가 로컬에 보관한다.

## 11. 현재 운영 보고와 남은 조건 (2026-11-25)

현재 담당은 Owner `xifoxy-ru`다. [운영 인계](README.md#현재-운영-인계-2026-11-25-kst)의 Owner·PM-2 보고에 따르면 수동 백업 1회와 서버 밖 Mac 암호화 금고 사본의 무결성 일치, Discord 실패 알림 수신을 확인했다. 첫 정기 timer는 **2026-11-26 04:00 KST 예정**이며 11/25 현재 실행 성공으로 세지 않는다. 문서 Worker가 원본 DB·사본·비밀값·Discord에 접근한 결과가 아니다.

- 첫 timer 실제 성공·실패와 새 백업/서버 밖 사본, 반복 실행·보존·디스크 상한을 예정 시각 뒤 확인한다.
- 금고 사본의 무결성 일치는 실제 운영 DB 복원 성공과 구분한다. EC2 복원·재접속 경합·운영 Compose/TLS/readiness 전체의 복구 시간·기능 확인과 RTO 충족은 미확인이다.
- 분석용 로그의 실제 보관 위치·삭제 시점과 컨테이너 교체 전 보관, 실패 알림의 대응 책임·후속 조치를 운영 기록에 남긴다.
- #105의 교체 후 실패·이전 DB 복귀 절차는 병합됐고 [11/20 격리 독립 검증](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75#issuecomment-7026821519)과 [11/22 수정 재검토](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75#issuecomment-7071685558)가 완료됐다. 이전 실패 재현을 미해결로 되돌리거나 이번 최신 main에서 재실행했다고 주장하지 않는다. 이것은 #75 전체 운영 완료가 아니다.
- `deploy-gates.json`의 `data_recovery` 증거·승인과 #76 인계는 실제 source·범위·수행자·시각·남은 조건을 Owner가 대조한다. 독립 운영 확인자는 해당 변경 구현자와 분리한다.
- 대량 데이터·t2.micro의 CPU credit·메모리·디스크·백업/복원 시간은 #76의 실제 운영 측정으로 남는다. [로컬 3인 mock 리허설](README.md#76-로컬-3인-mock-리허설-2026-11-25)은 EC2 부하나 운영 복원의 대체 증거가 아니다.
