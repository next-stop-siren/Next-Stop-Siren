/**
 * 회원 채팅 대기열 API 타입 (제안 초안)
 * 기준: docs/04-reference/chat-queue-api-draft.md (PR #56)
 *
 * 구현 전 계약이라 #53에서 확정되면 이 파일을 확정 내용에 맞춘다.
 * 현재 구현 계약(docs/04-reference/api.md)의 타입은 memberChatApiTypes.ts에 있다.
 */

import type { ApiMessage } from './memberChatApiTypes'
import type { ChatJobState } from './memberChatMock'

export type ChatJobKind = 'question' | 'retry'

/** 서버가 지금 허용하는 동작. 화면은 버튼 표시 여부를 이 값으로만 정한다. */
export type ChatJobAction = 'cancel' | 'retry' | 'skip'

export interface ChatJob {
  id: string
  kind: ChatJobKind
  conversation_id: string
  question_message_id: string
  answer_message_id: string | null
  state: ChatJobState
  position: number | null
  paused_by_message_id: string | null
  error_code: string | null
  accepted_at: string
  started_at: string | null
  finished_at: string | null
  expires_at: string
  allowed_actions: ChatJobAction[]
}

export interface ChatLimitWindow {
  limit: number
  used: number
  reserved: number
  remaining: number
  resets_at: string
}

export interface ChatQueueCounts {
  running: number
  questions_waiting: number
  questions_max: number
  retries_waiting: number
  retries_max: number
}

export interface ChatLimits {
  daily: ChatLimitWindow
  six_hour: ChatLimitWindow
  queue: ChatQueueCounts
  next_attempt_at: string | null
}

/** 구독 이벤트와 작업 조회가 돌려주는 묶음. 실행 전 작업의 answer는 null이다. */
export interface ChatJobEnvelope {
  job: ChatJob
  question: ApiMessage
  answer: ApiMessage | null
}

export interface ChatSnapshot {
  items: ChatJobEnvelope[]
  limits: ChatLimits
  server_time: string
}

export interface ChatDelta {
  job_id: string
  text: string
}

/** `GET /api/chat/events` 구독으로 받는 이벤트 */
export type ChatEvent =
  | { type: 'snapshot'; data: ChatSnapshot }
  | { type: 'job'; data: ChatJobEnvelope }
  | { type: 'delta'; data: ChatDelta }
  | { type: 'limits'; data: ChatLimits }

const TERMINAL_STATES: ReadonlySet<ChatJobState> = new Set([
  'completed',
  'failed',
  'interrupted',
  'canceled',
  'expired',
])

/** 종결 상태는 다시 바뀌지 않는다. */
export function isTerminalState(state: ChatJobState): boolean {
  return TERMINAL_STATES.has(state)
}
