/**
 * 대기열 계약 객체를 만드는 도우미. 테스트와 목업 서버가 함께 쓴다.
 * 값은 모두 가짜이며 초안(chat-queue-api-draft.md)의 예시 모양을 따른다.
 */

import type { ApiMessage } from './memberChatApiTypes'
import type { ChatJob, ChatJobEnvelope, ChatLimits } from './chatQueueTypes'

const BASE_TIME = '2026-10-20T03:00:00Z'

export function makeMessage(overrides: Partial<ApiMessage> & Pick<ApiMessage, 'id' | 'role'>): ApiMessage {
  return {
    status: 'completed',
    content: null,
    request_key: null,
    reply_to_message_id: null,
    attempt_no: null,
    error_code: null,
    created_at: BASE_TIME,
    updated_at: BASE_TIME,
    completed_at: null,
    ...overrides,
  }
}

export function makeJob(overrides: Partial<ChatJob> = {}): ChatJob {
  return {
    id: '7001',
    kind: 'question',
    conversation_id: '501',
    question_message_id: '9001',
    answer_message_id: null,
    state: 'queued',
    position: 1,
    paused_by_message_id: null,
    error_code: null,
    accepted_at: BASE_TIME,
    started_at: null,
    finished_at: null,
    expires_at: '2026-10-20T03:05:00Z',
    allowed_actions: ['cancel'],
    ...overrides,
  }
}

export function makeLimits(overrides: Partial<ChatLimits> = {}): ChatLimits {
  return {
    daily: { limit: 400, used: 0, reserved: 0, remaining: 400, resets_at: '2026-10-20T15:00:00Z' },
    six_hour: { limit: 100, used: 0, reserved: 0, remaining: 100, resets_at: '2026-10-20T09:00:00Z' },
    queue: { running: 0, questions_waiting: 0, questions_max: 3, retries_waiting: 0, retries_max: 1 },
    next_attempt_at: null,
    ...overrides,
  }
}

interface EnvelopeInput {
  job?: Partial<ChatJob>
  question?: Partial<ApiMessage>
  answer?: (Partial<ApiMessage> & Pick<ApiMessage, 'id'>) | null
}

/** 질문과 (있으면) 답변을 job의 ID와 맞춰서 묶는다. */
export function makeEnvelope({ job = {}, question = {}, answer = null }: EnvelopeInput = {}): ChatJobEnvelope {
  const builtJob = makeJob({ ...job, ...(answer ? { answer_message_id: answer.id } : {}) })
  const builtQuestion = makeMessage({
    id: builtJob.question_message_id,
    role: 'user',
    content: '어린이보호구역 제한속도는 얼마야?',
    ...question,
  })

  return {
    job: builtJob,
    question: builtQuestion,
    answer: answer
      ? makeMessage({
          role: 'assistant',
          status: 'pending',
          reply_to_message_id: builtQuestion.id,
          attempt_no: 1,
          ...answer,
        })
      : null,
  }
}

export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}
