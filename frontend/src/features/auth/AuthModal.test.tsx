// @vitest-environment jsdom
//
// AuthModal이 AuthContext와 올바르게 연결되어 성공/실패를 화면에 보여주는지 검증한다.

import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

test('이모지가 포함된 유효한 비밀번호를 자르지 않고 그대로 제출한다', async () => {
  const user = userEvent.setup()
  let capturedPassword: string | null = null

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/login') {
      const body = JSON.parse((init?.body as string) ?? '{}')
      capturedPassword = body.password
      return jsonResponse(200, {
        user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' },
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
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )

  // 코드포인트 기준 12자, UTF-16 기준으로는 더 길어지는 이모지 포함 비밀번호
  const password = '😀'.repeat(12)
  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  await user.type(screen.getByLabelText('비밀번호'), password)
  const submitButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submitButton)

  await waitFor(() => expect(capturedPassword).toBe(password))
})

// 리뷰 코멘트(AuthModal.tsx L186): 예전 maxLength=128(UTF-16 단위) 속성이
// 남아 있었다면 이모지 65개(코드포인트 65자, UTF-16 130유닛)가 뒷부분부터
// 잘려 서버가 등록 시 받았던 비밀번호와 로그인 시 보내는 값이 달라졌을
// 경계값이다. 지금은 입력에 maxLength 속성이 없고 JS 검증도 코드포인트
// 기준이라, 이 길이에서도 잘리지 않고 그대로 전송되는지 확인한다.
test('이모지 65개(코드포인트 기준 128자 경계를 넘는) 비밀번호를 자르지 않고 그대로 제출한다', async () => {
  const user = userEvent.setup()
  let capturedPassword: string | null = null

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/login') {
      const body = JSON.parse((init?.body as string) ?? '{}')
      capturedPassword = body.password
      return jsonResponse(200, {
        user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' },
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
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )

  const password = '😀'.repeat(65)
  const passwordInput = screen.getByLabelText('비밀번호') as HTMLInputElement
  // 이모지 65개(서로게이트 쌍 130개)를 user.type의 키 단위 입력으로 넣으면
  // 느리고, IME 유사 처리로 인한 왜곡 가능성도 있다. 여기서는 "브라우저가
  // 받은 값을 그대로 전송하는지"가 핵심이므로 fireEvent.change로 네이티브
  // value를 직접 설정해 실제 입력 이벤트와 동일한 change 경로를 검증한다.
  fireEvent.change(passwordInput, { target: { value: password } })
  expect(passwordInput.value).toBe(password)
  expect([...passwordInput.value].length).toBe(65)

  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  const submitButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submitButton)

  await waitFor(() => expect(capturedPassword).toBe(password))
})

// 리뷰 코멘트(AuthModal.tsx L186): 이메일 input이 기존 type="email"이면
// 브라우저 자체 검증이 로컬파트의 비ASCII(한글 등)를 막아, 서버는 허용하는
// 주소인데도 폼 제출이 막혔다. 지금은 type="text"+inputMode="email"로
// 바뀌었으므로, 한글이 포함된 이메일 주소도 브라우저 네이티브 검증에
// 막히지 않고 실제로 제출되는지 확인한다.
test('로컬파트에 한글이 포함된 이메일 주소를 브라우저 검증에 막히지 않고 제출한다', async () => {
  const user = userEvent.setup()
  let capturedEmail: string | null = null

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/login') {
      const body = JSON.parse((init?.body as string) ?? '{}')
      capturedEmail = body.email
      return jsonResponse(200, {
        user: { id: 'u1', email: body.email, created_at: '2026-01-01T00:00:00Z' },
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
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )

  const email = '사용자@example.com'
  const emailInput = screen.getByLabelText('이메일') as HTMLInputElement
  expect(emailInput.type).toBe('text')

  await user.type(emailInput, email)
  expect(emailInput.value).toBe(email)
  // type="email"이었다면 이 시점에 네이티브 validity가 비한글 로컬파트를
  // 기대해 invalid 상태가 됐을 입력이다. type="text"에서는 애초에 그런
  // 제약이 적용되지 않는다.
  expect(emailInput.validity.valid).toBe(true)

  await user.type(screen.getByLabelText('비밀번호'), 'validpassword123')
  const submitButton = document.querySelector('button[type="submit"]') as HTMLButtonElement
  await user.click(submitButton)

  await waitFor(() => expect(capturedEmail).toBe(email))
})

// 이슈 #67: 모드를 바꾼 뒤 끝난 이전 요청은 새 모드의 상태를 바꾸지 않는다.
test.each([
  ['실패', jsonResponse(401, errorBody('unauthenticated', '늦은 실패'))],
  [
    '성공',
    jsonResponse(200, {
      user: { id: 'u1', email: 'a@test.com', created_at: '2026-01-01T00:00:00Z' },
      access_token: 'a',
      token_type: 'Bearer',
      expires_in: 900,
      csrf_token: 'c',
    }),
  ],
])('모드를 바꾼 뒤 끝난 이전 로그인의 %s도 회원가입 모드의 상태를 바꾸지 않는다', async (_name, response) => {
  const user = userEvent.setup()
  const onClose = vi.fn()
  let resolveLogin: (value: Response) => void = () => {}

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/login') {
      return new Promise<Response>((resolve) => {
        resolveLogin = resolve
      })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })

  render(
    <AuthProvider>
      <AuthModal onClose={onClose} />
    </AuthProvider>,
  )

  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  await user.click(document.querySelector('button[type="submit"]') as HTMLButtonElement)
  await screen.findByRole('button', { name: '처리 중...' })

  await user.click(screen.getByRole('button', { name: '회원가입' }))
  const emailInput = screen.getByLabelText('이메일')
  await user.click(emailInput)

  resolveLogin(response)
  await new Promise((resolve) => setTimeout(resolve, 20))

  expect(onClose).not.toHaveBeenCalled()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByRole('heading', { name: '회원가입' })).toBeTruthy()
  expect((document.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(false)
  expect(document.activeElement).toBe(emailInput)
})

test('회원가입은 이름 없이 이메일·비밀번호만 보낸다', async () => {
  const user = userEvent.setup()
  let capturedBody: Record<string, unknown> | null = null

  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') return jsonResponse(401, errorBody('unauthenticated', '인증 필요'))
    if (url === '/api/auth/register') {
      capturedBody = JSON.parse((init?.body as string) ?? '{}')
      return jsonResponse(201, {
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
      <AuthModal onClose={() => {}} />
    </AuthProvider>,
  )

  await user.click(screen.getByRole('button', { name: '회원가입' }))
  expect(screen.queryByLabelText('이름')).toBeNull()
  await user.type(screen.getByLabelText('이메일'), 'new@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  await user.click(document.querySelector('button[type="submit"]') as HTMLButtonElement)

  await waitFor(() => expect(capturedBody).toEqual({ email: 'new@test.com', password: 'password123456' }))
})
