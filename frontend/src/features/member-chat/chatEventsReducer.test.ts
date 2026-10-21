import { expect, test } from 'vitest'
import {
  chatEventsReducer,
  initialChatEventsState,
  selectAnswerView,
  type ChatEventsAction,
  type ChatEventsState,
} from './chatEventsReducer'
import { makeEnvelope, makeLimits } from './chatQueueBuilders'
import type { ChatEvent, ChatJobEnvelope } from './chatQueueTypes'

function run(actions: ChatEventsAction[], from: ChatEventsState = initialChatEventsState): ChatEventsState {
  return actions.reduce(chatEventsReducer, from)
}

function ev(event: ChatEvent): ChatEventsAction {
  return { type: 'event', event }
}

const snapshot = (items: ChatJobEnvelope[], serverTime = '2026-10-20T03:00:53Z'): ChatEventsAction =>
  ev({ type: 'snapshot', data: { items, limits: makeLimits(), server_time: serverTime } })

const job = (envelope: ChatJobEnvelope): ChatEventsAction => ev({ type: 'job', data: envelope })
const delta = (jobId: string, text: string): ChatEventsAction => ev({ type: 'delta', data: { job_id: jobId, text } })

function running(id = '7004', answerId = '9007') {
  return makeEnvelope({
    job: { id, question_message_id: '9006', state: 'running', position: null, allowed_actions: [] },
    answer: { id: answerId, status: 'pending' },
  })
}

function completed(id = '7004', answerId = '9007', content = '도로교통법 제160조에 따라 과태료 대상입니다.') {
  return makeEnvelope({
    job: { id, question_message_id: '9006', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: answerId, status: 'completed', content, completed_at: '2026-10-20T03:01:03Z' },
  })
}

function failed(id = '7004', answerId = '9007') {
  return makeEnvelope({
    job: {
      id,
      question_message_id: '9006',
      state: 'failed',
      position: null,
      error_code: 'provider_failed',
      allowed_actions: ['retry', 'skip'],
    },
    answer: { id: answerId, status: 'failed', content: null, error_code: 'provider_failed' },
  })
}

// 실행 중 상태를 중간에 만든다. snapshot은 실행 중 작업을 놓친 delta로 취급하므로, job 이벤트로 시작을 알린다.
function startedLive(id = '7004') {
  return run([job(makeEnvelope({ job: { id, question_message_id: '9006', state: 'queued' } })), job(running(id))])
}

test('실행 중이 아니거나 모르는 작업의 delta는 무시한다', () => {
  const queued = run([job(makeEnvelope({ job: { id: '7004', state: 'queued' } })), delta('7004', '무시됨')])
  expect(queued.drafts).toEqual({})
  expect(selectAnswerView(queued, '7004')).toEqual({ kind: 'none' })

  expect(run([delta('9999', '무시됨')]).drafts).toEqual({})
})

test('실행 중 delta는 임시 출력으로 쌓이고, 확정 답변으로 보이지 않는다', () => {
  expect(selectAnswerView(startedLive(), '7004')).toEqual({ kind: 'generating' })

  const withDraft = run(
    [delta('7004', '도로교통법 제160조에 따라 '), delta('7004', '과태료 대상입니다.')],
    startedLive(),
  )

  expect(selectAnswerView(withDraft, '7004')).toEqual({
    kind: 'draft',
    text: '도로교통법 제160조에 따라 과태료 대상입니다.',
  })
})

test('저장이 확인된 completed 작업의 answer만 확정 답변으로 표시하고 임시 출력은 버린다', () => {
  const state = run([delta('7004', '임시'), job(completed())], startedLive())

  expect(selectAnswerView(state, '7004')).toEqual({
    kind: 'confirmed',
    content: '도로교통법 제160조에 따라 과태료 대상입니다.',
  })
  expect(state.drafts).toEqual({})
})

test('완료로 알렸어도 저장된 답변 내용이 없으면 확정 답변으로 보이지 않는다', () => {
  const withoutContent = makeEnvelope({
    job: { id: '7004', question_message_id: '9006', state: 'completed', position: null, allowed_actions: [] },
    answer: { id: '9007', status: 'pending', content: null },
  })

  expect(selectAnswerView(run([job(withoutContent)]), '7004')).toEqual({ kind: 'unconfirmed' })
})

test('실패·중단하면 받은 임시 출력을 지우고 다시 보여 주지 않는다', () => {
  const state = run([delta('7004', '반쪽짜리 답변'), job(failed())], startedLive())

  expect(selectAnswerView(state, '7004')).toEqual({
    kind: 'failed',
    state: 'failed',
    errorCode: 'provider_failed',
  })
  expect(state.drafts).toEqual({})

  // 실패 뒤에 늦게 도착한 delta도 임시 출력으로 되살아나지 않는다.
  const late = run([delta('7004', '늦은 조각')], state)
  expect(late.drafts).toEqual({})
  expect(selectAnswerView(late, '7004').kind).toBe('failed')
})

