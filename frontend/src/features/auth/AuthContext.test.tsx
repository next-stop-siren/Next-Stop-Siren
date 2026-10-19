// @vitest-environment jsdom
//
// S17 완료 확인 항목을 검증한다:
// 1) 이메일 로그인/회원가입 성공·실패 상태
// 2) access 만료 뒤 회원 화면 차단(guest로 복귀)
// 3) logout 후 회원 화면 차단
// 4) 403/409를 받았을 때 csrf→refresh 1회 재시도 계약
//
// AuthContext는 UI 없이 status 값만 노출하므로, 이 값을 직접 읽는 테스트용
// 소비자 컴포넌트를 하나 만들어 검증한다.

import { afterEach, expect, test, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AuthProvider, useAuth } from './AuthContext'
// (useAuth는 아래 R118 회귀 테스트들에서 ProbeWithRef 컴포넌트가 직접 사용한다.)

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

type ProbeActions = ReturnType<typeof useAuth>

function Probe({ actionsRef }: { actionsRef?: { current: ProbeActions | null } }) {
  const auth = useAuth()
  const { status, user, login, register, logout } = auth
  if (actionsRef) {
    actionsRef.current = auth
  }
  return (
    <div>
      <p data-testid="status">{status}</p>
      <p data-testid="email">{user?.email ?? ''}</p>
      <button onClick={() => login('a@test.com', 'password123!').catch(() => {})}>login</button>
      <button onClick={() => register('b@test.com', 'password123!').catch(() => {})}>register</button>
      <button onClick={() => logout().catch(() => {})}>logout</button>
    </div>
  )
}

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function errorBody(code: string, message = '실패') {
  return { error: { code, message, fields: {}, trace_id: 't' } }
}

function successBody(email: string, expires_in = 900) {
  return {
    user: { id: 'u1', email, created_at: '2026-01-01T00:00:00Z' },
    access_token: 'access-1',
    token_type: 'Bearer',
    expires_in,
    csrf_token: 'csrf-1',
  }
}

