// @vitest-environment jsdom
import { expect, test, vi } from 'vitest'
import { act } from '@testing-library/react'
import { jsonResponse, errorBody, deferred, regressionSession, regressionMount } from './authTestFixtures'

test('regression: ordinary logout retry uses fresh CSRF; captured stale retry cannot affect B', async () => {
  vi.useFakeTimers()
  let csrfCalls = 0
  let logoutCalls = 0
  const { ref, spy } = await regressionMount((url, init) => {
    if (url === '/api/auth/csrf')
      return ++csrfCalls === 1
        ? jsonResponse(401, errorBody('unauthenticated'))
        : jsonResponse(200, { csrf_token: `fresh-${csrfCalls}` })
    if (url === '/api/auth/login')
      return jsonResponse(200, regressionSession(JSON.parse(String(init?.body)).email.startsWith('A') ? 'A' : 'B'))
    if (url === '/api/auth/logout') {
      logoutCalls += 1
      expect(init?.headers).toEqual({ 'X-CSRF-Token': logoutCalls === 1 ? 'csrf-A' : `fresh-${csrfCalls}` })
      return logoutCalls === 1
        ? jsonResponse(503, errorBody('temporarily_unavailable'))
        : new Response(null, { status: 204 })
    }
  })
  await act(async () => {
    await expect(ref.current!.logout()).rejects.toThrow()
  })
  expect(ref.current!.status).toBe('guest')
  const retry = ref.current!.retryLogout
  await act(async () => {
    await ref.current!.retryLogout()
  })
  expect(logoutCalls).toBe(2)
  expect(ref.current!.status).toBe('guest')
  await act(async () => {
    await ref.current!.login('B@test.com', 'password123!')
  })
  const calls = spy.mock.calls.length
  await act(async () => {
    await retry()
  })
  expect(spy.mock.calls).toHaveLength(calls)
  expect(ref.current!.user?.id).toBe('B')
  expect(ref.current!.accessToken).toBe('token-B')
})

test('regression: retry queued behind a pending B login skips network after B succeeds', async () => {
  vi.useFakeTimers()
  const loginGate = deferred<Response>()
  let csrfCalls = 0
  const { ref, spy } = await regressionMount((url, init) => {
    if (url === '/api/auth/csrf')
      return ++csrfCalls === 1
        ? jsonResponse(401, errorBody('unauthenticated'))
        : jsonResponse(200, { csrf_token: 'fresh-csrf' })
    if (url === '/api/auth/login')
      return JSON.parse(String(init?.body)).email.startsWith('A')
        ? jsonResponse(200, regressionSession('A'))
        : loginGate.promise
    if (url === '/api/auth/logout') return jsonResponse(503, errorBody('temporarily_unavailable'))
  })
  await act(async () => {
    await expect(ref.current!.logout()).rejects.toThrow()
  })
  const retry = ref.current!.retryLogout
  let login: Promise<void>
  let retried: Promise<void>
  await act(async () => {
    login = ref.current!.login('B@test.com', 'password123!')
    await Promise.resolve()
    retried = retry()
  })
  const calls = spy.mock.calls.length
  await act(async () => {
    loginGate.resolve(jsonResponse(200, regressionSession('B')))
    await login!
    await retried!
  })
  expect(spy.mock.calls).toHaveLength(calls)
  expect(ref.current!.user?.id).toBe('B')
  expect(ref.current!.status).toBe('authenticated')
})

test('regression: pending legitimate retry exposes guest and no token before server success', async () => {
  vi.useFakeTimers()
  let csrfCalls = 0
  let logoutCalls = 0
  const gate = deferred<Response>()
  const { ref } = await regressionMount((url) => {
    if (url === '/api/auth/csrf')
      return ++csrfCalls === 1
        ? jsonResponse(401, errorBody('unauthenticated'))
        : jsonResponse(200, { csrf_token: 'fresh-csrf' })
    if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A'))
    if (url === '/api/auth/logout')
      return ++logoutCalls === 1 ? jsonResponse(503, errorBody('temporarily_unavailable')) : gate.promise
  })
  await act(async () => {
    await expect(ref.current!.logout()).rejects.toThrow()
  })
  let retry: Promise<void>
  await act(async () => {
    retry = ref.current!.retryLogout()
    await Promise.resolve()
  })
  expect(ref.current!.status).toBe('guest')
  expect(ref.current!.accessToken).toBeNull()
  expect(ref.current!.user).toBeNull()
  await act(async () => {
    gate.resolve(new Response(null, { status: 204 }))
    await retry!
  })
})
