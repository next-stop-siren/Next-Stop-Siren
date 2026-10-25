# S18 인증과 대화 소유권 통합 검증

[이슈 #19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)의 인증·소유권 검증 결과다. 기준 계약은 [API](../04-reference/api.md#회원-채팅-대기열-53)와 [인증](../04-reference/authentication.md)을 따른다. 전체 대기열 결함 검증은 #54 범위다.

## 완료 기준과 현재 결과

| 이슈 요구사항 | 확인 결과 | 남은 검증 |
| --- | --- | --- |
| 접수·조회·구독·실행 전 취소·재시도·건너뛰기의 인증·소유권 제한 | 전체 12개 경로에서 access 누락·변조·만료·refresh 오용을 `401`로 거부. 타인 대화·작업 조작은 미존재 자원과 같은 `404`. 거부 전후 DB 무변경 확인 | 정상 SSE의 회원별 이벤트 격리 |
| 두 회원의 작업·한도·대화·임시 출력 분리와 회원 전환 | 본인 대화·메시지·작업의 페이지 조회, 같은 요청 키와 같은 이메일의 Google·로컬 계정 분리 확인. 쿠키와 Bearer가 달라도 Bearer 회원을 소유자로 사용 | 한 회원의 작업 변화가 다른 회원 한도에 영향을 주지 않는지, 실제 화면 전환 시 이전 대화·임시 출력·구독 정리 |
| 로그아웃 시 화면·구독 정리, 서버 작업 유지, 기존 토큰 계약 유지 | 기존 인증·채팅 검사에서 logout 후 CSRF 거부와 잔여 access 조회 성공 확인 | 실행 중 logout과 완료 응답의 경합, 화면·구독 정리, 서버 작업 지속·재로그인 복구, refresh 폐기 증거 연결 |
| 실제 인증 HTTP·DB 결과와 화면 격리 결과 연결 | 아래 HTTP·DB 통합 검사 통과 | 실제 브라우저 결과와 연결. 목업 결과로 대체하지 않음 |

## 실행한 검사

2026-10-25 KST, macOS arm64·Python 3.13.15, 제품 소스 `2d50da5`에서 실행했다. 신규 검사 파일은 `backend/tests/test_auth_chat_boundaries_db.py`이며 제품 코드는 변경하지 않았다.

전용 테스트 DB 설정은 [온보딩](../01-start/onboarding.md#4-테스트와-품질-검사)을 따른다. `TEST_DATABASE_URL`과 프로젝트 uv 경로를 설정한 환경에서 `backend` 디렉터리 기준으로 실행했다.

```sh
.venv/bin/python -m pytest tests/test_auth_chat_db.py tests/test_chat_api_db.py tests/test_google_acceptance_db.py tests/test_auth_chat_boundaries_db.py -q --tb=short
```

- 결과: **46 passed**, 실패·skip 없음, 41.47초. 신규 검사는 22개다.
- 신규 파일의 Ruff lint·format과 `git diff --check` 통과.
- Starlette TestClient의 `BlockingPortal` 사용에 관한 DeprecationWarning 1건.

실제 FastAPI 라우트를 TestClient로 호출하고, 보호된 PostgreSQL 임시 스키마에 저장한 결과를 독립 DB 세션으로 확인했다. 인증 principal·앱 JWT 서명 검증은 대체하지 않았다. Google 외부 HTTP만 대체하고 RSA·OIDC 검증과 callback→CSRF→refresh는 실제 코드를 사용했다. 실제 Google 계정이나 5173 브라우저를 사용한 결과는 아니다.

신규 검사는 실행기를 끈 상태에서 queue 서비스로 대기·실행·실패·완료를 준비했다. 만료는 JWT 검증 시계를 16분 앞당기고, 접수 간격은 테스트 DB 시각을 조정해 재현했다. 기존 `test_auth_chat_db.py`에서는 기본 mock 제공자와 실행기를 사용했다.

## 남은 검증과 담당 인계

- 인증 담당 `yejoo0310`: 두 회원의 SSE·한도 변화 격리와 logout 중 서버 작업 지속을 확인하고 HTTP·DB 결과를 화면 결과와 연결한다.
- 화면 담당 `gittul-123`: 실제 회원 화면에서 A의 임시 출력이 있는 동안 logout→B login을 실행한다. A의 HTTP 응답·SSE 이벤트를 지연시킨 뒤 전달해 이전 비공개 상태가 다시 나타나지 않는지도 확인한다.
- 기준 소스의 회원 화면에는 실제 대기열 구독 연결이 남아 있고, 전송 응답 타입도 `answer: null`·`job`·`limits` 계약 반영이 필요하다. 화면 담당에게 연결 범위를 확인한다. 이는 소스상 확인 사항이며 실행으로 재현한 결함과 구분한다.
- 실행한 HTTP·DB 검사에서 제품 결함은 발견하지 못했다. 정상 SSE·실제 화면·로그아웃 경합은 아직 미실행이다. 후속 결과에 재현 절차·기대/실제 결과·담당을 남겨 #54에 인계한다.
