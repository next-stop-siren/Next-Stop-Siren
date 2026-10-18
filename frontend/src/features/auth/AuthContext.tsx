import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  AuthApiError,
  fetchCsrf,
  getMe,
  login as apiLogin,
  logout as apiLogout,
  refresh,
  register as apiRegister,
} from './authApi'
import type { PublicUser } from './authApi'

type AuthStatus = 'checking' | 'guest' | 'authenticated'

type AuthContextValue = {
  status: AuthStatus
  user: PublicUser | null
  accessToken: string | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// 403(csrf_failed) 또는 409(refresh_race)를 받으면 잠시 기다린 뒤 /csrf를 다시 읽고
// refresh를 한 번만 재시도한다. 원 요청을 포함해 POST는 최대 두 번이다.
async function refreshWithRetry() {
  const first = await fetchCsrf()
  try {
    return await refresh(first.csrf_token)
  } catch (error) {
    const shouldRetryOnce = error instanceof AuthApiError && (error.status === 403 || error.status === 409)
    if (!shouldRetryOnce) throw error

    await new Promise((resolve) => setTimeout(resolve, 1000))
    const second = await fetchCsrf()
    return await refresh(second.csrf_token)
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [user, setUser] = useState<PublicUser | null>(null)
  const [accessToken, setAccessToken] = useState<string | null>(null)

  // 화면 재렌더와 무관한 내부 상태라 ref로 둔다. access/csrf는 메모리 전용이며
  // localStorage, 쿠키 등 어떤 저장소에도 쓰지 않는다.
  const csrfTokenRef = useRef<string | null>(null)
  const expiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clearExpiryTimer = useCallback(() => {
    if (expiryTimerRef.current !== null) {
      clearTimeout(expiryTimerRef.current)
      expiryTimerRef.current = null
    }
  }, [])

  const setGuest = useCallback(() => {
    clearExpiryTimer()
    csrfTokenRef.current = null
    setAccessToken(null)
    setUser(null)
    setStatus('guest')
  }, [clearExpiryTimer])

  // access는 15분(900초)짜리라 만료 30초 전에 선제적으로 갱신을 시도한다.
  // 갱신이 실패하면(쿠키도 만료·폐기된 경우) 회원 화면을 차단하고 guest로 되돌린다.
  const scheduleExpiry = useCallback(
    (expiresInSeconds: number) => {
      clearExpiryTimer()
      const delayMs = Math.max((expiresInSeconds - 30) * 1000, 0)

      expiryTimerRef.current = setTimeout(async () => {
        try {
          const refreshed = await refreshWithRetry()
          csrfTokenRef.current = refreshed.csrf_token
          setAccessToken(refreshed.access_token)
          scheduleExpiry(refreshed.expires_in)
        } catch {
          setGuest()
        }
      }, delayMs)
    },
    [clearExpiryTimer, setGuest],
  )

  const applySession = useCallback(
    (nextAccessToken: string, nextCsrfToken: string, nextUser: PublicUser, expiresInSeconds: number) => {
      csrfTokenRef.current = nextCsrfToken
      setAccessToken(nextAccessToken)
      setUser(nextUser)
      setStatus('authenticated')
      scheduleExpiry(expiresInSeconds)
    },
    [scheduleExpiry],
  )

  // 앱이 처음 뜰 때(새로고침, Google 로그인 콜백 복귀 포함) 유효한 refresh 쿠키가
  // 있는지 조용히 확인한다. 실패하면 조용히 guest로 남는다 — 에러를 화면에 보여줄
  // 대상이 없는 자동 확인이기 때문이다.
  useEffect(() => {
    let cancelled = false

    async function bootstrap() {
      try {
        const refreshed = await refreshWithRetry()
        const me = await getMe(refreshed.access_token)
        if (cancelled) return
        applySession(refreshed.access_token, refreshed.csrf_token, me.user, refreshed.expires_in)
      } catch {
        if (cancelled) return
        setGuest()
      }
    }

    bootstrap()

    return () => {
      cancelled = true
      clearExpiryTimer()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      const result = await apiLogin(email, password)
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in)
    },
    [applySession],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const result = await apiRegister(email, password)
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in)
    },
    [applySession],
  )

  const logout = useCallback(async () => {
    const csrfToken = csrfTokenRef.current
    try {
      if (csrfToken) {
        await apiLogout(csrfToken)
      }
    } finally {
      // 서버 요청이 실패해도(이미 만료된 세션 등) 화면은 비로그인 상태로 되돌린다.
      // access 차단 목록이 없어 서버에 남은 access가 최대 15분 더 통과할 수 있지만,
      // 이 화면이 그걸 더 들고 있을 이유는 없다.
      setGuest()
    }
  }, [setGuest])

  const value: AuthContextValue = { status, user, accessToken, login, register, logout }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  }
  return context
}
