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
