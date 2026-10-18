import { useEffect, useRef, useState } from 'react'
import styles from './AuthModal.module.css'
import { AuthApiError, googleLoginStartUrl } from './authApi'
import { useAuth } from './AuthContext'

type AuthModalProps = {
  onClose: () => void
}

function validateEmail(rawEmail: string): string | null {
  const email = rawEmail.trim()
  if (email.length === 0) return '이메일을 입력해 주세요.'
  if ([...email].length > 320) return '이메일이 너무 깁니다.'
  // 서버와 같은 수준의 느슨한 형식 검사 — 로컬 파트의 비ASCII 문자를 막지 않는다.
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return '올바른 이메일 형식이 아닙니다.'
  return null
}

function validatePassword(password: string): string | null {
  const codePointLength = [...password].length
  if (codePointLength < 12 || codePointLength > 128) {
    return '비밀번호는 12자 이상 128자 이하여야 합니다.'
  }
  const byteLength = new TextEncoder().encode(password).length
  if (byteLength > 1024) {
    return '비밀번호가 너무 깁니다.'
  }
  return null
}

type FormStatus = 'idle' | 'submitting' | 'error'

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function AuthModal({ onClose }: AuthModalProps) {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [formStatus, setFormStatus] = useState<FormStatus>('idle')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const isLogin = mode === 'login'
  const isSubmitting = formStatus === 'submitting'

  const modalRef = useRef<HTMLElement>(null)
  const previouslyFocusedRef = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  // 탭을 바꾸면 이전 탭의 실패 메시지를 들고 있을 이유가 없다.
  useEffect(() => {
    setFormStatus('idle')
    setErrorMessage(null)
  }, [])

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

  const switchMode = (nextMode: 'login' | 'signup') => {
    setMode(nextMode)
    setFormStatus('idle')
    setErrorMessage(null)
  }

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const formData = new FormData(event.currentTarget)
    // trim 등으로 값 자체를 바꾸지 않는다 — 비교·전송 모두 원문을 그대로 쓴다.
    const email = String(formData.get('email') ?? '')
    const password = String(formData.get('password') ?? '')

    const emailError = validateEmail(email)
    const passwordError = validatePassword(password)
    const clientError = emailError ?? passwordError
    if (clientError) {
      setFormStatus('error')
      setErrorMessage(clientError)
      return
    }

    setFormStatus('submitting')
    setErrorMessage(null)

    try {
      if (isLogin) {
        await login(email.trim(), password)
      } else {
        await register(email.trim(), password)
      }
      onClose()
    } catch (error) {
      setFormStatus('error')
      setErrorMessage(error instanceof AuthApiError ? error.message : '요청을 처리하지 못했습니다. 다시 시도해 주세요.')
    }
  }

  return (
    <div className={styles.modalBackdrop}>
      <button type="button" className={styles.modalBackdropDismiss} onClick={onClose} aria-label="모달 닫기" />
      <section
        ref={modalRef}
        className={styles.authModal}
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()} // Prevent closing modal when clicking inside
      >
        <button type="button" className={styles.modalClose} onClick={onClose} aria-label="모달 닫기">
          ×
        </button>

        <p className="eyebrow">NEXT-STOP-SIREN</p>
        <h2 id="auth-title">{isLogin ? '다시 만나서 반가워요' : '회원가입'}</h2>
        <p className={styles.authDescription}>
          {isLogin ? '로그인하고 교통법 질문을 시작해 보세요.' : '계정을 만들고 교통법 정보를 확인해 보세요.'}
        </p>

        <a href={googleLoginStartUrl} className={styles.googleButton}>
          Google로 계속하기
        </a>

        <div className={styles.divider}>
          <span>또는</span>
        </div>

        <div className={styles.authTabs}>
          <button
            type="button"
            className={isLogin ? styles.active : ''}
            onClick={() => switchMode('login')}
            aria-pressed={isLogin}
          >
            로그인
          </button>
          <button
            type="button"
            className={!isLogin ? styles.active : ''}
            onClick={() => switchMode('signup')}
            aria-pressed={!isLogin}
          >
            회원가입
          </button>
        </div>

        <form onSubmit={handleSubmit}>
          {!isLogin && (
            <label>
              이름
              <input type="text" name="name" placeholder="이름을 입력하세요" autoComplete="name" required />
            </label>
          )}

          <label>
            이메일
            <input
              type="text"
              inputMode="email"
              name="email"
              placeholder="name@example.com"
              autoComplete="email"
              required
            />
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

          {errorMessage && (
            <p className={styles.formError} role="alert">
              {errorMessage}
            </p>
          )}

          <button type="submit" className={styles.authSubmit} disabled={isSubmitting}>
            {isSubmitting ? '처리 중...' : isLogin ? '로그인' : '회원가입'}
          </button>
        </form>
      </section>
    </div>
  )
}

export default AuthModal
