import { expect, test } from 'vitest'
import {
  allowedTransitions,
  checkEnqueue,
  createInitialMemberChatState,
  createQuestionMessage,
  expireState,
  expireWaitingMessages,
  hasBlockingFailure,
  QUESTIONS_MAX,
  summarizeLimits,
  summarizeQueue,
  transitionMockTask,
  transitionTask,
  WAIT_LIMIT_MS,
  type ChatJobState,
  type MemberChatConversation,
  type MemberChatMessage,
  type MemberChatMockState,
} from './memberChatMock'

const NOW = new Date('2026-10-21T03:00:00Z')

function message(id: string, status: ChatJobState, extra: Partial<MemberChatMessage> = {}): MemberChatMessage {
  return { id, question: `질문 ${id}`, status, createdAt: NOW.toISOString(), ...extra }
}

function conversation(id: string, messages: MemberChatMessage[]): MemberChatConversation {
  return { id, title: `대화 ${id}`, messages }
}

function stateOf(...conversations: MemberChatConversation[]): MemberChatMockState {
  return { activeConversationId: conversations[0].id, conversations }
}

const allStatuses = Object.keys(allowedTransitions) as ChatJobState[]

test('취소는 실행 전(queued·paused)에만 허용된다', () => {
  for (const status of allStatuses) {
    const canCancel = allowedTransitions[status].includes('canceled')
    expect(canCancel).toBe(status === 'queued' || status === 'paused')
  }

  // 실행 중·실패·중단·완료 작업은 취소 요청을 해도 그대로다.
  for (const status of ['running', 'failed', 'interrupted', 'completed'] as const) {
    const original = message('m1', status)
    expect(transitionMockTask(original, 'canceled')).toBe(original)
  }

  expect(transitionMockTask(message('m1', 'queued'), 'canceled').status).toBe('canceled')
  expect(transitionMockTask(message('m1', 'paused'), 'canceled').status).toBe('canceled')
})

test('종결 상태(완료·취소·만료)는 어떤 전이도 허용하지 않는다', () => {
  for (const status of ['completed', 'canceled', 'expired'] as const) {
    for (const next of allStatuses) {
      const original = message('m1', status)
      expect(transitionMockTask(original, next)).toBe(original)
    }
  }
})

test('답변 없이 완료로 바꾸면 완료가 아니라 실패가 된다', () => {
  expect(transitionMockTask(message('m1', 'running'), 'completed').status).toBe('failed')
  expect(transitionMockTask(message('m1', 'running', { answer: '답' }), 'completed').status).toBe('completed')
})

test('transitionTask는 완료로 바꿀 때 목업 답변을 채우고, 거절된 전이는 같은 state를 돌려준다', () => {
  const state = stateOf(conversation('c1', [message('m1', 'running')]))

  const completed = transitionTask(state, 'c1', 'm1', 'completed')
  expect(completed.conversations[0].messages[0].status).toBe('completed')
  expect(completed.conversations[0].messages[0].answer).toBeTruthy()

  // 이미 완료된 작업은 다시 바꿀 수 없다.
  expect(transitionTask(completed, 'c1', 'm1', 'failed')).toBe(completed)
  // 없는 대화·질문은 무시한다.
  expect(transitionTask(state, 'nope', 'm1', 'completed')).toBe(state)
  expect(transitionTask(state, 'c1', 'nope', 'completed')).toBe(state)
})

test('실행 중인 작업은 회원 전체에서 최대 1개다', () => {
  const state = stateOf(conversation('c1', [message('m1', 'running')]), conversation('c2', [message('m2', 'queued')]))

  // 다른 대화의 작업이 실행 중이면 새로 실행을 시작할 수 없다.
  expect(transitionTask(state, 'c2', 'm2', 'running')).toBe(state)

  const finished = transitionTask(state, 'c1', 'm1', 'completed')
  expect(transitionTask(finished, 'c2', 'm2', 'running').conversations[1].messages[0].status).toBe('running')
})

test('실패·중단은 같은 대화의 대기 질문만 일시 정지하고 다른 대화는 그대로 둔다', () => {
  for (const failure of ['failed', 'interrupted'] as const) {
    const state = stateOf(
      conversation('c1', [message('m1', 'running'), message('m2', 'queued'), message('m3', 'queued')]),
      conversation('c2', [message('m4', 'queued')]),
    )

    const next = transitionTask(state, 'c1', 'm1', failure)

    expect(next.conversations[0].messages.map((item) => item.status)).toEqual([failure, 'paused', 'paused'])
    expect(next.conversations[1].messages[0].status).toBe('queued')
  }
})

test('실패 질문보다 앞에 있는 질문은 정지하지 않는다', () => {
  const state = stateOf(conversation('c1', [message('m1', 'queued'), message('m2', 'running')]))

  const next = transitionTask(state, 'c1', 'm2', 'failed')

  expect(next.conversations[0].messages.map((item) => item.status)).toEqual(['queued', 'failed'])
})

test('정지 중 새로 접수한 질문도 정지 상태로 들어간다', () => {
  expect(hasBlockingFailure([message('m1', 'completed')])).toBe(false)
  expect(hasBlockingFailure([message('m1', 'completed'), message('m2', 'failed')])).toBe(true)
  expect(hasBlockingFailure([message('m1', 'interrupted')])).toBe(true)

  expect(createQuestionMessage('n1', '질문', true, NOW).status).toBe('paused')
  expect(createQuestionMessage('n2', '질문', false, NOW).status).toBe('queued')
})

