import type { ChatAdmission, ChatSkipResult } from './chatQueueTypes'
import type {
  ApiConversation,
  ApiConversationListResponse,
  ApiMessage,
  ApiMessageListResponse,
  ApiSendMessageRequest,
} from './memberChatApiTypes'

type ApiErrorBody = {
  error?: {
    message?: string
    code?: string
  }
}

/**
 * API 요청 실패. 서버가 거절했으면 `status`·`code`가 있고, 연결 실패·시간 초과처럼 응답 자체를 받지 못했으면 `status`가 null이다.
 * 응답을 받지 못한 요청은 서버에 접수됐는지 알 수 없으므로, 질문·재시도는 같은 키로 다시 보내 확인한다.
 */
export class ChatApiError extends Error {
  readonly status: number | null
  readonly code: string | null
  /** `429`의 `Retry-After`(초). 다시 보낼 수 있을 때까지 남은 시간이다. */
  readonly retryAfterSeconds: number | null

  constructor(
    message: string,
    status: number | null,
    code: string | null = null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.status = status
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
  }

  /** 서버가 요청을 받았는지 알 수 없는 실패인가. 연결 실패·시간 초과·5xx가 해당한다. */
  get outcomeUnknown(): boolean {
    return this.status === null || this.status >= 500
  }
}

function retryAfterSeconds(response: Response): number | null {
  const value = Number(response.headers.get('Retry-After'))
  return Number.isFinite(value) && value > 0 ? value : null
}

export async function requestJson<T>(path: string, accessToken: string, init: RequestInit = {}): Promise<T> {
  let response: Response

  try {
    response = await fetch(path, {
      ...init,
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${accessToken}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
    })
  } catch {
    throw init.signal?.aborted
      ? new ChatApiError('서버 응답이 늦어 기다리지 않았습니다.', null, 'timeout')
      : new ChatApiError('서버에 연결하지 못했습니다.', null, 'network')
  }

  const body: unknown = await response.json().catch(() => null)
  // 본문을 읽는 도중 시간 초과로 중단됐다면 성공 응답이라도 내용을 믿을 수 없다.
  if (init.signal?.aborted) throw new ChatApiError('서버 응답이 늦어 기다리지 않았습니다.', null, 'timeout')

  if (!response.ok) {
    const error = (body as ApiErrorBody | null)?.error
    const wait = retryAfterSeconds(response)
    let message = error?.message ?? `요청이 실패했습니다 (${response.status}).`
    if (error?.code === 'attempt_too_soon' && wait) message = `${wait}초 뒤에 보낼 수 있습니다.`

    throw new ChatApiError(message, response.status, error?.code ?? null, wait)
  }

  return body as T
}

export async function createConversation(accessToken: string, signal?: AbortSignal): Promise<ApiConversation> {
  const result = await requestJson<{
    conversation: ApiConversation
  }>('/api/conversations', accessToken, {
    method: 'POST',
    body: JSON.stringify({}),
    signal,
  })

  return result.conversation
}

export async function listConversations(
  accessToken: string,
  limit = 20,
  beforeId?: string,
): Promise<ApiConversationListResponse> {
  const params = new URLSearchParams({ limit: String(limit) })

  if (beforeId) {
    params.set('before_id', beforeId)
  }

  return requestJson(`/api/conversations?${params.toString()}`, accessToken)
}

export async function listMessages(
  accessToken: string,
  conversationId: string,
  limit = 20,
  afterId?: string,
): Promise<ApiMessageListResponse> {
  const params = new URLSearchParams({ limit: String(limit) })

  if (afterId) {
    params.set('after_id', afterId)
  }

  return requestJson(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages?${params.toString()}`,
    accessToken,
  )
}

// 메시지는 ID 오름차순이라 첫 페이지만 읽으면 긴 대화의 최신 답변을 놓친다.
// 메시지 한 페이지는 최대 100개이고, next_cursor가 null이 될 때까지 이어서 읽는다.
const MESSAGE_PAGE_SIZE = 100
const MAX_PAGES = 50

export async function listAllMessages(accessToken: string, conversationId: string): Promise<ApiMessage[]> {
  const items: ApiMessage[] = []
  let cursor: string | undefined

  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await listMessages(accessToken, conversationId, MESSAGE_PAGE_SIZE, cursor)
    items.push(...result.items)

    if (!result.next_cursor) break
    cursor = result.next_cursor
  }

  return items
}

// 대화는 생성 ID 내림차순이고 다음 페이지는 before_id로 읽는다.
export async function listAllConversations(accessToken: string): Promise<ApiConversation[]> {
  const items: ApiConversation[] = []
  let cursor: string | undefined

  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result = await listConversations(accessToken, 20, cursor)
    items.push(...result.items)

    if (!result.next_cursor) break
    cursor = result.next_cursor
  }

  return items
}

/** 질문을 접수한다. 서버는 답변을 기다리지 않고 작업(`job`)과 한도를 돌려주며, 결과는 구독으로 온다. */
export async function sendMessage(
  accessToken: string,
  conversationId: string,
  input: ApiSendMessageRequest,
  signal?: AbortSignal,
): Promise<ChatAdmission> {
  return requestJson(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, accessToken, {
    method: 'POST',
    body: JSON.stringify(input),
    signal,
  })
}

/** 가장 최근 실패 시도를 다시 시도한다. 같은 `retryKey`로 다시 보내면 새로 접수하지 않고 그 작업을 돌려준다. */
export async function retryQuestion(
  accessToken: string,
  conversationId: string,
  questionId: string,
  retryKey: string,
  signal?: AbortSignal,
): Promise<ChatAdmission> {
  return requestJson(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(questionId)}/retry`,
    accessToken,
    { method: 'POST', body: JSON.stringify({ retry_key: retryKey }), signal },
  )
}

/** 실패한 질문을 그대로 두고, 그 실패로 멈춘 같은 대화의 뒤 질문을 다시 대기열에 넣는다. 한도를 쓰지 않는다. */
export async function skipQuestion(
  accessToken: string,
  conversationId: string,
  questionId: string,
  signal?: AbortSignal,
): Promise<ChatSkipResult> {
  return requestJson(
    `/api/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(questionId)}/skip`,
    accessToken,
    { method: 'POST', body: JSON.stringify({}), signal },
  )
}

// 같은 질문에 연결된 답변 중 가장 최근 시도를 찾을 때 사용한다.
export function latestAnswer(messages: ApiMessage[], questionId: string): ApiMessage | undefined {
  return messages
    .filter((message) => message.role === 'assistant' && message.reply_to_message_id === questionId)
    .sort((a, b) => (b.attempt_no ?? 0) - (a.attempt_no ?? 0))[0]
}
