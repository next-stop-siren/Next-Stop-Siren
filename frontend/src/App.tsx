import { useRef, useState } from 'react'
import AuthModal from './features/auth/AuthModal'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import Sidebar from './features/layout/Sidebar'
import NonmemberIntro from './features/nonmember/NonmemberIntro'

function AppShell() {
  const { status, logout, retryLogout } = useAuth()
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)

  // 로그아웃 요청이 서버에서 실패했을 때(네트워크 오류, 403, 503 등) 보여줄 상태다.
  // AuthContext.logout()은 실패해도 로컬 상태를 즉시 guest로 비우므로(화면은 이미
  // 비회원처럼 보임), 서버의 refresh·CSRF 쿠키는 여전히 남아 있을 수 있다는 걸
  // 사용자에게 알리고 다시 시도할 수단을 준다.
  const [logoutError, setLogoutError] = useState(false)
  const [isRetryingLogout, setIsRetryingLogout] = useState(false)

  // 모달을 닫으면 AuthModal이 이전 포커스 요소(질문 입력창)로 초점을 되돌린다.
  // 그 입력창은 onFocus로 모달을 다시 여는 핸들러를 갖고 있어서, 복원된 포커스가
  // 그대로 재오픈을 유발할 수 있다. 닫은 직후 한 번의 open 요청만 무시해 이를 막는다.
  const suppressNextAuthRequestRef = useRef(false)

  const openAuthModal = () => {
    if (suppressNextAuthRequestRef.current) {
      suppressNextAuthRequestRef.current = false
      return
    }
    setIsAuthModalOpen(true)
  }

  const closeAuthModal = () => {
    suppressNextAuthRequestRef.current = true
    setIsAuthModalOpen(false)
    setTimeout(() => {
      suppressNextAuthRequestRef.current = false
    }, 0)
  }

  const handleLogout = () => {
    setLogoutError(false)
    logout().catch(() => {
      setLogoutError(true)
    })
  }

  // "다시 시도"는 logout()을 다시 부르지 않는다 — 첫 시도에서 이미 로컬
  // csrfTokenRef가 비워졌기 때문에 그걸 재사용하면 서버에 아무 요청도 가지
  // 않고 조용히 "성공" 처리되어 버린다(서버 세션은 그대로 남음). 대신
  // retryLogout()으로 CSRF를 새로 받아와 실제로 서버 로그아웃을 다시 요청하고,
  // 그 응답을 기다린 뒤에만 배너를 치운다.
  const handleRetryLogout = async () => {
    setIsRetryingLogout(true)
    try {
      await retryLogout()
      setLogoutError(false)
    } catch {
      setLogoutError(true)
    } finally {
      setIsRetryingLogout(false)
    }
  }

  // 'checking'은 새로고침 직후 세션 복구(csrf→refresh)가 끝나기 전의 짧은 순간이다.
  // 이 사이에 비회원 화면을 잘못 보여줬다가 회원 화면으로 바뀌는 깜빡임을 피한다.
  // (세션 복구 자체가 응답 없이 멈추는 경우의 타임아웃 처리는 AuthContext가 맡는다.)
  if (status === 'checking') {
    return (
      <div className="app-root">
        <div className="app-layout" aria-busy="true" />
      </div>
    )
  }

  return (
    <div className="app-root">
      {logoutError && (
        <div role="alert" className="logout-error-banner">
          <span>로그아웃 요청이 서버에 전달되지 못했습니다. 다시 시도해 주세요.</span>
          <button type="button" onClick={handleRetryLogout} disabled={isRetryingLogout}>
            {isRetryingLogout ? '재시도 중...' : '다시 시도'}
          </button>
        </div>
      )}

      <div className="app-layout">
        <Sidebar
          isAuthenticated={status === 'authenticated'}
          onLoginClick={openAuthModal}
          onLogoutClick={handleLogout}
        />

        <main className="main-content">
          {status === 'guest' ? (
            <NonmemberIntro onAuthRequired={openAuthModal} />
          ) : // 실제 회원(채팅) 화면은 이번 S16 범위 밖이다. S16은 인증 화면 상태
          // 연결까지만 다루고, 회원 화면 자체는 후속 티켓에서 만든다.
          null}
        </main>

        {isAuthModalOpen && <AuthModal onClose={closeAuthModal} />}
      </div>
    </div>
  )
}

function App() {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export default App