test('접수 시각부터 5분 뒤가 만료 기한이다', () => {
  const created = createQuestionMessage('n1', '질문', false, NOW)

  expect(created.acceptedAt).toBe(NOW.toISOString())
  expect(new Date(created.expiresAt ?? '').getTime()).toBe(NOW.getTime() + WAIT_LIMIT_MS)
})

test('5분이 지나면 대기와 일시 정지 질문이 모두 만료된다', () => {
  const expired = new Date(NOW.getTime() + WAIT_LIMIT_MS).toISOString()
  const future = new Date(NOW.getTime() + WAIT_LIMIT_MS + 1000).toISOString()
  const nowMs = NOW.getTime() + WAIT_LIMIT_MS

  const messages = [
    message('queued', 'queued', { expiresAt: expired }),
    message('paused', 'paused', { expiresAt: expired }),
    message('not-yet', 'queued', { expiresAt: future }),
    message('running', 'running', { expiresAt: expired }),
    message('completed', 'completed', { expiresAt: expired }),
    message('no-deadline', 'queued'),
  ]

  const result = expireWaitingMessages(messages, nowMs)

  expect(result.map((item) => item.status)).toEqual(['expired', 'expired', 'queued', 'running', 'completed', 'queued'])
})

test('만료할 것이 없으면 같은 배열과 같은 state를 돌려준다', () => {
  const messages = [message('m1', 'queued', { expiresAt: new Date(NOW.getTime() + WAIT_LIMIT_MS).toISOString() })]
  expect(expireWaitingMessages(messages, NOW.getTime())).toBe(messages)

  const state = stateOf(conversation('c1', messages))
  expect(expireState(state, NOW.getTime())).toBe(state)
})

test('초기 목업의 대기 질문도 5분 기한을 가진다', () => {
  const initial = createInitialMemberChatState(NOW)
  const waiting = initial.conversations[0].messages.find((item) => item.status === 'queued')

  expect(waiting?.expiresAt).toBe(new Date(NOW.getTime() + WAIT_LIMIT_MS).toISOString())

  const expiredState = expireState(initial, NOW.getTime() + WAIT_LIMIT_MS)
  expect(expiredState.conversations[0].messages.find((item) => item.id === waiting?.id)?.status).toBe('expired')
})

test('새 질문 대기 수는 대기와 일시 정지를 함께 세고 실행 중은 세지 않는다', () => {
  const summary = summarizeQueue([
    conversation('c1', [message('m1', 'running'), message('m2', 'queued'), message('m3', 'paused')]),
    conversation('c2', [message('m4', 'completed'), message('m5', 'failed')]),
  ])

  expect(summary).toEqual({ running: 1, waitingQuestions: 2, waitingRetries: 0 })
})

test('새 질문 대기가 3개면 접수를 거절한다', () => {
  const waiting = Array.from({ length: QUESTIONS_MAX }, (_, index) => message(`w${index}`, 'queued'))

  expect(checkEnqueue([conversation('c1', waiting.slice(0, QUESTIONS_MAX - 1))])).toBeNull()
  expect(checkEnqueue([conversation('c1', waiting)])).toBe('queue_full')
  // 일시 정지도 새 질문 대기 자리를 쓴다.
  expect(checkEnqueue([conversation('c1', [...waiting.slice(0, 2), message('p', 'paused')])])).toBe('queue_full')
  // 실행 중 작업은 대기 자리를 쓰지 않는다.
  expect(checkEnqueue([conversation('c1', [...waiting.slice(0, 2), message('r', 'running')])])).toBeNull()
})

test('한도는 완료를 차감, 실행 전·실행 중을 예약으로 세고 실패·취소·만료는 환불한다', () => {
  const limits = summarizeLimits([
    conversation('c1', [
      message('done', 'completed'),
      message('wait', 'queued'),
      message('pause', 'paused'),
      message('run', 'running'),
      message('fail', 'failed'),
      message('cancel', 'canceled'),
      message('expire', 'expired'),
    ]),
  ])

  expect(limits.daily).toEqual({ limit: 400, used: 1, reserved: 3, remaining: 396 })
  expect(limits.sixHour).toEqual({ limit: 100, used: 1, reserved: 3, remaining: 96 })

  // 취소하면 예약이 돌아온다.
  const state = stateOf(conversation('c1', [message('wait', 'queued')]))
  const canceled = transitionTask(state, 'c1', 'wait', 'canceled')
  expect(summarizeLimits(canceled.conversations).daily.remaining).toBe(400)
})

test('6시간 한도를 모두 쓰면 접수를 거절한다', () => {
  const completed = Array.from({ length: 100 }, (_, index) => message(`d${index}`, 'completed'))

  expect(summarizeLimits([conversation('c1', completed)]).sixHour.remaining).toBe(0)
  expect(summarizeLimits([conversation('c1', completed)]).daily.remaining).toBe(300)
  expect(checkEnqueue([conversation('c1', completed)])).toBe('quota_exceeded')
})
