import { useState } from 'react'
import Welcome from '../home/Welcome'
import Sidebar from '../layout/Sidebar'
import MemberChat from './MemberChat'
import { useMemberChat, type MemberChatOptions } from './useMemberChat'

type MemberWorkspaceProps = {
  onLogoutClick: () => void
  options?: MemberChatOptions
}

/**
 * 회원 화면: 사이드바의 대화 목록·새 대화와 본문이 같은 상태를 쓴다.
 * 호출한 쪽은 회원이 바뀔 때 새로 마운트하도록 `key`에 회원 ID를 준다. 그래야 첫 화면부터 이전 회원의 값이 섞이지 않는다.
 */
function MemberWorkspace({ onLogoutClick, options }: MemberWorkspaceProps) {
  const chat = useMemberChat(options)
  // 홈 전환은 보기만 바꾼다. 세션·대화 상태와 진행 중인 서버 작업은 유지한다.
  const [isHome, setIsHome] = useState(true)
  const startQuestion = () => {
    if (chat.sending) return
    chat.startNewConversation()
    setIsHome(false)
  }

  return (
    <>
      <Sidebar
        isAuthenticated
        onHomeClick={() => setIsHome(true)}
        onLoginClick={() => {}}
        onLogoutClick={onLogoutClick}
        conversations={{
          items: chat.conversations,
          selectedId: isHome ? null : chat.conversationId,
          onSelect: (id) => {
            setIsHome(false)
            void chat.selectConversation(id)
          },
          onNew: startQuestion,
          disabled: chat.sending,
          loading: chat.loading,
        }}
      />

      <main className="main-content">
        {isHome ? <Welcome isAuthenticated onStart={startQuestion} notice={chat.error} /> : <MemberChat chat={chat} />}
      </main>
    </>
  )
}

export default MemberWorkspace
