# 공통 개발 환경

현재 #52 기반과 #15 한도를 #53 API에 연결한 PR #62가 병합됐다. `CHAT_WORKER=off`는 테스트에서 실행기를 끄는 설정이며 일반 앱은 시작 시 실행기를 띄운다. `db-init`은 기존 테이블을 마이그레이션하지 않으며 공개 제공자는 mock이다.

## 기준과 소유

이 저장소의 구현 기준은 React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL이다. 선택한 ORM·인증 라이브러리와 현재 의존성의 차이는 [기술 구성](stack.md)에 정리했다. 공통 설정은 PM이 검토한다. 팀원은 같은 저장소와 잠금 파일을 사용하며 개인별로 프로젝트를 다시 초기화하지 않는다.

설치·API 확인 순서는 [온보딩](onboarding.md)에 있다. 과거 macOS Colima의 DB 런타임 검증 기록과 이번 소스·문서 대조를 구분한다. Windows 실행은 아직 **NOT RUN**이다.

### Node.js·npm

- **채택 기준:** Node.js 24.21.0 LTS와 함께 제공되는 npm 11.19.0
- **현재 잠금 파일:** `frontend/package-lock.json`

프런트엔드만 작업할 때는 `frontend-setup`/`frontend-dev`와 `frontend-check`/`frontend-test`/`frontend-build`를 사용한다. 이 경로는 Node.js/npm만 준비하며 아래 Python·uv·PostgreSQL·Docker, `.env`와 인증 키는 요구하지 않는다. Playwright 브라우저도 설치하지 않는다. 전체 API 연결은 별도 `setup`/`dev`가 필요하다. 명령 예시는 [온보딩](onboarding.md#프런트엔드만-준비하고-실행하기)에 있다.

### Python·uv

- **채택 기준:** Python 3.13.15 안정판과 uv 0.11.19
- **현재 잠금 파일:** `backend/uv.lock`

### PostgreSQL

- **채택 기준:** 지원 중인 17.11 안정판
- **현재 구성:** `compose.yaml`의 개발·테스트 DB 서비스

전체 환경에서는 macOS 터미널과 Windows PowerShell에서 Node.js, npm, Python, uv, Docker/Compose, PostgreSQL 서버의 실제 버전을 출력해 기록한다. 설치된 버전이 저장소의 고정 버전과 다르면 설치를 멈추고 안내에 맞춘다.

npm은 잠금 파일을 따르는 깨끗한 설치, uv는 잠금 파일의 변경을 막는 동기화를 기본으로 한다.

Python 3.13.15는 Astral의 고정된 독립 배포본을 프로젝트 전용 경로에 설치한다. 다운로드 파일의 SHA-256과 실행 버전을 확인한다.

## 공통 초기 준비 절차

macOS에는 Git과 실행 중인 Docker 엔진·Compose(Colima 또는 Docker Desktop)가 필요하다. Windows에는 Git과 Docker Desktop·Compose가 필요하다.

`setup`은 언어 도구를 `.cache/host-tools/`에 설치·재사용하고 버전과 Docker daemon을 확인한 뒤 잠금 파일에 따른 프로젝트 의존성·브라우저를 설치한다. 전역 PATH나 셸 설정은 변경하지 않는다.

실제 명령과 기대 결과는 [실행 체크리스트](onboarding.md)에 있다. macOS 프로젝트 전용 언어 도구와 잠금 설치는 기존 Git·Docker가 있는 환경에서 확인했다. 새 OS 전체 설치 검증은 아니다. Windows 실제 실행은 **NOT RUN**이다.

1. **호스트 준비 후 프로젝트 설치:** 팀원이 Git과 실행 중인 Docker를 준비한다. `setup`이 프로젝트 전용 언어 도구와 버전·daemon을 확인하고 잠금 파일로 화면·서버 의존성을 설치한다. `.env`가 없을 때만 예시 설정을 복사하며 기존 `.env`는 덮어쓰지 않는다.
2. **인증·스키마 준비와 시작:** 기존 `.env`에 독립 인증 키 4개와 정확한 Origin을 설정·검증한 뒤 `db-init`으로 누락 테이블을 만들고 `dev`를 실행한다. 일반 모의 채팅에는 Google·AI 키가 필요 없다. 인증 화면과 PR #78의 실제 MemberChat 대기열·SSE 연결, PR #80의 인증 모달 정리는 병합됐다. PR #79 가입 보호 구현 뒤 #71 독립 확인과 #19·#54 최종 통합은 남아 있다. Google 실패 화면·실제 AI·운영 연결은 별도다.
3. **기능 확인:** `health`·`ready`는 프로세스·DB 연결만 확인하므로 [Swagger 절차](onboarding.md#3-1-swagger로-가입로그인과-모의-대화-확인)에서 가입·로그인·본인 모의 대화 저장·재조회도 확인한다. 인증 설정·스키마 준비 상태를 health/ready 성공으로 대신 판단하지 않는다.
4. **검사·빌드:** 화면의 형식·린트·타입 검사, 서버 검사와 해당 테스트를 실행한 뒤 화면 빌드와 로컬 빌드 확인을 수행한다. 성공은 각 명령의 정상 종료와 실제 화면·API 연결 확인이다.
5. **중지·복구:** 로컬 화면·API 프로세스와 Compose 서비스를 중지한다. 재설치가 필요하면 의존성·생성 빌드 캐시만 정리하고 잠금 파일 기준으로 설치한다. `.env`, 개발·테스트 DB 볼륨과 데이터를 캐시 정리 대상으로 취급하지 않는다.

개발 DB는 Compose의 별도 서비스·볼륨으로 유지한다. 테스트는 개발 DB와 다른 이름·접속 정보·볼륨의 전용 PostgreSQL을 쓴다. 테스트 시작 전 대상 DB가 테스트 전용인지 확인하고, 개발 DB를 가리키면 즉시 실패시킨다.

[ORM 초기 준비](stack.md#개발과-초기-db)와 인증·대화 제품 모델 5개, #15 한도 모델 2개, #52 대기열 모델 3개가 구현되어 있다. `setup`은 제품 테이블을 만들지 않는다. `db-init`은 개발 DB, `db-init-test`는 별도 보호된 테스트 DB의 누락 테이블만 생성하고 등록 모델 수 10개를 보고한다. 기존 컬럼·제약을 변경하거나 데이터를 삭제하지 않는다. 이미 있는 스키마의 호환성·마이그레이션은 별도 검토가 필요하다. `test-db`에는 제품 모델·인증·대화·메시지 통합 검사도 있다.

운영 대상은 [단일 AWS EC2의 Compose 구성](stack.md#선택한-배포-구성)이다. 운영 컨테이너 구성과 배포 검증은 아직 구현되지 않았다.

공통 설정이나 잠금 파일의 변경은 이유, 영향, macOS·Windows 확인 결과를 PR에 적고 PM 검토를 받는다. OS별 미실행 항목은 검증 완료로 표시하지 않는다.

## S11 개인 AI 설정

[S11 GPT 기준](../04-reference/ai.md)의 개인별 서버 키·주간 할당·고정 모델 기준을 따른다. 키는 각자의 백엔드/서버 `.env`에만 보관하고 프런트엔드에 전달하지 않는다. `CODYSSEY_API_KEY`, `CODYSSEY_BASE_URL`, `CODYSSEY_MODEL` 파서와 어댑터는 격리 스모크 전용이다. 일반 앱의 `get_provider()`는 모의를 유지한다. 실제 키 없이도 모의 대화 저장·조회가 가능하다.

## PM 배포 준비 체크리스트

- [ ] EC2에서 Nginx만 외부에 공개하고 React 정적 산출물, `/api/*` 전달, SPA 경로 대체를 검증한다.
- [ ] 백엔드·PostgreSQL은 내부 Compose 네트워크에 두고 DB 이름 있는 볼륨·백업과 복구 절차를 검토한다.
- [ ] HTTPS, Origin, Secure/HttpOnly 쿠키·CSRF, Google callback을 실제 도메인과 대조한다.
- [ ] 스트리밍을 채택하면 프록시 버퍼링·시간 제한과 연결 중단 후 재조회를 시험한다.
- [ ] 이미지 빌드·배포, 비밀값 주입, 운영 검증과 되돌림 절차를 문서화한다.

## 향후 출시 확인

배포 방식은 단일 AWS EC2의 Nginx·백엔드·PostgreSQL Compose로 정했다. 출시 단계에서는 배포 대상과 빌드 산출물의 호환성, HTTPS, 비밀값 관리, 외부 접속, 쿠키와 Google callback을 실제 환경에서 확인해야 한다. 이 절은 향후 확인 항목이며 현재 배포나 검증 완료를 뜻하지 않는다.
