import { useState } from 'react'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import Sidebar from './features/layout/Sidebar'
import AuthModal from './features/auth/AuthModal'

// 'member' 분기는 추후 인증 작업(#5, #6)에서 실제 로그인 상태로 연결한다.
// 이번 티켓(S15) 범위에서는 항상 'guest'로 고정한다.
type ViewerState = 'guest' | 'member'

function App() {
  const [viewerState] = useState<ViewerState>('guest')
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)

  const openAuthModal = () => setIsAuthModalOpen(true)
  const closeAuthModal = () => setIsAuthModalOpen(false)

  return (
    <div className="app-layout">
      <Sidebar onLoginClick={openAuthModal} />

      <main className="main-content">
        {viewerState === 'guest' ? (
          <NonmemberIntro onAuthRequired={openAuthModal} />
        ) : // 추후 인증 연동 티켓에서 실제 회원 화면으로 교체 예정
        null}
      </main>

      {isAuthModalOpen && <AuthModal onClose={closeAuthModal} />}
    </div>
  )
}

export default App
