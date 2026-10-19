// @vitest-environment jsdom
import { expect, test, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { AuthProvider } from './AuthContext'
import {
  unexpectedFetches,
  type ProbeActions,
  Probe,
  jsonResponse,
  errorBody,
  successBody,
  hangUntilAborted,
  strictFetch,
} from './authTestFixtures'

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
    unexpectedFetches.push(url)
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
    unexpectedFetches.push(url)
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
    unexpectedFetches.push(url)
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

    unexpectedFetches.push(url)
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

    unexpectedFetches.push(url)
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

// Regression contract: unexpected fetch errors are recorded outside Provider catches.

test.each([403, 409])('regression: second %i rejects with exactly two refresh POSTs', async (status) => {
  vi.useFakeTimers()
  const spy = strictFetch((url, init) => {
    if (url === '/api/auth/csrf') {
      expect(init).toEqual({ method: 'GET', credentials: 'include', signal: expect.any(AbortSignal) })
      return jsonResponse(200, { csrf_token: 'csrf-fresh' })
    }
    if (url === '/api/auth/refresh') {
      expect(init?.method).toBe('POST')
      return jsonResponse(status, errorBody(status === 403 ? 'csrf_failed' : 'refresh_race'))
    }
  })
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000)
  })
  expect(screen.getByTestId('status').textContent).toBe('guest')
  expect(spy.mock.calls.map(([url]) => url)).toEqual([
    '/api/auth/csrf',
    '/api/auth/refresh',
    '/api/auth/csrf',
    '/api/auth/refresh',
  ])
})
