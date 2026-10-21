/**
 * UI 개발용 목업 모델입니다. 실제 대기열, API 요청, 서버 상태 복구를 구현하지 않습니다.
 * 규칙의 기준은 docs/04-reference/chat-policy.md(승인된 정책)와
 * docs/04-reference/chat-queue-api-draft.md(API 제안 초안)입니다.
 */

export type ChatJobState =
  | 'queued'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'canceled'
  | 'expired'

export interface MemberChatMessage {
  id: string
  question: string
  answer?: string
  status: ChatJobState
  createdAt: string
  acceptedAt?: string
  expiresAt?: string
}

export interface MemberChatConversation {
  id: string
  title: string
  messages: MemberChatMessage[]
}

export interface MemberChatMockState {
  conversations: MemberChatConversation[]
  activeConversationId: string
}

// 승인된 정책 값
export const DAILY_LIMIT = 400
export const SIX_HOUR_LIMIT = 100
export const RUNNING_MAX = 1
export const QUESTIONS_MAX = 3
export const RETRIES_MAX = 1
export const WAIT_LIMIT_MS = 5 * 60 * 1000

export const MOCK_ANSWER = '목업 화면에서 완료 상태를 확인하기 위한 예시 답변입니다. 실제 서버 응답이 아닙니다.'

/** 실행 전 상태입니다. 한도를 예약하고 있고, 접수부터 5분이 지나면 만료됩니다. */
export function isWaitingStatus(status: ChatJobState): boolean {
  return status === 'queued' || status === 'paused'
}

/**
 * 상태별로 허용하는 다음 상태입니다. 화면의 동작 버튼은 이 표로만 그립니다.
 * - 취소는 실행 전(queued·paused)에만 가능하고 실행 중에는 없습니다.
 * - paused → queued는 "앞 질문 재시도 성공 또는 건너뛰기"로 재개되는 경우를 흉내 냅니다.
 * - 실패·중단 뒤의 재시도·건너뛰기 동작은 별도 작업(#21) 범위라 여기서 다루지 않습니다.
 */
export const allowedTransitions: Record<ChatJobState, ChatJobState[]> = {
  queued: ['running', 'canceled'],
  paused: ['queued', 'canceled'],
  running: ['completed', 'failed', 'interrupted'],
  completed: [],
  failed: [],
  interrupted: [],
  canceled: [],
  expired: [],
}

export function createInitialMemberChatState(now: Date = new Date()): MemberChatMockState {
  const accepted = now.toISOString()

  return {
    activeConversationId: 'conversation-1',
    conversations: [
      {
        id: 'conversation-1',
        title: '첫 번째 대화',
        messages: [
          {
            id: 'message-1',
            question: 'Next Stop Siren은 어떤 서비스인가요?',
            answer: '이 답변은 화면 확인을 위한 목업 데이터입니다. 실제 AI 응답이나 서버 처리 결과가 아닙니다.',
            status: 'completed',
            createdAt: accepted,
          },
          {
            id: 'message-2',
            question: '새로운 질문은 어떤 방식으로 처리되나요?',
            status: 'queued',
            createdAt: accepted,
            acceptedAt: accepted,
            expiresAt: new Date(now.getTime() + WAIT_LIMIT_MS).toISOString(),
          },
        ],
      },
      {
        id: 'conversation-2',
        title: '새로운 대화',
        messages: [],
      },
    ],
  }
}

/** 메시지 한 개의 상태를 바꿉니다. 허용되지 않은 전이는 같은 객체를 돌려줍니다. */
export function transitionMockTask(message: MemberChatMessage, nextStatus: ChatJobState): MemberChatMessage {
  if (!allowedTransitions[message.status].includes(nextStatus)) {
    return message
  }

  // 완료 상태는 실제 답변이 있을 때만 허용합니다.
  if (nextStatus === 'completed' && !message.answer) {
    return {
      ...message,
      status: 'failed',
    }
  }

  return {
    ...message,
    status: nextStatus,
  }
}

/**
 * 대화 안의 한 질문 상태를 바꾸고 그에 따른 연쇄 규칙을 적용합니다. 거절되면 같은 state를 돌려줍니다.
 * - 회원 전체에서 실행 중인 작업은 최대 1개입니다.
 * - 실패·중단은 같은 대화에서 대기 중인 뒤 질문을 일시 정지합니다. 다른 대화에는 영향이 없습니다.
 */