// 초기 부트스트랩(csrf→refresh)은 항상 401로 실패시켜 guest로 빠르게 정착시킨다.
function mockGuestBootstrap(extra: (url: string, init?: RequestInit) => Response | null) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf' && !init) {
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    const result = extra(url, init as RequestInit | undefined)
    if (result) return result
    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${url} ${JSON.stringify(init)}`)
  })
}

test('이메일 로그인 성공 시 authenticated로 전환된다', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/csrf') {
      // bootstrap의 첫 호출은 위에서 401 처리됨. 이후 호출(없어야 함)만 여기로 온다.
      return null
    }
    if (url === '/api/auth/login') {
      return jsonResponse(200, successBody('a@test.com'))
    }
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  await user.click(screen.getByText('login'))

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('email').textContent).toBe('a@test.com')
})

test('이메일 로그인 실패 시 guest 상태를 유지한다', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/login') {
      return jsonResponse(401, errorBody('unauthenticated', '이메일 또는 비밀번호가 올바르지 않습니다.'))
    }
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  await user.click(screen.getByText('login'))

  // 실패했으므로 authenticated로 넘어가지 않고 guest에 머문다.
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
})

test('회원가입 성공 시 authenticated로 전환된다', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/register') {
      return jsonResponse(200, successBody('b@test.com'))
    }
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
  await user.click(screen.getByText('register'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('email').textContent).toBe('b@test.com')
})

test('csrf_failed(403)를 받으면 1초 뒤 csrf→refresh를 한 번만 재시도한다', async () => {
  vi.useFakeTimers()

  let refreshCalls = 0
  let csrfCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      csrfCalls += 1
      return jsonResponse(
        csrfCalls === 1 ? 401 : 200,
        csrfCalls === 1 ? errorBody('unauthenticated') : { csrf_token: `csrf-${csrfCalls}` },
      )
    }
    if (url === '/api/auth/login') {
      // expires_in을 짧게 둬서 '만료 30초 전 자동 갱신' 타이머가 테스트 가능한 시간 안에 발동하게 한다.
      return jsonResponse(200, successBody('a@test.com', 31))
    }
    if (url === '/api/auth/refresh') {
      refreshCalls += 1
      if (refreshCalls === 1) {
        return jsonResponse(403, errorBody('csrf_failed'))
      }
      // expires_in을 넉넉히 둬서 이 테스트 범위 안에서 다음 자동 갱신이 다시 발동하지 않게 한다.
      return jsonResponse(200, {
        access_token: 'access-2',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-3',
      })
    }
    throw new Error(`예상하지 못한 호출: ${url} ${JSON.stringify(init)}`)
  })

  const actionsRef: { current: ProbeActions | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await act(async () => {
    await vi.runOnlyPendingTimersAsync()
  })
  expect(screen.getByTestId('status').textContent).toBe('guest')

  // login()을 직접 await해서 상태 반영(applySession, scheduleExpiry 등록)까지 확실히 끝낸다.
  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // expires_in=31초 → 만료 30초 전 공식이면 1초(1000ms) 뒤 refresh가 시도된다.
  // 첫 refresh는 403 → 1초 대기 후 두 번째 csrf+refresh로 복구된다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2200)
  })

  expect(refreshCalls).toBe(2) // 원 요청 포함 최대 2번
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  fetchSpy.mockRestore()
})

test('access 만료 뒤 refresh가 끝내 실패하면 회원 화면이 차단된다(guest로 전환)', async () => {
  // R107 추가 답글 반영: soft 갱신이 즉시 실패(401, 재시도 대상 아님)해도 그
  // 순간 바로 로그아웃시키지 않는다 — 서버가 발급한 access는 issuedAt +
  // expires_in까지는 여전히 유효하기 때문이다. 대신 hard 타이머가 실제 만료
  // 시각에 무조건 로그아웃시키는 안전망 역할을 한다. 그래서 이 테스트는
  // "soft 실패 직후"가 아니라 "실제 만료 시각"에 guest가 되는지를 확인한다.
  vi.useFakeTimers()

  let refreshCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      return jsonResponse(200, { csrf_token: 'csrf-x' })
    }
    if (url === '/api/auth/login') {
      // 만료 30초 전 자동 갱신 공식에 따라 31초면 1초 뒤 soft 갱신이 시도된다.
      return jsonResponse(200, successBody('a@test.com', 31))
    }
    if (url === '/api/auth/refresh') {
      refreshCalls += 1
      return jsonResponse(401, errorBody('unauthenticated')) // 재시도 대상 아님(403/409 아님) → 즉시 실패
    }
    return jsonResponse(401, errorBody('unauthenticated'))
  })

  const actionsRef: { current: ProbeActions | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await act(async () => {
    await vi.runOnlyPendingTimersAsync()
  })

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 마운트 시 부트스트랩이 이미 refresh를 1회 호출했으므로, 로그인 이후
  // 타이머로 발생하는 refresh만 세기 위해 여기서 카운터를 리셋한다.
  refreshCalls = 0

  // 1초 뒤 soft 갱신 시도, 401이라 재시도 없이 즉시 실패한다. 하지만 아직
  // 실제 만료 시각(31초)은 아니므로 로그인 상태가 유지되어야 한다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100)
  })
  expect(refreshCalls).toBe(1)
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 실제 만료 시각(issuedAt + 31초)에 도달하면 soft 갱신 성공 여부와 무관하게
  // hard 타이머가 로그인 상태를 끊어낸다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000)
  })

  expect(screen.getByTestId('status').textContent).toBe('guest')
  expect(screen.getByTestId('email').textContent).toBe('')

  fetchSpy.mockRestore()
})

test('logout 호출 뒤에는 회원 화면이 차단된다(guest로 전환)', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    if (url === '/api/auth/logout') return new Response(null, { status: 204 })
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
  await user.click(screen.getByText('login'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

  await user.click(screen.getByText('logout'))

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
  expect(screen.getByTestId('email').textContent).toBe('')
})

test('logout API가 실패해도 화면은 guest로 차단된다', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    if (url === '/api/auth/logout') return jsonResponse(500, errorBody('temporarily_unavailable'))
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
  await user.click(screen.getByText('login'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

  await user.click(screen.getByText('logout'))

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
})

// --- R118 회귀 테스트: A 계정 로그아웃이 느리게 끝나는 동안 B 계정으로 로그인 ---
//
// 뮤텍스(enqueueMutation) 설계의 핵심 주장: A의 /api/auth/logout 요청이 서버에서
// '완료'되기 전에는 B의 /api/auth/login fetch가 '호출조차' 되지 않는다. 그래서
// 응답이 어떤 순서로 돌아오든 쿠키에 마지막으로 반영되는 요청은 항상 더 나중에
// 시작된(= B 로그인) 쪽이 된다.

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

test('A 로그아웃이 느린 동안 B로 로그인하면, B의 login 요청은 A의 logout이 끝난 뒤에만 전송된다', async () => {
  const logoutGate = deferred<Response>()
  let logoutCalledAt = -1
  let loginCalledAt = -1
  let callCounter = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    callCounter += 1

    if (url === '/api/auth/csrf') {
      // bootstrap은 401로 바로 guest 정착.
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    if (url === '/api/auth/login') {
      loginCalledAt = callCounter
      return jsonResponse(200, successBody('b@test.com'))
    }
    if (url === '/api/auth/logout') {
      logoutCalledAt = callCounter
      // 이 요청은 테스트가 명시적으로 resolve할 때까지 응답하지 않는다(= "느린 로그아웃").
      return logoutGate.promise
    }
    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${url}`)
  })

  const actionsRef: { current: ReturnType<typeof useAuth> | null } = { current: null }

  function ProbeWithRef() {
    const auth = useAuth()
    actionsRef.current = auth
    return <p data-testid="status">{auth.status}</p>
  }

  render(
    <AuthProvider>
      <ProbeWithRef />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  // A로 로그인된 상태를 가정하지 않고, csrfTokenRef를 직접 채울 수 없으므로
  // 먼저 A로 실제 로그인시킨 뒤 바로 로그아웃 → B 로그인을 연달아 트리거한다.
  fetchSpy.mockImplementationOnce(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    callCounter += 1
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // logout()은 즉시 반환되지 않고 내부적으로 큐에 올라간 apiLogout을 기다리므로
  // await하지 않고 바로 다음 login을 트리거한다(= "로그아웃 응답이 늦는 동안 로그인 클릭").
  let logoutPromise: Promise<void> | undefined
  let loginPromise: Promise<void> | undefined

  await act(async () => {
    logoutPromise = actionsRef.current?.logout()
    // 화면은 logout() 호출과 동시에 바로 guest로 비워진다.
  })
  expect(screen.getByTestId('status').textContent).toBe('guest')

  await act(async () => {
    loginPromise = actionsRef.current?.login('b@test.com', 'password123!')
    // 아직 logout 네트워크 요청을 풀어주지 않았으므로, 이 시점엔 login fetch가
    // 호출되지 않았어야 한다 — 큐에서 대기 중이어야 한다.
    await Promise.resolve()
    await Promise.resolve()
  })
  expect(loginCalledAt).toBe(-1)

  // 이제 A의 느린 logout 응답을 풀어준다.
  await act(async () => {
    logoutGate.resolve(new Response(null, { status: 204 }))
    await logoutPromise
    await loginPromise
  })

  expect(logoutCalledAt).toBeGreaterThan(-1)
  expect(loginCalledAt).toBeGreaterThan(-1)
  // login fetch는 logout fetch가 "호출된" 뒤에만 호출될 수 있는데, 뮤텍스가
  // 보장하는 것은 더 강하다: logout이 '완료'된 뒤에만 login이 호출된다.
  expect(loginCalledAt).toBeGreaterThan(logoutCalledAt)

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

  fetchSpy.mockRestore()
})

