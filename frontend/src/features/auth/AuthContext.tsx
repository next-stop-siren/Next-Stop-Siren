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
  const hardExpiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 로그인·회원가입·로그아웃·언마운트마다 증가하는 세대 번호. 비동기 요청을
  // 시작시킨 세션이 완료 시점에도 여전히 "현재" 세션인지 이 번호로 확인한다.
  // 예: A 계정의 갱신 응답이 늦는 사이 로그아웃 후 B로 로그인하면, A 시점에
  // 시작된 요청의 세대 번호(G1)와 그때의 현재 세대(로그아웃·로그인을 거쳐 G3)가
  // 달라지므로 늦게 도착한 A의 응답은 버려지고 B의 상태를 덮어쓰지 않는다.
  const sessionGenerationRef = useRef(0)

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

  // 현재 세션을 무효화하고 새 세대 번호를 반환한다. 로그인·회원가입·로그아웃·
  // 언마운트 시작 시점에 호출해, 그 이전 세대에서 시작된 비동기 작업(자동 갱신,
  // 부트스트랩 등)의 완료 처리가 이후 상태를 건드리지 못하도록 막는다.
  const invalidateSession = useCallback(() => {
    sessionGenerationRef.current += 1
    return sessionGenerationRef.current
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
  //
  // 이와 별도로 "강제 만료 타이머"를 같이 건다: /csrf나 /refresh 응답이 끝내
  // 돌아오지 않는 경우에도, 실제 만료 시각(expiresInSeconds 뒤)에는 무조건
  // 로컬 인증을 차단한다. 응답을 기다리는 시간만큼 만료가 뒤로 밀리지 않도록,
  // 이 타이머는 갱신 "시도"가 아니라 "성공"했을 때만 재스케줄되며 취소된다.
  const scheduleExpiry = useCallback(
    (expiresInSeconds: number, generation: number) => {
      clearExpiryTimer()

      const refreshDelayMs = Math.max((expiresInSeconds - 30) * 1000, 0)
      const hardExpiryMs = Math.max(expiresInSeconds * 1000, 0)

      hardExpiryTimerRef.current = setTimeout(() => {
        if (sessionGenerationRef.current !== generation) return
        setGuest()
      }, hardExpiryMs)

      expiryTimerRef.current = setTimeout(async () => {
        try {
          const refreshed = await refreshWithRetry()
          if (sessionGenerationRef.current !== generation) return

          // 다른 탭에서 계정이 바뀌면 공유 refresh 쿠키도 그 계정으로 바뀌어,
          // 이 탭의 다음 자동 갱신이 다른 계정의 access를 받을 수 있다. 그래서
          // access만 교체하지 않고 /me로 사용자 정보까지 함께 다시 맞춘다.
          const me = await getMe(refreshed.access_token)
          if (sessionGenerationRef.current !== generation) return

          csrfTokenRef.current = refreshed.csrf_token
          setAccessToken(refreshed.access_token)
          setUser(me.user)
          scheduleExpiry(refreshed.expires_in, generation)
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
    ) => {
      if (sessionGenerationRef.current !== generation) return
      csrfTokenRef.current = nextCsrfToken
      setAccessToken(nextAccessToken)
      setUser(nextUser)
      setStatus('authenticated')
      scheduleExpiry(expiresInSeconds, generation)
    },
    [scheduleExpiry],
  )

  // 앱이 처음 뜰 때(새로고침, Google 로그인 콜백 복귀 포함) 유효한 refresh 쿠키가
  // 있는지 조용히 확인한다. 실패하면 조용히 guest로 남는다 — 에러를 화면에 보여줄
  // 대상이 없는 자동 확인이기 때문이다.
  useEffect(() => {
    const generation = invalidateSession()

    async function bootstrap() {
      try {
        const refreshed = await refreshWithRetry()
        const me = await getMe(refreshed.access_token)
        applySession(refreshed.access_token, refreshed.csrf_token, me.user, refreshed.expires_in, generation)
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
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, generation)
    },
    [applySession, invalidateSession],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const generation = invalidateSession()
      const result = await apiRegister(email, password)
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, generation)
    },
    [applySession, invalidateSession],
  )

  const logout = useCallback(async () => {
    const csrfToken = csrfTokenRef.current

    // 서버 응답을 기다리지 않고 즉시 로컬 인증 상태와 자동 갱신 타이머를 비운다.
    // 응답이 늦어지는 동안에도 access·회원 메뉴가 계속 쓰이는 것을 막기 위해서다.
    // (서버 로그아웃 자체가 실패했을 때의 사용자 안내·재시도는 호출하는 쪽의 몫이며,
    // 이 함수는 실패 시 에러를 그대로 던진다.)
    invalidateSession()
    setGuest()

    if (csrfToken) {
      await apiLogout(csrfToken)
    }
  }, [setGuest, invalidateSession])

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
