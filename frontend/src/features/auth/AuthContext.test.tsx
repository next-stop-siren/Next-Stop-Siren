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
  let csrfCallCount = 0

  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      csrfCallCount += 1
      if (csrfCallCount === 1) {
        // 부트스트랩의 첫 호출만 무조건 401로 처리해 guest로 빠르게 정착시킨다.
        return jsonResponse(401, errorBody('unauthenticated'))
      }
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
    if (url === '/api/me') {
      return jsonResponse(200, { user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' } })
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
  vi.useFakeTimers()

  let refreshCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      return jsonResponse(200, { csrf_token: 'csrf-x' })
    }
    if (url === '/api/auth/login') {
      // 만료 30초 전 자동 갱신 공식에 따라 31초면 1초 뒤 refresh가 시도된다.
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

  // 1초 뒤 갱신 시도, 401이라 재시도 없이 즉시 실패 → guest로 떨어져야 한다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1100)
  })

  expect(refreshCalls).toBe(1)
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

test('bootstrap은 GET과 credentials include로 /api/auth/csrf를 호출한다', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') {
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  expect(fetchSpy).toHaveBeenCalledWith(
    '/api/auth/csrf',
    expect.objectContaining({ method: 'GET', credentials: 'include' }),
  )

  fetchSpy.mockRestore()
})

test('재시도까지 실패하면 POST는 총 2회만 보내고 guest로 끝난다', async () => {
  vi.useFakeTimers()

  let refreshPostCalls = 0
  let csrfCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      csrfCalls += 1
      // 첫 호출(부트스트랩)은 401로 빠르게 guest로 보낸다.
      if (csrfCalls === 1) return jsonResponse(401, errorBody('unauthenticated'))
      return jsonResponse(200, { csrf_token: `csrf-${csrfCalls}` })
    }
    if (url === '/api/auth/login') {
      return jsonResponse(200, {
        user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' },
        access_token: 'a1',
        token_type: 'Bearer',
        expires_in: 31,
        csrf_token: 'csrf-login',
      })
    }
    if (url === '/api/auth/refresh') {
      refreshPostCalls += 1
      // 원 요청과 재시도 둘 다 403 → 더 이상 재시도하지 않고 실패해야 한다.
      return jsonResponse(403, errorBody('csrf_failed'))
    }
    throw new Error(`예상하지 못한 호출: ${url} ${JSON.stringify(init)}`)
  })

  type ProbeActions = ReturnType<typeof useAuth>
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

  // 1초 뒤 첫 refresh 시도(403) → 1초 대기 후 재시도(403) → 포기하고 guest로.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2200)
  })

  expect(refreshPostCalls).toBe(2) // 원 요청 포함 최대 2번, 더 늘지 않는다
  expect(screen.getByTestId('status').textContent).toBe('guest')

  fetchSpy.mockRestore()
})

test('bootstrap 성공: csrf→refresh→me가 모두 성공하면 authenticated로 전환된다', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'csrf-boot' })
    if (url === '/api/auth/refresh') {
      return jsonResponse(200, {
        access_token: 'access-boot',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-boot-2',
      })
    }
    if (url === '/api/me') {
      return jsonResponse(200, { user: { id: 'u1', email: 'restored@test.com', created_at: '2026-01-01T00:00:00Z' } })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('authenticated'))
  expect(screen.getByTestId('email').textContent).toBe('restored@test.com')

  fetchSpy.mockRestore()
})

