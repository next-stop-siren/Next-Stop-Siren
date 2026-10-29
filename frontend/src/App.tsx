import { useEffect, useState } from 'react'
import AuthModal from './features/auth/AuthModal'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import {
  type GoogleLoginFailureKind,
  consumeGoogleLoginFailureParam,
  googleLoginFailureCopy,
  readGoogleLoginFailure,
} from './features/auth/googleLoginFailure'
import Sidebar from './features/layout/Sidebar'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import MemberWorkspace from './features/member-chat/MemberWorkspace'
import MemberChatMockPreview from './features/member-chat/MemberChatMockPreview'

function AppShell() {
  const { status, user, logout, retryLogout, logoutError } = useAuth()
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)
  const [isRetryingLogout, setIsRetryingLogout] = useState(false)
  // Google 실패 복귀 query는 첫 화면에서 한 번만 읽는다. 안내용 값이라 인증 상태는
  // 바꾸지 않고, 세션 복구가 끝난 뒤 비회원이면 모달, 기존 회원이면 배너로 보여준다.
  const [pendingGoogleFailure, setPendingGoogleFailure] = useState<GoogleLoginFailureKind | null>(() =>
    readGoogleLoginFailure(window.location.search),
  )
  const [modalGoogleFailure, setModalGoogleFailure] = useState<GoogleLoginFailureKind | null>(null)
  const [bannerGoogleFailure, setBannerGoogleFailure] = useState<GoogleLoginFailureKind | null>(null)

  // 질문 입력창은 초점만으로 모달을 열지 않으므로, 닫을 때 초점이 입력창으로
  // 돌아와도 다시 열리지 않는다.
  const openAuthModal = () => setIsAuthModalOpen(true)
  const closeAuthModal = () => {
    setIsAuthModalOpen(false)
    setModalGoogleFailure(null)
  }

  // 처리할 실패 값을 상태로 옮겼으므로 주소에서 지워 새로고침 때 안내가 반복되지 않게 한다.
  useEffect(() => {
    consumeGoogleLoginFailureParam()
  }, [])

  useEffect(() => {
    if (pendingGoogleFailure === null || status === 'checking') return
    setPendingGoogleFailure(null)
    if (status === 'authenticated') {
      setBannerGoogleFailure(pendingGoogleFailure)
    } else {
      setModalGoogleFailure(pendingGoogleFailure)
      setIsAuthModalOpen(true)
    }
  }, [pendingGoogleFailure, status])

  // 닫힌 모달의 늦은 로그인 성공도 세션은 적용된다. 회원 화면으로 바뀌면 모달
  // 열림 상태를 비워, 다시 열린 모달이 남거나 로그아웃 뒤 저절로 뜨지 않게 한다.
  // 회원 화면을 벗어나면 이전 세션에 붙인 Google 실패 배너도 함께 비운다.
  useEffect(() => {
    if (status === 'authenticated') {
      setIsAuthModalOpen(false)
      setModalGoogleFailure(null)
    } else {
      setBannerGoogleFailure(null)
    }
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

      {bannerGoogleFailure && status === 'authenticated' && (
        <div role="status" className="google-failure-banner">
          <span>{googleLoginFailureCopy(bannerGoogleFailure).title}. 현재 로그인 상태는 그대로 유지돼요.</span>
          <button type="button" onClick={() => setBannerGoogleFailure(null)}>
            닫기
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

        {isAuthModalOpen && status !== 'authenticated' && (
          <AuthModal
            onClose={closeAuthModal}
            googleFailure={modalGoogleFailure}
            onGoogleFailureDismiss={() => setModalGoogleFailure(null)}
          />
        )}
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
