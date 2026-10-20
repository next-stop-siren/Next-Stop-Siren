// @vitest-environment jsdom
import { expect, test, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { AuthProvider, type useAuth } from './AuthContext'
import {
  unexpectedFetches,
  Probe,
  jsonResponse,
  errorBody,
  successBody,
  mockGuestBootstrap,
  deferred,
} from './authTestFixtures'
import userEvent from '@testing-library/user-event'

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
    unexpectedFetches.push(url)
    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${url}`)
  })

  const actionsRef: { current: ReturnType<typeof useAuth> | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  // A로 로그인된 상태를 가정하지 않고, csrfTokenRef를 직접 채울 수 없으므로
  // 먼저 A로 실제 로그인시킨 뒤 바로 로그아웃 → B 로그인을 연달아 트리거한다.
  fetchSpy.mockImplementationOnce(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    callCounter += 1
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    unexpectedFetches.push(url)
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

test('느린 로그아웃 뒤 즉시 응답하는 B 로그인도 최종 B 세션을 유지한다', async () => {
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
    unexpectedFetches.push(url)
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  const actionsRef: { current: ReturnType<typeof useAuth> | null } = { current: null }

  render(
    <AuthProvider>
      <Probe actionsRef={actionsRef} />
    </AuthProvider>,
  )

  await waitFor(() => expect(screen.getByTestId('status').textContent).toBe('guest'))

  fetchSpy.mockImplementationOnce(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/login') return jsonResponse(200, successBody('a@test.com'))
    unexpectedFetches.push(url)
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
