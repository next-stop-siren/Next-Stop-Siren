# 담당 역할과 인계

## 2차 작업과 현재 인계 (2026-10-25)

담당별 권장 순서와 서로 병렬로 가능한 일은 [전체 공정 안내](development-flow.md#담당별-우선순서와-병렬-진행)를 따른다. 같은 사람이 맡은 작업을 모두 동시에 진행하라는 뜻은 아니다.

[2차 마일스톤](https://github.com/next-stop-siren/Next-Stop-Siren/milestone/2)에 새 구현·운영 작업 12개와 기존 #19·#54 최종검증을 연결했다. 등록 상태는 대기이며 개발을 시작했다는 뜻은 아니다. 기존 S01–S20 번호와 닫힌 목업의 완료 이력은 유지한다.

- `gittul-123`: [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65) 실제 회원 채팅 연결, [#67](https://github.com/next-stop-siren/Next-Stop-Siren/issues/67) 인증 모달, [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 화면을 맡는다.
- `davekim-dev`: [#66](https://github.com/next-stop-siren/Next-Stop-Siren/issues/66) 문맥 쌍 순서, [#70](https://github.com/next-stop-siren/Next-Stop-Siren/issues/70) 민감 로그 차단을 먼저 준비할 수 있다. [#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 실제 AI·긴 대화, [#72](https://github.com/next-stop-siren/Next-Stop-Siren/issues/72) 서비스 준비 확인, [#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75) 데이터·복구 작업도 맡는다. #70의 전체 추적은 [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65)·[#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 기능 연결 뒤 완성한다.
- `yejoo0310`: [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 서버, [#71](https://github.com/next-stop-siren/Next-Stop-Siren/issues/71) 가입 요청·해시 제한, [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74) 프록시 뒤 로그인 제한을 맡는다. 본인 인증 변경은 다른 담당자가 독립 확인한다.
- Owner `xifoxy-ru`가 [#73](https://github.com/next-stop-siren/Next-Stop-Siren/issues/73) 운영 구성과 [#76](https://github.com/next-stop-siren/Next-Stop-Siren/issues/76) 측정·장애·검증 자료 준비를 맡는다. [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74)는 `yejoo0310`, [#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75)는 `davekim-dev`와 운영 역할을 나눠 협업한다. #76의 실제 독립 확인은 구현자가 아닌 팀원이 수행하고 확인자·결과를 남긴다.

#19·#54는 관련 기능 구현과 담당 자체검사가 끝난 뒤 최종 통합검증을 진행한다. [PR #63](https://github.com/next-stop-siren/Next-Stop-Siren/pull/63)에 일부 결과가 제출돼 있으며 검토된 결과를 해당 범위에서 재사용한다. 실제 화면 격리와 전체 대기열 검증은 남아 있다. 운영·선택 리팩터링 전체를 기능 QA 선행으로 묶지 않는다.

PR #62의 서버 대기열과 PR #64의 Rev 1 문서는 완료된 기반이다. #18·#20·#21 닫힘은 목업 완료이며 실제 화면 연결은 #65에서 진행한다. 실제 AI·배포는 완료되지 않았다. 세부 동작과 고정 commit·Rev 참조는 각 이슈를 따른다.

#54의 검증 담당은 런타임 구현을 맡지 않는다. #19는 인증·소유권과 비공개 화면 격리를 담당한다. 기존 완료 이슈의 담당·범위 이력을 유지한다.

- **PM (`xifoxy-ru`):** 이슈와 완료 기준을 정리하고 공통 설정·통합 결과를 검토한다.
- **프론트엔드 (`gittul-123`):** 로그인 상태와 채팅 화면·API 연결을 맡는다.
- **인증 백엔드 (`yejoo0310`):** 이메일·비밀번호 및 Google 로그인의 계정 식별, OIDC 검증, 접근·갱신 JWT와 접근 제어를 맡는다.
- **채팅·데이터 백엔드 (`davekim-dev`):** 외부 AI 연결과 사용자별 대화 저장·조회를 맡는다.

두 백엔드 담당자는 서버에서 확인한 사용자 정보와 데이터 경계를 함께 검토한다.

작업을 넘길 때 변경한 동작, 요청·응답 형식, 필요한 설정, 실행한 확인과 결과를 이슈나 PR에 남긴다. 경계가 바뀌면 영향을 받는 담당자와 함께 확인한다. 담당자가 자신의 변경을 먼저 검사하고 PM과 관련 담당자가 검토한다.

결함은 담당자가 수정하고 같은 흐름을 다시 확인한다.

이슈는 요구 동작이 재현되고 정상·실패 확인 결과가 남으며 관련 문서와 인계 정보가 갱신되면 완료한다.
