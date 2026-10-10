import { useState } from 'react'
import NonmemberIntro from './features/nonmember/NonmemberIntro'
import Sidebar from './features/layout/Sidebar'
import AuthModal from './features/auth/AuthModal'

function App() {
  const [isAuthModalOpen, setIsAuthModalOpen] = useState(false)

  const openAuthModal = () => setIsAuthModalOpen(true)
  const closeAuthModal = () => setIsAuthModalOpen(false)

  return (
    <div className="app-layout">
      <Sidebar onLoginClick={openAuthModal} />

      <main className="main-content">
        <NonmemberIntro onAuthRequired={openAuthModal} />
      </main>

      {isAuthModalOpen && <AuthModal onClose={closeAuthModal} />}
    </div>
  )
}

export default App
