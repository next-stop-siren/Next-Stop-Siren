# 문서 지도

현재 코드/운영 인계는 main `f3df305a` (2026-11-25 KST)의 [공정진행도](02-team/development-flow.md)와 [배포 증거·리허설·다음 확인](../deploy/README.md)을 따른다. 실제 회원 채팅·SSE는 연결됐으며 시험 배포 앱은 `fe2ae6d`의 mock이다. 최신 main 병합과 운영 이미지 적용은 다르다. 실제 AI와 Google 신규 가입·재로그인은 미검증이다. 각 문서의 날짜·Rev·고정 SHA는 해당 시점의 기준/역사를 보존한다.

HTTP 경로·요청·응답·오류·이벤트의 단일 계약은 [API](04-reference/api.md), 수량·기간·정산·대기·재시도 정책과 알려진 차이는 [회원 채팅 정책](04-reference/chat-policy.md), 고유 예시와 화면 확인 목록은 [채팅 시나리오](04-reference/chat-queue-api-draft.md)에 둔다. [팀 인계](02-team/team-handoff.md)에서 현재 담당별 다음 작업을 확인한다.

| 폴더 | 문서 |
| --- | --- |
| [01-start/](01-start/) | [서비스 범위](01-start/service-scope.md) · [기술 구성](01-start/stack.md) · [환경](01-start/environment.md) · [온보딩](01-start/onboarding.md) |
| [02-team/](02-team/) | [역할](02-team/roles.md) · [팀 인계](02-team/team-handoff.md) · [공정진행도](02-team/development-flow.md) · [인증 공정](02-team/auth-flow.md) · [채팅 공정](02-team/chat-flow.md) · [화면 공정](02-team/frontend-flow.md) |
| [03-development/](03-development/) | [협업 흐름](03-development/workflow.md) · [품질](03-development/quality.md) · [서버](03-development/backend.md) · [화면](03-development/frontend.md) |
| [04-reference/](04-reference/) | [API](04-reference/api.md) · [DB](04-reference/database.md) · [인증](04-reference/authentication.md) · [S11 GPT](04-reference/ai.md) · [DDL 참고안](04-reference/database-reference.sql) |

처음 참여하는 사람은 기술 구성 → 환경 → 온보딩 순서로 읽습니다. 담당자는 팀 인계에서 시작해 공정진행도와 자기 담당 공정을 봅니다. 구현자는 관련 API·DB·인증 기준을 확인하고, PR 전에는 협업 흐름과 품질 지침을 확인합니다. 참조 문서는 현재 백엔드 계약과 후속 구현 기준을 구분합니다. DDL 참고안은 실행된 스키마가 아니며 로컬 제품 테이블은 온보딩의 `db-init`으로 준비합니다.
