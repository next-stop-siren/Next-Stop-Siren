import { useState } from 'react'

type AuthModalProps = {
  onClose: () => void
}

function AuthModal({ onClose }: AuthModalProps) {
  const [mode, setMode] = useState<'login' | 'signup'>('login')

  const isLogin = mode === 'login'

  return (
    <div className="modal-backdrop">
      <button type="button" className="modal-backdrop-dismiss" onClick={onClose} aria-label="모달 닫기" />
      <section
        className="auth-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        onMouseDown={(event) => event.stopPropagation()} // Prevent closing modal when clicking inside
      >
        <button type="button" className="modal-close" onClick={onClose} aria-label="모달 닫기">
          ×
        </button>

        <p className="eyebrow">NEXT-STOP-SIREN</p>
        <h2 id="auth-title">{isLogin ? '다시 만나서 반가워요' : '회원가입'}</h2>
        <p className="auth-description">
          {isLogin ? '로그인하고 교통법 질문을 시작해 보세요.' : '계정을 만들고 교통법 정보를 확인해 보세요.'}
        </p>

        <div className="auth-tabs">
          <button
            type="button"
            className={isLogin ? 'active' : ''}
            onClick={() => setMode('login')}
            aria-pressed={isLogin}
          >
            로그인
          </button>
          <button
            type="button"
            className={!isLogin ? 'active' : ''}
            onClick={() => setMode('signup')}
            aria-pressed={!isLogin}
          >
            회원가입
          </button>
        </div>

        <form
          onSubmit={(event) => {
            event.preventDefault()
          }}
        >
          {!isLogin && (
            <label>
              이름
              <input type="text" name="name" placeholder="이름을 입력하세요" autoComplete="name" required />
            </label>
          )}

          <label>
            이메일
            <input type="email" name="email" placeholder="name@example.com" autoComplete="email" required />
          </label>

          <label>
            비밀번호
            <input
              type="password"
              name="password"
              placeholder="비밀번호를 입력하세요"
              autoComplete={isLogin ? 'current-password' : 'new-password'}
              required
            />
          </label>

          <button type="submit" className="auth-submit">
            {isLogin ? '로그인' : '회원가입'}
          </button>
        </form>

        <p className="auth-notice">현재는 화면 시안이며 실제 인증 기능은 연결되지 않았습니다.</p>
      </section>
    </div>
  )
}

export default AuthModal