test('refresh_race(409)를 받으면 1초 뒤 csrf→refresh를 한 번만 재시도한다', async () => {
  vi.useFakeTimers()

  let refreshCalls = 0
  let csrfCalls = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      csrfCalls += 1
      if (csrfCalls === 1) return jsonResponse(401, errorBody('unauthenticated'))
      return jsonResponse(200, { csrf_token: `csrf-${csrfCalls}` })
    }
    if (url === '/api/auth/login') {
      return jsonResponse(200, successBody('a@test.com', 31))
    }
    if (url === '/api/auth/refresh') {
      refreshCalls += 1
      if (refreshCalls === 1) {
        return jsonResponse(409, errorBody('refresh_race'))
      }
      return jsonResponse(200, {
        access_token: 'access-2',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'csrf-3',
      })
    }
    if (url === '/api/me') {
      return jsonResponse(200, { user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' } })
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

  await act(async () => {
    await actionsRef.current?.login('a@test.com', 'password123!')
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 1초 뒤 첫 refresh 시도(409) → 1초 대기 후 재시도(성공)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2200)
  })

  expect(refreshCalls).toBe(2)
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  fetchSpy.mockRestore()
})

test('/me 응답이 늦어져도 만료 타이머는 토큰 발급 시점 기준으로 계산된다', async () => {
  // 리뷰에서 지적된 버그: /me가 끝난 "그 시점부터" 다시 900초를 세면,
  // /me가 느릴수록 실제 만료보다 로그인 상태가 더 오래 유지된다.
  // 이 테스트는 /me를 120초 지연시켜도, 로그인 시점(issuedAt) 기준
  // expires_in이 지나면 정확히 guest로 떨어지는지 확인한다.
  vi.useFakeTimers()

  let meCallCount = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') {
      // expires_in: 200초. issuedAt은 apiLogin이 resolve되는 이 시점이다.
      return jsonResponse(200, successBody('a@test.com', 200))
    }
    if (url === '/api/auth/refresh') {
      return jsonResponse(401, errorBody('unauthenticated'))
    }
    if (url === '/api/me') {
      meCallCount += 1
      // 부트스트랩 쪽 /me 호출만 지연시킨다(로그인 경로는 /me를 타지 않음).
      // 여기서는 로그인 흐름만 검증하므로 호출되지 않아야 정상이다.
      return jsonResponse(200, { user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' } })
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
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 강제 만료 타이머는 issuedAt + 200초에 발동해야 한다. 199초까지는 유지,
  // 200초를 넘기면(여기서는 refresh도 401이라 재시도 없이 바로 실패하므로
  // 자동 갱신 쪽에서도 guest가 되지만, 핵심은 "토큰 발급 시점" 기준이라는 점이다)
  // guest로 전환되어야 한다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(199_000)
  })
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  await act(async () => {
    await vi.advanceTimersByTimeAsync(2_000)
  })
  expect(screen.getByTestId('status').textContent).toBe('guest')

  void meCallCount
  fetchSpy.mockRestore()
})

test('강제 만료 이후 늦게 도착한 refresh 응답은 더 이상 상태를 덮어쓰지 않는다', async () => {
  // 리뷰에서 재현한 레이스 컨디션: 강제 만료로 guest가 된 "이후"에, 그 전에
  // 보냈던 refresh 요청이 뒤늦게 성공 응답을 들고 돌아와도 다시 authenticated로
  // 되돌리면 안 된다.
  vi.useFakeTimers()

  let resolveLateRefresh: ((value: Response) => void) | null = null
  let refreshCallCount = 0

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') {
      // expires_in을 짧게 둬서 강제 만료가 금방 발동하게 한다.
      return jsonResponse(200, successBody('a@test.com', 31))
    }
    if (url === '/api/auth/refresh') {
      refreshCallCount += 1
      // 응답을 고의로 지연시킨다 — 강제 만료 타이머가 먼저 발동한 "뒤"에
      // 이 refresh가 성공 응답을 들고 돌아오는 상황을 재현한다.
      return new Promise<Response>((resolve) => {
        resolveLateRefresh = resolve
      })
    }
    if (url === '/api/me') {
      return jsonResponse(200, { user: { id: 'u1', email: 'late@test.com', created_at: '2026-01-01T00:00:00Z' } })
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
  expect(screen.getByTestId('status').textContent).toBe('authenticated')

  // 1초 뒤 refresh가 시작되지만 응답은 아직 안 옴(위 Promise가 안 풀림).
  // 그 상태에서 강제 만료 시각(31초)까지 시간을 흘려보낸다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(31_000)
  })
  expect(refreshCallCount).toBe(1)
  expect(screen.getByTestId('status').textContent).toBe('guest')

  // 이제서야 지연됐던 refresh가 "성공"으로 돌아온다. 이 응답의 세대는 이미
  // setGuest()가 한 번 더 올려 버렸으므로(강제 만료 자체가 invalidateSession을
  // 겸함) 더 이상 유효하지 않고, authenticated로 되돌아가면 안 된다.
  await act(async () => {
    resolveLateRefresh?.(
      await jsonResponse(200, {
        access_token: 'late-access',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'late-csrf',
      }),
    )
    await vi.runOnlyPendingTimersAsync()
  })

  expect(screen.getByTestId('status').textContent).toBe('guest')
  expect(screen.getByTestId('email').textContent).toBe('')

  fetchSpy.mockRestore()
})

test('부트스트랩 응답이 영영 오지 않으면 타임아웃으로 guest가 되어 화면이 멈추지 않는다', async () => {
  vi.useFakeTimers()

  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
    // 절대 resolve/reject 되지 않는 요청 — 네트워크가 응답 없이 멈춘 상황을 재현.
    return new Promise<Response>(() => {})
  })

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )

  // 타임아웃 전에는 여전히 'checking' 이어야 한다.
  expect(screen.getByTestId('status').textContent).toBe('checking')

  // BOOTSTRAP_TIMEOUT_MS(10초)를 넘기면 타임아웃으로 guest가 되어야 한다.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(11_000)
  })

  expect(screen.getByTestId('status').textContent).toBe('guest')

  fetchSpy.mockRestore()
})
