import { useEffect, useRef, useState } from 'react'
import styles from './AuthModal.module.css'
import { AuthApiError, googleLoginStartUrl } from './authApi'
import { useAuth } from './AuthContext'
import { validateEmail, validatePassword } from './authValidation'

type AuthModalProps = {
  onClose: () => void
}

type FormStatus = 'idle' | 'submitting' | 'error'

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function AuthModal({ onClose }: AuthModalProps) {
  const { login, register } = useAuth()
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [formStatus, setFormStatus] = useState<FormStatus>('idle')
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [showGoogleFailurePreview, setShowGoogleFailurePreview] = useState(false)

  const isLogin = mode === 'login'
  const isSubmitting = formStatus === 'submitting'

  const modalRef = useRef<HTMLElement>(null)
  const previouslyFocusedRef = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)
  // 제출·모드 전환·닫기마다 바뀌는 요청 세대. 늦게 끝난 이전 요청의 성공·실패는
  // 세대가 다르면 이 모달의 상태와 초점, onClose에 손대지 않는다.
  const requestGenerationRef = useRef(0)

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    return () => {
      requestGenerationRef.current += 1
    }
  }, [])

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
    requestGenerationRef.current += 1
    setMode(nextMode)
    setFormStatus('idle')
    setErrorMessage(null)
  }

  const closeGoogleFailurePreview = () => {
    setShowGoogleFailurePreview(false)
  }

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    const formData = new FormData(event.currentTarget)
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

    requestGenerationRef.current += 1
    const generation = requestGenerationRef.current
    const isCurrent = () => requestGenerationRef.current === generation

    setFormStatus('submitting')
    setErrorMessage(null)

    try {
      if (isLogin) {
        await login(email.trim(), password)
      } else {
        await register(email.trim(), password)
      }
      if (!isCurrent()) return
      onCloseRef.current()
    } catch (error) {
      if (!isCurrent()) return
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
        aria-labelledby={showGoogleFailurePreview ? 'google-failure-title' : 'auth-title'}
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <button type="button" className={styles.modalClose} onClick={onClose} aria-label="모달 닫기">
          ×
        </button>

        {showGoogleFailurePreview ? (
          <>
            <p className="eyebrow">NEXT-STOP-SIREN</p>

            <div className={styles.googleFailureIcon} aria-hidden="true">
              !
            </div>

            <h2 id="google-failure-title">Google 로그인에 실패했어요</h2>

            <p className={styles.authDescription}>
              로그인을 완료하지 못했어요. 다시 시도하거나 이메일로 로그인해 주세요.
            </p>

            <div className={styles.googleFailureActions}>
              <button type="button" className={styles.authSubmit} onClick={closeGoogleFailurePreview}>
                다시 시도하기
              </button>

              <button
                type="button"
                className={styles.googleFailureSecondary}
                onClick={() => {
                  closeGoogleFailurePreview()
                  switchMode('login')
                }}
              >
                이메일로 로그인
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="eyebrow">NEXT-STOP-SIREN</p>

            <h2 id="auth-title">{isLogin ? '다시 만나서 반가워요' : '회원가입'}</h2>

            <p className={styles.authDescription}>
              {isLogin ? '로그인하고 교통법 질문을 시작해 보세요.' : '계정을 만들고 교통법 정보를 확인해 보세요.'}
            </p>

            <a href={googleLoginStartUrl} className={styles.googleButton}>
              Google로 계속하기
            </a>

            <button
              type="button"
              className={styles.googleFailurePreviewButton}
              onClick={() => setShowGoogleFailurePreview(true)}
            >
              [개발용] Google 로그인 실패 화면 보기
            </button>

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
          </>
        )}
      </section>
    </div>
  )
}

export default AuthModal