test('로그아웃 응답보다 로그인 응답이 먼저 처리되어도 최종 상태는 B 계정으로 유지된다', async () => {
  const logoutGate = deferred<Response>()

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    if (url === '/api/auth/logout') {
      return logoutGate.promise
    }
    if (url === '/api/auth/login') {
      // 이 login 응답 자체는 즉시 오지만, 뮤텍스 때문에 이 fetch 호출 자체가
      // logout이 끝난 뒤에만 발생하므로 "먼저 처리"될 수 없다 — 그게 이 테스트의 요지다.
      return jsonResponse(200, successBody('b@test.com'))
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  const actionsRef: { current: ReturnType<typeof useAuth> | null } = { current: null }

  function ProbeWithRef() {
    const auth = useAuth()
    actionsRef.current = auth
    return (
      <div>
        <p data-testid="status">{auth.status}</p>
        <p data-testid="email">{auth.user?.email ?? ''}</p>
      </div>
    )
  }

  render(
    <AuthProvider>
      <ProbeWithRef />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  fetchSpy.mockImplementationOnce(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })

  let logoutPromise: Promise<void> | undefined
  let loginPromise: Promise<void> | undefined

  await act(async () => {
    logoutPromise = actionsRef.current?.logout()
    loginPromise = actionsRef.current?.login('b@test.com', 'password123!')
  })

  await act(async () => {
    logoutGate.resolve(new Response(null, { status: 204 }))
    await logoutPromise
    await loginPromise
  })

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('email').textContent).toBe('b@test.com')

  fetchSpy.mockRestore()
})

