// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import ChatAnswerView from './ChatAnswerView'

afterEach(() => {
  cleanup()
})

test('실행 전·취소·만료처럼 보여 줄 답변이 없으면 아무것도 그리지 않는다', () => {
  const { container } = render(<ChatAnswerView view={{ kind: 'none' }} />)

  expect(container.innerHTML).toBe('')
})

test('임시 출력은 저장되지 않았다고 표시하고, 확정 답변처럼 보이는 문구를 쓰지 않는다', () => {
  render(<ChatAnswerView view={{ kind: 'draft', text: '도로교통법 제160조에 따라 ' }} />)

  expect(screen.getByText('생성 중인 임시 출력 · 아직 저장되지 않았습니다')).toBeTruthy()
  expect(screen.getByText('도로교통법 제160조에 따라')).toBeTruthy()
  expect(screen.queryByText(/답변 완료/)).toBeNull()
})

test('확정 답변은 저장됨으로 표시한다', () => {
  render(<ChatAnswerView view={{ kind: 'confirmed', content: '과태료 대상입니다.' }} />)

  expect(screen.getByText('답변 완료 · 저장됨')).toBeTruthy()
  expect(screen.getByText('과태료 대상입니다.')).toBeTruthy()
  expect(screen.queryByText(/임시 출력/)).toBeNull()
})

test('생성 중과 저장 확인 중은 답변 내용 없이 상태만 알린다', () => {
  const { rerender } = render(<ChatAnswerView view={{ kind: 'generating' }} />)
  expect(screen.getByRole('status').textContent).toBe('답변 생성 중입니다.')

  rerender(<ChatAnswerView view={{ kind: 'unconfirmed' }} />)
  expect(screen.getByRole('status').textContent).toBe('저장된 답변을 확인하는 중입니다.')
})

test('실패와 중단은 오류 코드와 함께 알리고 임시 출력을 보여 주지 않는다', () => {
  const { rerender } = render(
    <ChatAnswerView view={{ kind: 'failed', state: 'failed', errorCode: 'provider_failed' }} />,
  )
  expect(screen.getByRole('status').textContent).toBe('답변을 만들지 못했습니다. (오류 코드: provider_failed)')

  rerender(<ChatAnswerView view={{ kind: 'failed', state: 'interrupted', errorCode: 'outcome_unknown' }} />)
  expect(screen.getByRole('status').textContent).toBe('답변 생성이 중단되었습니다. (오류 코드: outcome_unknown)')
  expect(screen.queryByText(/임시 출력/)).toBeNull()
})
