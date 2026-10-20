import type { ApiConversation, ApiMessage } from './memberChatApiTypes'

import type { MemberChatConversation, MemberChatMessage } from './memberChatMock'

/**
 * 실제 API의 메시지 상태를 화면 상태로 변환한다.
 * API에 없는 UI 전용 상태는 임의로 만들어 내지 않는다.
 */
function toUiStatus(message: ApiMessage): MemberChatMessage['status'] {
  switch (message.status) {
    case 'pending':
      return 'pending'
    case 'completed':
      return 'completed'
    case 'failed':
    case 'interrupted':
      return 'failed'
  }
}

/**
 * API 메시지를 기존 화면에서 사용하는 메시지 모델로 변환한다.
 * 질문 메시지는 별도 처리하고, assistant 메시지는 질문과 연결한다.
 */
export function toUiMessages(messages: ApiMessage[]): MemberChatMessage[] {
  return messages
    .filter((message) => message.role === 'user')
    .map((question) => {
      const answer = messages
        .filter((message) => message.role === 'assistant' && message.reply_to_message_id === question.id)
        .sort((a, b) => (b.attempt_no ?? 0) - (a.attempt_no ?? 0))[0]

      return {
        id: question.id,
        question: question.content ?? '',
        ...(answer?.status === 'completed' && answer.content ? { answer: answer.content } : {}),
        status: answer ? toUiStatus(answer) : 'pending',
        createdAt: question.created_at,
      }
    })
}

/**
 * API 대화 목록을 화면 모델로 변환한다.
 * 현재 API에 title 필드가 없어 임시 제목을 사용한다.
 */
export function toUiConversation(conversation: ApiConversation, messages: ApiMessage[] = []): MemberChatConversation {
  return {
    id: conversation.id,
    title: `대화 ${conversation.id}`,
    messages: toUiMessages(messages),
  }
}
