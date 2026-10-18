// @vitest-environment jsdom
//
// AuthModal이 AuthContext와 올바르게 연결되어 성공/실패를 화면에 보여주는지 검증한다.

import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AuthModal from './AuthModal'
import { AuthProvider } from './AuthContext'
import { googleLoginStartUrl } from './authApi'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function errorBody(code: string, message: string) {
  return { error: { code, message, fields: {}, trace_id: 't' } }
}

function renderModal(extra: (url: string) => Response | null) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    const result = extra(url)
    if (result) return result
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  return render(
    <AuthProvider>
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )
}

test('로그인 실패 시 에러 메시지를 보여주고 입력값을 유지한다', async () => {
  const user = userEvent.setup()
  renderModal((url) => {
    if (url === '/api/auth/login') {
      return jsonResponse(401, errorBody('unauthenticated', '이메일 또는 비밀번호가 올바르지 않습니다.'))
    }
    return null
  })

  await user.type(screen.getByLabelText('이메일'), 'wrong@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'wrongpassword123')
  const submitButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submitButton)

  const alertEl = await screen.findByRole('alert')
  expect(alertEl.textContent).toBe('이메일 또는 비밀번호가 올바르지 않습니다.')
  expect((screen.getByLabelText('이메일') as HTMLInputElement).value).toBe('wrong@test.com')
})

test('회원가입 성공 시 onClose가 호출된다', async () => {
  const user = userEvent.setup()
  const onClose = vi.fn()

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/register') {
      return jsonResponse(200, {
        user: { id: 'u1', email: 'new@test.com', created_at: '2026-01-01T00:00:00Z' },
        access_token: 'a',
        token_type: 'Bearer',
        expires_in: 900,
        csrf_token: 'c',
      })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <AuthModal onClose={onClose} />
    </AuthProvider>,
  )

  await user.click(screen.getByRole('button', { name: '회원가입' }))
  await user.type(screen.getByLabelText('이름'), '홍길동')
  await user.type(screen.getByLabelText('이메일'), 'new@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  const submitButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submitButton)

  await waitFor(() => expect(onClose).toHaveBeenCalled())
})

test('제출 중에는 버튼이 비활성화되고 처리 중 텍스트를 보여준다', async () => {
  const user = userEvent.setup()
  let resolveLogin: () => void = () => {}

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/login') {
      await new Promise<void>((resolve) => {
        resolveLogin = resolve
      })
      return jsonResponse(401, errorBody('unauthenticated', '실패'))
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )

  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  const loginButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(loginButton)

  const submitButton = (await screen.findByRole('button', { name: '처리 중...' })) as HTMLButtonElement
  expect(submitButton.disabled).toBe(true)

  resolveLogin()
  await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
})

test('Google 로그인 버튼은 올바른 시작 URL로 전체 페이지 이동 링크를 갖는다', async () => {
  renderModal(() => null)
  const googleLink = screen.getByRole('link', { name: 'Google로 계속하기' })
  expect(googleLink.getAttribute('href')).toBe(googleLoginStartUrl)
})
