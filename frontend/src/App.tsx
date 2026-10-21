import { useRef, useState } from 'react'
import AuthModal from './features/auth/AuthModal'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import Sidebar from './features/layout/Sidebar'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import MemberChat from './features/member-chat/MemberChat'
import MemberChatMockPreview from './features/member-chat/MemberChatMockPreview'

function AppShell() {
  const { status, logout, retryLogout, logoutError } = useAuth()
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)
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
    // 실패 배너는 Provider가 해당 세대에만 연결한다.
    logout().catch(() => {})
  }

  const handleRetryLogout = async () => {
    setIsRetryingLogout(true)
    try {
      await retryLogout()
    } catch {
      // Provider가 현재 세대의 실패만 표시한다.
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
          ) : status === 'authenticated' ? (
            <MemberChat />
          ) : null}
        </main>

        {isAuthModalOpen && <AuthModal onClose={closeAuthModal} />}
      </div>
    </div>
  )
}

function App() {
  // 개발 모드에서만 켤 수 있는 독립 목업 화면입니다. 기본 앱/인증 흐름은 그대로 둡니다.
  const isMockPreview = import.meta.env.DEV && new URLSearchParams(window.location.search).get('chatMock') === '1'

  if (isMockPreview) {
    return <MemberChatMockPreview />
  }

  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  )
}

export default App
