# 처음 맡을 작업

현재 앱은 화면에서 API·DB 연결 상태를 확인하는 단계입니다. 로그인, 채팅, AI 호출은 아직 동작하지 않습니다. 인증 #2와 화면 #16은 공통 준비와 병행할 수 있습니다. 채팅은 PM의 공통 ORM 준비 및 #3·#9 모델 결과 뒤 #8을 시작합니다. 작업 전에 실제 이슈 본문과 PM이 확인한 저장소의 최신 문서를 확인하세요.

전체 직접 선행·후속 이슈는 [공정진행도](development-flow.md)에, 각 담당자의 작업과 외부 인계는 아래 담당 공정에 있습니다. 실제 진행·차단·검토 상태는 연결된 GitHub 이슈와 PR에서 확인하세요.

| 담당 | 첫 이슈 | 담당 공정 | 이번에 남길 결과 |
| --- | --- | --- | --- |
| `yejoo0310` | [#2](https://github.com/next-stop-siren/Next-Stop-Siren/issues/2) | [인증](auth-flow.md) | 후속 검사에서 불러 쓸 인증 pytest 값과 401 예시 |
| `davekim-dev` | [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9) → [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8) | [채팅·데이터·AI](chat-flow.md) | 대화 ORM 모델 뒤 소유권 조회 함수·자동 검사 |
| `gittul-123` | [#16](https://github.com/next-stop-siren/Next-Stop-Siren/issues/16) | [화면](frontend-flow.md) | 비회원 소개와 사람이 작성한 고정 예시 화면 |

## 함께 읽을 기준

1. [실행 체크리스트](onboarding.md)로 로컬 앱을 준비하고 현재 동작을 확인합니다. macOS 경로는 검증됐고 Windows 실제 실행은 **NOT RUN**입니다.
2. [서비스 범위](service-scope.md)에서 비회원·회원 경계와 첫 내부 목표를 확인합니다. [API 요청·응답](api.md), [DB 설계](database.md), [인증 구현 기준](authentication.md)은 향후 구현의 공통 기준입니다. 현재 라우트·테이블이 이미 있다는 뜻은 아닙니다.
3. 이슈에 적힌 범위대로 변경하고, [협업 흐름](workflow.md)에 따라 PR에 결과와 검사 기록을 남깁니다. PM `xifoxy-ru`가 공통 API·DB·보안 형식과 미정 결정을 정리하고 PR을 검토합니다. 각 담당자는 자기 구현과 검사를 맡습니다.

여기서 **fixture**는 pytest 검사에서 불러 쓰는 가짜 입력·예상 결과입니다. **ORM 모델**은 Python 클래스와 DB 테이블의 연결 정의이며, **DB 세션**은 한 작업의 DB 읽기·쓰기를 묶는 객체입니다. **확인된 사용자 정보(principal)**는 서버가 로그인 자격을 검사해 얻은 사용자 ID입니다. 화면에서 보낸 `user_id`만으로 소유권을 판단하지 않습니다.

## `yejoo0310`: #2 재사용 인증 검사 값

1. [#2](https://github.com/next-stop-siren/Next-Stop-Siren/issues/2)와 [API 인증 형식](api.md#인증-요청응답--후속-구현-기준)을 읽고 가짜 사용자·가입/로그인 입력·공개 응답을 pytest fixture로 만듭니다.
2. 검사에서 fixture를 실제로 불러 401과 확인된 사용자 ID 전달을 확인합니다. 실제 토큰 발급은 필요하지 않습니다.
3. 후속 인증 검사에서 재사용할 import 위치와 실행 결과를 PR에 남깁니다.

완료 확인 예: 가짜 사용자 `101`의 성공 응답과 인증 없는 요청의 `401`을 각각 재현할 수 있고, 실제 비밀값이 없습니다.

## `davekim-dev`: #9 모델 뒤 #8 조회 함수

1. PM의 [공통 ORM 준비](stack.md#개발과-초기-db)와 [#3](https://github.com/next-stop-siren/Next-Stop-Siren/issues/3)의 검증·병합 뒤 [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9)에서 대화·메시지 ORM 모델과 DB 제약 검사를 만듭니다. #9가 검증·병합되면 #8을 시작합니다.
2. [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8)에서는 확인된 사용자 ID로 소유자를 제한하는 ORM 조회 함수와 pytest 검사를 만듭니다. 두 사용자의 이력, 완료 답변 순서, 실패·중단 답변의 문맥 제외를 확인합니다.
3. [PR #25](https://github.com/next-stop-siren/Next-Stop-Siren/pull/25)의 가짜 데이터와 [PR #26](https://github.com/next-stop-siren/Next-Stop-Siren/pull/26)의 제약 예시는 검토 자료로 재사용하고 ORM 검사 결과를 PR에 남깁니다.

완료 확인 예: 사용자 `202`로 사용자 `101`의 대화를 조회하면 행이 나오지 않고, 완료된 답변만 다음 문맥에 포함됩니다.

## `gittul-123`: #16 비회원 화면

1. [#16](https://github.com/next-stop-siren/Next-Stop-Siren/issues/16), [서비스 범위](service-scope.md), [화면 지침](frontend.md)을 읽고 소개와 사람이 작성한 고정 예시를 구현합니다.
2. `useState`로 화면 상태를 표현하고 비회원에게 실제 질문 전송이나 개인 대화 기록 진입을 제공하지 않습니다. 고정 예시가 AI·DB 요청을 만들지 않게 합니다.
3. 화면 상태, 키보드 접근과 좁은 화면 확인, 비회원 접근 검사 결과를 PR에 남깁니다.

완료 확인 예: 로그인하지 않은 화면에서 예시는 볼 수 있지만 실제 전송·기록으로 들어갈 수 없고, 예시를 표시해도 AI·DB 호출이 없습니다.

## 다음 작업으로 넘어가기 전

PM 공통 ORM 준비와 #2 뒤 #3 인증 모델, #3 뒤 #9 대화 모델, #9 뒤 #8 조회 함수, #8 뒤 #10 API 순서입니다. #16 뒤 #17 로그인 화면에는 #5·#6·#7 결과도 필요합니다. 채팅과 화면의 첫 내부 목표는 **유료 AI 없이 모의 완성 답변을 저장하고 새로고침 뒤 다시 조회하는 것**입니다.

멈춘 답변을 회복할 유한 기한은 #11 시작 전에 PM이 정해야 합니다. 실제 AI 제공자·시험 비용, 스트리밍·수동 재시도 규칙, 회원 한도, 배포 Origin·키·Google 설정과 데이터 보존 기간도 각각 필요한 구현 전에 PM의 결정을 확인합니다. 결정되지 않은 값을 임의로 채우거나 기능이 이미 준비됐다고 표시하지 않습니다. 진행 상태는 이슈와 PR에서 확인합니다.
