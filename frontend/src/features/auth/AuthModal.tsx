import { useEffect, useRef, useState } from 'react'

type AuthModalProps = {
  onClose: () => void
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function AuthModal({ onClose }: AuthModalProps) {
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const isLogin = mode === 'login'

  const modalRef = useRef<HTMLElement>(null)
  const previouslyFocusedRef = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  // 모달 열림: 이전 포커스 저장, 모달 안으로 초점 이동, 포커스 트랩·Escape 등록
  // 모달 닫힘(언마운트): 이전에 초점이 있던 요소로 복원
  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null

    const modalNode = modalRef.current
    const getFocusable = () =>
      modalNode ? Array.from(modalNode.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)) : []

    const focusables = getFocusable()
    ;(focusables[0] ?? modalNode)?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onCloseRef.current()
        return
      }

      if (event.key !== 'Tab') return

      const items = getFocusable()
      if (items.length === 0) return

      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement

      if (event.shiftKey) {
        if (active === first || !modalNode?.contains(active)) {
          event.preventDefault()
          last.focus()
        }
      } else if (active === last || !modalNode?.contains(active)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)

    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      previouslyFocusedRef.current?.focus()
    }
  }, [])

  return (
    <div className="modal-backdrop">
      <button type="button" className="modal-backdrop-dismiss" onClick={onClose} aria-label="모달 닫기" />
      <section
        ref={modalRef}
        className="auth-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        tabIndex={-1}
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
