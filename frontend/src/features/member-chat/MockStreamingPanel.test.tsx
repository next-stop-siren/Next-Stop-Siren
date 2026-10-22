// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MockStreamingPanel from './MockStreamingPanel'

afterEach(() => {
  cleanup()
})

// 패널 설명 문구에도 "임시 출력"이 들어 있어서, 임시 출력 상자는 라벨 전체로 찾는다.
const DRAFT_LABEL = '생성 중인 임시 출력 · 아직 저장되지 않았습니다'

async function acceptAndStart(user: ReturnType<typeof userEvent.setup>, content = '어린이보호구역 제한속도는?') {
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())
  await user.type(screen.getByLabelText('스트리밍 목업 질문'), content)
  await user.click(screen.getByRole('button', { name: '질문 접수' }))
  await user.click(await screen.findByRole('button', { name: '실행 시작' }))
}

test('조각은 임시 출력으로만 보이고, 답변 저장을 알린 뒤에야 확정 답변이 된다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)

  await acceptAndStart(user)

  await user.click(screen.getByRole('button', { name: '조각 보내기' }))
  await user.click(screen.getByRole('button', { name: '조각 보내기' }))

  expect(screen.getByText(DRAFT_LABEL)).toBeTruthy()
  expect(screen.getByText(/도로교통법 제160조에 따라/)).toBeTruthy()
  expect(screen.queryByText('답변 완료 · 저장됨')).toBeNull()

  await user.click(screen.getByRole('button', { name: '답변 저장' }))

  expect(screen.getByText('답변 완료 · 저장됨')).toBeTruthy()
  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
})

test('실패하면 받은 조각을 보여 주지 않고 실패로만 표시한다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)

  await acceptAndStart(user)
  await user.click(screen.getByRole('button', { name: '조각 보내기' }))
  expect(screen.getByText(DRAFT_LABEL)).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '실패로 끝내기' }))

  expect(screen.getByText(/답변을 만들지 못했습니다/)).toBeTruthy()
  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
  expect(screen.queryByText(/도로교통법 제160조에 따라/)).toBeNull()
})

test('연결이 끊긴 사이 서버가 답변을 저장하면, 다시 연결했을 때 저장된 확정 답변이 보인다', async () => {
  const user = userEvent.setup()
  // 끊긴 사이에 서버 쪽 조작을 할 수 있도록 재연결을 잠시 늦춘다.
  render(<MockStreamingPanel reconnectDelayMs={() => 400} />)

  await acceptAndStart(user)
  await user.click(screen.getByRole('button', { name: '조각 보내기' }))

  await user.click(screen.getByRole('button', { name: '연결 끊기 (탭 닫기 흉내)' }))
  await waitFor(() => expect(screen.getByText(/구독: 끊김, 다시 연결 중/)).toBeTruthy())

  // 이 화면은 구독이 없지만 서버 작업은 살아 있어서 저장까지 끝낼 수 있다.
  // 끊긴 화면은 아직 실행 중으로 알고 있으므로 같은 버튼이 남아 있다.
  await user.click(screen.getByRole('button', { name: '답변 저장' }))

  await waitFor(() => expect(screen.getByText('답변 완료 · 저장됨')).toBeTruthy(), { timeout: 3000 })
  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
  expect(screen.getByText(/서버에는 작업 1개가 있습니다/)).toBeTruthy()
})

test('재연결 뒤 실행 중인 작업은 놓친 조각 없이 생성 중으로만 표시한다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)

  await acceptAndStart(user)
  await user.click(screen.getByRole('button', { name: '조각 보내기' }))
  expect(screen.getByText(DRAFT_LABEL)).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '연결 끊기 (탭 닫기 흉내)' }))
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
  expect(screen.getByText('답변 생성 중입니다.')).toBeTruthy()
})

test('로그아웃하면 화면의 대화와 임시 출력을 지우지만 서버 작업은 그대로이고, 다시 로그인하면 생성 중으로 복구한다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)

  await acceptAndStart(user)
  await user.click(screen.getByRole('button', { name: '조각 보내기' }))

  await user.click(screen.getByRole('button', { name: '로그아웃' }))

  expect(screen.getByText(/화면의 대화·임시 출력·구독을 모두 지웠습니다/)).toBeTruthy()
  expect(screen.queryByText('어린이보호구역 제한속도는?')).toBeNull()
  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
  expect(screen.getByText(/서버에는 작업 1개가 있습니다/)).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '다시 로그인' }))

  expect(await screen.findByText('어린이보호구역 제한속도는?')).toBeTruthy()
  expect(screen.getByText('답변 생성 중입니다.')).toBeTruthy()
  expect(screen.queryByText(DRAFT_LABEL)).toBeNull()
})

test('실행 전 질문은 취소할 수 있고 취소하면 대기 순번이 당겨진다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)

  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())
  const input = screen.getByLabelText('스트리밍 목업 질문')

  await user.type(input, '첫째')
  await user.click(screen.getByRole('button', { name: '질문 접수' }))
  await user.type(input, '둘째')
  await user.click(screen.getByRole('button', { name: '질문 접수' }))

  expect(await screen.findByText('대기 중 · 2번째')).toBeTruthy()

  await user.click(screen.getAllByRole('button', { name: '취소' })[0] as HTMLElement)

  expect(await screen.findByText('취소한 질문')).toBeTruthy()
  expect(screen.getByText('대기 중 · 1번째')).toBeTruthy()
})