export function transitionTask(
  state: MemberChatMockState,
  conversationId: string,
  messageId: string,
  nextStatus: ChatJobState,
): MemberChatMockState {
  const conversation = state.conversations.find((item) => item.id === conversationId)
  const targetIndex = conversation?.messages.findIndex((message) => message.id === messageId) ?? -1

  if (!conversation || targetIndex === -1) return state

  const target = conversation.messages[targetIndex]

  if (nextStatus === 'running' && summarizeQueue(state.conversations).running >= RUNNING_MAX) {
    return state
  }

  const withAnswer: MemberChatMessage =
    nextStatus === 'completed' && !target.answer ? { ...target, answer: MOCK_ANSWER } : target

  const updated = transitionMockTask(withAnswer, nextStatus)

  // 허용되지 않은 전이면 후속 질문도 그대로 둡니다.
  if (updated === withAnswer) return state

  const shouldPauseFollowing = updated.status === 'failed' || updated.status === 'interrupted'

  const messages = conversation.messages.map((message, index) => {
    if (index === targetIndex) return updated

    if (shouldPauseFollowing && index > targetIndex && message.status === 'queued') {
      return { ...message, status: 'paused' as const }
    }

    return message
  })

  return {
    ...state,
    conversations: state.conversations.map((item) => (item.id === conversationId ? { ...item, messages } : item)),
  }
}

/** 접수부터 5분이 지난 실행 전 작업(대기·일시 정지)을 만료시킵니다. 바뀐 것이 없으면 같은 배열을 돌려줍니다. */
export function expireWaitingMessages(messages: MemberChatMessage[], nowMs: number): MemberChatMessage[] {
  let changed = false

  const next = messages.map((message) => {
    if (isWaitingStatus(message.status) && message.expiresAt && new Date(message.expiresAt).getTime() <= nowMs) {
      changed = true
      return { ...message, status: 'expired' as const }
    }

    return message
  })

  return changed ? next : messages
}

export function expireState(state: MemberChatMockState, nowMs: number): MemberChatMockState {
  let changed = false

  const conversations = state.conversations.map((conversation) => {
    const messages = expireWaitingMessages(conversation.messages, nowMs)
    if (messages === conversation.messages) return conversation

    changed = true
    return { ...conversation, messages }
  })

  return changed ? { ...state, conversations } : state
}

/** 같은 대화에 실패·중단된 질문이 있으면, 새로 접수한 질문은 정지 상태로 들어갑니다. */
export function hasBlockingFailure(messages: MemberChatMessage[]): boolean {
  return messages.some((message) => message.status === 'failed' || message.status === 'interrupted')
}

export function createQuestionMessage(
  id: string,
  content: string,
  blocked: boolean,
  now: Date = new Date(),
): MemberChatMessage {
  const accepted = now.toISOString()

  return {
    id,
    question: content,
    status: blocked ? 'paused' : 'queued',
    createdAt: accepted,
    acceptedAt: accepted,
    expiresAt: new Date(now.getTime() + WAIT_LIMIT_MS).toISOString(),
  }
}

export interface QueueSummary {
  running: number
  /** 대기(queued)와 일시 정지(paused)인 새 질문의 수입니다. 실행 중 작업은 세지 않습니다. */
  waitingQuestions: number
  /** 수동 재시도 대기입니다. 재시도 동작이 별도 작업 범위라 목업에서는 항상 0입니다. */
  waitingRetries: number
}

export function summarizeQueue(conversations: MemberChatConversation[]): QueueSummary {
  const messages = conversations.flatMap((conversation) => conversation.messages)

  return {
    running: messages.filter((message) => message.status === 'running').length,
    waitingQuestions: messages.filter((message) => isWaitingStatus(message.status)).length,
    waitingRetries: 0,
  }
}

export interface LimitSummary {
  limit: number
  used: number
  reserved: number
  remaining: number
}

/**
 * 목업 한도입니다. 서버가 계산해 주는 값을 흉내 내기 위해 메시지 상태에서 파생합니다.
 * 완료는 1회 차감, 실행 전·실행 중은 예약이고 실패·중단·취소·만료는 환불이라 세지 않습니다.
 * 실제 화면은 서버의 limits를 그대로 표시하고 직접 계산하지 않습니다.
 */
export function summarizeLimits(conversations: MemberChatConversation[]): {
  daily: LimitSummary
  sixHour: LimitSummary
} {
  const messages = conversations.flatMap((conversation) => conversation.messages)
  const used = messages.filter((message) => message.status === 'completed').length
  const reserved = messages.filter((message) => isWaitingStatus(message.status) || message.status === 'running').length

  const build = (limit: number): LimitSummary => ({
    limit,
    used,
    reserved,
    remaining: Math.max(limit - used - reserved, 0),
  })

  return { daily: build(DAILY_LIMIT), sixHour: build(SIX_HOUR_LIMIT) }
}

export type EnqueueRejection = 'queue_full' | 'quota_exceeded'

/** 새 질문을 접수할 수 있는지 확인합니다. 초안의 판정 순서(queue_full → quota_exceeded)를 따릅니다. */
export function checkEnqueue(conversations: MemberChatConversation[]): EnqueueRejection | null {
  if (summarizeQueue(conversations).waitingQuestions >= QUESTIONS_MAX) return 'queue_full'

  const limits = summarizeLimits(conversations)
  if (limits.daily.remaining <= 0 || limits.sixHour.remaining <= 0) return 'quota_exceeded'

  return null
}
