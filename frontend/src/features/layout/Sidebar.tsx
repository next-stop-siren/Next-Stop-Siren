function Sidebar() {
  return (
    <aside className="sidebar" aria-label="사이드바">
      <div className="sidebar-header">
        <h2>교통법 AI</h2>
      </div>

      <button type="button" className="new-chat-button" disabled>
        + 새 대화
      </button>

      <div className="conversation-list">
        <p>대화 기록</p>
        <p>로그인하면 대화 기록을 확인할 수 있어요.</p>
      </div>

      <div className="sidebar-footer">
        <button type="button" className="login-button">
          로그인
        </button>
      </div>
    </aside>
  )
}

export default Sidebar