test('logout API가 실패해도 뮤텍스가 막히지 않아 다음 login이 정상 진행된다', async () => {
  const user = userEvent.setup()

  mockGuestBootstrap((url) => {
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    if (url === '/api/auth/logout') return jsonResponse(500, errorBody('temporarily_unavailable'))
    return null
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))
  await user.click(screen.getByText('login'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))

  await user.click(screen.getByText('logout'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  // 로그아웃 API가 500으로 실패했더라도, 같은 큐를 쓰는 다음 login이 영원히
  // 막히지 않고 정상적으로 전송·처리되어야 한다.
  await user.click(screen.getByText('login'))
  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('email').textContent).toBe('a@test.com')
})

// --- R107 회귀 테스트: issuedAt 기준 만료, 지연 응답 무시, 타임아웃 ---

// AbortSignal을 존중하는 "응답하지 않는" fetch 핸들러. authApi의 타임아웃이
// 실제로 AbortController.abort()를 호출하면 그제서야 reject된다.
function hangUntilAborted(signal?: AbortSignal | null): Promise<Response> {
  return new Promise((_, reject) => {
    if (!signal) return
    if (signal.aborted) {
      reject(new DOMException('aborted', 'AbortError'))
      return
    }
    signal.addEventListener('abort', () => {
      reject(new DOMException('aborted', 'AbortError'))
    })
  })
}

test('부트스트랩에서 /me 응답이 120초 지연돼도 만료는 refresh를 받은 시각 기준으로 계산된다', async () => {
  // 리뷰 지적 재현: /me 조회가 끝난 뒤부터 다시 만료를 세면, 느려진 만큼
  // 로그인 상태가 실제보다 오래 유지된다. issuedAt(= refresh 응답을 받은 시각)
  // 기준으로 고정되어 있는지를, "그 기준 만료 시각에 정확히 로그아웃되는지"로
  // 간접 검증한다.
  vi.useFakeTimers()

  let refreshCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'csrf-x' })
    if (url === '/api/auth/refresh') {
      refreshCalls += 1
      if (refreshCalls === 1) {
        return jsonResponse(200, {
          access_token: 'access-1',
          token_type: 'Bearer',
          expires_in: 200,
          csrf_token: 'csrf-y',
        })
      }
      // 소프트 갱신 시도(issuedAt+170초)가 섞여 들어와 만료를 다시 늘리지
      // 않도록, 두 번째 호출부터는 실패시킨다. 그래야 hard 타이머만으로
      // "issuedAt 기준 200초"가 정확히 지켜지는지 검증할 수 있다.
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    if (url === '/api/me') {
      await new Promise((resolve) => setTimeout(resolve, 120_000))
      return jsonResponse(200, { user: { id: 'u1', email: 'late@test.com', created_at: '2026-01-01T00:00:00Z' } })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  expect(screen.getByTestId('status').textContent).toBe('checking')

  // /me가 120초 뒤에 끝난다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120_000)
  })
  // fake timer가 켜진 상태에서는 waitFor의 내부 폴링(setTimeout 기반)이
  // 가짜 시계를 기다리게 되어 멈춘다 — act() 직후 바로 동기적으로 확인한다.
  expect(screen.getByTestId('status').textContent).toBe('authenticated')
  expect(screen.getByTestId('email').textContent).toBe('late@test.com')

  // "/me가 끝난 시점부터" 다시 200초를 센다면 이 시점엔 여전히 authenticated여야
  // 하지만, issuedAt(= refresh 수신 시각) 기준이라면 issuedAt+200초인 지금
  // guest여야 한다. (지금까지 120초 지남 → 80초 더 흘리면 총 200초.)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(80_000)
  })

  expect(screen.getByTestId('status').textContent).toBe('guest')

  fetchSpy.mockRestore()
})

