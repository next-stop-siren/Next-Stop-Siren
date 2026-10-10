// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest'
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

  await user.click(screen.getByRole('button', { name: '로그인' }))

  await user.click(screen.getByRole('button', { name: '회원가입' }))

  expect(screen.getByRole('heading', { name: '회원가입' })).toBeTruthy()

  expect(screen.getByRole('textbox', { name: '이름' })).toBeTruthy()
})
