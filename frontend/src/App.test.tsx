// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'

afterEach(() => {
  cleanup()
})

// App이 뜨면 AuthContext가 세션 복구를 위해 GET /api/auth/csrf를 한 번 호출한다.
// 테스트에서는 "로그인된 세션이 없다"(401)로 응답해, 비회원 화면으로 빠르게 정착시킨다.
function mockGuestSession() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()

    if (url === '/api/auth/csrf') {
      return new Response(
        JSON.stringify({
          error: { code: 'unauthenticated', message: '인증이 필요합니다.', fields: {}, trace_id: 'test-trace' },
        }),
        { status: 401, headers: { 'Content-Type': 'application/json' } },
      )
    }

    throw new Error(`테스트에서 예상하지 못한 fetch 호출: ${url}`)
  })
}

test('비회원 소개와 질문·답변 예시를 보여준다', async () => {
  const fetchSpy = mockGuestSession()
  render(<App />)

  expect(
    await screen.findByRole('heading', {
      name: /복잡한 교통법/,
    }),
  ).toBeTruthy()

  expect(
    screen.getByRole('heading', {
      name: '이런 질문도 많이 물어봐요',
    }),
  ).toBeTruthy()

  expect(screen.getByText('AI 답변 예시')).toBeTruthy()

  expect(screen.getByText('어린이 보호구역에서는 주정차를 어떻게 해야 하나요?')).toBeTruthy()

  fetchSpy.mockRestore()
})

test('질문 입력 영역을 클릭하면 로그인 모달이 열린다', async () => {
  const fetchSpy = mockGuestSession()
  const user = userEvent.setup()
  render(<App />)

  const promptInput = await screen.findByRole('textbox', {
    name: '질문하려면 로그인 또는 회원가입이 필요합니다',
  })
  await user.click(promptInput)

  expect(screen.getByRole('dialog')).toBeTruthy()

  expect(screen.getByRole('button', { name: '회원가입' })).toBeTruthy()

  fetchSpy.mockRestore()
})

test('로그인 모달에서 회원가입 탭으로 전환할 수 있다', async () => {
  const fetchSpy = mockGuestSession()
  const user = userEvent.setup()
  render(<App />)

  await user.click(await screen.findByRole('button', { name: /^로그인$/ }))

  await user.click(screen.getByRole('button', { name: '회원가입' }))

  expect(screen.getByRole('heading', { name: '회원가입' })).toBeTruthy()

  // 가입 API는 이메일·비밀번호만 받으므로 이름 입력을 요구하지 않는다.
  expect(screen.queryByRole('textbox', { name: '이름' })).toBeNull()
  expect(screen.getByLabelText('이메일')).toBeTruthy()
  expect(screen.getByLabelText('비밀번호')).toBeTruthy()

  fetchSpy.mockRestore()
})

// 이슈 #16 핵심 조건: 비회원은 실제 질문 전송이나 개인 대화 기록에 진입할 수 없다.
test('비회원은 새 대화를 시작하거나 대화 기록에 진입할 수 없다', async () => {
  const fetchSpy = mockGuestSession()
  render(<App />)

  const newChatButton = await screen.findByRole('button', { name: /새 대화/ })
  expect(newChatButton.hasAttribute('disabled')).toBe(true)

  // 대화 기록 영역에는 실제 기록 대신 로그인 안내만 노출되어야 한다.
  expect(screen.getByText('로그인하면 대화 기록을 확인할 수 있어요.')).toBeTruthy()
  expect(screen.queryByRole('list', { name: /대화 기록/ })).toBeNull()

  fetchSpy.mockRestore()
})

// 이슈 #16 핵심 조건: 고정 예시를 표시할 때 AI·대화 API 요청이 발생하지 않는다.
// 세션 복구를 위한 GET /api/auth/csrf 한 번은 로그인 여부 확인용으로 정당한 호출이라 제외하고,
// 그 외의 호출(AI 답변, 대화 기록 등)은 전혀 없어야 한다.
test('고정 예시를 표시할 때 세션 확인 외의 네트워크 요청이 발생하지 않는다', async () => {
  const fetchSpy = mockGuestSession()
  render(<App />)

  await screen.findByText('AI 답변 예시')

  expect(fetchSpy).toHaveBeenCalledTimes(1)
  expect(fetchSpy).toHaveBeenCalledWith('/api/auth/csrf', expect.objectContaining({ method: 'GET' }))

  fetchSpy.mockRestore()
})

// 이슈 #16 핵심 조건: 질문 영역과 전송 버튼을 눌러도 실제 전송 없이 인증 안내만 열린다.
test('질문 입력창과 전송 버튼은 실제 질문을 전송하지 않고 인증 모달만 연다', async () => {
  const fetchSpy = mockGuestSession()
  const user = userEvent.setup()
  render(<App />)

  const promptInput = (await screen.findByRole('textbox', {
    name: '질문하려면 로그인 또는 회원가입이 필요합니다',
  })) as HTMLInputElement

  expect(promptInput.readOnly).toBe(true)

  await user.click(promptInput)

  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(promptInput.value).toBe('')
  // 세션 확인(csrf) 호출 1번 외에 추가 호출이 없어야 한다.
  expect(fetchSpy).toHaveBeenCalledTimes(1)

  fetchSpy.mockRestore()
})

