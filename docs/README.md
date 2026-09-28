# 팀 개발 지침

이 문서는 웹 챗봇을 함께 만드는 팀의 작업 기준이다. 프론트엔드는 React와 TypeScript, 백엔드는 FastAPI, 데이터 저장은 PostgreSQL을 사용한다. Google 로그인에는 OIDC를 사용하고, 로그인 후에는 애플리케이션의 접근·갱신 JWT를 사용한다. 하나의 저장소에서 프론트엔드와 백엔드를 관리하며 각각 배포할 수 있게 구성한다.

PM이 작업과 검토를 조정하고, 프론트엔드·인증 백엔드·채팅 및 데이터 백엔드 담당자가 이슈별로 구현하고 확인한다. GitHub Projects에서 이슈를 관리하며 `main`을 기준으로 이슈 브랜치와 PR을 사용한다.

- [역할과 인계](roles.md)
- [프론트엔드](frontend.md)
- [백엔드](backend.md)
- [품질과 설정](quality.md)
- [공통 개발 환경과 인계](environment.md)
- [협업 흐름](workflow.md)
