// @vitest-environment jsdom

import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('비회원에게 서비스 소개와 고정 예시를 보여준다', () => {
  render(<App />)

  expect(screen.getByRole('heading', { name: '교통법이 궁금할 때 쉽게 확인해 보세요.' })).toBeTruthy()
  expect(screen.getByText('이런 질문을 할 수 있어요')).toBeTruthy()
  expect(screen.getByText('어린이 보호구역에서는 주정차를 어떻게 해야 하나요?')).toBeTruthy()
  expect(screen.getByText('답변 예시')).toBeTruthy()
})

test('고정 예시를 열고 닫을 때 API를 호출하지 않는다', async () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)

  const user = userEvent.setup()
  render(<App />)

  await user.click(screen.getByRole('button', { name: '예시 숨기기' }))

  expect(screen.queryByText('어린이 보호구역에서는 주정차를 어떻게 해야 하나요?')).toBeNull()

  await user.click(screen.getByRole('button', { name: '예시 보기' }))

  expect(screen.getByText('어린이 보호구역에서는 주정차를 어떻게 해야 하나요?')).toBeTruthy()
  expect(fetch).not.toHaveBeenCalled()
})
