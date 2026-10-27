import { useEffect, useState } from 'react'
import AuthModal from './features/auth/AuthModal'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import Sidebar from './features/layout/Sidebar'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import MemberWorkspace from './features/member-chat/MemberWorkspace'
import MemberChatMockPreview from './features/member-chat/MemberChatMockPreview'

function AppShell() {
  const { status, user, logout, retryLogout, logoutError } = useAuth()
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)
  const [isRetryingLogout, setIsRetryingLogout] = useState(false)

  // 질문 입력창은 초점만으로 모달을 열지 않으므로, 닫을 때 초점이 입력창으로
  // 돌아와도 다시 열리지 않는다.
  const openAuthModal = () => setIsAuthModalOpen(true)
  const closeAuthModal = () => setIsAuthModalOpen(false)

  // 닫힌 모달의 늦은 로그인 성공도 세션은 적용된다. 회원 화면으로 바뀌면 모달
  // 열림 상태를 비워, 다시 열린 모달이 남거나 로그아웃 뒤 저절로 뜨지 않게 한다.
  useEffect(() => {
    if (status === 'authenticated') setIsAuthModalOpen(false)
  }, [status])

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
        {status === 'authenticated' ? (
          // 회원이 바뀌면 새로 마운트해 이전 회원의 대화·임시 출력·구독이 첫 화면부터 남지 않게 한다.
          <MemberWorkspace key={user?.id ?? ''} onLogoutClick={handleLogout} />
        ) : (
          <>
            <Sidebar isAuthenticated={false} onLoginClick={openAuthModal} onLogoutClick={handleLogout} />

            <main className="main-content">
              {status === 'guest' ? <NonmemberIntro onAuthRequired={openAuthModal} /> : null}
            </main>
          </>
        )}

        {isAuthModalOpen && status !== 'authenticated' && <AuthModal onClose={closeAuthModal} />}
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
