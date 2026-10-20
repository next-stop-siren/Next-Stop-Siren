// @vitest-environment jsdom
import { StrictMode } from 'react'
import { act, render } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { AuthProvider } from './AuthContext'
import {
  Probe,
  type ProbeActions,
  deferred,
  errorBody,
  jsonResponse,
  regressionSession,
  strictFetch,
} from './authTestFixtures'

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}
function mount() {
  const ref: { current: ProbeActions | null } = { current: null }
  const view = render(
    <AuthProvider>
      <Probe actionsRef={ref} />
    </AuthProvider>,
  )
  return { ref, ...view }
}

test('lifecycle: unmount during automatic me never recreates expiry timers', async () => {
  vi.useFakeTimers()
  const me = deferred<Response>()
  strictFetch((url) => {
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A', 31))
    if (url === '/api/auth/refresh') return jsonResponse(200, regressionSession('B'))
    if (url === '/api/me') return me.promise
  })
  const view = mount()
  await flush()
  await act(async () => {
    await view.ref.current!.login('A@test.com', 'password123!')
  })
  await flush(1000)
  view.unmount()
  me.resolve(jsonResponse(200, { user: regressionSession('B').user }))
  await flush()
  expect(vi.getTimerCount()).toBe(0)
})

test.each(['login', 'register'] as const)(
  'lifecycle: pending %s and old captured actions cannot affect a fresh Provider',
  async (operation) => {
    vi.useFakeTimers()
    const pending = deferred<Response>()
    let mutations = 0
    const spy = strictFetch((url) => {
      if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
      if (url === '/api/auth/login' || url === '/api/auth/register') {
        return ++mutations === 1 ? pending.promise : jsonResponse(200, regressionSession('B'))
      }
    })
    const old = mount()
    await flush()
    const captured = old.ref.current![operation]
    let result!: Promise<void>
    await act(async () => {
      result = captured('A@test.com', 'password123!')
      await Promise.resolve()
    })
    old.unmount()
    const fresh = mount()
    await flush()
    await act(async () => {
      await fresh.ref.current!.login('B@test.com', 'password123!')
    })
    pending.resolve(jsonResponse(200, regressionSession('A')))
    await act(async () => {
      await result
    })
    const count = spy.mock.calls.length
    await expect(captured('C@test.com', 'password123!')).rejects.toThrow('종료된 인증')
    expect(spy.mock.calls).toHaveLength(count)
    expect(fresh.ref.current!.user?.id).toBe('B')
    expect(fresh.ref.current!.accessToken).toBe('token-B')
    expect(vi.getTimerCount()).toBe(2)
  },
)

test('lifecycle: queued login after unmount is rejected before transmission', async () => {
  vi.useFakeTimers()
  const pending = deferred<Response>()
  const spy = strictFetch((url) => {
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') return pending.promise
  })
  const view = mount()
  await flush()
  let first!: Promise<void>, second!: Promise<void>
  await act(async () => {
    first = view.ref.current!.login('A@test.com', 'password123!')
    second = view.ref.current!.login('B@test.com', 'password123!')
    // Attach rejection before the queue starts to avoid an unhandled Promise.
    second.catch(() => {})
    await Promise.resolve()
  })
  view.unmount()
  pending.resolve(jsonResponse(200, regressionSession('A')))
  await act(async () => {
    await first
    await expect(second).rejects.toThrow('종료된 인증')
  })
  expect(spy.mock.calls.map(([url]) => url)).toEqual(['/api/auth/csrf', '/api/auth/login'])
  expect(vi.getTimerCount()).toBe(0)
})

test('lifecycle: mounted queued A login and B register both complete in FIFO order', async () => {
  vi.useFakeTimers()
  const pending = deferred<Response>()
  const calls: string[] = []
  strictFetch((url) => {
    calls.push(url)
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated'))
    if (url === '/api/auth/login') return pending.promise
    if (url === '/api/auth/register') return jsonResponse(200, regressionSession('B'))
  })
  const view = mount()
  await flush()
  let first!: Promise<void>, second!: Promise<void>
  await act(async () => {
    first = view.ref.current!.login('A@test.com', 'password123!')
    second = view.ref.current!.register('B@test.com', 'password123!')
    await Promise.resolve()
  })
  expect(calls).toEqual(['/api/auth/csrf', '/api/auth/login'])
  await act(async () => {
    pending.resolve(jsonResponse(200, regressionSession('A')))
    await first
    await second
  })
  expect(calls).toEqual(['/api/auth/csrf', '/api/auth/login', '/api/auth/register'])
  expect(view.ref.current!.user?.id).toBe('B')
  expect(view.ref.current!.accessToken).toBe('token-B')
})

test('lifecycle: retry CSRF resolving after unmount cannot start logout POST', async () => {
  vi.useFakeTimers()
  const csrf = deferred<Response>()
  let csrfCalls = 0
  const spy = strictFetch((url) => {
    if (url === '/api/auth/csrf')
      return ++csrfCalls === 1 ? jsonResponse(401, errorBody('unauthenticated')) : csrf.promise
    if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A'))
    if (url === '/api/auth/logout') return jsonResponse(503, errorBody('temporarily_unavailable'))
  })
  const view = mount()
  await flush()
  await act(async () => {
    await view.ref.current!.login('A@test.com', 'password123!')
  })
  await act(async () => {
    await expect(view.ref.current!.logout()).rejects.toThrow()
  })
  let retry!: Promise<void>
  await act(async () => {
    retry = view.ref.current!.retryLogout()
    await Promise.resolve()
  })
  view.unmount()
  csrf.resolve(jsonResponse(200, { csrf_token: 'late' }))
  await act(async () => {
    await retry
  })
  expect(spy.mock.calls.filter(([url]) => url === '/api/auth/logout')).toHaveLength(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('lifecycle: refresh backoff cannot start another request after unmount', async () => {
  vi.useFakeTimers()
  const spy = strictFetch((url) => {
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'initial' })
    if (url === '/api/auth/refresh') return jsonResponse(409, errorBody('refresh_race'))
  })
  const view = mount()
  await flush()
  view.unmount()
  await flush(1000)
  expect(spy.mock.calls.map(([url]) => url)).toEqual(['/api/auth/csrf', '/api/auth/refresh'])
  expect(vi.getTimerCount()).toBe(0)
})

test('lifecycle: StrictMode effect replay still bootstraps the active lifetime', async () => {
  vi.useFakeTimers()
  const spy = strictFetch((url) => {
    if (url === '/api/auth/csrf') return jsonResponse(200, { csrf_token: 'boot' })
    if (url === '/api/auth/refresh') return jsonResponse(200, regressionSession('A'))
    if (url === '/api/me') return jsonResponse(200, { user: regressionSession('A').user })
  })
  const ref: { current: ProbeActions | null } = { current: null }
  render(
    <StrictMode>
      <AuthProvider>
        <Probe actionsRef={ref} />
      </AuthProvider>
    </StrictMode>,
  )
  await flush()
  expect(ref.current!.user?.id).toBe('A')
  expect(spy.mock.calls.map(([url]) => url)).toEqual(['/api/auth/csrf', '/api/auth/refresh', '/api/me'])
})
