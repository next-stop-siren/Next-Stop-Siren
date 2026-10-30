# 처음 맡을 작업

## 2차 작업과 현재 인계 (2026-10-25)

담당별 권장 순서와 서로 병렬로 가능한 일은 [전체 공정 안내](development-flow.md#담당별-우선순서와-병렬-진행)를 따른다. 같은 사람이 맡은 작업을 모두 동시에 진행하라는 뜻은 아니다.

[2차 마일스톤](https://github.com/next-stop-siren/Next-Stop-Siren/milestone/2)에 새 구현·운영 작업 12개와 기존 #19·#54 최종검증을 연결했다. 등록 상태는 대기이며 개발을 시작했다는 뜻은 아니다. 기존 S01–S20 번호와 닫힌 목업의 완료 이력은 유지한다.

- `gittul-123`: [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65) 실제 회원 채팅 연결, [#67](https://github.com/next-stop-siren/Next-Stop-Siren/issues/67) 인증 모달, [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 화면을 맡는다.
- `davekim-dev`: [#66](https://github.com/next-stop-siren/Next-Stop-Siren/issues/66) 문맥 쌍 순서, [#70](https://github.com/next-stop-siren/Next-Stop-Siren/issues/70) 민감 로그 차단을 먼저 준비할 수 있다. [#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 실제 AI·긴 대화, [#72](https://github.com/next-stop-siren/Next-Stop-Siren/issues/72) 서비스 준비 확인, [#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75) 데이터·복구 작업도 맡는다. #70의 전체 추적은 [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65)·[#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 기능 연결 뒤 완성한다.
- `yejoo0310`: [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 서버, [#71](https://github.com/next-stop-siren/Next-Stop-Siren/issues/71) 가입 요청·해시 제한, [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74) 프록시 뒤 로그인 제한을 맡는다. 본인 인증 변경은 다른 담당자가 독립 확인한다.
- Owner `xifoxy-ru`가 [#73](https://github.com/next-stop-siren/Next-Stop-Siren/issues/73) 운영 구성과 [#76](https://github.com/next-stop-siren/Next-Stop-Siren/issues/76) 측정·장애·검증 자료 준비를 맡는다. [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74)는 `yejoo0310`, [#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75)는 `davekim-dev`와 운영 역할을 나눠 협업한다. #76의 실제 독립 확인은 구현자가 아닌 팀원이 수행하고 확인자·결과를 남긴다.

#19·#54는 관련 기능 구현과 담당 자체검사가 끝난 뒤 최종 통합검증을 진행한다. [PR #63](https://github.com/next-stop-siren/Next-Stop-Siren/pull/63)에 일부 결과가 제출돼 있으며 검토된 결과를 해당 범위에서 재사용한다. 실제 화면 격리와 전체 대기열 검증은 남아 있다. 운영·선택 리팩터링 전체를 기능 QA 선행으로 묶지 않는다.

PR #62의 서버 대기열과 PR #64의 Rev 1 문서는 완료된 기반이다. #18·#20·#21 닫힘은 목업 완료이며 실제 화면 연결은 #65에서 진행한다. 실제 AI·배포는 완료되지 않았다. 세부 동작과 고정 commit·Rev 참조는 각 이슈를 따른다.

## 함께 읽을 기준

1. [온보딩](../01-start/onboarding.md)으로 로컬 환경을 준비한다. 모델 10개의 등록과 기존 DB의 스키마 변경은 별개다. Windows 실제 실행은 아직 NOT RUN이다.
2. [API Rev 4](../04-reference/api.md)에서 접수·조회·구독·오류 계약을 읽는다. [정책](../04-reference/chat-policy.md)은 의도와 현재 구현 상태를 설명한다. [시나리오](../04-reference/chat-queue-api-draft.md)의 가짜 데이터로 화면·서버 경계를 준비한다.
3. [인증](../04-reference/authentication.md), [DB](../04-reference/database.md), [AI](../04-reference/ai.md)의 실제 구현과 남은 검증을 확인한다. PR #50에서 제외했던 Google 실패 복귀의 서버 처리·자체 검증은 #69의 `1cbf709`에 반영됐다. [프런트 연동 안내](../03-development/google-login-failure.md)를 참고하고 실패 화면 연결·실제 Google 전체 흐름·독립 검토는 별도로 확인한다. 실제 AI·배포는 완료되지 않았다.
4. [협업 흐름](../03-development/workflow.md)의 문서 Rev·커밋 고정 참조와 [품질 지침](../03-development/quality.md)의 검사 경계를 따른다. 기존 이슈의 완료 이력은 소급 수정하지 않는다.

## 담당별 작업과 완료 선행

현재 새 작업·담당·직접 선행은 [2차 공정 표](development-flow.md#2차-직접-선행)가 기준이다. 각 구현 담당자가 자체검사를 남기고 관련 결과를 #19·#54에 인계한다. 인증 담당 자신의 변경은 다른 담당자가 독립 확인한다. 기존 PR #63 자료는 검토된 범위에서 재사용한다.

#70은 민감 로그 차단부터 진행하고 전체 추적은 기능 연결 뒤 완성한다. #66 문맥 순서는 #68의 입력 길이·토큰 한도 결정을 기다리지 않는다. 운영 구현·검증 자료 준비는 Owner가 맡고, 제한 수치·백업 보존·복구 시간·알림 책임 등 남은 설정은 각 작업에서 결정한다. 실제 독립 확인은 구현하지 않은 팀원이 수행한다.

과거 S01–S20과 대기열의 직접 선행 기록은 [공정진행도](development-flow.md), 담당별 기록은 [인증 공정](auth-flow.md)·[채팅 공정](chat-flow.md)·[화면 공정](frontend-flow.md)에 보존한다. 그 표는 현재 미완료 목록이나 새 배정이 아니다. 서버의 한도·실행기 구현 검사를 실제 화면 통합·운영 검증으로 표시하지 않는다.

작업 인계에는 변경 동작, 기준 문서의 commit+Rev, 요청·응답, 필요한 설정, 실행한 검사와 미실행 사유를 남긴다. fixture는 가짜 입력·예상 결과이며 principal은 서버가 인증한 사용자 ID다. 화면이 보낸 사용자 ID로 소유권을 판단하지 않는다.
