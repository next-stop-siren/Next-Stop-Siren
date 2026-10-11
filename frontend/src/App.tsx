import { useRef, useState } from 'react'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import Sidebar from './features/layout/Sidebar'
import AuthModal from './features/auth/AuthModal'

// 'member' 분기는 추후 인증 작업(#5, #6)에서 실제 로그인 상태로 연결한다.
// 이번 티켓(S15) 범위에서는 항상 'guest'로 고정한다.
type ViewerState = 'guest' | 'member'

function App() {
  const [viewerState] = useState<ViewerState>('guest')
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)

  // 모달을 닫으면 AuthModal이 이전 포커스 요소(예: 질문 입력창)로 초점을 되돌린다.
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
    // 포커스 복원으로 인한 즉시 재오픈 요청만 무시하면 되므로, 다음 틱에는 가드를 해제해
    // 이후의 정상적인 사용자 입력까지 막지 않도록 한다.
    setTimeout(() => {
      suppressNextAuthRequestRef.current = false
    }, 0)
  }

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
