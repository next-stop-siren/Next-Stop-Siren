/**
 * 회원 채팅 대기열 API 타입
 * 기준: docs/04-reference/api.md Rev 1 "회원 채팅 대기열 (#53)"
 *
 * 대화·메시지 자체의 타입은 memberChatApiTypes.ts에 있다.
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

/**
 * 질문 접수와 수동 재시도의 응답. 새로 접수했거나 아직 끝나지 않은 작업은 `202`, 끝난 작업을 같은 키로 다시 물으면 `200`이다.
 * `job`은 #53 이전 동기 API가 저장한 질문을 같은 키로 다시 보냈을 때만 null이고, 그때 `answer`는 그 질문의 최신 답변이다.
 */
export interface ChatAdmission {
  question: ApiMessage
  answer: ApiMessage | null
  job: ChatJob | null
  limits: ChatLimits
}

/** 실행 전 취소 — `POST /api/chat/jobs/{id}/cancel` */
export interface ChatCancelResult {
  job: ChatJob
  limits: ChatLimits
}

/** 건너뛰기 — `POST /api/conversations/{id}/messages/{id}/skip`. 다시 대기열에 들어간 뒤 질문의 작업이 온다. */
export interface ChatSkipResult {
  skipped_message_id: string
  resumed_jobs: ChatJob[]
  limits: ChatLimits
}

/** 작업 목록 — `GET /api/chat/jobs`. 대화를 지정하면 그 대화의 작업 전체(종결 포함)를 ID 내림차순으로 준다. */
export interface ChatJobPage {
  items: ChatJob[]
  next_cursor: string | null
  limits: ChatLimits
  server_time: string
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