test('종결 상태는 늦게 온 이전 이벤트로 되돌아가지 않는다', () => {
  const done = run([job(completed())])

  const afterStale = run([job(running())], done)

  expect(afterStale.jobs['7004']?.job.state).toBe('completed')
  expect(selectAnswerView(afterStale, '7004').kind).toBe('confirmed')
})

test('같은 작업이 snapshot과 job 이벤트에 겹쳐 와도 job.id로 덮어쓴다', () => {
  const state = run([snapshot([completed()]), job(completed())])

  expect(Object.keys(state.jobs)).toEqual(['7004'])
})

test('종결 상태에서 allowed_actions만 바뀌면 반영한다 (다른 탭에서 재시도가 끝난 경우)', () => {
  const withButtons = run([job(failed())])
  const noButtons = makeEnvelope({
    job: { ...failed().job, allowed_actions: [] },
    answer: { id: '9007', status: 'failed', content: null, error_code: 'provider_failed' },
  })

  const next = run([job(noButtons)], withButtons)

  expect(next.jobs['7004']?.job.allowed_actions).toEqual([])
})

test('snapshot 시점에 이미 실행 중인 작업은 생성 중으로만 표시하고 이후 delta를 임시 출력으로 보이지 않는다', () => {
  const state = run([snapshot([running()]), delta('7004', '중간부터 온 조각')])

  expect(state.gapped).toEqual(['7004'])
  expect(selectAnswerView(state, '7004')).toEqual({ kind: 'generating' })

  // 완료 이벤트가 오면 저장된 최종 답변으로 바뀐다.
  const done = run([job(completed())], state)
  expect(selectAnswerView(done, '7004').kind).toBe('confirmed')
  expect(done.gapped).toEqual([])
})

test('재연결 snapshot은 이전 임시 출력을 버린다 (끊긴 사이 놓친 조각이 있을 수 있다)', () => {
  const live = run([delta('7004', '앞부분 ')], startedLive())
  expect(selectAnswerView(live, '7004').kind).toBe('draft')

  const reconnected = run(
    [ev({ type: 'snapshot', data: { items: [running()], limits: makeLimits(), server_time: 't2' } })],
    live,
  )

  expect(reconnected.drafts).toEqual({})
  expect(selectAnswerView(reconnected, '7004')).toEqual({ kind: 'generating' })
})

test('끊긴 사이 끝난 답변은 snapshot의 저장된 결과로 확정 답변이 된다', () => {
  const live = run([delta('7004', '앞부분 ')], startedLive())

  const reconnected = run([snapshot([completed()])], live)

  expect(selectAnswerView(reconnected, '7004')).toEqual({
    kind: 'confirmed',
    content: '도로교통법 제160조에 따라 과태료 대상입니다.',
  })
  expect(reconnected.drafts).toEqual({})
  expect(reconnected.unresolved).toEqual([])
})

test('진행 중으로 알던 작업이 snapshot에 없으면 따로 조회할 목록에 넣고, 결과가 오면 지운다', () => {
  const live = startedLive()

  const reconnected = run([snapshot([])], live)

  expect(reconnected.unresolved).toEqual(['7004'])
  // 조회한 결과를 job 이벤트와 같은 방식으로 반영한다.
  const resolved = run([job(completed())], reconnected)
  expect(resolved.unresolved).toEqual([])
  expect(selectAnswerView(resolved, '7004').kind).toBe('confirmed')
})

test('이미 종결된 작업이 snapshot에 없는 것은 조회 대상이 아니다', () => {
  const state = run([job(completed()), snapshot([])])

  expect(state.unresolved).toEqual([])
})

test('snapshot은 server_time과 limits를 보관하고, limits 이벤트는 limits만 바꾼다', () => {
  const first = run([snapshot([], '2026-10-20T03:00:53Z')])
  expect(first.serverTime).toBe('2026-10-20T03:00:53Z')

  const limits = makeLimits({ next_attempt_at: '2026-10-20T03:01:00Z' })
  const next = run([ev({ type: 'limits', data: limits })], first)

  expect(next.limits).toEqual(limits)
  expect(next.serverTime).toBe('2026-10-20T03:00:53Z')
})

test('실행 전·취소·만료 작업은 보여 줄 답변이 없다', () => {
  const queued = makeEnvelope({ job: { id: '1', state: 'queued' } })
  const canceled = makeEnvelope({ job: { id: '2', state: 'canceled', position: null, allowed_actions: [] } })
  const expired = makeEnvelope({ job: { id: '3', state: 'expired', position: null, allowed_actions: [] } })

  const state = run([job(queued), job(canceled), job(expired)])

  expect(['1', '2', '3'].map((id) => selectAnswerView(state, id).kind)).toEqual(['none', 'none', 'none'])
  expect(selectAnswerView(state, 'unknown')).toEqual({ kind: 'none' })
})

test('reset하면 비공개 상태를 모두 비운다', () => {
  const state = run([snapshot([running()]), delta('7004', 'x'), { type: 'connection', status: 'open' }])

  expect(chatEventsReducer(state, { type: 'reset' })).toEqual(initialChatEventsState)
})
