// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'

afterEach(() => {
  cleanup()
})

test('비회원 소개와 질문·답변 예시를 보여준다', () => {
  render(<App />)

  expect(
    screen.getByRole('heading', {
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
})

test('질문 입력 영역을 클릭하면 로그인 모달이 열린다', async () => {
  const user = userEvent.setup()
  render(<App />)

  await user.click(
    screen.getByRole('textbox', {
      name: '질문하려면 로그인 또는 회원가입이 필요합니다',
    }),
  )

  expect(screen.getByRole('dialog')).toBeTruthy()

  expect(screen.getByRole('button', { name: '회원가입' })).toBeTruthy()
})

test('로그인 모달에서 회원가입 탭으로 전환할 수 있다', async () => {
  const user = userEvent.setup()
  render(<App />)

  await user.click(screen.getByRole('button', { name: /^로그인$/ }))

  await user.click(screen.getByRole('button', { name: '회원가입' }))

  expect(screen.getByRole('heading', { name: '회원가입' })).toBeTruthy()

  expect(screen.getByRole('textbox', { name: '이름' })).toBeTruthy()
})

// 이슈 #16 핵심 조건: 비회원은 실제 질문 전송이나 개인 대화 기록에 진입할 수 없다.
test('비회원은 새 대화를 시작하거나 대화 기록에 진입할 수 없다', () => {
  render(<App />)

  const newChatButton = screen.getByRole('button', { name: /새 대화/ })
  expect(newChatButton.hasAttribute('disabled')).toBe(true)

  // 대화 기록 영역에는 실제 기록 대신 로그인 안내만 노출되어야 한다.
  expect(screen.getByText('로그인하면 대화 기록을 확인할 수 있어요.')).toBeTruthy()
  expect(screen.queryByRole('list', { name: /대화 기록/ })).toBeNull()
})

// 이슈 #16 핵심 조건: 고정 예시를 표시할 때 AI·대화 API 요청이 발생하지 않는다.
test('고정 예시를 표시할 때 네트워크 요청이 발생하지 않는다', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch')

  render(<App />)

  expect(fetchSpy).not.toHaveBeenCalled()

  fetchSpy.mockRestore()
})

// 이슈 #16 핵심 조건: 질문 영역과 전송 버튼을 눌러도 실제 전송 없이 인증 안내만 열린다.
test('질문 입력창과 전송 버튼은 실제 질문을 전송하지 않고 인증 모달만 연다', async () => {
  const user = userEvent.setup()
  const fetchSpy = vi.spyOn(globalThis, 'fetch')
  render(<App />)

  const promptInput = screen.getByRole('textbox', {
    name: '질문하려면 로그인 또는 회원가입이 필요합니다',
  }) as HTMLInputElement

  // readOnly라서 실제 타이핑이 불가능하다. 포커스 트랩된 모달 안으로 키 입력이
  // 넘어가는 부작용(예: 모달 닫기 버튼에서 Space가 클릭으로 해석됨)을 피하기 위해
  // 타이핑을 직접 시도하지 않고 속성 자체로 "입력 불가"를 확인한다.
  expect(promptInput.readOnly).toBe(true)

  await user.click(promptInput)

  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(promptInput.value).toBe('')
  expect(fetchSpy).not.toHaveBeenCalled()

  fetchSpy.mockRestore()
  cleanup()

  const sendButtonSpy = vi.spyOn(globalThis, 'fetch')
  render(<App />)

  await user.click(screen.getByRole('button', { name: '로그인하고 질문하기' }))

  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(sendButtonSpy).not.toHaveBeenCalled()

  sendButtonSpy.mockRestore()
})
