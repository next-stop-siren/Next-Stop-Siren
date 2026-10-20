// @vitest-environment jsdom
import { expect, test, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { AuthProvider } from './AuthContext'
import {
  unexpectedFetches,
  type ProbeActions,
  Probe,
  jsonResponse,
  errorBody,
  successBody,
  hangUntilAborted,
  regressionUser,
  regressionSession,
  regressionMount,
} from './authTestFixtures'

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
    if (url === '/api/me') return jsonResponse(200, { user: successBody('a@test.com').user })
    unexpectedFetches.push(url)
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

  expect(fetchSpy).toHaveBeenCalledWith('/api/me', {
    headers: { Authorization: 'Bearer access-2' },
    signal: expect.any(AbortSignal),
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
    unexpectedFetches.push(url)
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
    unexpectedFetches.push(url)
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

test.each(['503', 'abort timeout'])(
  'regression: failed B me (%s) keeps A/A only until original deadline',
  async (failure) => {
    vi.useFakeTimers()
    let aborted = false
    const { ref, spy } = await regressionMount((url, init) => {
      if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
      if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A', 31))
      if (url === '/api/auth/refresh') return jsonResponse(200, regressionSession('B'))
      if (url === '/api/me') {
        expect(init?.headers).toEqual({ Authorization: 'Bearer token-B' })
        if (failure === '503') return jsonResponse(503, errorBody('temporarily_unavailable'))
        return new Promise<Response>((_, reject) =>
          init?.signal?.addEventListener('abort', () => {
            aborted = true
            reject(new DOMException('Aborted', 'AbortError'))
          }),
        )
      }
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000)
    })
    expect(aborted).toBe(failure === 'abort timeout')
    expect(ref.current!.user?.id).toBe('A')
    expect(ref.current!.accessToken).toBe('token-A')
    expect(ref.current!.status).toBe('authenticated')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000)
    })
    expect(ref.current!.status).toBe('guest')
    expect(ref.current!.accessToken).toBeNull()
    expect(spy.mock.calls.map(([url]) => url)).toEqual([
      '/api/auth/csrf',
      '/api/auth/login',
      '/api/auth/refresh',
      '/api/me',
    ])
  },
)

test('regression: 9s me does not extend new token deadline when later refresh fails', async () => {
  vi.useFakeTimers()
  let refreshCount = 0
  const { ref } = await regressionMount((url, init) => {
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A', 31))
    if (url === '/api/auth/refresh') {
      refreshCount += 1
      if (refreshCount === 1) return jsonResponse(200, regressionSession('B'))
      return new Promise<Response>((_, reject) =>
        init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))),
      )
    }
    if (url === '/api/me')
      return new Promise<Response>((resolve) =>
        setTimeout(() => resolve(jsonResponse(200, { user: regressionUser('B') })), 9000),
      )
  })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000)
  })
  expect(ref.current!.accessToken).toBe('token-A')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(9000)
  })
  expect(ref.current!.user?.id).toBe('B')
  expect(ref.current!.accessToken).toBe('token-B')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(890_999)
  })
  expect(refreshCount).toBe(2)
  expect(ref.current!.status).toBe('authenticated')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1)
  })
  expect(ref.current!.status).toBe('guest') // receipt t1 + 900s, not me completion t10 + 900s
})
