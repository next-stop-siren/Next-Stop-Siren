import styles from './Sidebar.module.css'

type SidebarProps = {
  isAuthenticated: boolean
  onLoginClick: () => void
  onLogoutClick: () => void
}

function Sidebar({ isAuthenticated, onLoginClick, onLogoutClick }: SidebarProps) {
  return (
    <aside className={styles.sidebar} aria-label="사이드바">
      <div className={styles.sidebarHeader}>
        <h2>교통법 AI</h2>
      </div>

      <button type="button" className={styles.newChatButton} disabled={!isAuthenticated}>
        <span>＋ 새 대화</span>
        {!isAuthenticated && <span className={styles.loginRequired}>로그인 필요</span>}
      </button>

      <div className={styles.conversationList}>
        <p>대화 기록</p>
        {isAuthenticated ? <p>대화 기록이 아직 없어요.</p> : <p>로그인하면 대화 기록을 확인할 수 있어요.</p>}
      </div>

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
