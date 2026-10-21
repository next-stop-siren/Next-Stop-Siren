/**
 * 실제 API 계약 타입
 * 기준: docs/04-reference/api.md
 *
 * 대화·메시지 데이터만 정의한다.
 * 회원 대기열 정책에만 존재하는 값은 여기에 넣지 않는다.
 */

export interface ApiConversation {
  id: string
  created_at: string
  updated_at: string
}

export interface ApiConversationListResponse {
  items: ApiConversation[]
  next_cursor: string | null
}

export interface ApiConversationResponse {
  conversation: ApiConversation
}

export type ApiMessageRole = 'user' | 'assistant'

export type ApiMessageStatus = 'pending' | 'completed' | 'failed' | 'interrupted'

export interface ApiMessage {
  id: string
  role: ApiMessageRole
  status: ApiMessageStatus
  content: string | null
  request_key: string | null
  reply_to_message_id: string | null
  attempt_no: number | null
  error_code: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface ApiMessageListResponse {
  items: ApiMessage[]
  next_cursor: string | null
}

export interface ApiSendMessageRequest {
  content: string
  request_key: string
}

export interface ApiSendMessageResponse {
  question: ApiMessage
  answer: ApiMessage
}

export interface ApiRetryMessageRequest {
  retry_key: string
}
