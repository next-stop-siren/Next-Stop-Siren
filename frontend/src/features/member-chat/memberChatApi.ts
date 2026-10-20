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
