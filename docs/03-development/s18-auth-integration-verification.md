# S18 인증과 대화 소유권 통합 검증

[이슈 #19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)의 인증·소유권 검증 결과다. 기준 계약은 [API](../04-reference/api.md#회원-채팅-대기열-53)와 [인증](../04-reference/authentication.md)을 따른다. 전체 대기열 결함 검증은 #54 범위다.

## 완료 기준과 현재 결과

| 이슈 요구사항 | 확인 결과 | 남은 검증 |
| --- | --- | --- |
| 접수·조회·구독·실행 전 취소·재시도·건너뛰기의 인증·소유권 제한 | 전체 12개 경로에서 access 누락·변조·만료·refresh 오용을 `401`로 거부. 타인 대화·작업 조작은 미존재 자원과 같은 `404`. 거부 전후 DB 무변경과 정상 SSE 회원별 격리 확인 | 없음 |
| 두 회원의 작업·한도·대화·임시 출력 분리와 회원 전환 | HTTP·DB·SSE 회원 격리 확인. 실제 브라우저에서 A→B 전환 시 A 대화·초안 정리와 늦은 목록·이력·접수 응답 무시 확인 | 화면의 임시 출력·구독 격리: 실제 SSE 미연동으로 BLOCKED |
| 로그아웃 시 화면·구독 정리, 서버 작업 유지, 기존 토큰 계약 유지 | logout 후 CSRF 거부와 잔여 access 조회 성공, 실제 화면의 대화·초안 정리 확인 | 실행 중 logout과 완료 이벤트의 경합, 구독 정리, 서버 작업 지속·재로그인 복구, refresh 폐기 증거 연결 |
| 실제 인증 HTTP·DB 결과와 화면 격리 결과 연결 | 아래 통합 검사와 실제 로그인·회원 전환 브라우저 검사 통과 | SSE 화면 연동 뒤 임시 출력·구독 결과 연결 |

## 실행한 검사

2026-10-25 KST, macOS arm64·Python 3.13.15, 제품 소스 `2d50da5`에서 실행했다. 검사 파일은 `backend/tests/test_auth_chat_boundaries_db.py`와 `backend/tests/test_auth_chat_events_db.py`이며 제품 코드는 변경하지 않았다.

전용 테스트 DB 설정은 [온보딩](../01-start/onboarding.md#4-테스트와-품질-검사)을 따른다. `TEST_DATABASE_URL`과 프로젝트 uv 경로를 설정한 환경에서 `backend` 디렉터리 기준으로 실행했다.

```sh
.venv/bin/python -m pytest tests/test_auth_chat_db.py tests/test_chat_api_db.py tests/test_google_acceptance_db.py tests/test_auth_chat_boundaries_db.py tests/test_auth_chat_events_db.py -q --tb=short
```

- 결과: **56 passed**, 실패·skip 없음, 61.00초. 인증·소유권 검사 22개와 HTTP·SSE 검사 10개를 추가했다.
- 신규 파일의 Ruff lint·format과 `git diff --check` 통과.
- Starlette TestClient의 `BlockingPortal` 사용에 관한 DeprecationWarning 1건.

위 검사에서 인증 발급은 실제 FastAPI 라우트를 TestClient로 호출하고, SSE는 임시 localhost 서버에 실제 HTTP 연결 두 개를 열었다. 보호된 PostgreSQL 임시 스키마의 저장 결과를 독립 세션으로 확인했다. 인증 principal·앱 JWT 서명 검증은 대체하지 않았다. Google 외부 HTTP만 대체하고 RSA·OIDC 검증과 callback→CSRF→refresh는 실제 코드를 사용했다. 실제 Google 계정 로그인은 미실행이다.

인증 경계 검사는 실행기를 끄고 queue 서비스로 상태를 준비했다. SSE 임시 출력·완료 검사는 실제 실행기와 제어 가능한 가짜 제공자를 사용했고, 취소·건너뛰기는 실행기를 끄고 확인했다. 만료 거부는 JWT 검증 시계를 앞당겨, 구독 종료는 HTTP로 발급한 access의 수명만 3초로 줄여 재현했다. 접수 간격은 테스트 DB 시각을 조정했다. 기존 `test_auth_chat_db.py`에서는 기본 mock 제공자와 실행기를 사용했다.

SSE 검사에서 두 회원의 `snapshot`·`job`·`delta`·`limits` 분리, A 완료·취소 중 B 한도·예약 유지, A 건너뛰기 중 B 정지 유지가 확인됐다. 생성 중 연결을 닫아도 작업은 완료됐고 재접속 snapshot은 본인의 저장 답변만 반환했다. access 만료로 구독이 끝나도 작업·예약은 유지됐으며 유효 access로 재연결됐다.

실제 화면 검사는 5173의 Vite 화면을 Chromium으로 열고 모든 `/api` 요청을 격리 테스트 서버로 전달했다. 응답 목업 없이 실제 가입·로그인·DB 저장·기본 mock 제공자를 사용했다. 브라우저 **4개 검사 통과**(6.9초), 실행용 pytest **1 passed**(8.07초), Biome lint·format과 프런트 타입 검사 통과. 사용자 요청에 따라 브라우저 검사 파일과 이를 호출하는 실행용 pytest는 최종 변경에서 제외했다.

```sh
# 검증 당시 실행 명령. 해당 실행 파일은 최종 변경에서 제외됨.
S18_BROWSER=1 .venv/bin/python -m pytest tests/test_auth_chat_browser_db.py -q -s --tb=short
```

새로고침 후 본인 이력 복구와 A→B 전환 시 대화·초안 정리를 확인했다. 경합은 A의 실제 목록·이력·질문 접수 응답을 브라우저 전달 직전에 보류하고, logout→B login 뒤 해제해 재현했다. B 대화·초안은 유지됐고 A 내용은 다시 나타나지 않았다. 실제 회원 화면의 SSE 요청은 0건으로 관찰되어 임시 출력·구독 정리는 미연동 항목으로 남긴다.

## 남은 검증과 담당 인계

- 사용자 수동 확인: B의 목록에는 `대화 4` 한 개만 있고 A의 대화는 보이지 않았다. 숫자는 회원별 대화 개수가 아닌 DB 대화 ID를 표시한 것으로 정상 동작이다.
- 화면 미연동: 왼쪽 사이드바의 대화 기록은 고정 문구이며 새 대화 버튼에도 동작이 없다. 가운데 `내 대화`만 실제 API 목록과 연결되어 있다. 화면 담당에게 인계한다.
- 가입 화면 계약 불일치: 이름을 필수 입력받지만 실제 가입 요청·저장에는 사용하지 않는다. 현재 이메일·비밀번호 가입 계약에 맞춰 이름 입력란을 제거하도록 화면 담당에게 인계한다.
- 사용자 수동 재현: 같은 대화에 질문 3개를 보내고 직접 아래로 스크롤하면 입력창이 메시지 사이에 나타나고 이후 질문·답변이 입력창 아래로 이어진다(첨부 화면). 기대 동작은 입력창과 메시지가 겹치지 않고 모든 메시지를 읽을 수 있는 것이다. 자동 스크롤 미구현과 별도의 화면 레이아웃 결함으로 `gittul-123`에게 인계한다. 저장된 메시지 전체 개수와 CSS 원인은 추가 확인이 필요하며 인증·소유권 결함으로 분류하지 않는다.
- 인증 담당 `yejoo0310`: logout 중 서버 작업 지속을 확인하고 HTTP·DB 결과를 화면 결과와 연결한다.
- 화면 담당 `gittul-123`: 실제 대기열·SSE·한도 화면을 연결하고 전송 응답 타입에 `answer: null`·`job`·`limits` 계약을 반영한다. 연결 후 A의 임시 출력 중 logout→B login과 늦은 SSE 전달 시 이전 상태가 나타나지 않는지 검증한다.
- 실행한 검사에서 인증·소유권 결함은 발견하지 못했다. 화면의 SSE 미연동과 로그아웃 중 서버 작업 지속 검증이 남았다. 후속 결과에 재현 절차·기대/실제 결과·담당을 남겨 #54에 인계한다.
