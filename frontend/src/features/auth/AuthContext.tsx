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
  retryLogout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

// 403(csrf_failed) 또는 409(refresh_race)를 받으면 잠시 기다린 뒤 /csrf를 다시 읽고
// refresh를 한 번만 재시도한다. 원 요청을 포함해 POST는 최대 두 번이다.
//
// knownCsrfToken이 있으면(이미 로그인된 상태에서 거는 자동 갱신) 그 토큰으로 곧바로
// refresh부터 시도한다 — 매번 /csrf를 먼저 조회할 필요가 없다. knownCsrfToken이
// 없으면(앱 최초 부트스트랩처럼 아직 아무 토큰도 모를 때) /csrf부터 조회한다.
async function refreshWithRetry(knownCsrfToken?: string) {
  const firstToken = knownCsrfToken ?? (await fetchCsrf()).csrf_token
  try {
    return await refresh(firstToken)
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

  // 만료 관련 타이머는 두 개로 분리한다.
  // - soft: 만료 30초 전에 "조용히" 갱신을 시도한다. 실패해도(네트워크 문제,
  //   서버 CSRF 서비스 일시 장애 등) 곧바로 로그아웃시키지 않는다 — 이 시점에도
  //   서버가 발급한 access는 아직 유효 기간 안이기 때문이다.
  // - hard: 토큰 발급 시점(issuedAt) + expires_in에 정확히 발동하며, 그때까지
  //   갱신이 성공하지 못했다면(soft가 실패했든, 응답 자체가 안 왔든) 무조건
  //   로그아웃시킨다. "refresh 응답이 멈춘 채 만료 시각을 넘기는" 경우의 안전망이다.
  const softExpiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const hardExpiryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // "세대" 카운터: login/register/logout(setGuest)가 호출될 때마다 올라간다.
  // 이보다 먼저 시작된 비동기 작업(특히 타이머가 건 자동 refresh)이 뒤늦게
  // 끝나더라도, 그 결과를 지금 세대의 state에 반영하지 않기 위한 용도다.
  // 주의: 이것만으로는 R118을 막지 못한다 — state 쓰기만 막지, 서버로 나간
  // 실제 쿠키 변경 요청 자체는 못 막기 때문이다. 아래 뮤텍스가 그 역할을 한다.
  const generationRef = useRef(0)

  // 쿠키(refresh·CSRF)를 바꾸는 요청들의 뮤텍스. login/register/logout과
  // 타이머가 거는 자동 refresh는 전부 이 큐를 통해서만 네트워크로 나간다.
  // 즉 A의 logout 요청이 서버에서 아직 완료되지 않았다면 B의 login 요청은
  // '전송 자체'가 보류된다 — logout이 끝난 뒤에야 login이 나가므로, 응답이
  // 어떤 순서로 돌아오든 쿠키에 마지막으로 반영되는 요청은 항상 더 나중에
  // "시작된"(= 사용자 의도상 더 최신인) 요청이 된다.
  const mutationQueueRef = useRef<Promise<unknown>>(Promise.resolve())

  const enqueueMutation = useCallback(<T,>(fn: () => Promise<T>): Promise<T> => {
    const run = mutationQueueRef.current.catch(() => undefined).then(fn)
    // 큐 자체는 절대 reject 상태로 남지 않는다 — 한 요청의 실패가 이후
    // 요청들을 영구히 막으면 안 되기 때문이다. 실제 에러는 run을 통해 호출자에게 간다.
    mutationQueueRef.current = run.then(
      () => undefined,
      () => undefined,
    )
    return run
  }, [])

  const clearExpiryTimers = useCallback(() => {
    if (softExpiryTimerRef.current !== null) {
      clearTimeout(softExpiryTimerRef.current)
      softExpiryTimerRef.current = null
    }
    if (hardExpiryTimerRef.current !== null) {
      clearTimeout(hardExpiryTimerRef.current)
      hardExpiryTimerRef.current = null
    }
  }, [])

  const setGuest = useCallback(() => {
    generationRef.current += 1
    clearExpiryTimers()
    csrfTokenRef.current = null
    setAccessToken(null)
    setUser(null)
    setStatus('guest')
  }, [clearExpiryTimers])

  // access는 15분(900초)짜리다. 만료 30초 전에 soft 타이머로 선제 갱신을
  // 시도하고, 그와 별도로 "토큰을 실제로 받은 시각(issuedAtMs) + expires_in"
  // 절대 시각에 hard 타이머를 건다. soft가 실패해도 hard가 발동하기 전까지는
  // 로그인 상태를 유지한다 — 그 시점까지는 서버에 있는 access가 여전히
  // 유효하기 때문이다. hard는 soft의 성공 여부와 무관하게 정확히 그 시각에
  // 무조건 로그아웃시키는 안전망이다.
  //
  // expiresInSeconds가 아니라 expiresAtMs(절대 시각)를 기준으로 계산하는 이유:
  // 이 함수를 호출하는 시점(예: /me 조회가 끝난 뒤)이 토큰을 실제로 발급받은
  // 시점보다 늦을 수 있다. "지금부터 expiresInSeconds"로 타이머를 걸면, 그
  // 사이에 흐른 시간만큼 실제 만료보다 로그인 상태가 더 오래 유지되어 버린다.
  // 그래서 항상 "토큰을 받은 바로 그 순간"을 호출자가 넘겨주게 하고, 여기서는
  // 그 시각 + expires_in을 고정된 목표 시각(expiresAtMs)으로만 다룬다.
  //
  // 자동 refresh(soft)도 뮤텍스를 거친다: 타이머가 발동한 시점에 마침
  // 로그아웃이 진행 중이면, 그 로그아웃이 끝날 때까지 기다렸다가 나간다.
  const scheduleExpiry = useCallback(
    (issuedAtMs: number, expiresInSeconds: number, generation: number) => {
      clearExpiryTimers()

      const expiresAtMs = issuedAtMs + expiresInSeconds * 1000
      const softAtMs = expiresAtMs - 30_000
      const softDelayMs = Math.max(softAtMs - Date.now(), 0)
      const hardDelayMs = Math.max(expiresAtMs - Date.now(), 0)
      const csrfTokenAtScheduleTime = csrfTokenRef.current

      softExpiryTimerRef.current = setTimeout(() => {
        enqueueMutation(() => refreshWithRetry(csrfTokenAtScheduleTime ?? undefined))
          .then((refreshed) => {
            // 이 타이머가 걸린 뒤 로그아웃/재로그인 등으로 세대가 바뀌었다면
            // (= 이미 다른 세션으로 넘어갔다면) 이 결과는 폐기한다. 타임아웃으로
            // 끝난 뒤 뒤늦게 들어오는 결과도 여기서 자연히 걸러진다.
            if (generationRef.current !== generation) return
            csrfTokenRef.current = refreshed.csrf_token
            setAccessToken(refreshed.access_token)
            // refresh 응답을 실제로 "받은" 이 시각 기준으로 다음 만료를 다시
            // 계산한다 — 403/409 재시도로 1초를 더 썼더라도 그 시간이 다음
            // 만료 계산에 묻히지 않는다.
            scheduleExpiry(Date.now(), refreshed.expires_in, generation)
          })
          .catch(() => {
            // 의도적으로 여기서는 setGuest()를 호출하지 않는다. soft 갱신
            // 실패가 "이 access가 지금 무효"라는 뜻은 아니다 — 서버는 여전히
            // issuedAt + expires_in까지 이 access를 유효하다고 본다. 타임아웃
            // (AuthTimeoutError)이든 명시적 거절(AuthApiError)이든 마찬가지로
            // 즉시 끊어내면 멀쩡한 세션을 조기에 로그아웃시키게 된다. 실제
            // 차단은 아래 hard 타이머가 맡는다.
          })
      }, softDelayMs)

      hardExpiryTimerRef.current = setTimeout(() => {
        // soft 타이머가 그 사이 성공해서 세대가 그대로 유지된 채 재스케줄됐다면
        // 이 특정 hard 타이머는 이미 clearExpiryTimers()로 취소됐을 것이다.
        // 따라서 여기 도달했다는 것 자체가 "이 세대의 세션이 soft 갱신으로
        // 구제되지 못한 채 실제 만료 시각에 도달했다"는 뜻이다.
        if (generationRef.current !== generation) return
        setGuest()
      }, hardDelayMs)
    },
    [clearExpiryTimers, enqueueMutation, setGuest],
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
    let cancelled = false

    // 이 부트스트랩이 시작될 때의 세대를 기록해둔다. getMe()가 느리게 끝나는
    // 동안 사용자가 직접 로그인/회원가입을 해버리면(= generation이 바뀌면),
    // 뒤늦게 끝난 이 부트스트랩의 결과가 그 새 세션을 덮어쓰면 안 된다.
    const bootstrapGeneration = generationRef.current

    async function bootstrap() {
      try {
        const refreshed = await enqueueMutation(refreshWithRetry)
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
      clearExpiryTimers()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      const result = await enqueueMutation(() => apiLogin(email, password))
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, Date.now())
    },
    [applySession, enqueueMutation],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const result = await enqueueMutation(() => apiRegister(email, password))
      applySession(result.access_token, result.csrf_token, result.user, result.expires_in, Date.now())
    },
    [applySession, enqueueMutation],
  )

  const logout = useCallback(async () => {
    // 화면의 인증 상태는 즉시 비운다(리뷰어 요청대로) — 사용자는 바로 비회원
    // 화면을 본다. 하지만 실제 네트워크 요청은 여전히 큐를 거쳐 나가므로,
    // 이 logout 호출 '직후' 사용자가 바로 다시 로그인해도 그 login 요청은
    // 이 logout 요청이 서버에서 완료된 뒤에야 전송된다 — 쿠키 레이스가 생기지 않는다.
    const csrfToken = csrfTokenRef.current
    setGuest()

    if (!csrfToken) return

    await enqueueMutation(() => apiLogout(csrfToken))
    // 실패하면 그대로 던진다 — 화면은 이미 guest지만, 서버에는 여전히
    // refresh·CSRF 쿠키가 남아 있을 수 있다는 걸 호출자(App)가 배너로
    // 알리고 retryLogout()으로 재시도할 수 있게 해야 한다.
  }, [enqueueMutation, setGuest])

  // logout()이 실패한 뒤의 "다시 시도"다. 원 시도에서 이미 setGuest()가
  // csrfTokenRef를 비웠으므로, 그 값을 재사용하면 서버에 아무 요청도 가지
  // 않고 조용히 "성공"한 것처럼 끝나버린다(서버 세션은 그대로 남는다). 그래서
  // 여기서는 /csrf로 새 토큰을 받아와 실제로 로그아웃을 다시 요청한다. 이
  // 요청도 같은 뮤텍스를 거치므로, 그 사이 새로 로그인한 세션의 쿠키 변경
  // 요청과 겹치지 않는다.
  const retryLogout = useCallback(async () => {
    await enqueueMutation(async () => {
      const { csrf_token } = await fetchCsrf()
      await apiLogout(csrf_token)
    })
  }, [enqueueMutation])

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
