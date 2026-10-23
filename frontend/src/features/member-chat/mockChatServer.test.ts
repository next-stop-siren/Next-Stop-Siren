import { expect, test } from 'vitest'
import { createMockChatServer, MOCK_ANSWER_TEXT } from './mockChatServer'
import type { ChatEvent } from './chatQueueTypes'

function subscribe(server: ReturnType<typeof createMockChatServer>, since: string | null = null) {
  const controller = new AbortController()
  const events: ChatEvent[] = []
  const closed = server.open({
    accessToken: 'token-1',
    since,
    signal: controller.signal,
    onEvent: (event) => events.push(event),
  })

  return { events, controller, closed }
}

function snapshotOf(events: ChatEvent[]) {
  const event = events.find((item) => item.type === 'snapshot')
  if (event?.type !== 'snapshot') throw new Error('snapshot이 없습니다')
  return event.data
}

test('새 구독은 snapshot으로 시작하고 종결 전 작업만 담는다', () => {
  const server = createMockChatServer()
  const done = server.accept('501', '끝난 질문')
  server.start(done.job.id)
  server.complete(done.job.id)
  const waiting = server.accept('501', '대기 질문')

  const { events } = subscribe(server)
  const snapshot = snapshotOf(events)

  expect(snapshot.items.map((item) => item.job.id)).toEqual([waiting.job.id])
  expect(snapshot.limits.daily.used).toBe(1)
})

test('접수한 질문은 순서대로 대기하고, 답변 행은 실행을 시작할 때 만든다', () => {
  const server = createMockChatServer()
  const first = server.accept('501', '첫째')
  const second = server.accept('502', '둘째')

  expect(server.getEnvelope(first.job.id)?.job.position).toBe(1)
  expect(server.getEnvelope(second.job.id)?.job.position).toBe(2)
  expect(server.getEnvelope(first.job.id)?.answer).toBeNull()

  server.start(first.job.id)

  const started = server.getEnvelope(first.job.id)
  expect(started?.job).toMatchObject({ state: 'running', position: null, allowed_actions: [] })
  expect(started?.answer).toMatchObject({ status: 'pending', content: null, attempt_no: 1 })
  expect(server.getEnvelope(second.job.id)?.job.position).toBe(1)
})

test('실행은 회원당 1개이고, 실행 전 작업만 취소할 수 있다', () => {
  const server = createMockChatServer()
  const first = server.accept('501', '첫째')
  const second = server.accept('501', '둘째')

  server.start(first.job.id)

  expect(() => server.start(second.job.id)).toThrow()
  expect(() => server.cancel(first.job.id)).toThrow()

  server.cancel(second.job.id)
  expect(server.getEnvelope(second.job.id)?.job).toMatchObject({ state: 'canceled', allowed_actions: [] })
})

test('구독자는 job·delta·limits 이벤트를 순서대로 받는다', () => {
  const server = createMockChatServer()
  const { events } = subscribe(server)
  const accepted = server.accept('501', '질문')

  server.start(accepted.job.id)
  server.emitDelta(accepted.job.id, '도로교통법 ')
  server.emitDelta(accepted.job.id, '제160조')
  server.complete(accepted.job.id)

  const types = events.map((event) => event.type)
  expect(types[0]).toBe('snapshot')
  expect(types).toContain('delta')

  const last = events.filter((event) => event.type === 'job').at(-1)
  expect(last).toMatchObject({
    type: 'job',
    data: { job: { state: 'completed' }, answer: { status: 'completed', content: MOCK_ANSWER_TEXT } },
  })
})

test('delta는 저장하지 않아서 늦게 연결한 구독자는 놓친 조각을 받지 못한다', () => {
  const server = createMockChatServer()
  const accepted = server.accept('501', '질문')
  server.start(accepted.job.id)
  server.emitDelta(accepted.job.id, '이미 지나간 조각')

  const { events } = subscribe(server)

  expect(events.map((event) => event.type)).toEqual(['snapshot'])
  const [item] = snapshotOf(events).items
  expect(item?.job.state).toBe('running')
  expect(item?.answer).toMatchObject({ status: 'pending', content: null })
})

