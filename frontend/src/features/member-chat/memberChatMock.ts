export type ChatTaskStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'canceled'
  | 'expired'
  | 'quota-exceeded'

export interface MemberChatMessage {
  id: string
  question: string
  answer?: string
  status: ChatTaskStatus
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
  remainingQuota: number
}

export const initialMemberChatState: MemberChatMockState = {
  activeConversationId: 'conversation-1',
  remainingQuota: 399,
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
          createdAt: new Date().toISOString(),
        },
        {
          id: 'message-2',
          question: '새로운 질문은 어떤 방식으로 처리되나요?',
          status: 'pending',
          createdAt: new Date().toISOString(),
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

/**
 * UI 개발용 목업 전이 함수입니다.
 * 실제 대기열, API 요청, 서버 상태 복구를 구현하지 않습니다.
 */
export function transitionMockTask(message: MemberChatMessage, nextStatus: ChatTaskStatus): MemberChatMessage {
  // 최종 상태에 도달한 작업은 다시 변경하지 않습니다.
  if (
    message.status === 'completed' ||
    message.status === 'canceled' ||
    message.status === 'expired' ||
    message.status === 'quota-exceeded'
  ) {
    return message
  }

  // 실행 중인 작업은 취소할 수 없습니다.
  if (message.status === 'running' && nextStatus === 'canceled') {
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
