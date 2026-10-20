// @vitest-environment jsdom
import { expect, test } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { AuthProvider } from './AuthContext'
import { Probe, jsonResponse, errorBody, successBody, mockGuestBootstrap } from './authTestFixtures'
import userEvent from '@testing-library/user-event'

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
