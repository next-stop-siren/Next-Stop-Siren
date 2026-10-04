# 공통 개발 환경

## 기준과 소유

이 저장소의 구현 기준은 React·TypeScript·Vite 화면, FastAPI 서버, PostgreSQL이다. 선택한 ORM·인증 라이브러리와 현재 의존성의 차이는 [기술 구성](stack.md)에 정리했다. 공통 설정은 PM이 검토한다. 팀원은 같은 저장소와 잠금 파일을 사용하며 개인별로 프로젝트를 다시 초기화하지 않는다.

초기 앱과 macOS·Windows 수동 실행 명령은 루트 [README](../README.md)에 있다. macOS Colima의 DB 런타임은 검증했으며 Windows 실행은 아직 검증하지 않았다.

### Node.js·npm

- **채택 기준:** Node.js 24.21.0 LTS와 함께 제공되는 npm 11.19.0
- **현재 잠금 파일:** `frontend/package-lock.json`

### Python·uv

- **채택 기준:** Python 3.13.15 안정판과 uv 0.11.19
- **현재 잠금 파일:** `backend/uv.lock`

### PostgreSQL

- **채택 기준:** 지원 중인 17.11 안정판
- **현재 구성:** `compose.yaml`의 개발·테스트 DB 서비스

macOS 터미널과 Windows PowerShell에서 Node.js, npm, Python, uv, Docker/Compose, PostgreSQL 서버의 실제 버전을 출력해 기록한다. 설치된 버전이 저장소의 고정 버전과 다르면 설치를 멈추고 안내에 맞춘다.

npm은 잠금 파일을 따르는 깨끗한 설치, uv는 잠금 파일의 변경을 막는 동기화를 기본으로 한다.

Python 3.13.15는 Astral의 고정된 독립 배포본을 프로젝트 전용 경로에 설치한다. 다운로드 파일의 SHA-256과 실행 버전을 확인한다.

## 공통 초기 준비 절차

macOS에는 Git과 실행 중인 Docker 엔진·Compose(Colima 또는 Docker Desktop)가 필요하다. Windows에는 Git과 Docker Desktop·Compose가 필요하다.

`setup`은 언어 도구를 `.cache/host-tools/`에 설치·재사용하고 버전과 Docker daemon을 확인한 뒤 잠금 파일에 따른 프로젝트 의존성·브라우저를 설치한다. 전역 PATH나 셸 설정은 변경하지 않는다.

실제 명령과 기대 결과는 [실행 체크리스트](onboarding.md)에 있다. macOS 프로젝트 전용 언어 도구와 잠금 설치는 기존 Git·Docker가 있는 환경에서 확인했다. 새 OS 전체 설치 검증은 아니다. Windows 실제 실행은 **NOT RUN**이다.

1. **호스트 준비 후 프로젝트 설치:** 팀원이 Git과 실행 중인 Docker를 준비한다. `setup`이 프로젝트 전용 언어 도구와 버전·daemon을 확인하고 잠금 파일로 화면·서버 의존성을 설치한다. `.env`가 없을 때만 예시 설정을 복사하며 기존 `.env`는 덮어쓰지 않는다.
2. **시작:** Compose로 개발용 PostgreSQL을 시작하고, 화면과 API는 로컬 프로세스로 각각 실행한다. 초기 통합에서는 기본 화면, API 상태 확인, DB 연결, 화면→API 연결을 확인한다.
3. **상태 확인:** DB 준비 상태, API 상태 응답, 브라우저의 화면 및 API 요청 성공을 확인한다. 실패하면 해당 단계의 로그와 버전을 기록한다.
4. **검사·빌드:** 화면의 형식·린트·타입 검사, 서버 검사와 해당 테스트를 실행한 뒤 화면 빌드와 로컬 빌드 확인을 수행한다. 성공은 각 명령의 정상 종료와 실제 화면·API 연결 확인이다.
5. **중지·복구:** 로컬 화면·API 프로세스와 Compose 서비스를 중지한다. 재설치가 필요하면 의존성·생성 빌드 캐시만 정리하고 잠금 파일 기준으로 설치한다. `.env`, 개발·테스트 DB 볼륨과 데이터를 캐시 정리 대상으로 취급하지 않는다.

개발 DB는 Compose의 별도 서비스·볼륨으로 유지한다. 테스트는 개발 DB와 다른 이름·접속 정보·볼륨의 전용 PostgreSQL을 쓴다. 테스트 시작 전 대상 DB가 테스트 전용인지 확인하고, 개발 DB를 가리키면 즉시 실패시킨다.

[공통 ORM 초기 준비](stack.md#개발과-초기-db)는 이 변경에서 구현 중이다. `setup`은 테이블을 생성하지 않는다. `db-init`·`db-init-test`는 등록된 앱 모델의 누락 테이블만 생성하며 현재 등록 모델이 없어 정상적으로 0개를 보고한다. `test-db`는 별도 테스트 전용 모델을 검사한다. #3·#9는 독립 검토와 병합을 거친 공통 준비 게이트 뒤 시작한다.

운영 대상은 [단일 AWS EC2의 Compose 구성](stack.md#선택한-배포-구성)이다. 운영 컨테이너 구성과 배포 검증은 아직 구현되지 않았다.

공통 설정이나 잠금 파일의 변경은 이유, 영향, macOS·Windows 확인 결과를 PR에 적고 PM 검토를 받는다. OS별 미실행 항목은 검증 완료로 표시하지 않는다.

## PM 배포 준비 체크리스트

- [ ] EC2에서 Nginx만 외부에 공개하고 React 정적 산출물, `/api/*` 전달, SPA 경로 대체를 검증한다.
- [ ] 백엔드·PostgreSQL은 내부 Compose 네트워크에 두고 DB 이름 있는 볼륨·백업과 복구 절차를 검토한다.
- [ ] HTTPS, Origin, Secure/HttpOnly 쿠키·CSRF, Google callback을 실제 도메인과 대조한다.
- [ ] 스트리밍을 채택하면 프록시 버퍼링·시간 제한과 연결 중단 후 재조회를 시험한다.
- [ ] 이미지 빌드·배포, 비밀값 주입, 운영 검증과 되돌림 절차를 문서화한다.

## 향후 출시 확인

배포 방식은 단일 AWS EC2의 Nginx·백엔드·PostgreSQL Compose로 정했다. 출시 단계에서는 배포 대상과 빌드 산출물의 호환성, HTTPS, 비밀값 관리, 외부 접속, 쿠키와 Google callback을 실제 환경에서 확인해야 한다. 이 절은 향후 확인 항목이며 현재 배포나 검증 완료를 뜻하지 않는다.
