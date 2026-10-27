import styles from './Sidebar.module.css'

export type SidebarConversation = {
  id: string
  created_at: string
}

/** 회원의 대화 목록. 본문과 같은 상태(useMemberChat)를 가리킨다. */
export type SidebarConversations = {
  items: SidebarConversation[]
  selectedId: string | null
  onSelect: (id: string) => void
  onNew: () => void
  /** 질문을 보내는 동안처럼 대화를 바꿀 수 없을 때 */
  disabled: boolean
  loading: boolean
}

type SidebarProps = {
  isAuthenticated: boolean
  onLoginClick: () => void
  onLogoutClick: () => void
  conversations?: SidebarConversations
}

const conversationTime = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul',
  month: 'long',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

/** 사이드바에 보일 대화 이름. 목록 API에는 제목이 없어 만든 시각(KST)으로 구분한다. */
export function conversationLabel(conversation: SidebarConversation): string {
  return `대화 · ${conversationTime.format(new Date(conversation.created_at))}`
}

function Sidebar({ isAuthenticated, onLoginClick, onLogoutClick, conversations }: SidebarProps) {
  const member = isAuthenticated ? conversations : undefined

  return (
    <aside className={styles.sidebar} aria-label="사이드바">
      <div className={styles.sidebarHeader}>
        <h2>교통법 AI</h2>
      </div>

      <button
        type="button"
        className={styles.newChatButton}
        disabled={!member || member.disabled}
        onClick={member?.onNew}
      >
        <span>＋ 새 대화</span>
        {!isAuthenticated && <span className={styles.loginRequired}>로그인 필요</span>}
      </button>

      <nav className={styles.conversationList} aria-label="대화 기록">
        <p>대화 기록</p>
        {!isAuthenticated ? (
          <p>로그인하면 대화 기록을 확인할 수 있어요.</p>
        ) : !member || member.items.length === 0 ? (
          <p>{member?.loading ? '대화 기록을 불러오는 중이에요.' : '대화 기록이 아직 없어요.'}</p>
        ) : (
          <ul>
            {member.items.map((conversation) => {
              const selected = conversation.id === member.selectedId

              return (
                <li key={conversation.id}>
                  <button
                    type="button"
                    className={styles.conversationButton}
                    aria-current={selected ? 'page' : undefined}
                    disabled={member.disabled && !selected}
                    onClick={() => {
                      if (!selected) member.onSelect(conversation.id)
                    }}
                  >
                    {conversationLabel(conversation)}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </nav>

      <div className={styles.sidebarFooter}>
        {isAuthenticated ? (
          <button type="button" className={styles.loginButton} onClick={onLogoutClick}>
            로그아웃
          </button>
        ) : (
          <button type="button" className={styles.loginButton} onClick={onLoginClick}>
            로그인
          </button>
        )}
      </div>
    </aside>
  )
}

export default Sidebar
