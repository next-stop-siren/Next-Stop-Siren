# 채팅·데이터·AI 공정

## 2차 작업과 현재 인계 (2026-10-25)

[2차 마일스톤](https://github.com/next-stop-siren/Next-Stop-Siren/milestone/2)에 새 구현·운영 작업 12개와 기존 #19·#54 최종검증을 연결했다. 등록 상태는 대기이며 개발을 시작했다는 뜻은 아니다. 기존 S01–S20 번호와 닫힌 목업의 완료 이력은 유지한다.

- `gittul-123`: [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65) 실제 회원 채팅 연결, [#67](https://github.com/next-stop-siren/Next-Stop-Siren/issues/67) 인증 모달, [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 화면을 맡는다.
- `davekim-dev`: [#66](https://github.com/next-stop-siren/Next-Stop-Siren/issues/66) 문맥 쌍 순서, [#70](https://github.com/next-stop-siren/Next-Stop-Siren/issues/70) 민감 로그 차단을 먼저 준비할 수 있다. [#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 실제 AI·긴 대화, [#72](https://github.com/next-stop-siren/Next-Stop-Siren/issues/72) 서비스 준비 확인, [#75](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75) 데이터·복구 작업도 맡는다. #70의 전체 추적은 [#65](https://github.com/next-stop-siren/Next-Stop-Siren/issues/65)·[#68](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68) 기능 연결 뒤 완성한다.
- `yejoo0310`: [#69](https://github.com/next-stop-siren/Next-Stop-Siren/issues/69) Google 실패 서버, [#71](https://github.com/next-stop-siren/Next-Stop-Siren/issues/71) 가입 요청·해시 제한, [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74) 프록시 뒤 로그인 제한을 맡는다. 본인 인증 변경은 다른 담당자가 독립 확인한다.
- [#73](https://github.com/next-stop-siren/Next-Stop-Siren/issues/73) EC2 구성과 [#76](https://github.com/next-stop-siren/Next-Stop-Siren/issues/76) 운영 부하·장애·알림의 운영 담당은 미배정이다. [#74](https://github.com/next-stop-siren/Next-Stop-Siren/issues/74)·#75의 운영 협업 역할도 별도 지정해야 한다.

#19·#54는 관련 기능 구현과 담당 자체검사가 끝난 뒤 최종 통합검증을 진행한다. [PR #63](https://github.com/next-stop-siren/Next-Stop-Siren/pull/63)에 일부 결과가 제출돼 있으며 검토된 결과를 해당 범위에서 재사용한다. 실제 화면 격리와 전체 대기열 검증은 남아 있다. 운영·선택 리팩터링 전체를 기능 QA 선행으로 묶지 않는다.

PR #62의 서버 대기열과 PR #64의 Rev 1 문서는 완료된 기반이다. #18·#20·#21 닫힘은 목업 완료이며 실제 화면 연결은 #65에서 진행한다. 실제 AI·배포는 완료되지 않았다. 세부 동작과 고정 commit·Rev 참조는 각 이슈를 따른다.

이 담당의 새 작업: [대화 문맥을 질문·완료 답변 쌍 순서로 구성하기](https://github.com/next-stop-siren/Next-Stop-Siren/issues/66), [실제 AI 답변을 연결하고 긴 대화 처리하기](https://github.com/next-stop-siren/Next-Stop-Siren/issues/68), [민감 정보 없는 오류 로그와 작업 단계 추적 보완하기](https://github.com/next-stop-siren/Next-Stop-Siren/issues/70), [서비스가 실제 요청을 받을 준비가 됐는지 확인하기](https://github.com/next-stop-siren/Next-Stop-Siren/issues/72), [데이터를 보존하며 배포하고 장애 시 복구하는 절차 만들기](https://github.com/next-stop-siren/Next-Stop-Siren/issues/75).

현재 직접 선행은 [2차 전체 표](development-flow.md#2차-직접-선행)를 따른다. 아래 그림은 기존 작업의 역사적 기록이다.

기존 S01–S20 그림과 표는 **대기열 도입 전 공정 기록**이다. 문서 끝의 승인된 대기열 목표 표·그림도 당시 계획의 직접 선행 기록으로 보존한다. 이력의 선행 간선을 실제 완료·미완료 목록으로 읽지 않는다. 현재 인계와 실제 이슈·PR 상태를 먼저 확인한다.

담당: **`davekim-dev`** · [전체 공정과 직접 선행·후속 표](development-flow.md)

첫 공동 목표는 **로그인 → 모의 완성 답변 → 저장 → 새로고침 뒤 이력 재조회 → #19 연동**이다. 기존 그림은 대기열 도입 전 인계를 기록한다. 목표의 필수 선행은 문서 끝의 목표 그림과 전체 목표 표를 따르며, 점선 상자는 다른 담당자의 작업이다. 이슈 상태와 PR 병합 여부는 GitHub에서 확인한다.

## 작업 흐름

```mermaid
flowchart TD
  N3["#3 인증 ORM 모델<br/>외부: 인증 / yejoo0310"]
  N8["#8 내 대화 ORM 조회 함수·테스트"]
  N9["#9 대화 ORM 모델"]
  N10["#10 대화 생성·목록·이력 API"]
  N11["#11 모의 답변 저장·재조회"]
  N12["#12 실제 AI 어댑터·제한 시험"]
  N13["#13 스트리밍"]
  N14["#14 수동 재시도"]
  N15["#15 회원 한도·중복 보호"]
  N3 --> N9
  N9 --> N8
  N8 --> N10
  N10 --> N11
  N11 --> N12
  N11 --> N13
  N11 --> N14
  N10 --> N15
  N11 --> N15
  classDef external stroke-dasharray:5 4;
  class N3 external;
  click N3 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/3" "이슈 #3 열기"
  click N8 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/8" "이슈 #8 열기"
  click N9 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/9" "이슈 #9 열기"
  click N10 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/10" "이슈 #10 열기"
  click N11 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/11" "이슈 #11 열기"
  click N12 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/12" "이슈 #12 열기"
  click N13 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/13" "이슈 #13 열기"
  click N14 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/14" "이슈 #14 열기"
  click N15 "https://github.com/next-stop-siren/Next-Stop-Siren/issues/15" "이슈 #15 열기"
```

## 내 이슈

| 이슈 | 작업 |
| --- | --- |
| [#9](https://github.com/next-stop-siren/Next-Stop-Siren/issues/9) | 대화 ORM 모델 |
| [#8](https://github.com/next-stop-siren/Next-Stop-Siren/issues/8) | 내 대화 ORM 조회 함수·테스트 |
| [#10](https://github.com/next-stop-siren/Next-Stop-Siren/issues/10) | 대화 생성·목록·이력 API |
| [#11](https://github.com/next-stop-siren/Next-Stop-Siren/issues/11) | 모의 답변 저장·재조회 |
| [#12](https://github.com/next-stop-siren/Next-Stop-Siren/issues/12) | 실제 AI 어댑터·제한 시험 |
| [#13](https://github.com/next-stop-siren/Next-Stop-Siren/issues/13) | 스트리밍 |
| [#14](https://github.com/next-stop-siren/Next-Stop-Siren/issues/14) | 수동 재시도 |
| [#15](https://github.com/next-stop-siren/Next-Stop-Siren/issues/15) | 회원 한도·중복 보호 |

## 인계와 참고 문서

- **#3 → #9:** 검증·병합된 인증 ORM 모델을 받아 대화 ORM 모델을 만든다. 인증 기능 전체 완료는 #9의 조건이 아니다.
- **#9 → #8 → #10:** 대화 ORM 모델 뒤 소유권을 포함한 조회 함수와 자동 검사를 만들고, #10의 API에서 함수를 재사용한다. 기존 [PR #25](https://github.com/next-stop-siren/Next-Stop-Siren/pull/25)의 가짜 행·예상 결과와 [PR #26](https://github.com/next-stop-siren/Next-Stop-Siren/pull/26)의 제약 검사 초안은 검토 자료다. 새 결과는 ORM 기준으로 검증한다.
- **#11 → #12·#13·#14·#15·#18:** 모의 완성 답변의 저장·재조회 경로를 먼저 검증하고 결과를 화면 담당에게 인계한다. #15에는 #10도 필요하다.
- **S11 / #12 시작 전:** [GPT 연결 기준](../04-reference/ai.md)을 반드시 읽는다. 제공자·고정 모델·개인 할당·30초 기한·자동 재시도 금지와 완성 쌍 문맥 기준을 유지한다. 어댑터·필터링·격리 검사 코드는 현재 있고 일반 앱은 mock을 유지한다. #11은 S10, #13은 S12다.
- **승인된 목표:** [회원 채팅 정책](../04-reference/chat-policy.md)의 서버 대기열·한도 결합은 PR #62에 구현됐다. 실제 화면 연결은 남아 있다.

[백엔드 지침](../03-development/backend.md) · [DB 설계](../04-reference/database.md) · [API 형식](../04-reference/api.md)

## 승인된 대기열 목표 인계

정책 의도와 현재 구현 경계를 구분하는 [회원 채팅 정책](../04-reference/chat-policy.md)을 따른다. 실제 연동 완료의 전체 직접 선행은 [공정진행도](development-flow.md#승인된-대기열-목표-작업의-직접-선행)가 기준이다. 아래 그림은 이 담당 작업에 닿는 동일한 직접 간선만 표시한다. 공유 인터페이스 계획 뒤 mock 준비는 먼저 가능하며 실제 완료에는 선행 결과의 검증·병합이 필요하다.

```mermaid
flowchart TD
  Q10["#10 대화 API"]
  Q11["#11 모의 답변"]
  Q15["#15 한도·정산"]
  Q18["#18 채팅·대기열 화면"]
  Q19["#19 인증·소유권 통합"]
  Q20["#20 구독·재접속 화면"]
  Q21["#21 재시도·건너뛰기 화면"]
  Q52["#52 영속 대기열·실행·복구"]
  Q53["#53 대기열 API 통합"]
  Q54["#54 독립 통합·결함 검증"]
  Q10 --> Q15
  Q11 --> Q15
  Q53 --> Q18
  Q53 --> Q19
  Q53 --> Q20
  Q53 --> Q21
  Q10 --> Q52
  Q11 --> Q52
  Q15 --> Q53
  Q52 --> Q53
  Q53 --> Q54
```
