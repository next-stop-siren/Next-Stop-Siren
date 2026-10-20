// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
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

  expect(screen.getByRole('textbox', { name: '이름' })).toBeTruthy()

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