test('연결을 끊어도 작업은 계속되고, since로 다시 연결하면 끊긴 사이 끝난 답변을 저장된 결과로 받는다', async () => {
  const server = createMockChatServer()
  const accepted = server.accept('501', '질문')
  server.start(accepted.job.id)

  const first = subscribe(server)
  const savedTime = snapshotOf(first.events).server_time

  server.disconnectAll()
  await first.closed
  expect(server.subscriberCount()).toBe(0)

  // 끊긴 사이 서버는 계속 일한다.
  server.emitDelta(accepted.job.id, '아무도 받지 못한 조각')
  server.complete(accepted.job.id)

  const second = subscribe(server, savedTime)
  const [item] = snapshotOf(second.events).items

  expect(item?.job.state).toBe('completed')
  expect(item?.answer).toMatchObject({ status: 'completed', content: MOCK_ANSWER_TEXT })
})

test('since가 없으면 끝난 작업은 snapshot에 들어가지 않는다', () => {
  const server = createMockChatServer()
  const accepted = server.accept('501', '질문')
  server.start(accepted.job.id)
  server.complete(accepted.job.id)

  expect(snapshotOf(subscribe(server).events).items).toEqual([])
})

test('since 이전에 끝난 작업은 snapshot에 다시 담지 않는다', () => {
  const server = createMockChatServer()
  const accepted = server.accept('501', '질문')
  server.start(accepted.job.id)
  server.complete(accepted.job.id)

  const since = server.serverTime()

  expect(snapshotOf(subscribe(server, since).events).items).toEqual([])
})

test('구독을 중단하면 AbortError로 끝나고 구독자에서 빠진다', async () => {
  const server = createMockChatServer()
  const { controller, closed } = subscribe(server)

  controller.abort()

  await expect(closed).rejects.toMatchObject({ name: 'AbortError' })
  expect(server.subscriberCount()).toBe(0)
})

test('실패하면 환불하고 재시도·건너뛰기를 허용한다', () => {
  const server = createMockChatServer()
  const accepted = server.accept('501', '질문')
  server.start(accepted.job.id)
  server.fail(accepted.job.id)

  const failed = server.getEnvelope(accepted.job.id)
  expect(failed?.job).toMatchObject({
    state: 'failed',
    error_code: 'provider_failed',
    allowed_actions: ['retry', 'skip'],
  })
  expect(failed?.answer).toMatchObject({ status: 'failed', content: null, error_code: 'provider_failed' })
  expect(snapshotOf(subscribe(server).events).limits.daily).toMatchObject({ used: 0, reserved: 0, remaining: 400 })
})

// --- 재시도·건너뛰기·일시 정지 (#21) ---

type Server = ReturnType<typeof createMockChatServer>

function failedQuestion(server: Server, conversationId = '501', content = '실패할 질문') {
  const { job } = server.accept(conversationId, content)
  server.start(job.id)
  server.fail(job.id)
  // 새 시도는 직전 접수 10초 뒤부터 보낼 수 있다.
  server.advance(10)
  return job.id
}

/** 질문을 접수하고 10초를 보낸다. 접수도 다음 새 시도까지 10초 간격을 만들기 때문이다. */
function acceptAndWait(server: Server, conversationId: string, content: string) {
  const envelope = server.accept(conversationId, content)
  server.advance(10)
  return envelope
}

const actionsOf = (server: Server, jobId: string) => server.getEnvelope(jobId)?.job.allowed_actions
const stateOf = (server: Server, jobId: string) => server.getEnvelope(jobId)?.job.state

test('실패하면 그 질문에만 재시도·건너뛰기가 허용되고, 같은 대화의 뒤 질문은 정지한다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const sameConversation = acceptAndWait(server, '501', '같은 대화의 뒤 질문')
  const otherConversation = server.accept('502', '다른 대화 질문')

  expect(actionsOf(server, failed)).toEqual(['retry', 'skip'])
  expect(server.getEnvelope(sameConversation.job.id)?.job).toMatchObject({
    state: 'paused',
    position: null,
    allowed_actions: ['cancel'],
    paused_by_message_id: server.getEnvelope(failed)?.answer?.id,
  })
  expect(stateOf(server, otherConversation.job.id)).toBe('queued')
})

