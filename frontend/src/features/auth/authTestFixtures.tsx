import { afterEach, expect, vi, type MockInstance } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { AuthProvider, useAuth } from './AuthContext'
// 각 테스트의 예상 요청은 handler에 직접 적는다. Provider catch가 삼킨
// 예상 밖 요청도 afterEach에서 실패로 드러내고 항상 렌더/타이머/mock을 정리한다.
export const unexpectedFetches: string[] = []
const bootstrapSpies: MockInstance<typeof fetch>[] = []

afterEach(() => {
  const unexpected = unexpectedFetches.splice(0)
  const spies = bootstrapSpies.splice(0)
  try {
    expect(unexpected).toEqual([])
    for (const spy of spies) {
      expect(spy).toHaveBeenCalledWith('/api/auth/csrf', {
        method: 'GET',
        credentials: 'include',
        signal: expect.any(AbortSignal),
      })
      expect(spy.mock.calls.filter(([url]) => url === '/api/auth/csrf')).toHaveLength(1)
    }
  } finally {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  }
})

export type ProbeActions = ReturnType<typeof useAuth>

export function Probe({ actionsRef }: { actionsRef?: { current: ProbeActions | null } }) {
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

export function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export function errorBody(code: string, message = '실패') {
  return { error: { code, message, fields: {}, trace_id: 't' } }
}

export function successBody(email: string, expires_in = 900) {
  return {
    user: { id: 'u1', email, created_at: '2026-01-01T00:00:00Z' },
    access_token: 'access-1',
    token_type: 'Bearer',
    expires_in,
    csrf_token: 'csrf-1',
  }
}

// 초기 부트스트랩(csrf→refresh)은 항상 401로 실패시켜 guest로 빠르게 정착시킨다.
export function mockGuestBootstrap(extra: (url: string, init?: RequestInit) => Response | null) {
  const spy = strictFetch((url, init) => {
    if (
      url === '/api/auth/csrf' &&
      init?.method === 'GET' &&
      init.credentials === 'include' &&
      init.signal instanceof AbortSignal
    )
      return jsonResponse(401, errorBody('unauthenticated'))
    return extra(url, init) ?? undefined
  })
  bootstrapSpies.push(spy)
  return spy
}

export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

export function hangUntilAborted(signal?: AbortSignal | null): Promise<Response> {
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

export const regressionUser = (id: string) => ({ id, email: `${id}@test.com`, created_at: '2026-01-01T00:00:00Z' })
export const regressionSession = (id: string, expires_in = 900) => ({
  user: regressionUser(id),
  access_token: `token-${id}`,
  csrf_token: `csrf-${id}`,
  token_type: 'Bearer',
  expires_in,
})

export function strictFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    let result: ReturnType<typeof handler>
    try {
      result = handler(String(input), init)
    } catch (error) {
      unexpectedFetches.push(`Fetch handler failed: ${String(error)}`)
      throw error
    }
    if (result) return result
    unexpectedFetches.push(`${input}`)
    throw new Error(`Unexpected fetch: ${input}`)
  })
}

export async function regressionMount(handler: Parameters<typeof strictFetch>[0]) {
  const ref: { current: ProbeActions | null } = { current: null }
  const spy = strictFetch(handler)
  render(
    <AuthProvider>
      <Probe actionsRef={ref} />
    </AuthProvider>,
  )
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  await act(async () => {
    await ref.current!.login('A@test.com', 'password123!')
  })
  return { ref, spy }
}
