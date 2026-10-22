/**
 * 회원 단위 구독(`GET /api/chat/events`) 클라이언트.
 * `EventSource`는 `Authorization` 헤더를 보낼 수 없어 `fetch` 스트림으로 읽는다.
 * 기준: docs/04-reference/chat-queue-api-draft.md (PR #56)
 */

import type { ChatEvent, ChatJobEnvelope, ChatLimits, ChatSnapshot } from './chatQueueTypes'

export interface SseFrame {
  event: string
  data: string
}

/** 이벤트 형식이 계약과 다를 때 던진다. 연결을 끊고 다시 연결하는 대상이다. */
export class ChatStreamProtocolError extends Error {}

/** 일정 시간 동안 아무 데이터도 받지 못했을 때 던진다. 서버는 15초마다 `: ping`을 보내므로 연결이 죽은 것으로 본다. */
export class ChatStreamIdleError extends Error {
  constructor(idleMs: number) {
    super(`구독에서 ${Math.round(idleMs / 1000)}초 동안 아무 데이터도 받지 못했습니다.`)
  }
}

/** 구독 연결 자체가 거절됐을 때 던진다(401, 404, 5xx 등). */
export class ChatStreamHttpError extends Error {
  readonly status: number

  constructor(status: number, message?: string) {
    super(message ?? `구독 연결에 실패했습니다 (${status}).`)
    this.status = status
  }
}

function parseFrame(raw: string): SseFrame | null {
  let event: string | null = null
  const data: string[] = []

  for (const line of raw.split('\n')) {
    // `:`로 시작하는 줄은 주석이다. 15초마다 오는 `: ping`도 여기에 해당한다.
    if (line === '' || line.startsWith(':')) continue

    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    let value = separator === -1 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }

  if (event === null && data.length === 0) return null

  return { event: event ?? 'message', data: data.join('\n') }
}

/** 조각난 텍스트를 받아 빈 줄로 끝난 프레임을 하나씩 돌려주는 SSE 파서 */
export function createSseParser(onFrame: (frame: SseFrame) => void) {
  let buffer = ''

  return {
    push(chunk: string) {
      buffer = (buffer + chunk).replace(/\r\n/g, '\n')

      for (let end = buffer.indexOf('\n\n'); end !== -1; end = buffer.indexOf('\n\n')) {
        const raw = buffer.slice(0, end)
        buffer = buffer.slice(end + 2)

        const frame = parseFrame(raw)
        if (frame) onFrame(frame)
      }
    },
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** `{job, question, answer}` 묶음이 계약 모양인지 확인한다. 구독 이벤트와 작업 조회가 함께 쓴다. */
export function isEnvelope(value: unknown): value is ChatJobEnvelope {
  return (
    isRecord(value) &&
    isRecord(value.job) &&
    typeof value.job.id === 'string' &&
    typeof value.job.state === 'string' &&
    Array.isArray(value.job.allowed_actions) &&
    isRecord(value.question) &&
    (value.answer === null || isRecord(value.answer))
  )
}

function isLimits(value: unknown): value is ChatLimits {
  return isRecord(value) && isRecord(value.daily) && isRecord(value.six_hour) && isRecord(value.queue)
}

function isSnapshot(value: unknown): value is ChatSnapshot {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isEnvelope) &&
    isLimits(value.limits) &&
    typeof value.server_time === 'string'
  )
}

/**
 * 프레임을 이벤트로 바꾼다. 모르는 이벤트 이름은 무시하고(null), 알려진 이벤트인데
 * 내용이 계약과 다르면 ChatStreamProtocolError를 던진다.
 */
export function parseChatEvent(frame: SseFrame): ChatEvent | null {
  if (frame.event !== 'snapshot' && frame.event !== 'job' && frame.event !== 'delta' && frame.event !== 'limits') {
    return null
  }

  let data: unknown

  try {
    data = JSON.parse(frame.data)
  } catch {
    throw new ChatStreamProtocolError(`${frame.event} 이벤트의 data가 JSON이 아닙니다.`)
  }

  const invalid = () => new ChatStreamProtocolError(`${frame.event} 이벤트의 형식이 계약과 다릅니다.`)

  switch (frame.event) {
    case 'snapshot':
      if (!isSnapshot(data)) throw invalid()
      return { type: 'snapshot', data }
    case 'job':
      if (!isEnvelope(data)) throw invalid()
      return { type: 'job', data }
    case 'delta':
      if (!isRecord(data) || typeof data.job_id !== 'string' || typeof data.text !== 'string') throw invalid()
      return { type: 'delta', data: { job_id: data.job_id, text: data.text } }
    case 'limits':
      if (!isLimits(data)) throw invalid()
      return { type: 'limits', data }
  }
}

