import { afterEach, expect, test, vi } from 'vitest'
import { ChatApiError, retryQuestion, sendMessage, skipQuestion } from './memberChatApi'

afterEach(() => {
  vi.restoreAllMocks()
})

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })
}

async function rejection(promise: Promise<unknown>): Promise<ChatApiError> {
  const error = await promise.then(
    () => null,
    (reason: unknown) => reason,
  )
  if (!(error instanceof ChatApiError)) throw new Error(`ChatApiError가 아닙니다: ${String(error)}`)
  return error
}

test('거절 응답의 상태·코드·Retry-After를 담고, 간격 거절은 남은 초를 문구로 보여 준다', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    json(429, { error: { code: 'attempt_too_soon', message: '잠시 후 다시 보내 주세요.' } }, { 'Retry-After': '4' }),
  )

  const error = await rejection(sendMessage('token', '501', { content: '질문', request_key: 'k' }))

  expect(error.status).toBe(429)
  expect(error.code).toBe('attempt_too_soon')
  expect(error.retryAfterSeconds).toBe(4)
  expect(error.message).toBe('4초 뒤에 보낼 수 있습니다.')
  expect(error.outcomeUnknown).toBe(false)
})

test('서버가 준 오류 문구를 그대로 쓴다', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    json(409, {
      error: { code: 'queue_full', message: '대기 중인 질문이 많습니다. 답변을 받은 뒤 다시 보내 주세요.' },
    }),
  )

  const error = await rejection(sendMessage('token', '501', { content: '질문', request_key: 'k' }))

  expect(error.code).toBe('queue_full')
  expect(error.message).toBe('대기 중인 질문이 많습니다. 답변을 받은 뒤 다시 보내 주세요.')
})

test('연결 실패와 5xx는 접수됐는지 모르는 실패로 본다', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('Failed to fetch'))
  const network = await rejection(sendMessage('token', '501', { content: '질문', request_key: 'k' }))
  expect(network.status).toBeNull()
  expect(network.code).toBe('network')
  expect(network.outcomeUnknown).toBe(true)

  fetchSpy.mockResolvedValueOnce(json(503, { error: { code: 'temporarily_unavailable', message: '일시적 오류' } }))
  const unavailable = await rejection(sendMessage('token', '501', { content: '질문', request_key: 'k' }))
  expect(unavailable.outcomeUnknown).toBe(true)
})

test('기다리기를 그만둬 중단되면 시간 초과로 알리고, 본문을 읽는 중 중단돼도 성공으로 보지 않는다', async () => {
  const controller = new AbortController()
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    controller.abort()
    return json(202, { question: {}, answer: null, job: null, limits: {} })
  })

  const error = await rejection(sendMessage('token', '501', { content: '질문', request_key: 'k' }, controller.signal))

  expect(error.code).toBe('timeout')
  expect(error.outcomeUnknown).toBe(true)
})

test('재시도·건너뛰기는 질문 ID 경로로 보내고, 재시도는 retry_key를 담는다', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(202, {}))

  await retryQuestion('token', '501', '9003', 'retry-key-1')
  await skipQuestion('token', '501', '9003')

  const [retryCall, skipCall] = fetchSpy.mock.calls
  expect(retryCall?.[0]).toBe('/api/conversations/501/messages/9003/retry')
  expect(JSON.parse(String(retryCall?.[1]?.body))).toEqual({ retry_key: 'retry-key-1' })
  expect(retryCall?.[1]?.method).toBe('POST')
  expect(skipCall?.[0]).toBe('/api/conversations/501/messages/9003/skip')
  expect(JSON.parse(String(skipCall?.[1]?.body))).toEqual({})
})
