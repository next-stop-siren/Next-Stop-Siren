# S18 인증과 대화 소유권 통합 검증

[이슈 #19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)의 인증·대화 소유권 검증 결과다. [API](../04-reference/api.md#회원-채팅-대기열-53)·[인증](../04-reference/authentication.md) Rev 3을 기준으로 검사했다. 전체 대기열 결함 검증은 #54 범위다.

## 검증 환경과 결과 요약

- 실행일: 2026-10-29 KST. 대상 커밋: `4892cf85b526e99aba225bffcc54ebf7b7020362`.
- 환경: macOS arm64, Python 3.13.15, Node 24.21.0·npm 11.19.0, Chromium, PostgreSQL 전용 db-test.
- DB 소유권·접속을 확인하고 임시 스키마·트랜잭션으로 격리했다. 브라우저 통합 검사는 API 8001·Vite 5174에서 실행했으며 개발 DB는 사용하지 않았다.
- 제품 코드·기존 테스트 파일은 변경하지 않았다. 추가 브라우저 경합 실행기는 저장소 밖의 임시 도구로 실행했다.

**실행한 검사에서 제품 결함은 발견하지 못했다.** 백엔드 600개, 기존 브라우저 20개와 추가 브라우저·DB 실행기 2개가 통과했다. 추가 실행기 안의 시나리오를 별도 테스트 수로 중복 집계하지 않았다.

| 검사 | 결과 | 시간 |
| --- | --- | --- |
| 가입 제한·설정·가입/로그인 API·비밀번호·로그 단위 회귀 | 345 passed | 4.85초 |
| 가입 보호·중복 경합·저장/쿠키 실패 보상·로그인 세션 DB 회귀 | 46 passed | 40.61초 |
| 인증·대화 소유권·회원별 SSE·logout 회귀 | 62 passed | 81.19초 |
| Google callback·세션 보상·로그 비노출 회귀 | 147 passed | 18.63초 |
| 회원 채팅 브라우저 검사 | 6 passed | 29.4초 |
| 인증 모달 키보드·Google 실패 화면 브라우저 검사 | 14 passed | 6.4초 |
| 추가 회원 전환·SSE 복구·DB 확인 실행기 | 1 passed | 25.58초 |
| 추가 인증 응답 경합·Google callback 화면·DB 확인 실행기 | 1 passed | 10.77초 |

최종 실행은 실패·skip이 없었다. 백엔드 4개 실행에 기존 Starlette `BlockingPortal` DeprecationWarning이 각각 1건 있었다.

## 인증·소유권과 로그아웃

검사 파일은 `test_auth_chat_db.py`, `test_chat_api_db.py`, `test_google_acceptance_db.py`, `test_auth_chat_boundaries_db.py`, `test_auth_chat_events_db.py`, `test_auth_chat_logout_db.py`다. 이메일·Google 인증 HTTP 경로에서 발급한 JWT로 검사했으며 principal·앱 JWT 서명 검증은 대체하지 않았다.

| 검증 항목 | 확인 결과 |
| --- | --- |
| 접수·조회·구독·실행 전 취소·재시도·건너뛰기의 인증 | 전체 12개 경로에서 access 누락·변조·만료·refresh 오용을 401로 거부 |
| 타인 자원 접근과 상태 변경 | 대기·실행·실패·완료 상태의 타인 대화·작업은 미존재 자원과 같은 404. 대화·질문 ID 혼합도 거부하고 거부 전후 DB 무변경 확인 |
| 두 회원의 작업·한도·SSE | snapshot·job·delta·limits가 회원별로 분리됨. A 완료·취소·건너뛰기가 B의 예약·한도·정지 상태를 바꾸지 않음 |
| logout 중 작업·토큰 계약 | 이메일·Google별 대기·생성·완료 경합에서 서버 작업은 계속됨. 현재 refresh 계보만 폐기되고 옛 쿠키 재사용은 401. 다른 로그인 세션은 유지되고 기존 access는 만료 전까지 유효 |
| 재접속·재로그인 복구 | 작업 완료·답변 저장·정산 후 A가 같은 답변을 복구하며 B의 접근은 404 |

SSE는 임시 localhost 서버의 실제 TCP 연결로 검사하고 DB는 별도 세션으로 읽었다. 제공자 완료와 logout을 첫 delta 뒤 barrier로 동시에 시작해 경합을 재현했다. 만료 거부는 JWT 검증 시계를 조정하고 구독 종료는 HTTP 발급 access 수명을 3초로 줄여 확인했다. 접수 간격은 테스트 DB 시각으로 제어했다.

## 회원 채팅·SSE 브라우저

기존 `frontend/e2e/member-chat.spec.ts` 6개를 실제 API·DB·실행기와 기본 모의 제공자로 실행했다. 기존 서버 점유로 별도 포트를 사용하여 Origin만 바꾼 임시 복사본을 실행했으며 원본은 수정하지 않았다.

- 질문 접수→저장 답변과 새로고침 후 같은 대화 복구: 통과.
- 응답 유실 뒤 입력 복구·같은 request_key 재확인·중복 저장 방지: 통과.
- SSE가 끊긴 사이 완료된 답변을 since 재연결 snapshot으로 복구: 통과.
- logout→다른 회원 로그인 뒤 이전 대화·기록 미노출: 통과.
- 질문 3개를 보낸 뒤 스크롤해도 마지막 답변이 입력창 위에 있고 메시지가 입력창 사이에 끼지 않음: 통과.

추가 검사는 첫 delta를 보낸 뒤 완료를 멈추는 제공자로 생성 중 상태를 재현했다.

| 경합·복구 절차 | 확인 결과 |
| --- | --- |
| A 임시 출력 중 logout→같은 화면에서 B login | A 질문·임시 출력·초안 제거. 서버 A 구독 제거·B 구독 1개, 전환 뒤 A 토큰 요청 없음 |
| A 이력 GET 응답을 보류하고 B 로그인 뒤 해제 | DOM 변경 관찰에서 A 내용 미노출, B 초안 유지 |
| B가 생성 중일 때 A 작업 완료 | B 임시 출력·한도 유지. A 재로그인 후 A 저장 답변만 복구 |
| 생성 중 SSE 연결 해제 또는 탭 닫기 | 취소 요청 0건. 연결 없이도 서버 완료, since 재연결·새 탭에서 저장 답변 복구 |
| 별도 DB 세션 조회 | 검증용 작업 4개 모두 completed, 저장 답변이 전체 출력과 일치, 예약 4개 모두 settled |

실행 중 취소 버튼 미노출과 사이드바의 본인 대화 표시도 확인했다. 이전에 보고된 SSE 미연동·사이드바 미연동·입력창 배치 문제는 현재 코드의 재검사에서 해소됐다.

## 인증 모달·Google 실패 복귀

`frontend/e2e/auth-modal-keyboard.spec.ts`와 `google-login-failure.spec.ts`의 14개 검사가 통과했다. Tab·Shift+Tab 가두기, Enter·Space 진입, Escape·초점 복귀, 이름 입력 제거, 실패 안내·URL 소비·재시도 진입을 확인했다. 기존 파일의 지연 로그인·CSRF·재시도 응답 모의 처리는 유지했으며 실제 API 증거와 구분한다.

| 추가 통합 검사 | 확인 결과 |
| --- | --- |
| 실제 login/register 성공·실패 × 모드 전환·닫기 후 재열기 | 8개 조합 통과. 실제 HTTP 응답만 전달 직전에 보류했다가 해제하여 새 모드의 오류·초점·제출 상태가 이전 응답에 오염되지 않음 |
| 늦은 로그인·가입 성공 | 성공 세션은 회원 화면에 적용. 이후 logout 때 모달이 저절로 다시 열리지 않음 |
| 잘못된 state·nonce, 유효 트랜잭션의 취소·제공자 오류·토큰 교환 timeout | 고정 실패 코드의 302로 화면 복귀, 실패 파라미터 소비·트랜잭션 쿠키 삭제·no-store/no-referrer 확인. 새 인증 쿠키 미발급 |
| 실패 뒤 재진입 | 역방향 Tab·이메일 입력 초점·닫기/재열기·새로고침 안내 미반복 확인. Google 재시도 후 서명된 대체 응답으로 성공하고 csrf→refresh→me 복구 |
| 기존 로그인 세션의 실패 | 회원 화면과 세션을 유지하고 배너로 안내. logout 요청 없음 |
| 안전한 복귀와 DB | 외부 절대 URL·프로토콜 상대 URL·미허용 경로는 422, redirect·쿠키 변경 없음. 실패 callback 전후 사용자·Google 신원·refresh 세션 수 동일. 성공 callback은 신원·세션 생성 |

Google 외부 HTTP와 동의 화면만 대체했다. 실제 start의 302 Location·state·nonce·PKCE 요청을 확인한 뒤 검증용 동의 화면으로 연결했다. callback·암호화 트랜잭션·RSA 서명/nonce 검증·앱 토큰·DB·프런트는 실제 코드다. `test_google_callback_db.py`와 `test_google_callback_logging.py` 147개로 성공·실패·세션 보상·로그 비노출도 재검사했다.

## 가입 제한·저장 원자성

- 기본 최근 600초 접수 10건·동시 1건과 설정 상한 3건을 검사했다. 20건의 겹친 HTTP 요청에서 각각 처리 1/거절 19, 처리 3/거절 17을 확인했다.
- 처리 자리 부족은 대기 없이 503·Retry-After 5이며 접수량을 소모하지 않았다. 10건 접수 후 429·제한 해제까지 남은 초를 반환했다. 접수 후 실패는 횟수에 남았다.
- 실제 Argon2 함수 진입~반환 관찰로 동시 상한을 확인했다. 저장 해시 검증과 기존 64 MiB/t=3/p=1·salt 16/해시 32 설정을 유지했다. 시간창 경계·재시도 초 올림은 제어 시계로 검사했다.
- 실제 TCP 연결 종료 후에도 진행 중 해시가 끝나기 전 다음 가입은 503이었다. 완료 후 자리가 반환되고 응답 유실의 동일 이메일 재시도는 409, 중복 행은 없었다.
- 해시·저장·쿠키 실패의 rollback/보상, 사용자·세션 무잔여 또는 기존 계정 보존, 토큰/쿠키 미반환과 처리 자리 재사용을 확인했다. 동일 이메일 경합·로그인 회귀도 통과했다.

## 수동 확인

2026-10-29, 실제 5173 화면에서 수행한 수동 확인 보고다. 네트워크 원본은 별도로 수집하지 않았다.

| 검사 | 결과 |
| --- | --- |
| 실제 Google 로그인 | 5173 복귀·회원 화면 전환·새로고침 세션 유지·logout 정상 |
| 합성 취소 callback | Google 자동 복귀를 차단한 상태에서 실제 start의 새 state와 error=access_denied 사용. cancelled 복귀·안내 확인 |
| 합성 제공자 오류 callback | 새 트랜잭션의 state와 error=server_error 사용. temporarily_unavailable 복귀·안내 확인 |

합성 callback 검사는 실제 Google이 보낸 취소·장애와 구분한다. 이미 동의한 계정으로 취소할 동의 화면이 나오지 않아 실제 Google 취소는 미확인이다.

## 실행 명령과 재실행 범위

[전용 테스트 DB 설정](../01-start/onboarding.md#4-테스트와-품질-검사)에 맞는 TEST_DATABASE_URL·프로젝트 도구를 준비하고 backend 디렉터리에서 실행했다.

```sh
.venv/bin/python -m pytest tests/test_register_api.py tests/test_registration_limits.py tests/test_registration_settings.py tests/test_auth_settings.py tests/test_login.py tests/test_login_api.py tests/test_login_limits.py tests/test_login_limit_integration.py tests/test_passwords.py tests/test_registration_logging.py -q --tb=short
.venv/bin/python -m pytest tests/test_registration_protection_db.py tests/test_registration_db.py tests/test_register_api_db.py tests/test_register_concurrency_db.py tests/test_register_compensation_db.py tests/test_login_db.py tests/test_login_sessions_db.py -q --tb=short
.venv/bin/python -m pytest tests/test_auth_chat_db.py tests/test_chat_api_db.py tests/test_google_acceptance_db.py tests/test_auth_chat_boundaries_db.py tests/test_auth_chat_events_db.py tests/test_auth_chat_logout_db.py -q --tb=short
.venv/bin/python -m pytest tests/test_google_callback_db.py tests/test_google_callback_logging.py -q --tb=short
```

기존 브라우저 검사의 일반 실행 진입점은 `./local.sh test-e2e`다. 이번 별도 포트 실행과 추가 경합 검사에는 임시 도구를 사용했다. 실행 당시 경로·명령은 아래와 같으며 파일은 저장소에 포함하지 않아 다른 환경에서 그대로 재실행할 수 없다.

```sh
# 저장소 루트 기준. /tmp의 검증 파일이 있는 실행 환경에서만 사용 가능
backend/.venv/bin/python /tmp/s18-phase8/run_controlled.py
S18_CONTROL_ONLY=1 backend/.venv/bin/python /tmp/s18-phase8/run_controlled.py
backend/.venv/bin/python /tmp/s18-phase9/run.py
S18_ACTUAL_ONLY=1 backend/.venv/bin/python /tmp/s18-phase9/run.py
```

2026-10-25의 기존 인증·소유권 62개 검사(제품 2d50da5, 검사 24f418e, 69.19초)와 당시 브라우저 4개 검사는 최초 결과다. 위 표는 2026-10-29 재검사 결과이며 최초 결과를 합산하지 않았다.

## 검증 한계

- 실제 AI는 호출하지 않았다. 답변은 기본 모의 제공자 또는 완료 시점을 제어하는 제공자를 사용했다.
- 실제 Google 성공은 수동 확인, 취소·제공자 오류는 합성 callback과 대체 제공자 검사다. 실제 Google 취소·장애 발생 검증은 포함하지 않는다.
- 추가 공동·비구현자 독립 확인의 실행 결과는 이 문서에 포함하지 않는다.
- 임시 브라우저 경합 도구는 저장소에 없어 재실행에 별도 준비가 필요하다.
- 운영 배포·부하·다중 worker 제한 공유와 FIFO·전체 대기열 용량·KST 정산 경계·서버 장애 주입 전체는 검증하지 않았다. 관련 통합 범위는 #54, 운영 측정은 #76에서 다룬다.