// 이슈 #67: 초점만으로는 열지 않고, 명시적 Enter·Space로 연다. 닫은 뒤 초점이 돌아와도 다시 열리지 않는다.
test('Tab으로 질문 입력창을 지나가기만 하면 모달이 열리지 않는다', async () => {
  const fetchSpy = mockGuestSession()
  const user = userEvent.setup()
  render(<App />)

  const promptInput = await screen.findByRole('textbox', { name: '질문하려면 로그인 또는 회원가입이 필요합니다' })
  for (let i = 0; i < 20 && document.activeElement !== promptInput; i += 1) {
    await user.tab()
  }
  expect(document.activeElement).toBe(promptInput)
  expect(screen.queryByRole('dialog')).toBeNull()

  await user.tab()
  expect(screen.queryByRole('dialog')).toBeNull()

  fetchSpy.mockRestore()
})

test.each([
  ['Enter', '{Enter}'],
  ['Space', ' '],
])('질문 입력창에서 %s로 열고 Escape로 닫으면 초점이 돌아오고 다시 열 수 있다', async (_name, key) => {
  const fetchSpy = mockGuestSession()
  const user = userEvent.setup()
  render(<App />)

  const promptInput = await screen.findByRole('textbox', { name: '질문하려면 로그인 또는 회원가입이 필요합니다' })
  promptInput.focus()
  expect(screen.queryByRole('dialog')).toBeNull()

  await user.keyboard(key)
  const dialog = screen.getByRole('dialog')
  expect(dialog.contains(document.activeElement)).toBe(true)

  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(promptInput)

  // 초점 복귀만으로는 다시 열리지 않는다.
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(screen.queryByRole('dialog')).toBeNull()

  await user.keyboard(key)
  expect(screen.getByRole('dialog')).toBeTruthy()

  fetchSpy.mockRestore()
})

function authBody(email: string) {
  return {
    user: { id: 'u1', email, created_at: '2026-01-01T00:00:00Z' },
    access_token: 'a',
    token_type: 'Bearer',
    expires_in: 900,
    csrf_token: 'c',
  }
}

// 늦은 login 실패가 닫혔다가 다시 열린 모달의 상태와 초점을 바꾸지 않는다.
test('닫은 모달의 늦은 로그인 실패는 다시 연 모달에 오류나 초점 변화를 남기지 않는다', async () => {
  let rejectLogin: (response: Response) => void = () => {}
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') {
      return new Response(
        JSON.stringify({ error: { code: 'unauthenticated', message: '인증', fields: {}, trace_id: 't' } }),
        {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        },
      )
    }
    if (url === '/api/auth/login') {
      return new Promise<Response>((resolve) => {
        rejectLogin = resolve
      })
    }
    throw new Error(`예상하지 못한 호출: ${url}`)
  })
  const user = userEvent.setup()
  render(<App />)

  const loginButton = await screen.findByRole('button', { name: /^로그인$/ })
  await user.click(loginButton)
  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  await user.click(document.querySelector('button[type="submit"]') as HTMLButtonElement)
  await screen.findByRole('button', { name: '처리 중...' })

  await user.keyboard('{Escape}')
  expect(screen.queryByRole('dialog')).toBeNull()
  await user.click(loginButton)
  const emailInput = screen.getByLabelText('이메일')
  await user.click(emailInput)

  rejectLogin(
    new Response(
      JSON.stringify({ error: { code: 'unauthenticated', message: '늦은 실패', fields: {}, trace_id: 't' } }),
      {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      },
    ),
  )
  await new Promise((resolve) => setTimeout(resolve, 20))

  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(document.activeElement).toBe(emailInput)

  fetchSpy.mockRestore()
})

// 늦은 login 성공은 세션을 적용하지만, 다시 연 모달은 회원 화면 전환과 함께 정리된다.
test('닫은 모달의 늦은 로그인 성공은 회원 화면으로 전환하고 모달을 남기지 않는다', async () => {
  let resolveLogin: (response: Response) => void = () => {}
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url === '/api/auth/csrf') {
      return new Response(
        JSON.stringify({ error: { code: 'unauthenticated', message: '인증', fields: {}, trace_id: 't' } }),
        {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        },
      )
    }
    if (url === '/api/auth/login') {
      return new Promise<Response>((resolve) => {
        resolveLogin = resolve
      })
    }
    // 회원 화면이 뜨며 부르는 조회는 이 검사의 대상이 아니다.
    return new Promise<Response>(() => {})
  })
  const user = userEvent.setup()
  render(<App />)

  const loginButton = await screen.findByRole('button', { name: /^로그인$/ })
  await user.click(loginButton)
  await user.type(screen.getByLabelText('이메일'), 'a@test.com')
  await user.type(screen.getByLabelText('비밀번호'), 'password123456')
  await user.click(document.querySelector('button[type="submit"]') as HTMLButtonElement)
  await screen.findByRole('button', { name: '처리 중...' })

  await user.keyboard('{Escape}')
  await user.click(loginButton)
  expect(screen.getByRole('dialog')).toBeTruthy()

  resolveLogin(
    new Response(JSON.stringify(authBody('a@test.com')), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )

  await waitFor(() => expect(screen.queryByRole('button', { name: /^로그인$/ })).toBeNull())
  expect(screen.queryByRole('dialog')).toBeNull()

  fetchSpy.mockRestore()
})
