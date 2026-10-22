// @vitest-environment jsdom
import { afterEach, expect, test } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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

  await user.click(screen.getAllByRole('button', { name: '질문 취소' })[0] as HTMLElement)

  expect(await screen.findByText('취소한 질문')).toBeTruthy()
  expect(screen.getByText('대기 중 · 1번째')).toBeTruthy()
})

// --- 재시도·건너뛰기 (#21) ---

type User = ReturnType<typeof userEvent.setup>

async function acceptQuestion(user: User, content: string) {
  await user.type(screen.getByLabelText('스트리밍 목업 질문'), content)
  await user.click(screen.getByRole('button', { name: '질문 접수' }))
}

/** 질문 하나의 카드. 같은 질문의 처음 답변과 재시도가 한 카드에 모인다. */
function cardOf(content: string) {
  const card = screen.getByText(content).closest('article')
  if (!card) throw new Error(`${content} 카드가 없습니다`)
  return within(card)
}

async function failFirstQuestion(user: User, content: string) {
  await acceptQuestion(user, content)
  await user.click(await screen.findByRole('button', { name: '실행 시작' }))
  await user.click(screen.getByRole('button', { name: '실패로 끝내기' }))
}

test('실패하면 재시도·건너뛰기가 보이고, 새 시도 간격이 지나기 전에는 재시도가 이유와 함께 막힌다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '실패할 질문')

  const card = cardOf('실패할 질문')
  expect((card.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(true)
  expect(card.getByText(/초 뒤에 보낼 수 있습니다/)).toBeTruthy()
  expect(card.getByRole('button', { name: '건너뛰기' })).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '10초 보내기 (목업 시계)' }))

  expect((card.getByRole('button', { name: '재시도' }) as HTMLButtonElement).disabled).toBe(false)
  expect(card.queryByText(/초 뒤에 보낼 수 있습니다/)).toBeNull()
})

test('재시도하면 원래 작업은 안내만 보이고, 재시도가 성공하면 확정 답변이 보이며 버튼은 없다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '다시 시도할 질문')
  await user.click(screen.getByRole('button', { name: '10초 보내기 (목업 시계)' }))
  await user.click(cardOf('다시 시도할 질문').getByRole('button', { name: '재시도' }))

  const card = cardOf('다시 시도할 질문')
  expect(await card.findByText('재시도 대기 중 · 취소하면 건너뛸 수 있습니다')).toBeTruthy()
  expect(card.queryByRole('button', { name: '재시도' })).toBeNull()
  expect(card.queryByRole('button', { name: '건너뛰기' })).toBeNull()
  expect(card.getByRole('button', { name: '재시도 취소' })).toBeTruthy()

  await user.click(card.getByRole('button', { name: '실행 시작' }))
  expect(await card.findByText('재시도 중')).toBeTruthy()

  await user.click(card.getByRole('button', { name: '답변 저장' }))

  expect(await card.findByText('답변 완료 · 저장됨')).toBeTruthy()
  expect(card.queryByText('재시도 중')).toBeNull()
  expect(card.queryByRole('button', { name: '재시도' })).toBeNull()
  expect(card.queryByRole('button', { name: '건너뛰기' })).toBeNull()
})

test('재시도를 취소하면 원래 실패 작업의 재시도·건너뛰기가 다시 보인다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '취소 확인 질문')
  await user.click(screen.getByRole('button', { name: '10초 보내기 (목업 시계)' }))
  await user.click(cardOf('취소 확인 질문').getByRole('button', { name: '재시도' }))
  await user.click(await cardOf('취소 확인 질문').findByRole('button', { name: '재시도 취소' }))

  const card = cardOf('취소 확인 질문')
  expect(await card.findByText('재시도 · 취소한 질문')).toBeTruthy()
  expect(card.getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(card.getByRole('button', { name: '건너뛰기' })).toBeTruthy()
})

test('앞 질문이 실패하면 뒤 질문은 정지하고, 건너뛰면 대기로 돌아오며 재시도만 남는다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '앞 질문')
  await acceptQuestion(user, '뒤 질문')

  expect(await cardOf('뒤 질문').findByText('앞 질문 실패로 대기 중')).toBeTruthy()

  await user.click(cardOf('앞 질문').getByRole('button', { name: '건너뛰기' }))

  expect(await cardOf('뒤 질문').findByText('대기 중 · 1번째')).toBeTruthy()
  expect(cardOf('앞 질문').getByRole('button', { name: '재시도' })).toBeTruthy()
  expect(cardOf('앞 질문').queryByRole('button', { name: '건너뛰기' })).toBeNull()
})

test('뒤 질문이 실행을 시작하면 앞 질문의 재시도 버튼이 사라진다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 0} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '앞 질문')
  await acceptQuestion(user, '뒤 질문')
  await user.click(cardOf('앞 질문').getByRole('button', { name: '건너뛰기' }))

  await user.click(await cardOf('뒤 질문').findByRole('button', { name: '실행 시작' }))

  await waitFor(() => expect(cardOf('앞 질문').queryByRole('button', { name: '재시도' })).toBeNull())
})

test('연결이 끊긴 사이 바뀐 버튼은 다시 연결했을 때 저장된 상태로 받는다', async () => {
  const user = userEvent.setup()
  render(<MockStreamingPanel reconnectDelayMs={() => 400} />)
  await waitFor(() => expect(screen.getByText('구독: 연결됨')).toBeTruthy())

  await failFirstQuestion(user, '끊긴 사이 질문')
  expect(cardOf('끊긴 사이 질문').getByRole('button', { name: '건너뛰기' })).toBeTruthy()

  await user.click(screen.getByRole('button', { name: '연결 끊기 (탭 닫기 흉내)' }))
  await user.click(cardOf('끊긴 사이 질문').getByRole('button', { name: '건너뛰기' }))

  await waitFor(() => expect(cardOf('끊긴 사이 질문').queryByRole('button', { name: '건너뛰기' })).toBeNull(), {
    timeout: 3000,
  })
  expect(cardOf('끊긴 사이 질문').getByRole('button', { name: '재시도' })).toBeTruthy()
})
