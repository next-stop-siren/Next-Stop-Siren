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
  // 화면이 로그아웃 실패를 보여준 뒤 "다시 시도"에서 쓰는 전용 함수.
  // logout()과 달리 이미 비워진 csrfTokenRef에 의존하지 않고, 매번 새로
  // /api/auth/csrf를 읽어 그 값으로 서버 로그아웃을 다시 시도한다.
  retryLogout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// 세션 확인(csrf→refresh, /me)이 응답 없이 멈추는 경우, 화면이 'checking'
// 상태에 무한정 머무르지 않도록 두는 상한이다. 시간 안에 응답이 없으면
// 타임아웃으로 처리해 guest로 떨어뜨리고, 사용자는 평소처럼 로그인 버튼으로
// 다시 시도할 수 있다(= 재시도 경로).
const BOOTSTRAP_TIMEOUT_MS = 10000

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timeout')), ms)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

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
  const hardExpiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 로그인·회원가입·로그아웃·세션 종료마다 증가하는 세대 번호. 비동기 요청을
  // 시작시킨 세션이 완료 시점에도 여전히 "현재" 세션인지 이 번호로 확인한다.
  // setGuest()가 호출될 때마다(강제 만료, 로그아웃, 부트스트랩 실패 등) 세대를
  // 반드시 함께 올려야, 그 직후 뒤늦게 도착하는 이전 세대의 refresh·getMe
  // 응답이 이미 끝난 세션의 상태를 다시 덮어쓰지 못한다.
  const sessionGenerationRef = useRef(0)

  const invalidateSession = useCallback(() => {
    sessionGenerationRef.current += 1
    return sessionGenerationRef.current
  }, [])

  const clearExpiryTimer = useCallback(() => {
    if (expiryTimerRef.current !== null) {
      clearTimeout(expiryTimerRef.current)
      expiryTimerRef.current = null
    }
    if (hardExpiryTimerRef.current !== null) {
      clearTimeout(hardExpiryTimerRef.current)
      hardExpiryTimerRef.current = null
    }
  }, [])

  // setGuest()는 세션이 "끝났다"는 모든 경로(로그아웃, 자동 갱신 실패, 강제 만료,
  // 부트스트랩 실패)가 공유하는 유일한 출구다. 그래서 여기서 세대를 함께 올려야
  // 위에서 설명한 레이스 컨디션을 모든 경로에서 빠짐없이 막을 수 있다.
  const setGuest = useCallback(() => {
    invalidateSession()
    clearExpiryTimer()
    csrfTokenRef.current = null
    setAccessToken(null)
    setUser(null)
    setStatus('guest')
  }, [clearExpiryTimer, invalidateSession])

  // access는 15분(900초)짜리라 만료 30초 전에 선제적으로 갱신을 시도한다.
  // 갱신이 실패하면(쿠키도 만료·폐기된 경우) 회원 화면을 차단하고 guest로 되돌린다.
  //
  // issuedAt은 이 access가 "실제로 발급된 시각"이다. /me 조회처럼 발급 이후에
  // 걸리는 추가 네트워크 시간은 만료 시계에 포함하면 안 되므로, 타이머 지연은
  // 항상 issuedAt 기준 남은 시간으로 계산한다 — "지금부터 다시 900초"가 아니라
  // "발급 시점부터 900초 중 이미 흐른 시간을 뺀 나머지"를 쓴다.
  //
  // 강제 만료 타이머는 갱신 "시도"와 별개로, 실제 만료 시각에는 무조건 발동해
  // /csrf·/refresh 응답이 끝내 오지 않는 경우에도 로컬 인증을 차단한다.
  const scheduleExpiry = useCallback(
    (expiresInSeconds: number, generation: number, issuedAt: number) => {
      clearExpiryTimer()

      const elapsedMs = Date.now() - issuedAt
      const remainingMs = Math.max(expiresInSeconds * 1000 - elapsedMs, 0)
      const refreshDelayMs = Math.max(remainingMs - 30000, 0)

      hardExpiryTimerRef.current = setTimeout(() => {
        if (sessionGenerationRef.current !== generation) return
        setGuest()
      }, remainingMs)

      expiryTimerRef.current = setTimeout(async () => {
        try {
          const refreshed = await refreshWithRetry()
          if (sessionGenerationRef.current !== generation) return

          const refreshedAt = Date.now()

          // 다른 탭에서 계정이 바뀌면 공유 refresh 쿠키도 그 계정으로 바뀌어,
          // 이 탭의 다음 자동 갱신이 다른 계정의 access를 받을 수 있다. 그래서
          // access만 교체하지 않고 /me로 사용자 정보까지 함께 다시 맞춘다.
          const me = await getMe(refreshed.access_token)
          if (sessionGenerationRef.current !== generation) return

          csrfTokenRef.current = refreshed.csrf_token
          setAccessToken(refreshed.access_token)
          setUser(me.user)
          scheduleExpiry(refreshed.expires_in, generation, refreshedAt)
        } catch {
          if (sessionGenerationRef.current !== generation) return
          setGuest()
        }
      }, refreshDelayMs)
    },
    [clearExpiryTimer, setGuest],
  )

  const applySession = useCallback(
    (
      nextAccessToken: string,
      nextCsrfToken: string,
      nextUser: PublicUser,
      expiresInSeconds: number,
      generation: number,
      issuedAt: number,
    ) => {
      if (sessionGenerationRef.current !== generation) return
      csrfTokenRef.current = nextCsrfToken
      setAccessToken(nextAccessToken)
      setUser(nextUser)
      setStatus('authenticated')
      scheduleExpiry(expiresInSeconds, generation, issuedAt)
    },
    [scheduleExpiry],
  )

  // 앱이 처음 뜰 때(새로고침, Google 로그인 콜백 복귀 포함) 유효한 refresh 쿠키가
  // 있는지 조용히 확인한다. 실패하면(응답이 없는 타임아웃 포함) 조용히 guest로
  // 남는다 — 에러를 화면에 보여줄 대상이 없는 자동 확인이기 때문이다. guest로
  // 떨어지면 사용자는 평소 로그인 버튼으로 다시 시도할 수 있다.
  useEffect(() => {
    const generation = invalidateSession()

    async function bootstrap() {
      try {
        const refreshed = await withTimeout(refreshWithRetry(), BOOTSTRAP_TIMEOUT_MS)
        if (sessionGenerationRef.current !== generation) return
        const issuedAt = Date.now()

        const me = await withTimeout(getMe(refreshed.access_token), BOOTSTRAP_TIMEOUT_MS)
        if (sessionGenerationRef.current !== generation) return

        applySession(refreshed.access_token, refreshed.csrf_token, me.user, refreshed.expires_in, generation, issuedAt)
      } catch {
        if (sessionGenerationRef.current !== generation) return
        setGuest()
      }
    }

    bootstrap()

    return () => {
      invalidateSession()
      clearExpiryTimer()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      const generation = invalidateSession()
      const result = await apiLogin(email, password)
      const issuedAt = Date.now()
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, generation, issuedAt)
    },
    [applySession, invalidateSession],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const generation = invalidateSession()
      const result = await apiRegister(email, password)
      const issuedAt = Date.now()
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, generation, issuedAt)
    },
    [applySession, invalidateSession],
  )

  const logout = useCallback(async () => {
    const csrfToken = csrfTokenRef.current

    // 서버 응답을 기다리지 않고 즉시 로컬 인증 상태·타이머·세대를 비운다.
    // 응답이 늦어지는 동안에도 access·회원 메뉴가 계속 쓰이는 것을 막기 위해서다.
    // 실패하면 에러를 그대로 던진다 — 호출하는 쪽(App.tsx)이 실패 안내와
    // "다시 시도"를 보여줄 수 있도록.
    setGuest()

    if (csrfToken) {
      await apiLogout(csrfToken)
    }
  }, [setGuest])

  // "다시 시도"에서 쓰는 전용 경로. logout()이 이미 setGuest()로 csrfTokenRef를
  // 비워버렸으므로, 그 값에 기대지 않고 매번 새로 /api/auth/csrf를 읽어 받은
  // CSRF 값으로 서버 로그아웃을 다시 요청한다. refresh 쿠키 자체가 이미 없다면
  // (401) 서버 세션도 이미 사라진 것으로 보고 성공으로 간주한다.
  const retryLogout = useCallback(async () => {
    try {
      const { csrf_token } = await fetchCsrf()
      await apiLogout(csrf_token)
    } catch (error) {
      if (error instanceof AuthApiError && error.status === 401) return
      throw error
    }
  }, [])

  const value: AuthContextValue = { status, user, accessToken, login, register, logout, retryLogout }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  }
  return context
}
