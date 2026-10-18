# 처음 맡을 작업

백엔드 인증·본인 대화·모의 답변 저장·재조회와 제품 ORM 모델은 구현되어 있습니다. 화면은 비회원 소개·인증 모달이며 실제 로그인·채팅 API 연결은 남았습니다. 아래 첫 이슈와 순서는 담당·의존 관계의 안내이며 현재 미완료 목록이 아닙니다. 기존 코드·테스트를 확인하고 최신 이슈·PR의 남은 범위를 따르세요.

전체 직접 선행·후속 이슈는 [공정진행도](development-flow.md)에, 각 담당자의 작업과 외부 인계는 아래 담당 공정에 있습니다. 실제 진행·차단·검토 상태는 연결된 GitHub 이슈와 PR에서 확인하세요.

| 담당 | 첫 이슈 | 담당 공정 | 이번에 남길 결과 |
| --- | --- | --- | --- |
| `yejoo0310` | [#2](https://github.com/next-stop-siren/Next-Stop-Siren/issues/2) | [인증](auth-flow.md) | 후속 검사에서 불러 쓸 인증 pytest 값과 401 예시 |
| `davekim-dev` | [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9) → [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8) | [채팅·데이터·AI](chat-flow.md) | 대화 ORM 모델 뒤 소유권 조회 함수·자동 검사 |
| `gittul-123` | [#16](https://github.com/next-stop-siren/Next-Stop-Siren/issues/16) | [화면](frontend-flow.md) | 비회원 소개와 사람이 작성한 고정 예시 화면 |

## 함께 읽을 기준

1. [실행 체크리스트](../01-start/onboarding.md)로 로컬 앱을 준비하고 현재 동작을 확인합니다. 과거 macOS 검증과 현재 소스 대조를 구분하며 Windows 실제 실행은 **NOT RUN**입니다.
2. [서비스 범위](../01-start/service-scope.md)에서 비회원·회원 경계와 첫 내부 목표를 확인합니다. [API 요청·응답](../04-reference/api.md), [DB 설계](../04-reference/database.md), [인증 구현 기준](../04-reference/authentication.md)은 현재 백엔드 계약과 후속 구현의 공통 기준입니다. 제품 5개 테이블은 등록되어 있지만 로컬 DB에는 `db-init`이 필요합니다.
3. 이슈에 적힌 범위대로 변경하고, [협업 흐름](../03-development/workflow.md)에 따라 PR에 결과와 검사 기록을 남깁니다. PM `xifoxy-ru`가 공통 API·DB·보안 형식과 미정 결정을 정리하고 PR을 검토합니다. 각 담당자는 자기 구현과 검사를 맡습니다.

여기서 **fixture**는 pytest 검사에서 불러 쓰는 가짜 입력·예상 결과입니다. **ORM 모델**은 Python 클래스와 DB 테이블의 연결 정의이며, **DB 세션**은 한 작업의 DB 읽기·쓰기를 묶는 객체입니다. **확인된 사용자 정보(principal)**는 서버가 로그인 자격을 검사해 얻은 사용자 ID입니다. 화면에서 보낸 `user_id`만으로 소유권을 판단하지 않습니다.

## `yejoo0310`: #2 이후 인증 인계

1. [#2](https://github.com/next-stop-siren/Next-Stop-Siren/issues/2)와 [API 인증 형식](../04-reference/api.md#인증-요청응답)을 읽고 가짜 사용자·가입/로그인 입력·공개 응답을 pytest fixture로 만듭니다.
2. 검사에서 fixture를 실제로 불러 401과 확인된 사용자 ID 전달을 확인합니다. fixture만 검사하는 단계와 실제 Bearer·쿠키 세션 검사를 구분합니다. 현재 토큰·인증 테스트도 함께 확인합니다.
3. 후속 인증 검사에서 재사용할 import 위치와 실행 결과를 PR에 남깁니다.

완료 확인 예: 가짜 사용자 `101`의 성공 응답과 인증 없는 요청의 `401`을 각각 재현할 수 있고, 실제 비밀값이 없습니다.

## `davekim-dev`: #9 모델 → #8 조회 함수 인계

1. PM의 [공통 ORM 준비](../01-start/stack.md#개발과-초기-db)와 [#3](https://github.com/next-stop-siren/Next-Stop-Siren/issues/3)의 검증·병합 뒤 [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9)에서 대화·메시지 ORM 모델과 DB 제약 검사를 만듭니다. #9가 검증·병합되면 #8을 시작합니다.
2. [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8)에서는 확인된 사용자 ID로 소유자를 제한하는 ORM 조회 함수와 pytest 검사를 만듭니다. 두 사용자의 이력, 완료 답변 순서, 실패·중단 답변의 문맥 제외를 확인합니다.
3. [PR #25](https://github.com/next-stop-siren/Next-Stop-Siren/pull/25)의 가짜 데이터와 [PR #26](https://github.com/next-stop-siren/Next-Stop-Siren/pull/26)의 제약 예시는 검토 자료로 재사용하고 ORM 검사 결과를 PR에 남깁니다.

완료 확인 예: 사용자 `202`로 사용자 `101`의 대화를 조회하면 행이 나오지 않고, 완료된 답변만 다음 문맥에 포함됩니다.

## `gittul-123`: #16 이후 화면 인계

1. [#16](https://github.com/next-stop-siren/Next-Stop-Siren/issues/16), [서비스 범위](../01-start/service-scope.md), [화면 지침](../03-development/frontend.md)을 읽고 소개와 사람이 작성한 고정 예시를 구현합니다.
2. `useState`로 화면 상태를 표현하고 비회원에게 실제 질문 전송이나 개인 대화 기록 진입을 제공하지 않습니다. 고정 예시가 AI·DB 요청을 만들지 않게 합니다.
3. 화면 상태, 키보드 접근과 좁은 화면 확인, 비회원 접근 검사 결과를 PR에 남깁니다.

완료 확인 예: 로그인하지 않은 화면에서 예시는 볼 수 있지만 실제 전송·기록으로 들어갈 수 없고, 예시를 표시해도 AI·DB 호출이 없습니다.

## 다음 작업으로 넘어가기 전

PM 공통 ORM 준비와 #2 뒤 #3 인증 모델, #3 뒤 #9 대화 모델, #9 뒤 #8 조회 함수, #8 뒤 #10 API 순서입니다. #16 뒤 #17 로그인 화면에는 #5·#6·#7 결과도 필요합니다. 채팅과 화면의 첫 내부 목표는 **유료 AI 없이 모의 완성 답변을 저장하고 새로고침 뒤 다시 조회하는 것**입니다.

현재 #11 구현은 답변 기한 30초, 다음 POST에서 60초를 초과한 pending 회복 기준을 적용합니다. S11 / #12 시작 전에는 승인된 [GPT 연결 기준](../04-reference/ai.md)을 반드시 읽습니다. 완성 쌍 문맥 필터링과 GPT 어댑터는 구현·테스트되어 있고, 일반 앱 제공자는 계속 mock입니다. 스트리밍·수동 재시도 규칙, 회원 한도, 배포 Origin·키·Google 설정과 데이터 보존 기간도 각각 필요한 구현 전에 PM의 결정을 확인합니다. 결정되지 않은 값을 임의로 채우거나 기능이 이미 준비됐다고 표시하지 않습니다. Google callback URL은 API `:8000`이며 `GOOGLE_FRONTEND_ORIGIN`을 명시하면 성공 후 `:5173`으로 복귀합니다. 기존 `.env`의 수동 추가·API 재시작이 필요하고, 설정 없이는 상대 경로를 유지합니다. 화면의 CSRF·refresh와 로그인 상태 완료 연결은 아직 미구현입니다([#17](https://github.com/next-stop-siren/Next-Stop-Siren/issues/17), [#19](https://github.com/next-stop-siren/Next-Stop-Siren/issues/19)). 스트리밍 PR #44는 현재 기준 커밋에 포함되지 않았습니다. 진행 상태는 이슈와 PR에서 확인합니다.