/** 서버의 `: ping` 간격(15초)의 세 배. 이보다 오래 조용하면 연결이 끊긴 것으로 본다. */
export const DEFAULT_IDLE_TIMEOUT_MS = 45_000

export interface OpenChatEventsOptions {
  accessToken: string
  /** 마지막으로 받은 snapshot의 server_time. 끊긴 사이 끝난 답변과 바뀐 버튼을 받기 위해 보낸다. */
  since?: string | null
  signal: AbortSignal
  onEvent: (event: ChatEvent) => void
  /** 이 시간 동안 바이트(주석 ping 포함)를 하나도 받지 못하면 ChatStreamIdleError로 끝낸다. */
  idleTimeoutMs?: number
  fetchImpl?: typeof fetch
}

/** 구독을 여는 함수의 모양. 실제 구현과 목업 서버가 같은 모양을 쓴다. */
export type OpenChatEvents = (options: Omit<OpenChatEventsOptions, 'fetchImpl'>) => Promise<void>

/**
 * 구독을 열고 서버가 연결을 닫을 때까지 이벤트를 전달한다. 정상 종료하면 resolve하고,
 * 연결 거절은 ChatStreamHttpError, 형식 오류는 ChatStreamProtocolError, 오래 조용하면 ChatStreamIdleError,
 * 호출한 쪽의 중단(abort)은 AbortError로 reject한다.
 * 연결이 끊겨도 서버 작업은 계속되므로, 호출한 쪽이 `since`를 담아 다시 연다.
 */
export async function openChatEvents(options: OpenChatEventsOptions): Promise<void> {
  const { accessToken, since, signal, onEvent, fetchImpl = fetch, idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS } = options
  const path = since ? `/api/chat/events?since=${encodeURIComponent(since)}` : '/api/chat/events'

  // 바깥에서 받은 signal과 별개로, 오래 조용할 때 우리가 직접 연결을 끊기 위한 signal이다.
  const inner = new AbortController()
  const forwardAbort = () => inner.abort()
  if (signal.aborted) inner.abort()
  else signal.addEventListener('abort', forwardAbort, { once: true })

  let idle = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const armIdleTimer = () => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      idle = true
      inner.abort()
    }, idleTimeoutMs)
  }

  try {
    // 연결을 여는 동안에도 응답이 오지 않을 수 있어 fetch 전에 시작한다.
    armIdleTimer()

    const response = await fetchImpl(path, {
      headers: { Accept: 'text/event-stream', Authorization: `Bearer ${accessToken}` },
      signal: inner.signal,
    })

    if (!response.ok) throw new ChatStreamHttpError(response.status)
    if (!response.body) throw new ChatStreamHttpError(response.status, '구독 응답에 본문이 없습니다.')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    const parser = createSseParser((frame) => {
      const event = parseChatEvent(frame)
      if (event) onEvent(event)
    })

    // 응답 스트림이 abort에 반응하지 않아도 읽기가 풀리도록 직접 취소한다.
    const cancelReader = () => void reader.cancel().catch(() => {})
    // 응답을 받기 전에 이미 중단됐다면 리스너가 호출되지 않으므로 바로 취소한다.
    if (inner.signal.aborted) cancelReader()
    else inner.signal.addEventListener('abort', cancelReader, { once: true })

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        armIdleTimer()
        parser.push(decoder.decode(value, { stream: true }))
      }
    } finally {
      inner.signal.removeEventListener('abort', cancelReader)
      // 형식 오류나 onEvent 오류로 빠져나와도 응답이 열린 채 남지 않게 읽기를 취소하고 요청을 중단한다.
      // releaseLock만 하면 서버가 닫기 전까지 연결이 남고, 재연결을 거듭할수록 쌓인다. 정상 종료 때는 영향이 없다.
      await reader.cancel().catch(() => {})
      inner.abort()
      reader.releaseLock()
    }
  } catch (error) {
    if (idle) throw new ChatStreamIdleError(idleTimeoutMs)
    throw error
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', forwardAbort)
  }

  if (idle) throw new ChatStreamIdleError(idleTimeoutMs)
  if (signal.aborted) throw new DOMException('구독이 중단되었습니다.', 'AbortError')
}