test('부트스트랩의 느린 응답이 그 사이 사용자가 직접 로그인한 세션을 덮어쓰지 않는다', async () => {
  vi.useFakeTimers()

  let resolveMe: ((value: Response) => void) | null = null

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'csrf-boot' })
    if (url === '/api/auth/refresh') {
      return jsonResponse(200, {
        access_token: 'boot-access',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-boot2',
      })
    }
    if (url === '/api/me') {
      // 부트스트랩의 /me는 테스트가 수동으로 풀어줄 때까지 대기한다.
      return new Promise<Response>((resolve) => {
        resolveMe = resolve
      })
    }
    if (url === '/api/auth/login') {
      return jsonResponse(200, successBody('manual@test.com'))
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  const actionsRef: { current: ProbeActions | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  expect(screen.getByTestId('status').textContent).toBe('checking')

  // 부트스트랩의 /me가 아직 안 끝난 상태에서, 사용자가 직접 로그인 폼을 제출한다.
  await act(async () => {
    await actionsRef.current?.login('manual@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')
  expect(screen.getByTestId('email').textContent).toBe('manual@test.com')

  // 이제서야 부트스트랩의 오래된 /me가 응답한다 — 다른 사용자 정보로 현재
  // (수동 로그인된) 세션을 덮어쓰면 안 된다.
  await act(async () => {
    resolveMe?.(
      jsonResponse(200, { user: { id: 'u-boot', email: 'boot@test.com', created_at: '2026-01-01T00:00:00Z' } }),
    )
    await Promise.resolve()
    await Promise.resolve()
  })

  expect(screen.getByTestId('status').textContent).toBe('authenticated')
  expect(screen.getByTestId('email').textContent).toBe('manual@test.com')

  fetchSpy.mockRestore()
})

test('초기 세션 확인(csrf)이 응답하지 않아도 타임아웃 후 guest로 종료되어 checking에 머물지 않는다', async () => {
  vi.useFakeTimers()

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') {
      return hangUntilAborted((init as RequestInit | undefined)?.signal)
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  expect(screen.getByTestId('status').textContent).toBe('checking')

  // authApi의 기본 타임아웃(10초)을 넘기면 AbortController가 요청을 끊는다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })

  // fake timer가 켜진 상태에서는 waitFor의 내부 폴링이 가짜 시계를 기다리게
  // 되어 멈춘다 — act() 직후 바로 동기적으로 확인한다.
  expect(screen.getByTestId('status').textContent).toBe('guest')

  fetchSpy.mockRestore()
})

test('자동 갱신 중 refresh 응답이 멈춰도 타임아웃 후 처리가 종료되고, 이후 로그인 큐도 막히지 않는다', async () => {
  vi.useFakeTimers()

  let bootstrapDone = false

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    const signal = (init as RequestInit | undefined)?.signal

    if (url === '/api/auth/csrf') {
      if (!bootstrapDone) return jsonResponse(401, errorBody('unauthenticated'))
      return jsonResponse(200, { csrf_token: 'csrf-z' })
    }
    if (url === '/api/auth/login') {
      // 만료 30초 전 자동 갱신이 금방 걸리도록 짧게 둔다.
      return jsonResponse(200, successBody('a@test.com', 31))
    }
    if (url === '/api/auth/refresh') {
      // soft 타이머가 건 refresh는 응답 없이 멈춘다 — timeout으로만 끝난다.
      return hangUntilAborted(signal)
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  const actionsRef: { current: ProbeActions | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await act(async () => {
    await vi.runOnlyPendingTimersAsync()
  })
  bootstrapDone = true

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 1초 뒤 soft 갱신 시도 → refresh가 응답 없이 멈춤 → 10초 뒤 타임아웃으로
  // 끝나지만, soft 실패는 즉시 로그아웃시키지 않으므로 아직 authenticated다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(11_000)
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 실제 만료 시각(31초)에는 hard 타이머가 무조건 로그아웃시킨다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000)
  })
  expect(screen.getByTestId('status').textContent).toBe('guest')

  // 뮤텍스가 막히지 않았는지: 이어서 로그인이 정상적으로 진행되어야 한다.
  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  fetchSpy.mockRestore()
})

test('다른 탭에서 계정이 바뀐 뒤 자동 갱신이 일어나면 user도 함께 최신화된다', async () => {
  // 리뷰 지적 재현: refresh·CSRF 쿠키는 탭 사이에 공유된다. 다른 탭에서 B
  // 계정으로 로그인하면 이 탭의 다음 자동 갱신도 B의 access를 받는다. access만
  // 갱신하고 user를 그대로 두면 "A 사용자 정보 + B access" 조합이 생긴다.
  vi.useFakeTimers()

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com', 31))
    if (url === '/api/auth/refresh') {
      // 다른 탭에서 B로 로그인해 공유 쿠키가 바뀐 상황 — 이 refresh는 B의
      // access를 돌려준다.
      return jsonResponse(200, {
        access_token: 'access-b',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-b',
      })
    }
    if (url === '/api/me') {
      return jsonResponse(200, { user: { id: 'u-b', email: 'b@test.com', created_at: '2026-01-01T00:00:00Z' } })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  const actionsRef: { current: ProbeActions | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await act(async () => {
    await vi.runOnlyPendingTimersAsync()
  })

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('email').textContent).toBe('a@test.com')

  // 1초 뒤 soft 갱신이 발동 → 공유 쿠키가 이미 B로 바뀐 상태라 B의 access가
  // 돌아온다. /me까지 다시 조회해 user를 B로 갱신해야 한다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_100)
  })

  expect(screen.getByTestId('status').textContent).toBe('authenticated')
  expect(screen.getByTestId('email').textContent).toBe('b@test.com')

  fetchSpy.mockRestore()
})

