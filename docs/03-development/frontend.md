# 프론트엔드 작업 지침

## 프런트엔드만 준비하고 실행하기

화면 작업에는 Git과 Node.js/npm만 필요하다. macOS는 저장소 루트에서 다음을 실행한다.

```sh
./local.sh frontend-setup
./local.sh frontend-dev
# 별도 터미널에서
./local.sh frontend-check
./local.sh frontend-test
./local.sh frontend-build
```

Windows PowerShell에서는 같은 명령 이름을 `.\local.ps1 frontend-setup`, `.\local.ps1 frontend-dev`, `.\local.ps1 frontend-check`, `.\local.ps1 frontend-test`, `.\local.ps1 frontend-build`로 실행한다.

`frontend-setup`은 고정 Node.js 24.21.0/npm 11.19.0을 프로젝트 캐시에 설치·검증하고 `npm ci`만 실행한다. Python·uv·백엔드·Docker·`.env`·인증 키 없이 실행하며 Playwright 브라우저는 설치하지 않는다. `B71_NODE`/`B71_NPM`으로 지정한 도구도 실행 버전이 일치해야 한다. `frontend-check`는 Biome 포맷·린트·타입, `frontend-test`는 프런트엔드 단위 검사, `frontend-build`는 정적 빌드를 수행한다.

`frontend-dev`는 http://127.0.0.1:5173 에서 실행하며 `Ctrl+C`로 이 명령이 시작한 화면 프로세스를 종료한다. 포트가 사용 중이면 다른 포트로 넘어가지 않고 실패한다. 기존 프로세스의 소유자를 확인해 수동으로 중지한다. `stop`은 DB용 명령이며 화면 종료 명령이 아니다.

현재 UI의 정적 예시·임시 상태와 단위 검사의 mock은 화면 개발용이다. 이 경로는 API 서버나 실제 로그인·저장·Google 연동을 제공하지 않는다. `/api` 요청은 기존처럼 127.0.0.1:8000으로 전달되므로 API가 없으면 실패한다. 실제 API·DB 준비와 현재 화면 연결의 한계는 [온보딩](../01-start/onboarding.md#전체-환경apidb-연동)을 따른다.

React 컴포넌트는 화면과 입력을 담당하고, API 호출·응답 변환은 별도 모듈로 둔다. TypeScript 타입은 화면 상태와 합의한 요청·응답 형식에 맞춘다. 기능별 코드는 `src/features/<기능>/`에 모으고 공유 UI는 `src/components/`, 공통 API 코드는 `src/lib/`처럼 역할이 드러나게 배치한다.

실제 폴더를 만들 때 사용처가 없는 추상 계층은 만들지 않는다.

## 화면 상태와 접근

컴포넌트 파일은 `PascalCase.tsx`, 훅은 `useName.ts`, 일반 함수·변수는 `camelCase`, 타입은 `PascalCase`를 쓴다.

후속 채팅 화면은 `useState`로 로그인 상태와 질문 전송의 로딩·성공·오류 상태를 표현한다. 비회원에게는 서비스 소개와 고정 예시만 보이고, 실제 질문·답변과 개인 기록은 로그인 후 제공한다.

질문 입력, 답변, 기록 조회에는 비어 있음·진행 중·실패 상태를 표현하고, 실패 후 입력을 유지해 다시 시도할 수 있게 한다. 빈 질문은 화면에서 안내하고 서버 검사도 유지한다. 사용자 및 외부 서비스에서 온 글은 신뢰하지 않고 텍스트로 렌더링한다.

키보드 접근, 입력 이름, 초점, 오류 안내와 좁은 화면을 확인한다. 브라우저 코드에 서버 비밀값을 넣지 않는다.

## 변경 확인

PR 전에는 저장소에 마련된 Biome 포맷·린트, TypeScript 검사와 관련 테스트를 실행하고 결과를 남긴다. 실제 명령은 `frontend/package.json`의 `format:check`, `lint`, `typecheck`다. 자동 포맷이 많은 파일을 바꾸면 기능 변경과 구분해 검토한다.

테스트 기준은 [품질 지침](quality.md)을 따른다.
