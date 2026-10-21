import type {
  ApiConversation,
  ApiConversationListResponse,
  ApiMessage,
  ApiMessageListResponse,
  ApiSendMessageRequest,
  ApiSendMessageResponse,
} from './memberChatApiTypes'

type ApiErrorBody = {
  error?: {
    message?: string
    code?: string
  }
}

async function requestJson<T>(path: string, accessToken: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      Authorization: `Bearer ${accessToken}`,
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...init.headers,
    },
  })

  const body: unknown = await response.json().catch(() => null)

  if (!response.ok) {
    const errorBody = body as ApiErrorBody | null
    throw new Error(errorBody?.error?.message ?? `요청이 실패했습니다 (${response.status}).`)
  }

  return body as T
}

export async function createConversation(accessToken: string): Promise<ApiConversation> {
  const result = await requestJson<{
    conversation: ApiConversation
  }>('/api/conversations', accessToken, {
    method: 'POST',
    body: JSON.stringify({}),
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

export async function sendMessage(
  accessToken: string,
  conversationId: string,
  input: ApiSendMessageRequest,
): Promise<ApiSendMessageResponse> {
  return requestJson(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, accessToken, {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// 같은 질문에 연결된 답변 중 가장 최근 시도를 찾을 때 사용한다.
export function latestAnswer(messages: ApiMessage[], questionId: string): ApiMessage | undefined {
  return messages
    .filter((message) => message.role === 'assistant' && message.reply_to_message_id === questionId)
    .sort((a, b) => (b.attempt_no ?? 0) - (a.attempt_no ?? 0))[0]
}
