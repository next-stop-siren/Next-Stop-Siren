// @vitest-environment jsdom
import { expect, test, vi } from 'vitest'
import { act, render, screen, fireEvent } from '@testing-library/react'
import { jsonResponse, errorBody, deferred, regressionSession, strictFetch } from './authTestFixtures'
import App from '../../App'

function submitAppLogin(email: string) {
  fireEvent.click(screen.getByRole('button', { name: /^로그인$/ }))
  fireEvent.change(screen.getByLabelText('이메일'), { target: { value: email } })
  fireEvent.change(screen.getByLabelText('비밀번호'), { target: { value: 'password123!' } })
  fireEvent.submit(screen.getByLabelText('이메일').closest('form')!)
}

test.each([false, true])(
  'regression: real App clears A logout failure after B login (delayed failure=%s)',
  async (delayed) => {
    vi.useFakeTimers()
    let csrfCalls = 0
    let logoutCalls = 0
    let loginCalls = 0
    const gate = deferred<Response>()
    strictFetch((url, init) => {
      if (url === '/api/auth/csrf')
        return ++csrfCalls === 1
          ? jsonResponse(401, errorBody('unauthenticated'))
          : jsonResponse(200, { csrf_token: 'fresh-csrf' })
      if (url === '/api/auth/login') {
        loginCalls += 1
        return jsonResponse(200, regressionSession(JSON.parse(String(init?.body)).email.startsWith('A') ? 'A' : 'B'))
      }
      if (url === '/api/auth/logout') {
        logoutCalls += 1
        return delayed ? gate.promise : jsonResponse(503, errorBody('temporarily_unavailable'))
      }
      if (url === '/api/conversations?limit=20') {
        return jsonResponse(200, {
          items: [],
          next_cursor: null,
        })
      }
      return undefined
    })

    render(<App />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    submitAppLogin('A@test.com')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    fireEvent.click(screen.getByRole('button', { name: '로그아웃' }))
    expect(screen.queryByRole('button', { name: '로그아웃' })).toBeNull()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    if (!delayed) expect(screen.getByRole('alert')).toBeTruthy()
    submitAppLogin('B@test.com')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    if (delayed) {
      expect(loginCalls).toBe(1) // B must remain queued until A logout finishes
      await act(async () => {
        gate.resolve(jsonResponse(503, errorBody('temporarily_unavailable')))
        await vi.advanceTimersByTimeAsync(0)
      })
    }
    expect(loginCalls).toBe(2)
    expect(screen.getByRole('button', { name: '로그아웃' })).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: '다시 시도' })).toBeNull()
    expect(logoutCalls).toBe(1)
  },
)

test('regression: actual App keeps disabled retry banner until response; failed retry remains retryable', async () => {
  vi.useFakeTimers()
  let csrfCalls = 0
  let logoutCalls = 0
  let gate = deferred<Response>()
  strictFetch((url) => {
    if (url === '/api/auth/csrf')
      return ++csrfCalls === 1
        ? jsonResponse(401, errorBody('unauthenticated'))
        : jsonResponse(200, { csrf_token: 'fresh-csrf' })
    if (url === '/api/auth/login') return jsonResponse(200, regressionSession('A'))
    if (url === '/api/auth/logout')
      return ++logoutCalls === 1 ? jsonResponse(503, errorBody('temporarily_unavailable')) : gate.promise
    if (url === '/api/conversations?limit=20') {
      return jsonResponse(200, {
        items: [],
        next_cursor: null,
      })
    }
    return undefined
  })
  render(<App />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  submitAppLogin('A@test.com')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  fireEvent.click(screen.getByRole('button', { name: '로그아웃' }))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(screen.getByRole('alert')).toBeTruthy()
  expect((screen.getByRole('button', { name: '재시도 중...' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.queryByRole('button', { name: '로그아웃' })).toBeNull()
  await act(async () => {
    gate.resolve(jsonResponse(503, errorBody('temporarily_unavailable')))
    await vi.advanceTimersByTimeAsync(0)
  })
  expect((screen.getByRole('button', { name: '다시 시도' }) as HTMLButtonElement).disabled).toBe(false)
  gate = deferred<Response>()
  fireEvent.click(screen.getByRole('button', { name: '다시 시도' }))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(screen.getByRole('alert')).toBeTruthy()
  expect((screen.getByRole('button', { name: '재시도 중...' }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => {
    gate.resolve(new Response(null, { status: 204 }))
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(logoutCalls).toBe(3)
  expect(csrfCalls).toBe(3)
})
