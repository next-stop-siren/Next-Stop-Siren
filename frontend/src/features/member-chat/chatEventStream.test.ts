import { afterEach, expect, test, vi } from 'vitest'
import {
  ChatStreamHttpError,
  ChatStreamIdleError,
  ChatStreamProtocolError,
  createSseParser,
  openChatEvents,
  parseChatEvent,
  type SseFrame,
} from './chatEventStream'
import { makeEnvelope, makeLimits, sseFrame } from './chatQueueBuilders'
import type { ChatEvent } from './chatQueueTypes'

function collectFrames() {
  const frames: SseFrame[] = []
  const parser = createSseParser((frame) => frames.push(frame))
  return { frames, parser }
}

test('빈 줄로 끝난 프레임을 이벤트 이름과 data로 나눈다', () => {
  const { frames, parser } = collectFrames()

  parser.push('event: delta\ndata: {"job_id":"1","text":"가"}\n\n')

  expect(frames).toEqual([{ event: 'delta', data: '{"job_id":"1","text":"가"}' }])
})

test('프레임이 여러 조각으로 나뉘어 도착해도 한 번만 전달한다', () => {
  const { frames, parser } = collectFrames()

  parser.push('event: del')
  parser.push('ta\ndata: {"a"')
  parser.push(':1}\n')
  expect(frames).toEqual([])

  parser.push('\nevent: limits\ndata: {}\n\n')

  expect(frames).toEqual([
    { event: 'delta', data: '{"a":1}' },
    { event: 'limits', data: '{}' },
  ])
})

test('CRLF 줄바꿈과, CR과 LF 사이에서 끊긴 조각도 처리한다', () => {
  const { frames, parser } = collectFrames()

  parser.push('event: job\r\ndata: {}\r')
  parser.push('\n\r\n')

  expect(frames).toEqual([{ event: 'job', data: '{}' }])
})

test('주석 줄(: ping)은 프레임으로 전달하지 않는다', () => {
  const { frames, parser } = collectFrames()

  parser.push(': ping\n\n')
  parser.push(': ping\nevent: limits\ndata: {}\n\n')

  expect(frames).toEqual([{ event: 'limits', data: '{}' }])
})

test('data가 여러 줄이면 줄바꿈으로 이어 붙이고, event가 없으면 message로 본다', () => {
  const { frames, parser } = collectFrames()

  parser.push('data: 첫째\ndata: 둘째\n\n')

  expect(frames).toEqual([{ event: 'message', data: '첫째\n둘째' }])
})

test('알려진 네 이벤트를 타입이 붙은 객체로 바꾼다', () => {
  const envelope = makeEnvelope()
  const limits = makeLimits()
  const snapshot = { items: [envelope], limits, server_time: '2026-10-20T03:00:52Z' }

  expect(parseChatEvent({ event: 'snapshot', data: JSON.stringify(snapshot) })).toEqual({
    type: 'snapshot',
    data: snapshot,
  })
  expect(parseChatEvent({ event: 'job', data: JSON.stringify(envelope) })).toEqual({ type: 'job', data: envelope })
  expect(parseChatEvent({ event: 'delta', data: '{"job_id":"7001","text":"도로"}' })).toEqual({
    type: 'delta',
    data: { job_id: '7001', text: '도로' },
  })
  expect(parseChatEvent({ event: 'limits', data: JSON.stringify(limits) })).toEqual({ type: 'limits', data: limits })
})

test('모르는 이벤트 이름은 무시한다', () => {
  expect(parseChatEvent({ event: 'future-event', data: '{}' })).toBeNull()
  expect(parseChatEvent({ event: 'message', data: 'x' })).toBeNull()
})

test('알려진 이벤트인데 JSON이 아니거나 계약과 다르면 형식 오류를 던진다', () => {
  expect(() => parseChatEvent({ event: 'delta', data: 'not-json' })).toThrow(ChatStreamProtocolError)
  expect(() => parseChatEvent({ event: 'delta', data: '{"job_id":"1"}' })).toThrow(ChatStreamProtocolError)
  expect(() => parseChatEvent({ event: 'job', data: '{"job":{"id":"1"}}' })).toThrow(ChatStreamProtocolError)
  expect(() => parseChatEvent({ event: 'snapshot', data: '{"items":[],"server_time":"t"}' })).toThrow(
    ChatStreamProtocolError,
  )
})

const encoder = new TextEncoder()

function streamResponse(chunks: string[], init: ResponseInit = { status: 200 }) {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })

  return new Response(body, { ...init, headers: { 'Content-Type': 'text/event-stream' } })
}