test('중단(interrupted)도 실패와 같게 재시도·건너뛰기를 허용한다', () => {
  const server = createMockChatServer()
  const { job } = server.accept('501', '중단될 질문')
  server.start(job.id)
  server.interrupt(job.id)

  expect(actionsOf(server, job.id)).toEqual(['retry', 'skip'])
})

test('앞 질문이 이미 실패한 대화에 새로 접수한 질문은 처음부터 paused다', () => {
  const server = createMockChatServer()
  failedQuestion(server)

  const later = server.accept('501', '실패 뒤에 접수')

  expect(later.job.state).toBe('paused')
  expect(() => server.start(later.job.id)).toThrow('대기 중인 작업만')
})

test('재시도는 새 작업을 만들고, 원래 실패 작업과 재시도 작업의 job 이벤트를 모두 보낸다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const { events } = subscribe(server)
  events.length = 0

  const retry = server.retry(failed, 'key-1')

  expect(retry.job).toMatchObject({ kind: 'retry', state: 'queued', allowed_actions: ['cancel'] })
  expect(retry.job.id).not.toBe(failed)
  expect(retry.job.question_message_id).toBe(server.getEnvelope(failed)?.job.question_message_id)
  // 원래 실패 작업은 실패로 남고 버튼만 사라진다.
  expect(server.getEnvelope(failed)?.job).toMatchObject({ state: 'failed', allowed_actions: [] })

  const changedIds = events.flatMap((event) => (event.type === 'job' ? [event.data.job.id] : []))
  expect(changedIds).toContain(failed)
  expect(changedIds).toContain(retry.job.id)
})

test('재시도가 성공하면 뒤 질문이 다시 대기하고, 재시도 답변은 같은 질문의 다음 답변이 된다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  const retry = server.retry(failed, 'key-1')

  expect(stateOf(server, later.job.id)).toBe('paused')

  server.start(retry.job.id)
  expect(stateOf(server, later.job.id)).toBe('paused')
  expect(actionsOf(server, failed)).toEqual([])

  server.complete(retry.job.id)

  expect(server.getEnvelope(retry.job.id)?.answer).toMatchObject({ status: 'completed', attempt_no: 2 })
  expect(actionsOf(server, failed)).toEqual([])
  expect(server.getEnvelope(later.job.id)?.job).toMatchObject({ state: 'queued', position: 1 })
})

test('재시도가 실패하면 새 실패 작업에만 버튼이 있고, 뒤 질문은 계속 정지한다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  const retry = server.retry(failed, 'key-1')
  server.start(retry.job.id)
  server.fail(retry.job.id)

  expect(actionsOf(server, retry.job.id)).toEqual(['retry', 'skip'])
  expect(actionsOf(server, failed)).toEqual([])
  expect(stateOf(server, later.job.id)).toBe('paused')
})

test.each([
  ['취소', (server: Server, id: string) => server.cancel(id)],
  ['5분 만료', (server: Server, id: string) => server.expire(id)],
])('대기 중인 재시도가 %s되면 원래 실패 작업의 버튼이 돌아오고 뒤 질문은 계속 정지한다', (_name, end) => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  const retry = server.retry(failed, 'key-1')

  expect(actionsOf(server, failed)).toEqual([])

  end(server, retry.job.id)

  expect(actionsOf(server, failed)).toEqual(['retry', 'skip'])
  expect(stateOf(server, later.job.id)).toBe('paused')
})

test('건너뛰면 뒤 질문이 다시 대기하고 원래 작업에는 재시도만 남는다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')

  server.skip(failed)

  expect(actionsOf(server, failed)).toEqual(['retry'])
  expect(server.getEnvelope(later.job.id)?.job).toMatchObject({ state: 'queued', position: 1 })
})