test('부트스트랩 성공 시 csrf→refresh→me 순서로 세션을 복원한다', async () => {
  const calls: string[] = []

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    calls.push(url)

    if (url === '/api/auth/csrf') {
      return jsonResponse(200, { csrf_token: 'csrf-boot' })
    }

    if (url === '/api/auth/refresh') {
      return jsonResponse(200, {
        access_token: 'access-boot',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-boot-2',
      })
    }

    if (url === '/api/me') {
      return jsonResponse(200, {
        user: {
          id: 'u1',
          email: 'restored@test.com',
          created_at: '2026-01-01T00:00:00Z',
        },
      })
    }

    throw new Error(`예상하지 못한 fetch 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => {
    expect(screen.getByTestId('status').textContent).toBe('authenticated')
  })

  expect(screen.getByTestId('email').textContent).toBe('restored@test.com')
  expect(calls).toEqual(['/api/auth/csrf', '/api/auth/refresh', '/api/me'])
  fetchSpy.mockRestore()
})

test('부트스트랩에서 409(refresh_race)를 받으면 csrf→refresh를 한 번 재시도한다', async () => {
  vi.useFakeTimers()

  const calls: string[] = []
  let refreshCalls = 0
  let csrfCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    calls.push(url)

    if (url === '/api/auth/csrf') {
      csrfCalls += 1
      return jsonResponse(200, {
        csrf_token: `csrf-${csrfCalls}`,
      })
    }

    if (url === '/api/auth/refresh') {
      refreshCalls += 1

      if (refreshCalls === 1) {
        return jsonResponse(409, errorBody('refresh_race'))
      }

      return jsonResponse(200, {
        access_token: 'access-retried',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-retried',
      })
    }

    if (url === '/api/me') {
      return jsonResponse(200, {
        user: {
          id: 'u1',
          email: 'retry@test.com',
          created_at: '2026-01-01T00:00:00Z',
        },
      })
    }

    throw new Error(`예상하지 못한 fetch 호출: ${url}`)
  })

  try {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    // 최초 CSRF 조회와 refresh 요청이 처리되고 재시도 타이머가
    // 등록될 수 있도록 비동기 작업을 진행한다.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(screen.getByTestId('status').textContent).toBe('authenticated')
    expect(screen.getByTestId('email').textContent).toBe('retry@test.com')

    expect(refreshCalls).toBe(2)
    expect(csrfCalls).toBe(2)

    expect(calls).toEqual(['/api/auth/csrf', '/api/auth/refresh', '/api/auth/csrf', '/api/auth/refresh', '/api/me'])
  } finally {
    fetchSpy.mockRestore()
  }
})