test('구독을 열면 Authorization과 since를 보내고 이벤트를 순서대로 전달한다', async () => {
  const envelope = makeEnvelope({ job: { id: '7004', state: 'running', allowed_actions: [] } })
  const snapshot = { items: [envelope], limits: makeLimits(), server_time: '2026-10-20T03:00:53Z' }
  // 한 프레임을 바이트 중간에서 자른다(한글은 3바이트라 글자 중간에서 끊길 수 있다).
  const delta = sseFrame('delta', { job_id: '7004', text: '도로교통법 제160조' })
  const whole = `${sseFrame('snapshot', snapshot)}: ping\n\n${delta}`
  const bytes = encoder.encode(whole)
  const cut = bytes.length - 7

  const fetchImpl = vi.fn(async () => {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, cut))
        controller.enqueue(bytes.slice(cut))
        controller.close()
      },
    })
    return new Response(body, { status: 200 })
  })

  const events: ChatEvent[] = []

  await openChatEvents({
    accessToken: 'token-1',
    since: '2026-10-20T03:00:52Z',
    signal: new AbortController().signal,
    onEvent: (event) => events.push(event),
    fetchImpl: fetchImpl as unknown as typeof fetch,
  })

  expect(fetchImpl).toHaveBeenCalledWith(
    '/api/chat/events?since=2026-10-20T03%3A00%3A52Z',
    expect.objectContaining({
      headers: { Accept: 'text/event-stream', Authorization: 'Bearer token-1' },
    }),
  )
  expect(events.map((event) => event.type)).toEqual(['snapshot', 'delta'])
  expect(events[1]).toEqual({ type: 'delta', data: { job_id: '7004', text: '도로교통법 제160조' } })
})

test('since가 없으면 쿼리 없이 연다', async () => {
  const fetchImpl = vi.fn(async (_path: string, _init?: RequestInit) => streamResponse([]))

  await openChatEvents({
    accessToken: 't',
    signal: new AbortController().signal,
    onEvent: () => {},
    fetchImpl: fetchImpl as unknown as typeof fetch,
  })

  expect(fetchImpl.mock.calls[0]?.[0]).toBe('/api/chat/events')
})

test('연결이 거절되면 상태 코드를 담은 오류를 던진다', async () => {
  const fetchImpl = async () => new Response('{}', { status: 401 })

  await expect(
    openChatEvents({
      accessToken: 't',
      signal: new AbortController().signal,
      onEvent: () => {},
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }),
  ).rejects.toMatchObject({ name: 'Error', status: 401 })

  await expect(
    openChatEvents({
      accessToken: 't',
      signal: new AbortController().signal,
      onEvent: () => {},
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }),
  ).rejects.toBeInstanceOf(ChatStreamHttpError)
})

test('계약과 다른 이벤트가 오면 형식 오류로 끝난다', async () => {
  const fetchImpl = async () => streamResponse(['event: delta\ndata: {"job_id":1}\n\n'])

  await expect(
    openChatEvents({
      accessToken: 't',
      signal: new AbortController().signal,
      onEvent: () => {},
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }),
  ).rejects.toBeInstanceOf(ChatStreamProtocolError)
})

afterEach(() => {
  vi.useRealTimers()
})

/** abort에 반응하지 않는 열린 스트림. 끊긴 연결처럼 데이터가 오지 않다가 직접 보낸 조각만 전달한다. */
function openStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  const encoder = new TextEncoder()

  return {
    fetchImpl: (async () => new Response(body, { status: 200 })) as unknown as typeof fetch,
    send: (text: string) => controller.enqueue(encoder.encode(text)),
  }
}

test('오래 아무 데이터도 오지 않으면 연결이 끊긴 것으로 보고 형식 오류가 아닌 유휴 오류로 끝낸다', async () => {
  vi.useFakeTimers()
  const { fetchImpl } = openStream()

  const result = expect(
    openChatEvents({
      accessToken: 't',
      signal: new AbortController().signal,
      onEvent: () => {},
      idleTimeoutMs: 1000,
      fetchImpl,
    }),
  ).rejects.toBeInstanceOf(ChatStreamIdleError)

  await vi.advanceTimersByTimeAsync(1000)
  await result
})

test('ping 주석만 와도 연결이 살아 있는 것으로 보고 시간을 다시 센다', async () => {
  vi.useFakeTimers()
  const { fetchImpl, send } = openStream()
  let settled = false

  const result = openChatEvents({
    accessToken: 't',
    signal: new AbortController().signal,
    onEvent: () => {},
    idleTimeoutMs: 1000,
    fetchImpl,
  }).catch((error: unknown) => {
    settled = true
    return error
  })

  await vi.advanceTimersByTimeAsync(800)
  send(': ping\n\n')
  await vi.advanceTimersByTimeAsync(800)
  expect(settled).toBe(false)

  await vi.advanceTimersByTimeAsync(300)
  expect(await result).toBeInstanceOf(ChatStreamIdleError)
})

test('연결을 여는 동안 응답이 오지 않아도 유휴 오류로 끝낸다', async () => {
  vi.useFakeTimers()
  const fetchImpl = ((_path: string, init?: RequestInit) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })) as unknown as typeof fetch

  const result = expect(
    openChatEvents({
      accessToken: 't',
      signal: new AbortController().signal,
      onEvent: () => {},
      idleTimeoutMs: 1000,
      fetchImpl,
    }),
  ).rejects.toBeInstanceOf(ChatStreamIdleError)

  await vi.advanceTimersByTimeAsync(1000)
  await result
})

test('호출한 쪽이 중단하면 유휴 오류가 아니라 AbortError로 끝난다', async () => {
  const { fetchImpl } = openStream()
  const controller = new AbortController()

  const result = openChatEvents({
    accessToken: 't',
    signal: controller.signal,
    onEvent: () => {},
    idleTimeoutMs: 60_000,
    fetchImpl,
  })
  controller.abort()

  await expect(result).rejects.toMatchObject({ name: 'AbortError' })
})
