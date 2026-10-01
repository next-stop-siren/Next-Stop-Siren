# 팀 개발 문서 안내

이 저장소는 React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL을 사용하는 챗봇 프로젝트의 공통 개발 규칙을 담는다. 현재 앱은 API·DB 연결 확인 단계다. 초기 앱, 의존성 잠금 파일과 로컬 실행 명령은 루트 [README](../README.md)에 있다.

처음 참여한다면 [공통 환경](environment.md)에서 설치 전제를 확인하고 [실행 체크리스트](onboarding.md)를 따른다. 변경 작업은 [협업 흐름](workflow.md)과 [품질 지침](quality.md)을 확인한다. 아래 문서의 로그인·채팅 계획은 후속 단계의 기준이며 현재 동작을 설명하지 않는다.

읽는 순서는 다음과 같다.

1. [공통 환경](environment.md)과 [실행 체크리스트](onboarding.md)로 현재 앱을 실행한다.
2. [역할](roles.md)과 [처음 맡을 작업](team-handoff.md)에서 담당 범위와 첫 이슈를 확인한다.
3. [서비스 범위](service-scope.md)를 읽고 담당 분야의 [화면](frontend.md) 또는 [서버](backend.md) 지침을 따른다.
4. 구현 전에 [API](api.md)·[DB](database.md)·[인증](authentication.md) 기준을 확인하고, PR 전에는 [협업 흐름](workflow.md)과 [품질 지침](quality.md)을 확인한다.

문서를 고칠 때는 한 문단에 한 주제를 두고 긴 절은 짧은 제목과 목록으로 나눈다. 필드 설명 표는 한 필드에 한 행을 쓰며, 현재 동작과 앞으로 구현할 기준을 구분한다.

- [처음 맡을 작업과 인계](team-handoff.md)
- [서비스 범위와 첫 작업](service-scope.md)
- [데이터베이스 설계](database.md)
- [공통 API 형식과 모의 응답](api.md)
- [인증 구현 기준](authentication.md)
- [공통 환경과 초기 준비 절차](environment.md)
- [macOS·Windows 실행 체크리스트](onboarding.md)
- [프론트엔드](frontend.md)
- [백엔드와 API](backend.md)
- [품질·테스트·비밀값](quality.md)
- [협업 흐름](workflow.md)
- [역할과 인계](roles.md)
