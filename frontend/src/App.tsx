import { useRef, useState } from 'react'
import AuthModal from './features/auth/AuthModal'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import Sidebar from './features/layout/Sidebar'
import NonmemberIntro from './features/nonmember/NonmemberIntro'

function AppShell() {
  const { status, logout } = useAuth()
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)

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

  // 'checking'은 새로고침 직후 세션 복구(csrf→refresh)가 끝나기 전의 짧은 순간이다.
  // 이 사이에 비회원 화면을 잘못 보여줬다가 회원 화면으로 바뀌는 깜빡임을 피한다.
  if (status === 'checking') {
    return <div className="app-layout" aria-busy="true" />
  }

  return (
    <div className="app-layout">
      <Sidebar
        isAuthenticated={status === 'authenticated'}
        onLoginClick={openAuthModal}
        onLogoutClick={() => {
          logout().catch(() => {})
        }}
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
