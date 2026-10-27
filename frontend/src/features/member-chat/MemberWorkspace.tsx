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

  return (
    <>
      <Sidebar
        isAuthenticated
        onLoginClick={() => {}}
        onLogoutClick={onLogoutClick}
        conversations={{
          items: chat.conversations,
          selectedId: chat.conversationId,
          onSelect: (id) => void chat.selectConversation(id),
          onNew: chat.startNewConversation,
          disabled: chat.sending,
          loading: chat.loading,
        }}
      />

      <main className="main-content">
        <MemberChat chat={chat} />
      </main>
    </>
  )
}

export default MemberWorkspace
