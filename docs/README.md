# 문서 지도

현재 앱은 React 화면에서 FastAPI와 PostgreSQL의 준비 상태를 확인합니다. 제품 로그인·채팅·AI와 운영 배포는 아직 구현되지 않았습니다. 실행 명령과 현재 기능은 루트 [README](../README.md)에 있습니다.

| 폴더 | 문서 |
| --- | --- |
| [01-start/](01-start/) | [서비스 범위](01-start/service-scope.md) · [기술 구성](01-start/stack.md) · [환경](01-start/environment.md) · [온보딩](01-start/onboarding.md) |
| [02-team/](02-team/) | [역할](02-team/roles.md) · [팀 인계](02-team/team-handoff.md) · [공정진행도](02-team/development-flow.md) · [인증 공정](02-team/auth-flow.md) · [채팅 공정](02-team/chat-flow.md) · [화면 공정](02-team/frontend-flow.md) |
| [03-development/](03-development/) | [협업 흐름](03-development/workflow.md) · [품질](03-development/quality.md) · [서버](03-development/backend.md) · [화면](03-development/frontend.md) |
| [04-reference/](04-reference/) | [API](04-reference/api.md) · [DB](04-reference/database.md) · [인증](04-reference/authentication.md) · [DDL 참고안](04-reference/database-reference.sql) |

처음 참여하는 사람은 기술 구성 → 환경 → 온보딩 순서로 읽습니다. 담당자는 팀 인계에서 시작해 공정진행도와 자기 담당 공정을 봅니다. 구현자는 관련 API·DB·인증 기준을 확인하고, PR 전에는 협업 흐름과 품질 지침을 확인합니다. 참조 문서는 후속 구현 기준이며 DDL 참고안은 실행된 스키마가 아닙니다.