test('뒤 질문이 실행을 시작하면 앞 질문의 재시도와 건너뛰기는 막힌다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  server.skip(failed)

  server.start(later.job.id)

  expect(actionsOf(server, failed)).toEqual([])
  expect(() => server.retry(failed, 'late-key')).toThrow('재시도할 수 없습니다')
})

test('뒤 질문이 실행 전이면 건너뛰기와 재시도가 겹쳐도 한쪽만 반영된다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  acceptAndWait(server, '501', '뒤 질문')

  const retry = server.retry(failed, 'key-1')

  // 재시도가 먼저 접수되어 원래 작업의 건너뛰기는 이미 막혔다.
  expect(() => server.skip(failed)).toThrow('건너뛸 수 없습니다')
  expect(stateOf(server, retry.job.id)).toBe('queued')
})

test('같은 retry_key를 다시 보내면 새 작업·새 예약 없이 기존 작업을 돌려준다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)

  const first = server.retry(failed, 'key-1')
  const jobCount = server.listJobs().length
  const reserved = subscribe(server).events

  // 재시도 대기와 10초 간격에 걸리는 상태에서도, 같은 키는 기존 결과를 돌려준다.
  const again = server.retry(failed, 'key-1')

  expect(again.job.id).toBe(first.job.id)
  expect(server.listJobs()).toHaveLength(jobCount)
  expect(snapshotOf(reserved).limits.queue.retries_waiting).toBe(1)
  expect(() => server.retry(failed, 'key-2')).toThrow()
})

test('같은 retry_key를 다른 작업에 쓰면 거절한다', () => {
  const server = createMockChatServer()
  const first = failedQuestion(server, '501')
  const second = failedQuestion(server, '502')
  server.retry(first, 'key-1')

  expect(() => server.retry(second, 'key-1')).toThrow('다른 작업')
})

test('재시도 대기는 1개까지다. 다른 대화의 재시도도 대기 중이면 새 재시도를 막는다', () => {
  const server = createMockChatServer()
  const first = failedQuestion(server, '501')
  const second = failedQuestion(server, '502')

  server.retry(first, 'key-1')
  server.advance(10)

  expect(() => server.retry(second, 'key-2')).toThrow('다른 재시도가 대기 중')
})

test('새 시도는 직전 접수 10초 뒤부터 보낼 수 있다', () => {
  const server = createMockChatServer()
  const { job } = server.accept('501', '질문')
  server.start(job.id)
  server.fail(job.id)

  const { events } = subscribe(server)
  const waiting = snapshotOf(events).limits.next_attempt_at
  expect(waiting).not.toBeNull()
  expect(() => server.retry(job.id, 'early-key')).toThrow('잠시 뒤')

  server.advance(10)

  expect(snapshotOf(subscribe(server).events).limits.next_attempt_at).toBeNull()
  expect(server.retry(job.id, 'late-key').job.kind).toBe('retry')
})

test('예약 한도는 대기·재시도 작업을 예약으로 세고, 실패한 시도는 환불한다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)

  expect(snapshotOf(subscribe(server).events).limits.daily).toMatchObject({ used: 0, reserved: 0 })

  server.retry(failed, 'key-1')

  expect(snapshotOf(subscribe(server).events).limits.daily).toMatchObject({ used: 0, reserved: 1 })
})

test('연결이 끊긴 사이 바뀐 버튼은 since 뒤의 snapshot으로 받는다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const since = server.serverTime()

  server.skip(failed)

  const snapshot = snapshotOf(subscribe(server, since).events)

  expect(snapshot.items.find((item) => item.job.id === failed)?.job.allowed_actions).toEqual(['retry'])
})

test('같은 대화의 재시도는 뒤 질문보다 먼저 실행되고, 뒤 질문은 재시도가 끝난 뒤에 시작할 수 있다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  server.skip(failed)
  const retry = server.retry(failed, 'key-1')

  // 건너뛰어서 풀린 뒤 질문보다 재시도가 앞선다.
  expect(server.getEnvelope(retry.job.id)?.job.position).toBe(1)
  expect(server.getEnvelope(later.job.id)?.job.position).toBe(2)
  expect(() => server.start(later.job.id)).toThrow('재시도가 먼저')
  expect(stateOf(server, later.job.id)).toBe('queued')

  server.start(retry.job.id)
  server.complete(retry.job.id)

  expect(server.getEnvelope(later.job.id)?.job.position).toBe(1)
  expect(() => server.start(later.job.id)).not.toThrow()
})

