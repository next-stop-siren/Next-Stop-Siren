import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { fetchCsrf, getMe, login as apiLogin, logout as apiLogout, register as apiRegister } from './authApi'
import type { PublicUser } from './authApi'
import { refreshWithRetry } from './authRefresh'
import { createMutationQueue } from './mutationQueue'
import { createExpiryTimers } from './expiryTimers'

type AuthStatus = 'checking' | 'guest' | 'authenticated'

type AuthContextValue = {
  status: AuthStatus
  user: PublicUser | null
  accessToken: string | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  retryLogout: () => Promise<void>
  logoutError: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('checking')
  const [user, setUser] = useState<PublicUser | null>(null)
  const [accessToken, setAccessToken] = useState<string | null>(null)
  const [logoutFailure, setLogoutFailure] = useState<number | null>(null)
  const logoutFailureRef = useRef<number | null>(null)

  // 화면 재렌더와 무관한 내부 상태라 ref로 둔다. access/csrf는 메모리 전용이며
  // localStorage, 쿠키 등 어떤 저장소에도 쓰지 않는다.
  const csrfTokenRef = useRef<string | null>(null)

  // 세션 교체와 mount 종료는 다른 경계다. queued login A→B는 허용하되,
  // unmount 이전 요청이 뒤늦게 완료되어 타이머를 다시 만들면 안 된다.
  const lifecycleEpochRef = useRef(0)
  const activeRef = useRef(true)
  const generationRef = useRef(0)
  const [enqueue] = useState(createMutationQueue)
  const [expiryTimers] = useState(createExpiryTimers)
  const clearExpiryTimers = expiryTimers.clear

  const enqueueMutation = useCallback(
    <T,>(operation: (isActive: () => boolean) => Promise<T>): Promise<T> => {
      const epoch = lifecycleEpochRef.current
      const isActive = () => activeRef.current && lifecycleEpochRef.current === epoch
      return enqueue(async () => {
        if (!isActive()) throw new Error('종료된 인증 세션의 요청입니다.')
        return operation(isActive)
      })
    },
    [enqueue],
  )

  const setGuest = useCallback(() => {
    generationRef.current += 1
    clearExpiryTimers()
    csrfTokenRef.current = null
    setAccessToken(null)
    setUser(null)
    setStatus('guest')
  }, [clearExpiryTimers])

  // 프런트엔드가 refresh 응답을 받은 시각에 expires_in을 더한다.
  // /me 대기 시간은 deadline을 연장하지 않는다. 이는 서버 JWT 시계나
  // 네트워크 전송 시간을 보정하거나 다른 탭의 쿠키 순서를 보장하지 않는다.
  const scheduleExpiry = useCallback(
    (issuedAtMs: number, expiresInSeconds: number, generation: number) => {
      const csrfTokenAtScheduleTime = csrfTokenRef.current
      expiryTimers.schedule(
        issuedAtMs + expiresInSeconds * 1000,
        () => {
          enqueueMutation((isActive) => {
            if (generationRef.current !== generation) throw new Error('이전 세션의 자동 갱신입니다.')
            return refreshWithRetry(csrfTokenAtScheduleTime ?? undefined, isActive)
          })
            .then(async (refreshed) => {
              // 이 타이머가 걸린 뒤 로그아웃/재로그인 등으로 세대가 바뀌었다면
              // (= 이미 다른 세션으로 넘어갔다면) 이 결과는 폐기한다. 타임아웃으로
              // 끝난 뒤 뒤늦게 들어오는 결과도 여기서 자연히 걸러진다.
              if (generationRef.current !== generation) return

              const issuedAtMs = Date.now()

              // refresh/CSRF 쿠키는 탭 사이에 공유된다. 다른 탭에서 B 계정으로
              // 로그인하면 이 탭의 다음 자동 갱신도 B의 access를 받게 되므로,
              // access만 바꾸고 user를 그대로 두면 "이전 탭의 user + 새 access"
              // 조합이 생긴다. 그래서 갱신된 access로 /me를 다시 조회해 user도
              // 함께 최신화한다.
              // 신원을 확인하지 못하면 새 access를 적용하지 않는다. 기존의 일관된
              // user/access와 hard 타이머를 유지해 기존 만료 시각에 차단한다.
              const me = await getMe(refreshed.access_token)
              if (generationRef.current !== generation) return

              csrfTokenRef.current = refreshed.csrf_token
              setAccessToken(refreshed.access_token)
              setUser(me.user)
              scheduleExpiry(issuedAtMs, refreshed.expires_in, generation)
            })
            .catch(() => {
              // 새 신원을 확인하지 못했으므로 기존 user/access와 hard deadline을 유지한다.
            })
        },
        () => {
          if (generationRef.current !== generation) return
          setGuest()
        },
      )
    },
    [expiryTimers, enqueueMutation, setGuest],
  )

  const applySession = useCallback(
    (
      nextAccessToken: string,
      nextCsrfToken: string,
      nextUser: PublicUser,
      expiresInSeconds: number,
      issuedAtMs: number,
    ) => {
      generationRef.current += 1
      const generation = generationRef.current
      logoutFailureRef.current = null
      setLogoutFailure(null)
      csrfTokenRef.current = nextCsrfToken
      setAccessToken(nextAccessToken)
      setUser(nextUser)
      setStatus('authenticated')
      scheduleExpiry(issuedAtMs, expiresInSeconds, generation)
    },
    [scheduleExpiry],
  )

  // 앱이 처음 뜰 때(새로고침, Google 로그인 콜백 복귀 포함) 유효한 refresh 쿠키가
  // 있는지 조용히 확인한다. 실패하면 조용히 guest로 남는다 — 에러를 화면에 보여줄
  // 대상이 없는 자동 확인이기 때문이다. bootstrap도 쿠키를 다루는 refresh를 쓰므로
  // 큐를 거쳐, 혹시 같은 타이밍에 다른 쿠키 요청과 겹치지 않게 한다.
  useEffect(() => {
    activeRef.current = true
    let cancelled = false

    // 이 부트스트랩이 시작될 때의 세대를 기록해둔다. getMe()가 느리게 끝나는
    // 동안 사용자가 직접 로그인/회원가입을 해버리면(= generation이 바뀌면),
    // 뒤늦게 끝난 이 부트스트랩의 결과가 그 새 세션을 덮어쓰면 안 된다.
    const bootstrapGeneration = generationRef.current

    async function bootstrap() {
      try {
        const refreshed = await enqueueMutation((isActive) => refreshWithRetry(undefined, isActive))
        if (cancelled || generationRef.current !== bootstrapGeneration) return
        // 토큰을 "받은" 바로 이 시점을 issuedAt으로 고정한다. 아래 getMe()가
        // 느리게 끝나더라도(리뷰에서 지적된, /me 120초 지연 시나리오) 만료
        // 계산은 이 시각 기준으로 움직이지 않는다.
        const issuedAtMs = Date.now()
        const me = await getMe(refreshed.access_token)
        if (cancelled || generationRef.current !== bootstrapGeneration) return
        applySession(refreshed.access_token, refreshed.csrf_token, me.user, refreshed.expires_in, issuedAtMs)
      } catch {
        if (cancelled || generationRef.current !== bootstrapGeneration) return
        setGuest()
      }
    }

    bootstrap()

    return () => {
      cancelled = true
      activeRef.current = false
      lifecycleEpochRef.current += 1
      generationRef.current += 1
      clearExpiryTimers()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      const epoch = lifecycleEpochRef.current
      const result = await enqueueMutation(() => apiLogin(email, password))
      if (lifecycleEpochRef.current !== epoch) return
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, Date.now())
    },
    [applySession, enqueueMutation],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const epoch = lifecycleEpochRef.current
      const result = await enqueueMutation(() => apiRegister(email, password))
      if (lifecycleEpochRef.current !== epoch) return
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, Date.now())
    },
    [applySession, enqueueMutation],
  )

  // 실패 상태는 로그아웃으로 비워진 세대에만 속한다. 새 로그인은 이를
  // 무효화하며, 이전 요청의 늦은 실패도 새 세션의 배너를 만들 수 없다.
  const requestLogout = useCallback(
    async (knownCsrfToken?: string) => {
      if (!activeRef.current) return
      setGuest()
      const generation = generationRef.current
      logoutFailureRef.current = null
      if (knownCsrfToken) setLogoutFailure(null)
      try {
        await enqueueMutation(async (isActive) => {
          if (generationRef.current !== generation) return
          const csrfToken = knownCsrfToken ?? (await fetchCsrf()).csrf_token
          if (!isActive() || generationRef.current !== generation) return
          await apiLogout(csrfToken)
        })
        if (generationRef.current === generation) setLogoutFailure(null)
      } catch (error) {
        if (generationRef.current === generation) {
          logoutFailureRef.current = generation
          setLogoutFailure(generation)
        }
        throw error
      }
    },
    [enqueueMutation, setGuest],
  )

  const logout = useCallback(async () => {
    if (!activeRef.current) return
    const csrfToken = csrfTokenRef.current
    if (!csrfToken) {
      setGuest()
      return
    }
    await requestLogout(csrfToken)
  }, [requestLogout, setGuest])

  // 렌더 당시 실패 세대를 캡처한다. 오래 보관한 콜백도 새 로그인이나
  // 이미 시작한 재시도 이후에는 현재 세션에 요청을 보낼 수 없다.
  const retryLogout = useCallback(async () => {
    if (
      logoutFailure === null ||
      logoutFailureRef.current !== logoutFailure ||
      generationRef.current !== logoutFailure
    ) {
      return
    }
    await requestLogout()
  }, [logoutFailure, requestLogout])

  const value: AuthContextValue = {
    status,
    user,
    accessToken,
    login,
    register,
    logout,
    retryLogout,
    logoutError: logoutFailure !== null,
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth는 AuthProvider 안에서만 사용할 수 있습니다.')
  }
  return context
}
