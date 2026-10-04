# 서비스 범위와 첫 작업

현재 앱은 화면에서 API와 DB 연결만 확인한다. 아래 기능은 후속 작업이며 아직 동작하지 않는다.

## 확정된 범위

- 이메일·비밀번호 가입과 Google 로그인을 제공한다.
- 이메일 인증과 비밀번호 찾기는 후속 범위로 둔다.
- 같은 이메일의 Google 계정과 로컬 계정은 별도 계정으로 유지한다.
- 이메일을 기준으로 계정을 자동 연결하지 않는다.
- 비회원에게는 소개와 사람이 작성한 고정 예시만 보여 준다.
- 로그인한 사용자에게만 실제 채팅과 대화 기록을 제공한다.
- 화면은 React `useState`, 서버는 FastAPI, 저장소는 PostgreSQL과 SQLAlchemy 2.x ORM을 사용한다. 선택한 구성과 현재 구현의 차이는 [기술 구성](stack.md)에 있다.
- 첫 내부 목표는 모의 제공자의 완성 답변을 저장하고 다시 조회하는 것이다.

## 데이터와 결정 경계

데이터의 논리 구조는 `users`, `auth_identities`, `refresh_sessions`, `conversations`, `messages` 다섯 테이블이다. 사용자 소유권은 서버에서 확인한 사용자 정보(서버가 로그인 자격을 확인해 얻은 사용자 ID)를 기준으로 검사한다.

컬럼·관계·담당 순서와 구현 전 결정 경계는 [데이터베이스 설계](database.md)에 있다. 초기 범위에는 계정·대화 삭제 기능이 없고, FK `ON DELETE RESTRICT`는 참조되는 행의 삭제를 막는다.

가짜 테스트 데이터는 가짜 사용자와 응답만 사용한다.

다음 결정과 구현 검증이 남아 있다.

- 초기 ORM 모델·빈 개발/테스트 DB 초기화 검증, 이후 스키마 변경 절차와 데이터 보존 기간·일괄 삭제·백업 처리
- 승인된 [인증 구현 기준](authentication.md)의 실제 구현과 검증, 배포 Origin·키·Google client 및 로그인 제한 방식
- AI 제공자, 시험 비용, 회원 한도
- 스트리밍과 수동 재시도 규칙

이 문서는 실행된 DB 초기화나 인증 기능을 뜻하지 않는다. D1 A·B·C 인증 정책은 승인되어 문서화됐고, 운영 입력과 구현 검증은 남아 있다. 실제 구현 이슈는 선행 작업을 확인한 뒤 시작한다.

## 처음 맡을 수 있는 작업

담당자별 실제 이슈 번호와 시작 순서는 [팀 작업 인계](team-handoff.md)에 있다.

1. 인증 백엔드 `yejoo0310`: [#2](https://github.com/next-stop-siren/Next-Stop-Siren/issues/2)에서 후속 검사에 재사용할 pytest fixture를 만들고 인증 없는 요청의 401 응답을 확인한다.
2. 채팅·데이터 백엔드 `davekim-dev`: 공통 ORM 준비가 main에 병합됐다. [#3](https://github.com/next-stop-siren/Next-Stop-Siren/issues/3)의 검증·병합 뒤 [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9)에서 대화·메시지 ORM 모델을 만들고 DB 제약을 검사한다. #9 검증·병합 뒤 [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8)의 소유권 조회 함수와 pytest 검사를 맡는다.
3. 프론트엔드 `gittul-123`: [#16](https://github.com/next-stop-siren/Next-Stop-Siren/issues/16)에서 비회원 소개와 사람이 작성한 고정 예시 화면을 만든다.

담당 역할은 [역할과 인계](roles.md)를, 구현과 검증 흐름은 [협업 안내](workflow.md)를 확인한다.