test('재시도는 접수된 자리를 지키고 같은 대화의 뒤 질문만 뒤로 민다. 먼저 접수된 다른 대화의 질문은 앞지르지 않는다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server, '501')
  const sameConversation = acceptAndWait(server, '501', '같은 대화 뒤 질문')
  const otherConversation = acceptAndWait(server, '502', '다른 대화 질문')
  server.skip(failed)
  const retry = server.retry(failed, 'key-1')

  // 접수 순서는 같은 대화의 뒤 질문 B → 다른 대화의 질문 C → 재시도 R이다. 순번은 C → R → B.
  expect(server.getEnvelope(otherConversation.job.id)?.job.position).toBe(1)
  expect(server.getEnvelope(retry.job.id)?.job.position).toBe(2)
  expect(server.getEnvelope(sameConversation.job.id)?.job.position).toBe(3)
  // 다른 대화의 질문은 재시도를 기다리지 않고 시작할 수 있다.
  expect(() => server.start(otherConversation.job.id)).not.toThrow()
})

test('재시도를 다른 대화의 질문보다 먼저 접수했다면 R → B → C 순이다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server, '501')
  const sameConversation = acceptAndWait(server, '501', '같은 대화 뒤 질문')
  server.skip(failed)
  const retry = server.retry(failed, 'key-1')
  server.advance(10)
  const otherConversation = server.accept('502', '다른 대화 질문')

  expect(server.getEnvelope(retry.job.id)?.job.position).toBe(1)
  expect(server.getEnvelope(sameConversation.job.id)?.job.position).toBe(2)
  expect(server.getEnvelope(otherConversation.job.id)?.job.position).toBe(3)
})

test('재시도가 취소되면 뒤 질문이 다시 시작할 수 있다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  server.skip(failed)
  const retry = server.retry(failed, 'key-1')

  server.cancel(retry.job.id)

  expect(() => server.start(later.job.id)).not.toThrow()
})

test.each([
  ['실패', (server: Server, id: string) => server.fail(id)],
  ['중단', (server: Server, id: string) => server.interrupt(id)],
])('건너뛴 질문의 재시도가 %s하면 뒤 질문을 다시 정지시키고, 새 실패에 건너뛰기를 다시 허용한다', (_name, end) => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')

  server.skip(failed)
  expect(stateOf(server, later.job.id)).toBe('queued')

  const retry = server.retry(failed, 'key-1')
  server.start(retry.job.id)
  end(server, retry.job.id)

  // 새 실패가 뒤 질문을 다시 멈춰 세운다.
  expect(server.getEnvelope(later.job.id)?.job).toMatchObject({
    state: 'paused',
    position: null,
    paused_by_message_id: server.getEnvelope(retry.job.id)?.answer?.id,
  })
  // 버튼은 새 실패에만 있고, 이전 실패에는 없다.
  expect(actionsOf(server, retry.job.id)).toEqual(['retry', 'skip'])
  expect(actionsOf(server, failed)).toEqual([])

  // 새 실패도 건너뛰면 뒤 질문이 다시 풀린다.
  server.skip(retry.job.id)

  expect(stateOf(server, later.job.id)).toBe('queued')
  expect(actionsOf(server, retry.job.id)).toEqual(['retry'])
})

test('건너뛴 뒤 접수한 재시도가 취소돼도 뒤 질문은 정지하지 않고, 원래 실패에는 재시도만 남는다', () => {
  const server = createMockChatServer()
  const failed = failedQuestion(server)
  const later = acceptAndWait(server, '501', '뒤 질문')
  server.skip(failed)
  const retry = server.retry(failed, 'key-1')

  server.cancel(retry.job.id)

  expect(stateOf(server, later.job.id)).toBe('queued')
  expect(actionsOf(server, failed)).toEqual(['retry'])
})
